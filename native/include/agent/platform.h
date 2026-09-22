// Платформенный слой: всё, что зависит от ОС, спрятано за IPlatform.
//
// Windows-реализация (platform_win32.cpp) использует нативные вызовы:
//   * процессы   — CreateToolhelp32Snapshot / OpenProcess / TerminateProcess
//   * окна       — EnumWindows / SetForegroundWindow / ShowWindow / SetWinEventHook
//   * ввод       — SendInput (мышь/клавиатура/хоткеи), без посимвольного ввода при длинном тексте
//   * файлы      — Win32 file API (ReadDirectoryChangesW для ожиданий)
//   * оболочка   — ShellExecuteEx / CreateProcess + CREATE_NO_WINDOW
//   * реестр     — RegOpenKeyEx / RegQueryValueEx (App Paths, Uninstall, URLAssociations)
//   * приложения — Start Menu (.lnk), Store/UWP (shell:AppsFolder), PATH
//   * экран      — DXGI Desktop Duplication (быстро) → Windows.Graphics.Capture (окно) → GDI (fallback)
//   * буфер      — Clipboard API
//   * обои/настр.— SystemParametersInfo / ms-settings:
//
// Linux-реализация (platform_linux.cpp) — dev-режим: нужна, чтобы ядро логики
// (router/intent/registry/optimizer/batch) собиралось и покрывалось тестами там,
// где нет Windows. Системные вызовы там честные, но упрощённые.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "core.h"

namespace agent {

struct ProcessInfo {
    uint32_t pid = 0;
    std::string name;
    std::string path;
    double cpu_percent = 0.0;
    uint64_t memory_bytes = 0;
};

struct WindowInfo {
    uint64_t handle = 0;      // HWND на Windows
    uint32_t pid = 0;
    std::string title;
    std::string process;
    int x = 0, y = 0, width = 0, height = 0;
    bool visible = true;
    bool minimized = false;
};

struct MonitorInfo {
    int index = 1;
    std::string name;
    int x = 0, y = 0, width = 0, height = 0;
    bool primary = true;
    double dpi_scale = 1.0;
};

struct Frame {
    int width = 0;
    int height = 0;
    int origin_x = 0;         // координаты области в системе экрана
    int origin_y = 0;
    int stride = 0;           // байт в строке (BGRA)
    std::vector<uint8_t> pixels;
    int64_t captured_ms = 0;
    bool headless = false;    // синтетический кадр (нет дисплея)
    const char* backend = "none";
};

struct ExecResult {
    bool started = false;
    int exit_code = -1;
    std::string stdout_text;
    std::string stderr_text;
    bool timed_out = false;
    double ms = 0.0;
    std::string error;
    bool ok() const { return started && !timed_out && exit_code == 0; }
};

struct LaunchResult {
    bool ok = false;
    uint32_t pid = 0;
    std::string method;
    std::string message;
    std::string error;
    double ms = 0.0;
};

struct FileEntry {
    std::string path;
    std::string name;
    bool is_dir = false;
    uint64_t size = 0;
    int64_t modified_ms = 0;
};

class IPlatform {
public:
    virtual ~IPlatform() = default;
    virtual const char* name() const = 0;           // "windows" | "linux" | "macos"
    virtual bool has_display() const = 0;
    // --- процессы ---
    virtual std::vector<ProcessInfo> processes() const = 0;
    virtual bool process_running(std::string_view name) const = 0;
    virtual bool kill_process(std::string_view name, bool force) = 0;
    virtual LaunchResult spawn_detached(const std::string& command, std::string_view args = {}) = 0;
    // --- окна ---
    virtual std::vector<WindowInfo> windows() const = 0;
    virtual std::optional<WindowInfo> find_window(std::string_view title) const = 0;
    virtual std::optional<WindowInfo> active_window() const = 0;
    virtual bool activate_window(uint64_t handle) = 0;
    virtual bool close_window(uint64_t handle) = 0;
    // --- ввод (SendInput) ---
    virtual bool mouse_move(int x, int y) = 0;
    virtual bool mouse_click(int x, int y, int button, int clicks) = 0;
    virtual bool key_press(std::string_view key) = 0;
    virtual bool hotkey(std::string_view keys) = 0;
    virtual bool type_text(std::string_view text) = 0;   // длинный текст → clipboard + Ctrl+V
    // --- буфер обмена ---
    virtual std::string clipboard_get() = 0;
    virtual bool clipboard_set(std::string_view text) = 0;
    // --- файлы ---
    virtual bool file_exists(std::string_view path) const = 0;
    virtual bool is_dir(std::string_view path) const = 0;
    virtual bool mkdir(std::string_view path, bool recursive = true) = 0;
    virtual bool write_file(std::string_view path, std::string_view content, bool append = false) = 0;
    virtual std::string read_file(std::string_view path, size_t max_bytes = 1 << 20) = 0;
    virtual bool remove_path(std::string_view path, bool recursive, bool to_trash) = 0;
    virtual bool move_path(std::string_view src, std::string_view dst) = 0;
    virtual bool copy_path(std::string_view src, std::string_view dst) = 0;
    virtual std::vector<FileEntry> list_dir(std::string_view path) = 0;
    virtual std::vector<FileEntry> search_files(std::string_view root, std::string_view pattern,
                                                size_t limit) = 0;
    // --- оболочка ---
    virtual ExecResult run_command(std::string_view command, std::string_view cwd,
                                   int timeout_ms) = 0;
    virtual ExecResult run_powershell(std::string_view script, std::string_view cwd,
                                      int timeout_ms) = 0;
    virtual bool open_uri(std::string_view uri) = 0;      // ссылка/протокол через оболочку
    virtual bool open_path(std::string_view path) = 0;    // файл/папка в системном приложении
    // --- реестр приложений / обнаружение ---
    virtual int discover_apps(class AppRegistry& reg) = 0;
    virtual std::string resolve_command(std::string_view command) const = 0;  // PATH/App Paths
    virtual std::string default_browser() const = 0;
    // --- экран ---
    virtual std::vector<MonitorInfo> monitors() const = 0;
    virtual Frame capture(int monitor, const int* region_xywh) = 0;   // region: x,y,w,h
    // --- система ---
    virtual bool set_wallpaper(std::string_view path) = 0;
    virtual bool open_settings(std::string_view page) = 0;
    virtual bool set_volume(int percent) = 0;
    virtual int get_volume() const = 0;
    virtual std::string cwd() const = 0;
    virtual std::string env(std::string_view name) const = 0;
};

std::unique_ptr<IPlatform> make_platform();

// ---------------------------------------------------------------------------
//  Кадры для зрения: уменьшение, PNG для модели, сравнение (0..1), base64.
//  Нужно, чтобы «посмотреть на экран» стоило миллисекунды, а не секунды:
//  в модель уходит уменьшенный PNG, а не сырой BGRA всего рабочего стола.
// ---------------------------------------------------------------------------
Frame frame_shrink(const Frame& src, int max_pixels, int max_side = 2560);

// Пересчёт запрошенной области экрана в координаты кадра (монитор может быть не
// (0,0): при нескольких мониторах часть из них имеет отрицательные координаты).
// Нужен, чтобы снимок и клик жили в одной системе координат.
struct FrameCrop {
    bool covered = false;   // запрошенная область целиком внутри кадра
    int x = 0;              // смещение области внутри кадра
    int y = 0;
    int width = 0;
    int height = 0;
};
FrameCrop crop_into_frame(const Frame& frame, int req_x, int req_y, int req_w, int req_h);
bool png_encode(const Frame& frame, std::vector<uint8_t>& out);
double frame_difference(const Frame& a, const Frame& b);
std::string base64_encode(const uint8_t* data, size_t size);

// ---------------------------------------------------------------------------
//  Ожидания вместо sleep (ТЗ §17): состояние, а не время.
// ---------------------------------------------------------------------------
struct WaitResult {
    bool ok = false;
    double elapsed_ms = 0.0;
    int polls = 0;
    std::string detail;
};

class WaitManager {
public:
    explicit WaitManager(IPlatform* platform, int default_timeout_ms = 10000)
        : platform_(platform), default_timeout_ms_(default_timeout_ms) {}

    WaitResult wait_condition(const std::function<bool()>& predicate, int timeout_ms,
                              const std::string& description = "");
    WaitResult wait_process_started(std::string_view name, int timeout_ms = 10000);
    WaitResult wait_process_finished(std::string_view name, int timeout_ms = 15000);
    WaitResult wait_window_created(std::string_view title, int timeout_ms = 8000);
    WaitResult wait_window_active(std::string_view title, int timeout_ms = 8000);
    WaitResult wait_file_exists(std::string_view path, int timeout_ms = 5000,
                                uint64_t min_size = 0, double stable_ms = 0.0);
    WaitResult wait_file_gone(std::string_view path, int timeout_ms = 5000);
    WaitResult wait_file_changed(std::string_view path, int64_t since_ms, int timeout_ms = 10000);
    WaitResult wait_port_open(std::string_view host_port, int timeout_ms = 5000);
    WaitResult wait_url_loaded(std::string_view url, std::string_view title, int timeout_ms = 15000);
    // Отмена (например, пользователь нажал «Стоп»): проверяется между опросами.
    void set_cancel(std::function<bool()> fn) { cancel_ = std::move(fn); }
    // Адаптивный шаг опроса: 2 мс → 25 мс → 100 мс → 250 мс.
    static int next_interval_us(int current_us);

private:
    IPlatform* platform_ = nullptr;
    int default_timeout_ms_ = 10000;
    std::function<bool()> cancel_;
};

}  // namespace agent
