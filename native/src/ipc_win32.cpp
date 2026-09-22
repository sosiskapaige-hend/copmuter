// Канал C++ ↔ Python worker на Windows: Named Pipes (\\.\pipe\agent_ai_v1).
// Тот же framed-протокол, что и в POSIX-варианте: [uint32 BE длина][UTF-8 JSON].
#include "agent/ipc.h"

#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>  // NOLINT

#include <chrono>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

namespace agent {
namespace {

int64_t mono_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::wstring to_wide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), nullptr, 0);
    if (n <= 0) return {};
    std::wstring out(size_t(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), out.data(), n);
    return out;
}

void put_be32(char* p, uint32_t v) {
    p[0] = char(v >> 24);
    p[1] = char((v >> 16) & 0xFF);
    p[2] = char((v >> 8) & 0xFF);
    p[3] = char(v & 0xFF);
}

uint32_t be32(const char* p) {
    return (uint32_t(uint8_t(p[0])) << 24) | (uint32_t(uint8_t(p[1])) << 16) |
           (uint32_t(uint8_t(p[2])) << 8) | uint32_t(uint8_t(p[3]));
}

}  // namespace

struct AiLink::Impl {
    HANDLE pipe = INVALID_HANDLE_VALUE;
    std::mutex mu;

    bool write_all(const char* data, size_t size) {
        size_t sent = 0;
        while (sent < size) {
            DWORD written = 0;
            const DWORD chunk = DWORD(size - sent > 1 << 20 ? 1 << 20 : size - sent);
            if (!WriteFile(pipe, data + sent, chunk, &written, nullptr) || written == 0) return false;
            sent += written;
        }
        return true;
    }

    bool cancelled = false;   // выставляется, если сработал опрос «Стоп»

    bool read_all(char* data, size_t size, int timeout_ms,
                  const std::function<bool()>& cancel = {}) {
        size_t got = 0;
        const int64_t deadline = mono_ms() + (timeout_ms > 0 ? timeout_ms : 30000);
        while (got < size) {
            DWORD available = 0;
            if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr)) return false;
            if (available == 0) {
                // Опрос «Стоп» идёт постоянно: ожидание ответа модели прерывается
                // за десятки миллисекунд, а не через llm_timeout_ms.
                if (cancel && cancel()) {
                    cancelled = true;
                    return false;
                }
                if (mono_ms() >= deadline) return false;
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
                continue;
            }
            DWORD read = 0;
            const DWORD want = DWORD(size - got > available ? available : (size - got));
            if (!ReadFile(pipe, data + got, want, &read, nullptr) || read == 0) return false;
            got += read;
        }
        return true;
    }
};

AiLink::AiLink() : impl_(std::make_unique<Impl>()) {}
AiLink::~AiLink() { close(); }

bool AiLink::connect(const std::string& address, int timeout_ms, std::string& error) {
    close();
    address_ = address;
    const std::wstring wname = to_wide(address);
    const int64_t deadline = mono_ms() + (timeout_ms > 0 ? timeout_ms : 5000);
    while (true) {
        // Ждём сервер: воркер поднимается параллельно с рантаймом.
        if (WaitNamedPipeW(wname.c_str(), 200)) {
            HANDLE h = CreateFileW(wname.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                                   OPEN_EXISTING, 0, nullptr);
            if (h != INVALID_HANDLE_VALUE) {
                DWORD mode = PIPE_READMODE_BYTE;
                SetNamedPipeHandleState(h, &mode, nullptr, nullptr);
                impl_->pipe = h;
                ++reconnects_;
                return true;
            }
        }
        if (mono_ms() >= deadline) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    error = "воркер не отвечает на " + address + " (GetLastError=" +
            std::to_string(GetLastError()) + ")";
    return false;
}

bool AiLink::connected() const {
    return impl_ && impl_->pipe != INVALID_HANDLE_VALUE;
}

void AiLink::close() {
    if (!impl_) return;
    if (impl_->pipe != INVALID_HANDLE_VALUE) {
        FlushFileBuffers(impl_->pipe);
        DisconnectNamedPipe(impl_->pipe);
        CloseHandle(impl_->pipe);
        impl_->pipe = INVALID_HANDLE_VALUE;
    }
}

bool AiLink::ensure_connected(std::string& error) {
    if (connected()) return true;
    if (address_.empty()) {
        error = "канал не настроен";
        return false;
    }
    return connect(address_, 3000, error);
}

AiReply AiLink::request(const std::string& json, int timeout_ms,
                        const std::function<bool()>& cancel) {
    AiReply reply;
    if (!impl_ || impl_->pipe == INVALID_HANDLE_VALUE) {
        reply.error = "нет соединения с воркером";
        return reply;
    }
    std::lock_guard<std::mutex> lock(impl_->mu);
    const int64_t t0 = mono_ms();
    char header[4];
    put_be32(header, uint32_t(json.size()));
    if (!impl_->write_all(header, 4) || !impl_->write_all(json.data(), json.size())) {
        reply.error = "не удалось отправить запрос воркеру";
        close();
        return reply;
    }
    impl_->cancelled = false;
    char len_buf[4];
    if (!impl_->read_all(len_buf, 4, timeout_ms, cancel)) {
        reply.ms = double(mono_ms() - t0);
        if (impl_->cancelled) {
            reply.cancelled = true;
            reply.error = "остановлено пользователем";
        } else {
            reply.error = "воркер не ответил вовремя";
        }
        close();                    // ответ в пути не должен достаться следующему запросу
        return reply;
    }
    const uint32_t size = be32(len_buf);
    if (size > (64u << 20)) {
        reply.error = "слишком большой ответ воркера";
        close();
        return reply;
    }
    reply.json.resize(size);
    if (size && !impl_->read_all(reply.json.data(), size, timeout_ms, cancel)) {
        reply.ms = double(mono_ms() - t0);
        if (impl_->cancelled) {
            reply.cancelled = true;
            reply.error = "остановлено пользователем";
        } else {
            reply.error = "ответ воркера оборвался";
        }
        close();
        return reply;
    }
    reply.ok = true;
    reply.ms = double(mono_ms() - t0);
    return reply;
}

}  // namespace agent

#endif  // _WIN32
