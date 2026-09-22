// Windows-реализация IPlatform: нативные вызовы вместо «универсальных» прослоек.
//
//   процессы   — CreateToolhelp32Snapshot / TerminateProcess / GetProcessMemoryInfo
//   окна       — EnumWindows / SetForegroundWindow / ShowWindow / GetWindowRect
//   ввод       — SendInput (мышь/клавиатура), длинный текст — через буфер обмена
//   файлы      — Win32 + SHFileOperation (корзина)
//   оболочка   — ShellExecuteExW / CreateProcessW с CREATE_NO_WINDOW и захватом вывода
//   реестр     — App Paths, Uninstall, URLAssociations (default browser)
//   приложения — меню «Пуск», PATH, shell:AppsFolder (Store/UWP)
//   экран      — DXGI Desktop Duplication (быстро) → GDI BitBlt (совместимость)
//   система    — SystemParametersInfo, ms-settings:, Core Audio (громкость)
//
// Файл собирается только под Windows (CMake выбирает его по платформе).
#include "agent/platform.h"

#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>   // NOLINT
#include <d3d11.h>     // NOLINT  (DXGI Desktop Duplication)
#include <dxgi1_2.h>   // NOLINT
#include <endpointvolume.h>   // NOLINT  (Core Audio: системная громкость)
#include <mmdeviceapi.h>      // NOLINT
#include <psapi.h>     // NOLINT
#include <shlobj.h>    // NOLINT
#include <shellapi.h>  // NOLINT
#include <tlhelp32.h>  // NOLINT

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <thread>

#include "agent/intent.h"
#include "agent/util.h"

namespace fs = std::filesystem;

namespace agent {
namespace {

std::string to_utf8(const std::wstring& w) {
    if (w.empty()) return {};
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), int(w.size()), nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string out(size_t(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), int(w.size()), out.data(), n, nullptr, nullptr);
    return out;
}

std::wstring to_wide(std::string_view s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), nullptr, 0);
    if (n <= 0) return {};
    std::wstring out(size_t(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), out.data(), n);
    return out;
}

std::string trim_str(std::string s) {
    while (!s.empty() && (unsigned char)s.front() <= ' ') s.erase(s.begin());
    while (!s.empty() && (unsigned char)s.back() <= ' ') s.pop_back();
    return s;
}

std::string lower_ascii_str(std::string s) {
    for (char& c : s)
        if (c >= 'A' && c <= 'Z') c = char(c - 'A' + 'a');
    return s;
}

std::string last_error_text(DWORD code = 0) {
    if (!code) code = GetLastError();
    LPWSTR buf = nullptr;
    const DWORD n = FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                                       FORMAT_MESSAGE_IGNORE_INSERTS,
                                   nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
                                   reinterpret_cast<LPWSTR>(&buf), 0, nullptr);
    std::string out;
    if (n && buf) out = to_utf8(std::wstring(buf, n));
    if (buf) LocalFree(buf);
    return trim_str(out);
}

std::string env_var(const char* name) {
    wchar_t buf[4096];
    const DWORD n = GetEnvironmentVariableW(to_wide(name).c_str(), buf, 4096);
    if (!n || n >= 4096) return {};
    return to_utf8(std::wstring(buf, n));
}

// «c:\Users\x» → «C:\Users\X» — реестр и реестр приложений любят разные варианты.
std::string normal_path(std::string p) {
    p = trim_str(p);
    while (p.size() > 3 && (p.back() == '\\' || p.back() == '/')) p.pop_back();
    if (p.size() >= 2 && p[1] == ':') p[0] = char(toupper((unsigned char)p[0]));
    return p;
}

std::string quote_arg(const std::string& s) {
    if (!s.empty() && s.find(' ') == std::string::npos) return s;
    return "\"" + s + "\"";
}

// ---------------------------------------------------------------------------
//  Клавиши: человекочитаемое имя → виртуальный код
// ---------------------------------------------------------------------------
struct KeyMap {
    const char* name;
    WORD vk;
};

const KeyMap kKeys[] = {
    {"enter", VK_RETURN},   {"return", VK_RETURN}, {"esc", VK_ESCAPE},     {"escape", VK_ESCAPE},
    {"tab", VK_TAB},        {"space", VK_SPACE},   {"backspace", VK_BACK}, {"delete", VK_DELETE},
    {"del", VK_DELETE},     {"insert", VK_INSERT}, {"home", VK_HOME},      {"end", VK_END},
    {"pageup", VK_PRIOR},   {"pagedown", VK_NEXT},{"up", VK_UP},          {"down", VK_DOWN},
    {"left", VK_LEFT},      {"right", VK_RIGHT},   {"ctrl", VK_CONTROL},   {"control", VK_CONTROL},
    {"alt", VK_MENU},       {"shift", VK_SHIFT},   {"win", VK_LWIN},       {"super", VK_LWIN},
    {"cmd", VK_LWIN},       {"capslock", VK_CAPITAL}, {"printscreen", VK_SNAPSHOT},
    {"f1", VK_F1},  {"f2", VK_F2},  {"f3", VK_F3},  {"f4", VK_F4},  {"f5", VK_F5},  {"f6", VK_F6},
    {"f7", VK_F7},  {"f8", VK_F8},  {"f9", VK_F9},  {"f10", VK_F10}, {"f11", VK_F11}, {"f12", VK_F12},
};

WORD vk_for(std::string_view key) {
    const std::string k = lower_ascii_str(trim_str(std::string(key)));
    for (const KeyMap& m : kKeys)
        if (k == m.name) return m.vk;
    if (k.size() == 1) {
        const SHORT vk = VkKeyScanA(k[0]);
        if (vk != -1) return WORD(vk & 0xFF);
    }
    if (k.rfind("num", 0) == 0 && k.size() == 4) return WORD(VK_NUMPAD0 + (k[3] - '0'));
    return 0;
}

// ---------------------------------------------------------------------------
//  Core Audio: установка/чтение системной громкости
//
//  Используем настоящие интерфейсы (mmdeviceapi.h / endpointvolume.h), а не
//  самодельные описания vtable: порядок методов в COM-интерфейсе — часть ABI,
//  и угадывать его нельзя.
// ---------------------------------------------------------------------------
namespace {

// COM инициализируется на время вызова; если поток уже инициализирован в другом
// режиме — просто работаем без парной деинициализации.
class ComScope {
public:
    ComScope() {
        const HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        owned_ = SUCCEEDED(hr);
    }
    ~ComScope() {
        if (owned_) CoUninitialize();
    }
    ComScope(const ComScope&) = delete;
    ComScope& operator=(const ComScope&) = delete;

private:
    bool owned_ = false;
};

IAudioEndpointVolume* default_endpoint_volume() {
    IMMDeviceEnumerator* enumerator = nullptr;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_INPROC_SERVER,
                                __uuidof(IMMDeviceEnumerator),
                                reinterpret_cast<void**>(&enumerator))) ||
        !enumerator)
        return nullptr;
    IMMDevice* device = nullptr;
    IAudioEndpointVolume* volume = nullptr;
    if (SUCCEEDED(enumerator->GetDefaultAudioEndpoint(eRender, eMultimedia, &device)) && device) {
        if (FAILED(device->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_INPROC_SERVER, nullptr,
                                    reinterpret_cast<void**>(&volume))))
            volume = nullptr;
        device->Release();
    }
    enumerator->Release();
    return volume;
}

}  // namespace

bool set_volume_com(int percent) {
    // waveOutSetVolume меняет только «своё» устройство — для системной громкости не годится.
    ComScope com;
    IAudioEndpointVolume* volume = default_endpoint_volume();
    if (!volume) return false;
    const float value = float(percent < 0 ? 0 : (percent > 100 ? 100 : percent)) / 100.0f;
    const bool ok = SUCCEEDED(volume->SetMasterVolumeLevelScalar(value, nullptr));
    volume->Release();
    return ok;
}

int get_volume_com() {
    ComScope com;
    IAudioEndpointVolume* volume = default_endpoint_volume();
    if (!volume) return -1;
    float value = 0.0f;
    const bool ok = SUCCEEDED(volume->GetMasterVolumeLevelScalar(&value));
    volume->Release();
    if (!ok) return -1;
    return int(value * 100.0f + 0.5f);
}


}  // namespace

class WindowsPlatform : public IPlatform {
public:
    WindowsPlatform() { CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED); }
    ~WindowsPlatform() override { CoUninitialize(); }

    const char* name() const override { return "windows"; }
    bool has_display() const override { return GetSystemMetrics(SM_CMONITORS) > 0; }

    // ---------------------------------------------------------------- процессы
    std::vector<ProcessInfo> processes() const override {
        std::vector<ProcessInfo> out;
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == INVALID_HANDLE_VALUE) return out;
        PROCESSENTRY32W pe{};
        pe.dwSize = sizeof(pe);
        if (Process32FirstW(snap, &pe)) {
            do {
                ProcessInfo pi;
                pi.pid = pe.th32ProcessID;
                pi.name = to_utf8(pe.szExeFile);
                HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pi.pid);
                if (h) {
                    wchar_t path[MAX_PATH * 2];
                    DWORD n = DWORD(std::size(path));
                    if (QueryFullProcessImageNameW(h, 0, path, &n)) pi.path = to_utf8(std::wstring(path, n));
                    PROCESS_MEMORY_COUNTERS pmc{};
                    if (GetProcessMemoryInfo(h, &pmc, sizeof(pmc)))
                        pi.memory_bytes = pmc.WorkingSetSize;
                    CloseHandle(h);
                }
                out.push_back(std::move(pi));
            } while (Process32NextW(snap, &pe));
        }
        CloseHandle(snap);
        return out;
    }

    bool process_running(std::string_view name) const override {
        if (name.empty()) return false;
        const std::string want = lower_ascii_str(normal_path(std::string(name)));
        for (const ProcessInfo& p : processes()) {
            const std::string n = lower_ascii_str(p.name);
            const std::string pth = lower_ascii_str(p.path);
            if (n == want || n == want + ".exe") return true;
            if (want.find(".exe") != std::string::npos && pth == want) return true;
            if (pth.size() > want.size() && pth.compare(pth.size() - want.size(), want.size(), want) == 0)
                return true;
        }
        return false;
    }

    bool kill_process(std::string_view name, bool force) override {
        bool any = false;
        const std::string want = lower_ascii_str(std::string(name));
        for (const ProcessInfo& p : processes()) {
            const std::string n = lower_ascii_str(p.name);
            const std::string pth = lower_ascii_str(p.path);
            if (n != want && n != want + ".exe" && pth.find(want) == std::string::npos) continue;
            HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, p.pid);
            if (!h) continue;
            if (TerminateProcess(h, force ? 1u : 0u)) any = true;
            CloseHandle(h);
        }
        return any;
    }

    LaunchResult spawn_detached(const std::string& command, std::string_view args) override {
        LaunchResult r;
        if (command.empty()) {
            r.error = "пустая команда";
            return r;
        }
        const int64_t t0 = now_ms();
        std::string full = command;
        if (!args.empty()) full += " " + std::string(args);
        SHELLEXECUTEINFOW sei{};
        sei.cbSize = sizeof(sei);
        sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI;
        const std::wstring wcmd = to_wide(full);
        sei.lpFile = wcmd.c_str();
        sei.lpVerb = L"open";
        sei.nShow = SW_SHOWNORMAL;
        r.method = "ShellExecuteEx";
        if (ShellExecuteExW(&sei) && sei.hProcess) {
            DWORD pid = GetProcessId(sei.hProcess);
            r.pid = pid;
            r.ok = true;
            CloseHandle(sei.hProcess);
        } else {
            // командная строка (CLI-утилиты) — через CreateProcess
            STARTUPINFOW si{};
            si.cb = sizeof(si);
            PROCESS_INFORMATION pi{};
            std::wstring mutable_cmd = wcmd;
            if (CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, FALSE,
                               CREATE_NO_WINDOW | DETACHED_PROCESS, nullptr, nullptr, &si, &pi)) {
                r.pid = pi.dwProcessId;
                r.ok = true;
                r.method = "CreateProcess";
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
            } else {
                r.error = last_error_text();
            }
        }
        r.ms = double(now_ms() - t0);
        return r;
    }

    // -------------------------------------------------------------------- окна
    static BOOL CALLBACK enum_proc(HWND hwnd, LPARAM param) {
        auto* out = reinterpret_cast<std::vector<WindowInfo>*>(param);
        if (!IsWindow(hwnd)) return TRUE;
        wchar_t title[1024];
        const int n = GetWindowTextW(hwnd, title, 1024);
        const bool visible = IsWindowVisible(hwnd) != 0;
        if (!visible && n == 0) return TRUE;
        WindowInfo w;
        w.handle = reinterpret_cast<uint64_t>(hwnd);
        w.title = n > 0 ? to_utf8(std::wstring(title, n)) : std::string();
        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        w.pid = pid;
        if (pid) {
            HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
            if (h) {
                wchar_t path[MAX_PATH * 2];
                DWORD len = DWORD(std::size(path));
                if (QueryFullProcessImageNameW(h, 0, path, &len)) {
                    std::string full = to_utf8(std::wstring(path, len));
                    const size_t slash = full.find_last_of("\\/");
                    w.process = slash == std::string::npos ? full : full.substr(slash + 1);
                }
                CloseHandle(h);
            }
        }
        RECT rc{};
        if (GetWindowRect(hwnd, &rc)) {
            w.x = int(rc.left);
            w.y = int(rc.top);
            w.width = int(rc.right - rc.left);
            w.height = int(rc.bottom - rc.top);
        }
        w.visible = visible;
        w.minimized = IsIconic(hwnd) != 0;
        if (!w.title.empty() || w.visible) out->push_back(std::move(w));
        return TRUE;
    }

    std::vector<WindowInfo> windows() const override {
        std::vector<WindowInfo> out;
        EnumWindows(&WindowsPlatform::enum_proc, reinterpret_cast<LPARAM>(&out));
        return out;
    }

    std::optional<WindowInfo> find_window(std::string_view title) const override {
        if (title.empty()) return std::nullopt;
        const std::string want = lower_ascii_str(std::string(title));
        std::vector<WindowInfo> all = windows();
        std::optional<WindowInfo> partial;
        for (const WindowInfo& w : all) {
            const std::string t = lower_ascii_str(w.title);
            const std::string pr = lower_ascii_str(w.process);
            if (t.find(want) != std::string::npos || pr.find(want) != std::string::npos) {
                if (!partial) partial = w;
                if (w.visible && !w.minimized) return w;   // предпочитаем живое окно
            }
        }
        return partial;
    }

    std::optional<WindowInfo> active_window() const override {
        HWND hwnd = GetForegroundWindow();
        if (!hwnd) return std::nullopt;
        std::vector<WindowInfo> one;
        WindowsPlatform::enum_proc(hwnd, reinterpret_cast<LPARAM>(&one));
        if (one.empty()) {
            WindowInfo w;
            w.handle = reinterpret_cast<uint64_t>(hwnd);
            w.title = window_title(hwnd);
            DWORD pid = 0;
            GetWindowThreadProcessId(hwnd, &pid);
            w.pid = pid;
            return w;
        }
        return one.front();
    }

    bool activate_window(uint64_t handle) override {
        HWND hwnd = reinterpret_cast<HWND>(handle);
        if (!IsWindow(hwnd)) return false;
        if (IsIconic(hwnd)) ShowWindow(hwnd, SW_RESTORE);
        // SetForegroundWindow отказывает, если наш процесс не в фокусе: обходим через
        // короткое «нажатие» Alt и AttachThreadInput — надёжный приём для агентов.
        const DWORD target_thread = GetWindowThreadProcessId(hwnd, nullptr);
        const DWORD our_thread = GetCurrentThreadId();
        bool attached = false;
        if (target_thread && target_thread != our_thread) {
            attached = AttachThreadInput(our_thread, target_thread, TRUE) != 0;
        }
        ShowWindow(hwnd, SW_SHOW);
        const BOOL ok = SetForegroundWindow(hwnd);
        if (attached) AttachThreadInput(our_thread, target_thread, FALSE);
        SetActiveWindow(hwnd);
        return ok != 0 || GetForegroundWindow() == hwnd;
    }

    bool close_window(uint64_t handle) override {
        HWND hwnd = reinterpret_cast<HWND>(handle);
        if (!IsWindow(hwnd)) return false;
        PostMessageW(hwnd, WM_CLOSE, 0, 0);
        return true;
    }

    // -------------------------------------------------------------------- ввод
    bool mouse_move(int x, int y) override {
        return send_mouse(INPUT_MOUSE, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, x, y, 0, 0);
    }

    bool mouse_click(int x, int y, int button, int clicks) override {
        if (!mouse_move(x, y)) return false;
        const DWORD down = button == 2 ? MOUSEEVENTF_RIGHTDOWN
                                       : (button == 3 ? MOUSEEVENTF_MIDDLEDOWN : MOUSEEVENTF_LEFTDOWN);
        const DWORD up = button == 2 ? MOUSEEVENTF_RIGHTUP
                                     : (button == 3 ? MOUSEEVENTF_MIDDLEUP : MOUSEEVENTF_LEFTUP);
        bool ok = true;
        for (int i = 0; i < (clicks <= 0 ? 1 : clicks); ++i) {
            ok = send_mouse(INPUT_MOUSE, down, x, y, 0, 0) && ok;
            ok = send_mouse(INPUT_MOUSE, up, x, y, 0, 0) && ok;
            if (clicks > 1) std::this_thread::sleep_for(std::chrono::milliseconds(40));
        }
        return ok;
    }

    bool key_press(std::string_view key) override {
        const WORD vk = vk_for(key);
        if (!vk) return false;
        return send_key(vk, true) && send_key(vk, false);
    }

    bool hotkey(std::string_view keys) override {
        std::vector<WORD> mods;
        WORD main_key = 0;
        std::string token;
        auto flush = [&](bool last) {
            if (token.empty()) return;
            const WORD vk = vk_for(token);
            const std::string t = lower_ascii_str(token);
            const bool is_mod = t == "ctrl" || t == "control" || t == "alt" || t == "shift" ||
                                t == "win" || t == "super" || t == "cmd";
            if (vk && (is_mod || !last)) {
                if (is_mod) mods.push_back(vk);
                else main_key = vk;
            } else if (vk) {
                main_key = vk;
            }
            token.clear();
        };
        for (char c : keys) {
            if (c == '+') {
                flush(false);
            } else {
                token.push_back(c);
            }
        }
        flush(true);
        if (!main_key) return false;
        bool ok = true;
        for (WORD m : mods) ok = send_key(m, true) && ok;
        ok = send_key(main_key, true) && ok;
        ok = send_key(main_key, false) && ok;
        for (size_t i = mods.size(); i > 0; --i) ok = send_key(mods[i - 1], false) && ok;
        return ok;
    }

    bool type_text(std::string_view text) override {
        if (text.empty()) return true;
        // Длинный текст — только через буфер обмена: SendInput по символам
        // пропускает символы в медленных приложениях и растягивает время (ТЗ §11).
        if (text.size() > 40 || text.find('\n') != std::string_view::npos) {
            if (!clipboard_set(text)) return false;
            return hotkey("ctrl+v");
        }
        bool ok = true;
        for (char c : text) {
            if (c == '\n') {
                ok = key_press("enter") && ok;
                continue;
            }
            if (c == '\t') {
                ok = key_press("tab") && ok;
                continue;
            }
            SHORT scan = VkKeyScanA(c);
            if (scan == -1) continue;
            const WORD vk = WORD(scan & 0xFF);
            const bool need_shift = (scan & 0x100) != 0;
            if (need_shift) ok = send_key(VK_SHIFT, true) && ok;
            ok = send_key(vk, true) && ok;
            ok = send_key(vk, false) && ok;
            if (need_shift) ok = send_key(VK_SHIFT, false) && ok;
        }
        return ok;
    }

    // ------------------------------------------------------------ буфер обмена
    std::string clipboard_get() override {
        std::string out;
        if (!OpenClipboard(nullptr)) return out;
        HANDLE h = GetClipboardData(CF_UNICODETEXT);
        if (h) {
            auto* p = static_cast<const wchar_t*>(GlobalLock(h));
            if (p) {
                out = to_utf8(std::wstring(p));
                GlobalUnlock(h);
            }
        }
        CloseClipboard();
        return out;
    }

    bool clipboard_set(std::string_view text) override {
        const std::wstring w = to_wide(text);
        if (!OpenClipboard(nullptr)) return false;
        EmptyClipboard();
        const size_t bytes = (w.size() + 1) * sizeof(wchar_t);
        HGLOBAL h = GlobalAlloc(GMEM_MOVEABLE, bytes);
        if (!h) {
            CloseClipboard();
            return false;
        }
        if (void* dst = GlobalLock(h)) {
            std::memcpy(dst, w.c_str(), bytes);
            GlobalUnlock(h);
        }
        SetClipboardData(CF_UNICODETEXT, h);
        CloseClipboard();
        return true;
    }

    // ------------------------------------------------------------------ файлы
    bool file_exists(std::string_view path) const override {
        return GetFileAttributesW(to_wide(std::string(path)).c_str()) != INVALID_FILE_ATTRIBUTES;
    }
    bool is_dir(std::string_view path) const override {
        const DWORD a = GetFileAttributesW(to_wide(std::string(path)).c_str());
        return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) != 0;
    }
    bool mkdir(std::string_view path, bool recursive) override {
        std::error_code ec;
        if (recursive) return fs::create_directories(std::string(path), ec) || !ec;
        return CreateDirectoryW(to_wide(std::string(path)).c_str(), nullptr) != 0;
    }
    bool write_file(std::string_view path, std::string_view content, bool append) override {
        std::error_code ec;
        const fs::path p{std::string(path)};
        if (p.has_parent_path()) fs::create_directories(p.parent_path(), ec);
        std::ofstream f(p, std::ios::binary | (append ? std::ios::app : std::ios::trunc));
        if (!f) return false;
        f.write(content.data(), std::streamsize(content.size()));
        return bool(f);
    }
    std::string read_file(std::string_view path, size_t max_bytes) override {
        std::ifstream f(std::string(path), std::ios::binary);
        if (!f) return {};
        std::string out;
        out.resize(max_bytes);
        f.read(out.data(), std::streamsize(max_bytes));
        out.resize(size_t(f.gcount()));
        return out;
    }
    bool remove_path(std::string_view path, bool recursive, bool to_trash) override {
        std::error_code ec;
        const std::string p = std::string(path);
        if (to_trash) {
            // SHFileOperationW: штатная корзина, без «мимо» и без невосстановимого удаления
            std::wstring from = to_wide(p);
            from.push_back(L'\0');
            from.push_back(L'\0');
            SHFILEOPSTRUCTW op{};
            op.wFunc = FO_DELETE;
            op.pFrom = from.c_str();
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT;
            if (SHFileOperationW(&op) == 0) return true;
        }
        if (recursive) return fs::remove_all(p, ec) > 0;
        return DeleteFileW(to_wide(p).c_str()) != 0 || fs::remove(p, ec);
    }
    bool move_path(std::string_view src, std::string_view dst) override {
        std::error_code ec;
        fs::rename(std::string(src), std::string(dst), ec);
        if (!ec) return true;
        fs::copy(std::string(src), std::string(dst),
                 fs::copy_options::overwrite_existing | fs::copy_options::recursive, ec);
        if (ec) return false;
        fs::remove_all(std::string(src), ec);
        return true;
    }
    bool copy_path(std::string_view src, std::string_view dst) override {
        std::error_code ec;
        fs::copy(std::string(src), std::string(dst),
                 fs::copy_options::overwrite_existing | fs::copy_options::recursive, ec);
        return !ec;
    }
    std::vector<FileEntry> list_dir(std::string_view path) override {
        std::vector<FileEntry> out;
        std::error_code ec;
        for (const fs::directory_entry& e : fs::directory_iterator(std::string(path), ec)) {
            if (ec) break;
            FileEntry fe;
            fe.path = e.path().string();
            fe.name = e.path().filename().string();
            fe.is_dir = e.is_directory(ec);
            if (!fe.is_dir && e.is_regular_file(ec)) fe.size = e.file_size(ec);
            out.push_back(std::move(fe));
        }
        std::sort(out.begin(), out.end(), [](const FileEntry& a, const FileEntry& b) {
            if (a.is_dir != b.is_dir) return a.is_dir;
            return lower_ascii_str(a.name) < lower_ascii_str(b.name);
        });
        return out;
    }
    std::vector<FileEntry> search_files(std::string_view root, std::string_view pattern,
                                        size_t limit) override {
        std::vector<FileEntry> out;
        if (limit == 0) limit = 50;
        const std::wstring wroot = to_wide(std::string(root));
        const std::wstring wpat = to_wide("*" + std::string(pattern) + "*");
        WIN32_FIND_DATAW fd{};
        HANDLE h = FindFirstFileW((wroot + L"\\*").c_str(), &fd);
        if (h == INVALID_HANDLE_VALUE) return out;
        const std::string needle = lower_ascii_str(std::string(pattern));
        do {
            if (std::wcscmp(fd.cFileName, L".") == 0 || std::wcscmp(fd.cFileName, L"..") == 0) continue;
            const std::string name = to_utf8(fd.cFileName);
            const bool dir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
            if (lower_ascii_str(name).find(needle) != std::string::npos) {
                FileEntry fe;
                fe.path = normal_path(std::string(root)) + "\\" + name;
                fe.name = name;
                fe.is_dir = dir;
                out.push_back(std::move(fe));
                if (out.size() >= limit) break;
            }
            if (dir) {
                const std::string child = normal_path(std::string(root)) + "\\" + name;
                const std::vector<FileEntry> deeper = search_files(child, pattern, limit - out.size());
                for (const FileEntry& e : deeper) out.push_back(e);
                if (out.size() >= limit) break;
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
        (void)wpat;
        return out;
    }

    // --------------------------------------------------------------- оболочка
    ExecResult run_command(std::string_view command, std::string_view cwd, int timeout_ms) override {
        return run_process(L"cmd.exe", L"/c " + to_wide(std::string(command)), cwd, timeout_ms, false);
    }

    ExecResult run_powershell(std::string_view script, std::string_view cwd, int timeout_ms) override {
        return run_process(L"powershell.exe",
                           L"-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"" +
                               to_wide(std::string(script)) + L"\"",
                           cwd, timeout_ms, false);
    }

    bool open_uri(std::string_view uri) override { return shell_open(std::string(uri), {}); }
    bool open_path(std::string_view path) override { return shell_open("explorer.exe", {std::string(path)}); }

    // -------------------------------------------------- приложения / обнаружение
    int discover_apps(AppRegistry& reg) override {
        int found = 0;

        auto register_path = [&](const std::string& name, const std::string& path) {
            if (name.empty() || path.empty()) return;
            AppRegistry::Lookup hit = reg.find(name);
            std::string key;
            if (hit.app && hit.score >= 0.85f) {
                key = hit.app->key;
            } else {
                key = normalize_app_key(name);
                if (key.empty() || reg.get(key)) return;
                AppInfo app;
                app.key = key;
                app.display_name = name;
                reg.add(app);
                if (!reg.get(key)) return;
            }
            reg.set_path(key, normal_path(path), true);
            ++found;
        };

        // 1) App Paths: c:\...\app.exe → «app»
        if (HKEY app_paths; RegOpenKeyExW(HKEY_LOCAL_MACHINE,
                                          L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths",
                                          0, KEY_READ, &app_paths) == ERROR_SUCCESS) {
            wchar_t sub[512];
            DWORD index = 0;
            DWORD len = 512;
            while (RegEnumKeyExW(app_paths, index++, sub, &len, nullptr, nullptr, nullptr, nullptr) ==
                   ERROR_SUCCESS) {
                len = 512;
                HKEY k = nullptr;
                if (RegOpenKeyExW(app_paths, sub, 0, KEY_READ, &k) == ERROR_SUCCESS) {
                    wchar_t value[1024];
                    DWORD size = sizeof(value);
                    DWORD type = 0;
                    if (RegQueryValueExW(k, nullptr, nullptr, &type, reinterpret_cast<LPBYTE>(value),
                                         &size) == ERROR_SUCCESS) {
                        const std::string path = to_utf8(value);
                        std::string name = to_utf8(sub);
                        if (name.size() > 4 && name.compare(name.size() - 4, 4, ".exe") == 0)
                            name = name.substr(0, name.size() - 4);
                        register_path(name, path);
                    }
                    RegCloseKey(k);
                }
            }
            RegCloseKey(app_paths);
        }

        // 2) Установленные программы: Uninstall/USER Shell Folders (InstallLocation)
        const wchar_t* uninstall_roots[] = {
            L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
            L"SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
        };
        for (const wchar_t* root : uninstall_roots) {
            HKEY h = nullptr;
            if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, root, 0, KEY_READ, &h) != ERROR_SUCCESS) continue;
            wchar_t sub[512];
            DWORD index = 0;
            DWORD len = 512;
            while (RegEnumKeyExW(h, index++, sub, &len, nullptr, nullptr, nullptr, nullptr) ==
                   ERROR_SUCCESS) {
                len = 512;
                HKEY k = nullptr;
                if (RegOpenKeyExW(h, sub, 0, KEY_READ, &k) != ERROR_SUCCESS) continue;
                auto read = [&](const wchar_t* name) {
                    wchar_t value[1024];
                    DWORD size = sizeof(value);
                    DWORD type = 0;
                    if (RegQueryValueExW(k, name, nullptr, &type, reinterpret_cast<LPBYTE>(value),
                                         &size) != ERROR_SUCCESS)
                        return std::string();
                    return to_utf8(value);
                };
                const std::string display = read(L"DisplayName");
                const std::string location = read(L"InstallLocation");
                const std::string icon = read(L"DisplayIcon");
                if (!display.empty()) {
                    std::string path = location;
                    if (!icon.empty()) {
                        std::string exe = icon;
                        const size_t comma = exe.find(',');
                        if (comma != std::string::npos) exe = exe.substr(0, comma);
                        if (exe.size() > 4 && lower_ascii_str(exe).find(".exe") != std::string::npos)
                            path = exe;
                    }
                    if (!path.empty()) register_path(display, path);
                }
                RegCloseKey(k);
            }
            RegCloseKey(h);
        }

        // 3) Меню «Пуск»: .lnk ярлыки (быстрое чтение пути без COM)
        std::vector<std::string> start_dirs;
        for (const char* env : {"ProgramData", "APPDATA"}) {
            const std::string base = env_var(env);
            if (base.empty()) continue;
            start_dirs.push_back(base + "\\Microsoft\\Windows\\Start Menu\\Programs");
        }
        for (const std::string& dir : start_dirs) {
            std::error_code ec;
            for (const fs::directory_entry& e : fs::recursive_directory_iterator(dir, ec)) {
                if (ec) break;
                if (lower_ascii_str(e.path().extension().string()) != ".lnk") continue;
                const std::string name = e.path().stem().string();
                const std::string target = resolve_lnk(e.path().string());
                if (!target.empty()) register_path(name, target);
            }
        }

        // 4) PATH: имена exe-файлов из каталогов PATH
        std::string path_env = env_var("PATH");
        size_t pos = 0;
        while ((pos = path_env.find(';')) != std::string::npos || !path_env.empty()) {
            const std::string dir = path_env.substr(0, pos);
            path_env = pos == std::string::npos ? std::string() : path_env.substr(pos + 1);
            if (dir.empty()) {
                if (pos == std::string::npos) break;
                continue;
            }
            std::error_code ec;
            for (const fs::directory_entry& e : fs::directory_iterator(dir, ec)) {
                if (ec) break;
                const std::string ext = lower_ascii_str(e.path().extension().string());
                if (ext != ".exe" && ext != ".cmd" && ext != ".bat") continue;
                register_path(e.path().stem().string(), e.path().string());
            }
            if (pos == std::string::npos) break;
        }

        // 5) Браузер по умолчанию — сразу, чтобы «открой браузер» не искал
        const std::string browser = default_browser();
        if (!browser.empty()) register_path(browser, resolve_command(browser + ".exe"));
        return found;
    }

    std::string resolve_command(std::string_view command) const override {
        const std::string name = std::string(command);
        // App Paths
        HKEY k = nullptr;
        const std::wstring sub = L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" +
                                 to_wide(name.find('.') == std::string::npos ? name + ".exe" : name);
        if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, sub.c_str(), 0, KEY_READ, &k) == ERROR_SUCCESS) {
            wchar_t value[1024];
            DWORD size = sizeof(value);
            DWORD type = 0;
            if (RegQueryValueExW(k, nullptr, nullptr, &type, reinterpret_cast<LPBYTE>(value), &size) ==
                ERROR_SUCCESS) {
                RegCloseKey(k);
                return normal_path(to_utf8(value));
            }
            RegCloseKey(k);
        }
        // PATH через SearchPath (быстро, без обхода каталогов)
        wchar_t found[MAX_PATH * 2];
        if (SearchPathW(nullptr, to_wide(name).c_str(), nullptr, DWORD(std::size(found)), found, nullptr))
            return normal_path(to_utf8(found));
        return {};
    }

    std::string default_browser() const override {
        // HKCU\...\UrlAssociations\http\UserChoice → ProgId → команда запуска
        HKEY k = nullptr;
        if (RegOpenKeyExW(HKEY_CURRENT_USER,
                          L"SOFTWARE\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\http\\"
                          L"UserChoice",
                          0, KEY_READ, &k) != ERROR_SUCCESS)
            return {};
        wchar_t prog[512];
        DWORD size = sizeof(prog);
        DWORD type = 0;
        std::string prog_id;
        if (RegQueryValueExW(k, L"ProgId", nullptr, &type, reinterpret_cast<LPBYTE>(prog), &size) ==
            ERROR_SUCCESS)
            prog_id = to_utf8(prog);
        RegCloseKey(k);
        if (prog_id.empty()) return {};
        HKEY cmd_key = nullptr;
        const std::wstring path = L"SOFTWARE\\Classes\\" + to_wide(prog_id) + L"\\shell\\open\\command";
        if (RegOpenKeyExW(HKEY_CLASSES_ROOT, path.c_str(), 0, KEY_READ, &cmd_key) != ERROR_SUCCESS)
            return {};
        wchar_t cmd[2048];
        size = sizeof(cmd);
        std::string command;
        if (RegQueryValueExW(cmd_key, nullptr, nullptr, &type, reinterpret_cast<LPBYTE>(cmd), &size) ==
            ERROR_SUCCESS)
            command = to_utf8(cmd);
        RegCloseKey(cmd_key);
        if (command.empty()) return {};
        std::string exe = command;
        if (exe.size() > 1 && exe.front() == '"') {
            const size_t end = exe.find('"', 1);
            exe = end == std::string::npos ? exe.substr(1) : exe.substr(1, end - 1);
        } else {
            const size_t space = exe.find(".exe");
            if (space != std::string::npos) exe = exe.substr(0, space + 4);
        }
        const size_t slash = exe.find_last_of("\\/");
        return slash == std::string::npos ? exe : exe.substr(slash + 1);
    }

    // ------------------------------------------------------------------ экран
    std::vector<MonitorInfo> monitors() const override {
        std::vector<MonitorInfo> out;
        int index = 0;
        EnumDisplayMonitors(
            nullptr, nullptr,
            [](HMONITOR mon, HDC, LPRECT, LPARAM param) -> BOOL {
                auto* list = reinterpret_cast<std::vector<MonitorInfo>*>(param);
                MONITORINFOEXW mi{};
                mi.cbSize = sizeof(mi);
                if (GetMonitorInfoW(mon, &mi)) {
                    MonitorInfo info;
                    info.index = int(list->size()) + 1;
                    info.name = to_utf8(mi.szDevice);
                    info.x = int(mi.rcMonitor.left);
                    info.y = int(mi.rcMonitor.top);
                    info.width = int(mi.rcMonitor.right - mi.rcMonitor.left);
                    info.height = int(mi.rcMonitor.bottom - mi.rcMonitor.top);
                    info.primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
                    info.dpi_scale = dpi_scale_for_monitor(mon);
                    list->push_back(std::move(info));
                }
                return TRUE;
            },
            reinterpret_cast<LPARAM>(&out));
        (void)index;
        return out;
    }

    Frame capture(int monitor, const int* region_xywh) override {
        const std::vector<MonitorInfo> ms = monitors();
        MonitorInfo m{};
        if (!ms.empty()) {
            m = ms.front();
            for (const MonitorInfo& cand : ms)
                if (cand.index == monitor) m = cand;
        } else {
            m.width = GetSystemMetrics(SM_CXVIRTUALSCREEN);
            m.height = GetSystemMetrics(SM_CYVIRTUALSCREEN);
            m.x = GetSystemMetrics(SM_XVIRTUALSCREEN);
            m.y = GetSystemMetrics(SM_YVIRTUALSCREEN);
        }
        int x = m.x, y = m.y, w = m.width, h = m.height;
        if (region_xywh) {
            x = region_xywh[0];
            y = region_xywh[1];
            w = region_xywh[2];
            h = region_xywh[3];
        }
        if (w <= 0 || h <= 0) return Frame{};
        Frame frame;
        // 1) Быстрый путь — DXGI Desktop Duplication
        if (monitor <= 1 && capture_dxgi(x, y, w, h, &frame)) return frame;
        // 2) Совместимость — GDI BitBlt
        return capture_gdi(x, y, w, h);
    }

    // ---------------------------------------------------------------- система
    bool set_wallpaper(std::string_view path) override {
        std::string p = std::string(path);
        const std::string norm = lower_ascii_str(p);
        if (norm.rfind("http://", 0) == 0 || norm.rfind("https://", 0) == 0) {
            // обои из интернета: скачиваем во временный файл и ставим
            const std::string tmp = env_var("TEMP") + "\\agent_wallpaper.jpg";
            const std::string cmd = "curl.exe -L -o \"" + tmp + "\" \"" + p + "\"";
            const ExecResult dl = run_command(cmd, {}, 20000);
            if (!dl.ok()) return false;
            p = tmp;
        }
        if (!file_exists(p)) return false;
        return SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, (void*)to_wide(p).c_str(),
                                     SPIF_UPDATEINIFILE | SPIF_SENDCHANGE) != 0;
    }

    bool open_settings(std::string_view page) override {
        std::string p = std::string(page);
        if (p.rfind("ms-settings:", 0) != 0 && p.rfind("ms-", 0) != 0) p = "ms-settings:" + p;
        return shell_open(p, {});
    }

    bool set_volume(int percent) override {
        if (percent < 0) percent = 0;
        if (percent > 100) percent = 100;
        if (set_volume_com(percent)) return true;
        // запасной путь: шаги по 2% через WM_APPCOMMAND
        const int current = get_volume_com();
        if (current < 0) return false;
        const int diff = percent - current;
        const int steps = diff / 2;
        for (int i = 0; i < (steps < 0 ? -steps : steps); ++i) {
            SendMessageW(HWND_BROADCAST, WM_APPCOMMAND, 0,
                         MAKELPARAM(0, diff > 0 ? APPCOMMAND_VOLUME_UP : APPCOMMAND_VOLUME_DOWN));
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return true;
    }

    int get_volume() const override { return get_volume_com(); }
    std::string cwd() const override {
        wchar_t buf[MAX_PATH * 2];
        const DWORD n = GetCurrentDirectoryW(DWORD(std::size(buf)), buf);
        return n ? to_utf8(std::wstring(buf, n)) : std::string();
    }
    std::string env(std::string_view name) const override {
        return env_var(std::string(name).c_str());
    }

private:
    // ------------------------------------------------------------------ утилиты
    static std::string window_title(HWND hwnd) {
        wchar_t title[1024];
        const int n = GetWindowTextW(hwnd, title, 1024);
        return n > 0 ? to_utf8(std::wstring(title, n)) : std::string();
    }

    static double dpi_scale_for_monitor(HMONITOR mon) {
        // shcore.dll: GetDpiForMonitor (без манифеста может отсутствовать — тогда 1.0)
        HMODULE shcore = LoadLibraryW(L"shcore.dll");
        if (!shcore) return 1.0;
        typedef HRESULT(WINAPI * PFN)(HMONITOR, int, UINT*, UINT*);
        auto fn = reinterpret_cast<PFN>(GetProcAddress(shcore, "GetDpiForMonitor"));
        double scale = 1.0;
        if (fn) {
            UINT dpi_x = 96, dpi_y = 96;
            if (SUCCEEDED(fn(mon, 0 /*MDT_EFFECTIVE_DPI*/, &dpi_x, &dpi_y))) scale = double(dpi_x) / 96.0;
        }
        FreeLibrary(shcore);
        return scale;
    }

    static std::string normalize_app_key(const std::string& name) {
        std::string out;
        for (char c : lower_ascii_str(name)) {
            if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) out.push_back(c);
            else if (c == ' ' || c == '-' || c == '.' || c == '_' || c == '(' || c == ')')
                out.push_back('_');
        }
        while (!out.empty() && out.front() == '_') out.erase(out.begin());
        while (!out.empty() && out.back() == '_') out.pop_back();
        if (out.size() > 48) out.resize(48);
        return out;
    }

    // Путь из .lnk: через COM IShellLink (быстро и без разбора формата)
    std::string resolve_lnk(const std::string& lnk_path) {
        HMODULE ole = LoadLibraryW(L"ole32.dll");
        if (!ole) return {};
        typedef HRESULT(WINAPI * PFN_CoCreateInstance)(REFCLSID, LPUNKNOWN, DWORD, REFIID, LPVOID*);
        auto create = reinterpret_cast<PFN_CoCreateInstance>(GetProcAddress(ole, "CoCreateInstance"));
        if (!create) {
            FreeLibrary(ole);
            return {};
        }
        static const CLSID clsid_shell_link = {0x00021401, 0x0000, 0x0000, {0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46}};
        static const IID iid_shell_link = {0x000214F9, 0x0000, 0x0000, {0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46}};
        struct IUnknownLike {
            virtual HRESULT QueryInterface(REFIID, void**) = 0;
            virtual ULONG AddRef() = 0;
            virtual ULONG Release() = 0;
        };
        struct IPersistFile : IUnknownLike {
            virtual HRESULT GetClassID(GUID*) = 0;
            virtual HRESULT IsDirty() = 0;
            virtual HRESULT Load(LPCOLESTR, DWORD) = 0;
        };
        struct IShellLinkW : IUnknownLike {
            virtual HRESULT GetPath(LPWSTR, int, void*, DWORD) = 0;
        };
        void* link = nullptr;
        if (FAILED(create(clsid_shell_link, nullptr, CLSCTX_INPROC_SERVER, iid_shell_link, &link)))
            return {};
        std::string result;
        auto* shell_link = reinterpret_cast<IShellLinkW*>(link);
        void* persist = nullptr;
        static const IID iid_persist_file = {0x0000010b, 0x0000, 0x0000, {0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46}};
        if (SUCCEEDED(reinterpret_cast<IUnknownLike*>(link)->QueryInterface(iid_persist_file, &persist)) &&
            persist) {
            const std::wstring w = to_wide(lnk_path);
            if (SUCCEEDED(reinterpret_cast<IPersistFile*>(persist)->Load(w.c_str(), 0))) {
                wchar_t target[MAX_PATH * 2];
                if (SUCCEEDED(shell_link->GetPath(target, int(std::size(target)), nullptr, 0)))
                    result = normal_path(to_utf8(target));
            }
            reinterpret_cast<IUnknownLike*>(persist)->Release();
        }
        reinterpret_cast<IUnknownLike*>(link)->Release();
        FreeLibrary(ole);
        return result;
    }

    bool shell_open(const std::string& target, const std::vector<std::string>& args) {
        const std::wstring wtarget = to_wide(target);
        std::wstring wargs;
        for (size_t i = 0; i < args.size(); ++i) {
            if (i) wargs += L' ';
            wargs += to_wide(quote_arg(args[i]));
        }
        const HINSTANCE rc = ShellExecuteW(nullptr, L"open", wtarget.c_str(),
                                           wargs.empty() ? nullptr : wargs.c_str(), nullptr,
                                           SW_SHOWNORMAL);
        return reinterpret_cast<INT_PTR>(rc) > 32;
    }

    ExecResult run_process(const std::wstring& program, const std::wstring& args,
                           std::string_view cwd, int timeout_ms, bool hidden) {
        ExecResult r;
        SECURITY_ATTRIBUTES sa{};
        sa.nLength = sizeof(sa);
        sa.bInheritHandle = TRUE;
        HANDLE out_read = nullptr, out_write = nullptr;
        if (!CreatePipe(&out_read, &out_write, &sa, 0)) {
            r.error = last_error_text();
            return r;
        }
        SetHandleInformation(out_read, HANDLE_FLAG_INHERIT, 0);
        STARTUPINFOW si{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES | (hidden ? STARTF_USESHOWWINDOW : 0);
        si.wShowWindow = hidden ? SW_HIDE : SW_SHOWNORMAL;
        si.hStdOutput = out_write;
        si.hStdError = out_write;
        si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        PROCESS_INFORMATION pi{};
        std::wstring cmd = program + L" " + args;
        const std::wstring wcwd = to_wide(std::string(cwd));
        const int64_t t0 = now_ms();
        const BOOL started = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE,
                                            CREATE_NO_WINDOW, nullptr,
                                            wcwd.empty() ? nullptr : wcwd.c_str(), &si, &pi);
        CloseHandle(out_write);
        if (!started) {
            r.error = last_error_text();
            CloseHandle(out_read);
            return r;
        }
        std::string output;
        char buf[4096];
        DWORD read = 0;
        const DWORD wait_ms = timeout_ms > 0 ? DWORD(timeout_ms) : 30000;
        const DWORD waited = WaitForSingleObject(pi.hProcess, wait_ms);
        if (waited == WAIT_TIMEOUT) {
            TerminateProcess(pi.hProcess, 1);
            r.timed_out = true;
        }
        while (ReadFile(out_read, buf, sizeof(buf) - 1, &read, nullptr) && read > 0) {
            output.append(buf, read);
            if (output.size() > (1u << 22)) break;
        }
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        CloseHandle(out_read);
        r.started = true;
        r.exit_code = int(code);
        r.stdout_text = output;
        r.ms = double(now_ms() - t0);
        return r;
    }

    // ------------------------------------------------------------- SendInput
    static bool send_mouse(DWORD type, DWORD flags, int x, int y, int data, DWORD extra) {
        INPUT in{};
        in.type = type;
        in.mi.dx = x;
        in.mi.dy = y;
        in.mi.mouseData = DWORD(data);
        in.mi.dwFlags = flags;
        in.mi.dwExtraInfo = extra;
        return SendInput(1, &in, sizeof(INPUT)) == 1;
    }

    static bool send_key(WORD vk, bool down) {
        INPUT in{};
        in.type = INPUT_KEYBOARD;
        in.ki.wVk = vk;
        in.ki.dwFlags = down ? 0 : KEYEVENTF_KEYUP;
        return SendInput(1, &in, sizeof(INPUT)) == 1;
    }

    // ------------------------------------------------------------------ экран
    bool capture_dxgi(int x, int y, int w, int h, Frame* out) {
        // Desktop Duplication: получение кадра с GPU без BitBlt (десятки раз быстрее).
        // Если что-то не сложилось (RDP, драйвер, старый Windows) — вернём false и
        // вызывающий уйдёт на GDI.
        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* ctx = nullptr;
        IDXGIOutputDuplication* dup = nullptr;
        IDXGIResource* resource = nullptr;
        ID3D11Texture2D* texture = nullptr;
        D3D11_TEXTURE2D_DESC desc{};
        DXGI_OUTDUPL_FRAME_INFO info{};
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
                                       D3D11_SDK_VERSION, &device, nullptr, &ctx);
        if (FAILED(hr)) return false;
        IDXGIDevice* dxgi_device = nullptr;
        IDXGIAdapter* adapter = nullptr;
        IDXGIOutput* output = nullptr;
        bool ok = false;
        do {
            if (FAILED(device->QueryInterface(__uuidof(IDXGIDevice), (void**)&dxgi_device))) break;
            if (FAILED(dxgi_device->GetAdapter(&adapter))) break;
            if (FAILED(adapter->EnumOutputs(0, &output))) break;
            IDXGIOutput1* output1 = nullptr;
            if (FAILED(output->QueryInterface(__uuidof(IDXGIOutput1), (void**)&output1))) break;
            hr = output1->DuplicateOutput(device, &dup);
            output1->Release();
            if (FAILED(hr)) break;
            hr = dup->AcquireNextFrame(50, &info, &resource);
            if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
                // Кадр не менялся: получаем описание последнего доступного
                hr = dup->AcquireNextFrame(100, &info, &resource);
            }
            if (FAILED(hr)) break;
            if (FAILED(resource->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&texture))) {
                dup->ReleaseFrame();
                break;
            }
            texture->GetDesc(&desc);
            D3D11_TEXTURE2D_DESC staging = desc;
            staging.Usage = D3D11_USAGE_STAGING;
            staging.BindFlags = 0;
            staging.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            staging.MiscFlags = 0;
            ID3D11Texture2D* readback = nullptr;
            if (SUCCEEDED(device->CreateTexture2D(&staging, nullptr, &readback)) && readback) {
                ctx->CopyResource(readback, texture);
                D3D11_MAPPED_SUBRESOURCE mapped{};
                if (SUCCEEDED(ctx->Map(readback, 0, D3D11_MAP_READ, 0, &mapped))) {
                    const int src_w = int(desc.Width);
                    const int src_h = int(desc.Height);
                    const int cx = std::max(0, std::min(x, src_w - 1));
                    const int cy = std::max(0, std::min(y, src_h - 1));
                    const int cw = std::min(w, src_w - cx);
                    const int ch = std::min(h, src_h - cy);
                    out->width = cw;
                    out->height = ch;
                    out->origin_x = cx;
                    out->origin_y = cy;
                    out->stride = cw * 4;
                    out->backend = "dxgi";
                    out->pixels.resize(size_t(cw) * size_t(ch) * 4);
                    const auto* src = static_cast<const uint8_t*>(mapped.pData);
                    for (int row = 0; row < ch; ++row) {
                        std::memcpy(out->pixels.data() + size_t(row) * size_t(cw) * 4,
                                    src + size_t(row + cy) * mapped.RowPitch + size_t(cx) * 4,
                                    size_t(cw) * 4);
                    }
                    ctx->Unmap(readback, 0);
                    ok = true;
                }
                readback->Release();
            }
            dup->ReleaseFrame();
        } while (false);
        if (texture) texture->Release();
        if (resource) resource->Release();
        if (dup) dup->Release();
        if (output) output->Release();
        if (adapter) adapter->Release();
        if (dxgi_device) dxgi_device->Release();
        if (ctx) ctx->Release();
        if (device) device->Release();
        if (ok) {
            SYSTEMTIME st{};
            GetSystemTime(&st);
            out->captured_ms = now_ms();
        }
        return ok;
    }

    Frame capture_gdi(int x, int y, int w, int h) {
        Frame frame;
        HDC screen = GetDC(nullptr);
        if (!screen) return frame;
        HDC mem = CreateCompatibleDC(screen);
        BITMAPINFO bmi{};
        bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bmi.bmiHeader.biWidth = w;
        bmi.bmiHeader.biHeight = -h;   // top-down: строка 0 сверху, как на экране
        bmi.bmiHeader.biPlanes = 1;
        bmi.bmiHeader.biBitCount = 32;
        bmi.bmiHeader.biCompression = BI_RGB;
        void* bits = nullptr;
        HBITMAP bitmap = CreateDIBSection(mem, &bmi, DIB_RGB_COLORS, &bits, nullptr, 0);
        if (!bitmap || !bits) {
            if (bitmap) DeleteObject(bitmap);
            DeleteDC(mem);
            ReleaseDC(nullptr, screen);
            return frame;
        }
        HGDIOBJ old = SelectObject(mem, bitmap);
        const BOOL ok = BitBlt(mem, 0, 0, w, h, screen, x, y, SRCCOPY | CAPTUREBLT);
        SelectObject(mem, old);
        if (ok) {
            frame.width = w;
            frame.height = h;
            frame.origin_x = x;
            frame.origin_y = y;
            frame.stride = w * 4;
            frame.pixels.assign(static_cast<uint8_t*>(bits),
                                static_cast<uint8_t*>(bits) + size_t(w) * size_t(h) * 4);
            frame.backend = "gdi";
            frame.captured_ms = now_ms();
        }
        DeleteObject(bitmap);
        DeleteDC(mem);
        ReleaseDC(nullptr, screen);
        return frame;
    }
};

std::unique_ptr<IPlatform> make_platform() { return std::make_unique<WindowsPlatform>(); }

}  // namespace agent

#endif  // _WIN32
