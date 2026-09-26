// Кадры для модели зрения: кодирование PNG, уменьшение и сравнение.
//
// Почему так, а не «отдать сырой BGRA»: модель принимает PNG/JPEG. Кодируем без
// внешних зависимостей — на Linux своим минимальным PNG-писателем (stored-deflate),
// на Windows нативным GDI+ (сжатие, быстро). До отправки кадр уменьшается до
// разумного числа пикселей: гнать 4K в модель бессмысленно и медленно.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "agent/platform.h"

#ifdef _WIN32
#include <windows.h>   // NOLINT
#include <objidl.h>    // IStream/COM declarations omitted by WIN32_LEAN_AND_MEAN
#include <gdiplus.h>   // NOLINT
#pragma comment(lib, "gdiplus.lib")
#endif

namespace agent {
namespace {

inline uint32_t crc32_of(const uint8_t* data, size_t size, uint32_t crc = 0xFFFFFFFFu) {
    static uint32_t table[256];
    static bool ready = false;
    if (!ready) {
        for (uint32_t n = 0; n < 256; ++n) {
            uint32_t c = n;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            table[n] = c;
        }
        ready = true;
    }
    for (size_t i = 0; i < size; ++i) crc = table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    return crc;
}

void put_u32(std::vector<uint8_t>& out, uint32_t value) {
    out.push_back(uint8_t(value >> 24));
    out.push_back(uint8_t(value >> 16));
    out.push_back(uint8_t(value >> 8));
    out.push_back(uint8_t(value));
}

void put_chunk(std::vector<uint8_t>& out, const char* type, const std::vector<uint8_t>& data) {
    put_u32(out, uint32_t(data.size()));
    const size_t start = out.size();
    out.insert(out.end(), type, type + 4);
    out.insert(out.end(), data.begin(), data.end());
    const uint32_t crc = crc32_of(out.data() + start, out.size() - start) ^ 0xFFFFFFFFu;
    put_u32(out, crc);
}

// Минимальный deflate: несжатые блоки (zlib-обёртка). Работает везде и не тянет zlib.
std::vector<uint8_t> deflate_stored(const std::vector<uint8_t>& raw) {
    std::vector<uint8_t> out;
    out.push_back(0x78);        // zlib: CM=8, CINFO=7
    out.push_back(0x01);        // без словаря, быстрый уровень
    size_t offset = 0;
    while (offset < raw.size() || raw.empty()) {
        const size_t chunk = std::min<size_t>(65535, raw.size() - offset);
        const bool last = (offset + chunk) >= raw.size();
        out.push_back(last ? 0x01 : 0x00);
        const uint16_t len = uint16_t(chunk);
        out.push_back(uint8_t(len & 0xFF));
        out.push_back(uint8_t(len >> 8));
        const uint16_t nlen = uint16_t(~len);
        out.push_back(uint8_t(nlen & 0xFF));
        out.push_back(uint8_t(nlen >> 8));
        out.insert(out.end(), raw.begin() + long(offset), raw.begin() + long(offset + chunk));
        offset += chunk;
        if (last) break;
    }
    // adler32
    uint32_t a = 1, b = 0;
    for (uint8_t byte : raw) {
        a = (a + byte) % 65521;
        b = (b + a) % 65521;
    }
    const uint32_t adler = (b << 16) | a;
    out.push_back(uint8_t(adler >> 24));
    out.push_back(uint8_t(adler >> 16));
    out.push_back(uint8_t(adler >> 8));
    out.push_back(uint8_t(adler));
    return out;
}

std::vector<uint8_t> png_from_rgb(int width, int height, const std::vector<uint8_t>& rgb,
                                  const std::vector<uint8_t>& alpha) {
    std::vector<uint8_t> raw;
    raw.reserve(size_t(height) * (size_t(width) * 4 + 1));
    const bool has_alpha = alpha.size() == size_t(width) * size_t(height);
    for (int y = 0; y < height; ++y) {
        raw.push_back(0);       // фильтр строки «нет»
        const size_t row = size_t(y) * size_t(width) * 3;
        for (int x = 0; x < width; ++x) {
            raw.push_back(rgb[row + size_t(x) * 3]);
            raw.push_back(rgb[row + size_t(x) * 3 + 1]);
            raw.push_back(rgb[row + size_t(x) * 3 + 2]);
            if (has_alpha) raw.push_back(alpha[size_t(y) * size_t(width) + size_t(x)]);
        }
    }
    std::vector<uint8_t> out{0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};
    std::vector<uint8_t> ihdr;
    put_u32(ihdr, uint32_t(width));
    put_u32(ihdr, uint32_t(height));
    ihdr.push_back(8);                       // бит на канал
    ihdr.push_back(has_alpha ? 6 : 2);       // RGBA | RGB
    ihdr.push_back(0);
    ihdr.push_back(0);
    ihdr.push_back(0);
    put_chunk(out, "IHDR", ihdr);
    put_chunk(out, "IDAT", deflate_stored(raw));
    put_chunk(out, "IEND", {});
    return out;
}

}  // namespace

// Уменьшение кадра: длинная сторона — не больше limit, пикселей — не больше max_pixels.
// Запрошенная область экрана → координаты кадра. Вынесено из платформенного слоя,
// чтобы логику можно было проверить тестами без Windows (мониторы бывают с
// отрицательными координатами, и путать системы координат нельзя).
FrameCrop crop_into_frame(const Frame& frame, int req_x, int req_y, int req_w, int req_h) {
    FrameCrop crop;
    if (frame.width <= 0 || frame.height <= 0 || req_w <= 0 || req_h <= 0) return crop;
    const int fx = req_x - frame.origin_x;      // запрос в координатах кадра
    const int fy = req_y - frame.origin_y;
    if (fx < 0 || fy < 0) return crop;          // область начинается вне кадра
    if (fx + req_w > frame.width || fy + req_h > frame.height) return crop;
    crop.covered = true;
    crop.x = fx;
    crop.y = fy;
    crop.width = req_w;
    crop.height = req_h;
    return crop;
}

Frame frame_shrink(const Frame& src, int max_pixels, int max_side) {
    Frame out = src;
    if (src.width <= 0 || src.height <= 0 || src.pixels.empty()) return out;
    double scale = 1.0;
    if (max_side > 0) scale = std::min(scale, double(max_side) / double(std::max(src.width, src.height)));
    const double total = double(src.width) * double(src.height);
    if (max_pixels > 0 && total * scale * scale > double(max_pixels))
        scale = std::sqrt(double(max_pixels) / total);
    if (scale >= 1.0) return out;
    const int w = std::max(1, int(src.width * scale));
    const int h = std::max(1, int(src.height * scale));
    out.width = w;
    out.height = h;
    out.stride = w * 4;
    out.pixels.assign(size_t(w) * size_t(h) * 4, 0);
    for (int y = 0; y < h; ++y) {
        const int sy = std::min(src.height - 1, int(y / scale));
        for (int x = 0; x < w; ++x) {
            const int sx = std::min(src.width - 1, int(x / scale));
            const uint8_t* s = src.pixels.data() + size_t(sy) * size_t(src.stride) + size_t(sx) * 4;
            uint8_t* d = out.pixels.data() + size_t(y) * size_t(out.stride) + size_t(x) * 4;
            d[0] = s[0];
            d[1] = s[1];
            d[2] = s[2];
            d[3] = s[3];
        }
    }
    // Геометрия: при уменьшении экранные координаты = кадровые × (1/scale).
    out.backend = src.backend;
    return out;
}

// Сравнение кадров: 0.0 — одинаково, 1.0 — полностью разное. Используется для
// проверки «клик что-то изменил» без полноценного diff-а изображений.
double frame_difference(const Frame& a, const Frame& b) {
    if (a.width <= 0 || b.width <= 0 || a.pixels.empty() || b.pixels.empty()) return 1.0;
    const int step_x = std::max(1, a.width / 64);
    const int step_y = std::max(1, a.height / 64);
    const double sx = double(a.width) / double(b.width);
    const double sy = double(a.height) / double(b.height);
    double sum = 0.0;
    size_t count = 0;
    for (int y = 0; y < a.height; y += step_y) {
        const int by = std::min(b.height - 1, int(y / sy));
        for (int x = 0; x < a.width; x += step_x) {
            const int bx = std::min(b.width - 1, int(x / sx));
            const uint8_t* pa = a.pixels.data() + size_t(y) * size_t(a.stride) + size_t(x) * 4;
            const uint8_t* pb = b.pixels.data() + size_t(by) * size_t(b.stride) + size_t(bx) * 4;
            const double gray_a = 0.114 * pa[0] + 0.587 * pa[1] + 0.299 * pa[2];
            const double gray_b = 0.114 * pb[0] + 0.587 * pb[1] + 0.299 * pb[2];
            sum += std::abs(gray_a - gray_b) / 255.0;
            ++count;
        }
    }
    return count ? sum / double(count) : 1.0;
}

// PNG для модели зрения (BGRA → PNG).
bool png_encode(const Frame& frame, std::vector<uint8_t>& out) {
    if (frame.width <= 0 || frame.height <= 0 || frame.pixels.empty()) return false;
#ifdef _WIN32
    // GDI+ умеет PNG нативно: без внешних библиотек и со сжатием.
    static ULONG_PTR token = 0;
    Gdiplus::GdiplusStartupInput input;
    if (token == 0 && Gdiplus::GdiplusStartup(&token, &input, nullptr) != Gdiplus::Ok) return false;
    Gdiplus::Bitmap bitmap(frame.width, frame.height, frame.stride, PixelFormat32bppARGB,
                           const_cast<BYTE*>(frame.pixels.data()));
    if (bitmap.GetLastStatus() != Gdiplus::Ok) return false;
    CLSID png_clsid{};
    {
        UINT count = 0, size = 0;
        if (Gdiplus::GetImageEncodersSize(&count, &size) != Gdiplus::Ok || size == 0) return false;
        std::vector<uint8_t> buffer(size);
        auto* codecs = reinterpret_cast<Gdiplus::ImageCodecInfo*>(buffer.data());
        if (Gdiplus::GetImageEncoders(count, size, codecs) != Gdiplus::Ok) return false;
        bool found = false;
        for (UINT i = 0; i < count; ++i) {
            if (std::wstring(codecs[i].MimeType) == L"image/png") {
                png_clsid = codecs[i].Clsid;
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    IStream* stream = nullptr;
    if (CreateStreamOnHGlobal(nullptr, TRUE, &stream) != S_OK || !stream) return false;
    const Gdiplus::Status status = bitmap.Save(stream, &png_clsid, nullptr);
    if (status != Gdiplus::Ok) {
        stream->Release();
        return false;
    }
    HGLOBAL memory = nullptr;
    if (GetHGlobalFromStream(stream, &memory) != S_OK || !memory) {
        stream->Release();
        return false;
    }
    const SIZE_T size = GlobalSize(memory);
    const void* data = GlobalLock(memory);
    if (!data || size == 0) {
        if (data) GlobalUnlock(memory);
        stream->Release();
        return false;
    }
    out.assign(static_cast<const uint8_t*>(data), static_cast<const uint8_t*>(data) + size);
    GlobalUnlock(memory);
    stream->Release();
    return !out.empty();
#else
    std::vector<uint8_t> rgb(size_t(frame.width) * size_t(frame.height) * 3);
    for (int y = 0; y < frame.height; ++y) {
        const uint8_t* src = frame.pixels.data() + size_t(y) * size_t(frame.stride);
        uint8_t* dst = rgb.data() + size_t(y) * size_t(frame.width) * 3;
        for (int x = 0; x < frame.width; ++x) {
            dst[size_t(x) * 3] = src[size_t(x) * 4 + 2];      // R
            dst[size_t(x) * 3 + 1] = src[size_t(x) * 4 + 1];  // G
            dst[size_t(x) * 3 + 2] = src[size_t(x) * 4];      // B
        }
    }
    out = png_from_rgb(frame.width, frame.height, rgb, {});
    return !out.empty();
#endif
}

std::string base64_encode(const uint8_t* data, size_t size) {
    static const char* kAlphabet =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve((size + 2) / 3 * 4);
    for (size_t i = 0; i < size; i += 3) {
        const uint32_t b0 = data[i];
        const uint32_t b1 = (i + 1 < size) ? data[i + 1] : 0;
        const uint32_t b2 = (i + 2 < size) ? data[i + 2] : 0;
        const uint32_t triple = (b0 << 16) | (b1 << 8) | b2;
        out.push_back(kAlphabet[(triple >> 18) & 0x3F]);
        out.push_back(kAlphabet[(triple >> 12) & 0x3F]);
        out.push_back(i + 1 < size ? kAlphabet[(triple >> 6) & 0x3F] : '=');
        out.push_back(i + 2 < size ? kAlphabet[triple & 0x3F] : '=');
    }
    return out;
}

}  // namespace agent
