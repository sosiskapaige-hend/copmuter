// Linux-реализация IPlatform — dev/headless-режим.
//
// Нужна, чтобы ядро логики (intent/router/registry/optimizer/batch/waits) собиралось
// и покрывалось тестами без Windows. Системные вызовы честные, но упрощённые:
//   процессы  — /proc (name/cmdline/rss)
//   окна      — xdotool, если есть DISPLAY; иначе пусто (headless)
//   ввод      — xdotool/xclip, иначе виртуальный режим (без GUI тесты всё равно идут)
//   файлы     — std::filesystem
//   оболочка  — bash -c с таймаутом и убийством группы процессов
//   приложения— .desktop из /usr/share/applications и ~/.local/share/applications
//   экран     — синтетический кадр, если дисплея нет (headless=true)
#include <poll.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <thread>

#include "agent/intent.h"
#include "agent/platform.h"
#include "agent/util.h"

namespace fs = std::filesystem;

namespace agent {
namespace {

std::string read_small(const std::string& path, size_t max_bytes = 1 << 16) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::string out;
    out.resize(max_bytes);
    f.read(out.data(), std::streamsize(max_bytes));
    out.resize(size_t(f.gcount()));
    return out;
}

std::string trim(std::string_view s) {
    size_t b = 0, e = s.size();
    while (b < e && (unsigned char)s[b] <= ' ') ++b;
    while (e > b && (unsigned char)s[e - 1] <= ' ') --e;
    return std::string(s.substr(b, e - b));
}

std::string lower(std::string s) {
    for (char& c : s)
        if (c >= 'A' && c <= 'Z') c = char(c - 'A' + 'a');
    return s;
}

// Однократный запуск процесса с захватом вывода и таймаутом.
ExecResult run_capture(const std::vector<std::string>& argv, const std::string& cwd,
                       int timeout_ms) {
    ExecResult r;
    if (argv.empty()) {
        r.error = "empty command";
        return r;
    }
    int out_pipe[2];
    if (pipe(out_pipe) != 0) {
        r.error = "pipe() failed";
        return r;
    }
    const int64_t t0 = now_ms();
    pid_t pid = fork();
    if (pid < 0) {
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        r.error = "fork() failed";
        return r;
    }
    if (pid == 0) {
        // ребёнок: свой процесс-группа, чтобы можно было убить всё дерево
        setpgid(0, 0);
        dup2(out_pipe[1], STDOUT_FILENO);
        dup2(out_pipe[1], STDERR_FILENO);
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        if (!cwd.empty() && chdir(cwd.c_str()) != 0) _exit(127);
        std::vector<char*> cargv;
        cargv.reserve(argv.size() + 1);
        for (const std::string& a : argv) cargv.push_back(const_cast<char*>(a.c_str()));
        cargv.push_back(nullptr);
        execvp(cargv[0], cargv.data());
        _exit(127);
    }
    setpgid(pid, pid);
    ::close(out_pipe[1]);

    std::string output;
    std::array<char, 4096> buf{};
    int status = 0;
    const int poll_ms = 5;
    int waited = 0;
    bool exited = false;
    while (true) {
        // читаем всё, что готово (неблокирующе через poll)
        struct pollfd pfd{out_pipe[0], POLLIN, 0};
        const int pr = ::poll(&pfd, 1, poll_ms);
        if (pr > 0 && (pfd.revents & POLLIN)) {
            const ssize_t n = ::read(out_pipe[0], buf.data(), buf.size());
            if (n > 0) output.append(buf.data(), size_t(n));
        }
        const pid_t w = waitpid(pid, &status, WNOHANG);
        if (w == pid) {
            exited = true;
        }
        if (exited) {
            // добираем остаток
            ssize_t n;
            while ((n = ::read(out_pipe[0], buf.data(), buf.size())) > 0)
                output.append(buf.data(), size_t(n));
            break;
        }
        waited += poll_ms;
        if (timeout_ms > 0 && waited >= timeout_ms) {
            kill(-pid, SIGKILL);
            kill(pid, SIGKILL);
            waitpid(pid, &status, 0);
            r.timed_out = true;
            break;
        }
    }
    ::close(out_pipe[0]);
    r.started = true;
    r.ms = double(now_ms() - t0);
    r.stdout_text = output;
    if (WIFEXITED(status)) r.exit_code = WEXITSTATUS(status);
    else if (WIFSIGNALED(status)) r.exit_code = 128 + WTERMSIG(status);
    return r;
}

bool which(const std::string& bin, std::string* out_path = nullptr) {
    const char* path_env = ::getenv("PATH");
    if (!path_env) return false;
    std::stringstream ss(path_env);
    std::string dir;
    while (std::getline(ss, dir, ':')) {
        if (dir.empty()) continue;
        const std::string candidate = dir + "/" + bin;
        if (access(candidate.c_str(), X_OK) == 0) {
            if (out_path) *out_path = candidate;
            return true;
        }
    }
    return false;
}

std::string tool(const std::string& bin) {
    std::string p;
    return which(bin, &p) ? p : std::string();
}

// «Visual Studio Code» → «visual_studio_code» (ключ записи в реестре приложений).
std::string normalize_key(const std::string& name) {
    std::string out;
    for (char c : name) {
        const unsigned char u = (unsigned char)c;
        if (u >= 'A' && u <= 'Z') out.push_back(char(u - 'A' + 'a'));
        else if ((u >= 'a' && u <= 'z') || (u >= '0' && u <= '9')) out.push_back(char(u));
        else if (u == ' ' || u == '-' || u == '.') out.push_back('_');
    }
    while (!out.empty() && out.front() == '_') out.erase(out.begin());
    while (!out.empty() && out.back() == '_') out.pop_back();
    return out;
}

}  // namespace

class LinuxPlatform : public IPlatform {
public:
    LinuxPlatform() {
        display_ = env("DISPLAY");
        try {
            cwd_ = fs::current_path().string();
        } catch (...) {
            cwd_ = ".";
        }
        xdotool_ = tool("xdotool");
        xclip_ = tool("xclip");
        xdg_open_ = tool("xdg-open");
        if (xdotool_.empty() || display_.empty()) xdotool_.clear();  // без X11 окна недоступны
        clip_store_path_ = (fs::temp_directory_path() / "agent_clipboard.txt").string();
    }

    const char* name() const override { return "linux"; }
    bool has_display() const override { return !xdotool_.empty(); }

    // ---------------------------------------------------------------- процессы
    std::vector<ProcessInfo> processes() const override {
        std::vector<ProcessInfo> out;
        std::error_code ec;
        for (const fs::directory_entry& e : fs::directory_iterator("/proc", ec)) {
            if (ec) break;
            const std::string name = e.path().filename().string();
            if (name.empty() || !std::all_of(name.begin(), name.end(), ::isdigit)) continue;
            ProcessInfo pi;
            pi.pid = uint32_t(std::strtoul(name.c_str(), nullptr, 10));
            if (pi.pid == 0) continue;
            std::string comm = trim(read_small("/proc/" + name + "/comm", 256));
            if (comm.empty()) continue;
            pi.name = comm;
            std::string cl = read_small("/proc/" + name + "/cmdline", 4096);
            std::replace(cl.begin(), cl.end(), '\0', ' ');
            cl = trim(cl);
            if (!cl.empty()) {
                std::stringstream ss(cl);
                std::getline(ss, pi.path, ' ');
            } else {
                pi.path = pi.name;
            }
            // RSS из /proc/<pid>/statm (страницы * 4096)
            std::string statm = read_small("/proc/" + name + "/statm", 256);
            std::stringstream sm(statm);
            uint64_t size_pages = 0, rss_pages = 0;
            if (sm >> size_pages >> rss_pages) pi.memory_bytes = rss_pages * 4096ull;
            out.push_back(std::move(pi));
        }
        return out;
    }

    bool process_running(std::string_view name) const override {
        if (name.empty()) return false;
        const std::string want = lower(std::string(name));
        for (const ProcessInfo& p : processes()) {
            const std::string n = lower(p.name);
            if (n == want || n == want + ".exe" || want == n + ".exe") return true;
            if (n.find(want) != std::string::npos) return true;
        }
        return false;
    }

    bool kill_process(std::string_view name, bool force) override {
        if (name.empty()) return false;
        const std::vector<std::string> argv = {"pkill", force ? "-9" : "-15", "-f",
                                               std::string(name)};
        const ExecResult r = run_capture(argv, {}, 5000);
        return r.started && !r.timed_out && r.exit_code <= 1;
    }

    LaunchResult spawn_detached(const std::string& command, std::string_view args) override {
        LaunchResult r;
        if (command.empty()) {
            r.error = "empty command";
            return r;
        }
        std::string cmd = command;
        if (!args.empty()) cmd += " " + std::string(args);
        // Проверяем, что запускать действительно есть что: иначе shell с «&»
        // вернёт 0 и мы бы отчитались об успехе, которого нет.
        const std::string program = cmd.substr(0, cmd.find(' '));
        bool found = false;
        if (!program.empty() && program[0] == '/') {
            found = access(program.c_str(), X_OK) == 0;
        } else if (!program.empty()) {
            found = which(program, nullptr);
        }
        if (!found) {
            r.error = "команда не найдена: " + program;
            return r;
        }
        // запуск через sh -c в фоне (аналог ShellExecute); PID не отслеживаем —
        // вызывающий проверяет запуск через wait_process_started
        const std::string full = "setsid " + cmd + " >/dev/null 2>&1 &";
        const int64_t t0 = now_ms();
        const int rc = std::system(full.c_str());
        r.ms = double(now_ms() - t0);
        r.method = "spawn";
        r.ok = (rc == 0);
        if (!r.ok) r.error = "spawn failed: " + std::to_string(rc);
        return r;
    }

    // -------------------------------------------------------------------- окна
    std::vector<WindowInfo> windows() const override {
        std::vector<WindowInfo> out;
        if (xdotool_.empty()) return out;
        const ExecResult ids = run_capture({xdotool_, "search", "--onlyvisible", "--name", ".*"},
                                           {}, 3000);
        if (!ids.started) return out;
        for (const std::string& line : split_lines(ids.stdout_text)) {
            WindowInfo w;
            w.handle = std::strtoull(line.c_str(), nullptr, 10);
            if (!w.handle) continue;
            const std::string id = line;
            w.title = trim(run_capture({xdotool_, "getwindowname", id}, {}, 1000).stdout_text);
            if (w.title.empty() || lower(w.title).find("traceback") != std::string::npos) continue;
            const ExecResult geo =
                run_capture({xdotool_, "getwindowgeometry", "--shell", id}, {}, 1000);
            for (const std::string& g : split_lines(geo.stdout_text)) {
                const size_t eq = g.find('=');
                if (eq == std::string::npos) continue;
                const std::string key = g.substr(0, eq);
                const int val = std::atoi(g.c_str() + eq + 1);
                if (key == "X") w.x = val;
                else if (key == "Y") w.y = val;
                else if (key == "WIDTH") w.width = val;
                else if (key == "HEIGHT") w.height = val;
            }
            const ExecResult pidv = run_capture({xdotool_, "getwindowpid", id}, {}, 1000);
            w.pid = uint32_t(std::strtoul(trim(pidv.stdout_text).c_str(), nullptr, 10));
            if (w.pid) {
                const std::string comm = trim(read_small("/proc/" + std::to_string(w.pid) + "/comm"));
                w.process = comm.empty() ? std::string() : comm;
            }
            out.push_back(std::move(w));
        }
        return out;
    }

    std::optional<WindowInfo> find_window(std::string_view title) const override {
        if (title.empty()) return std::nullopt;
        const std::string want = lower(std::string(title));
        for (const WindowInfo& w : windows()) {
            const std::string t = lower(w.title);
            if (t.find(want) != std::string::npos || lower(w.process).find(want) != std::string::npos)
                return w;
        }
        return std::nullopt;
    }

    std::optional<WindowInfo> active_window() const override {
        if (xdotool_.empty()) return std::nullopt;
        const ExecResult a = run_capture({xdotool_, "getactivewindow"}, {}, 1000);
        const std::string id = trim(a.stdout_text);
        if (id.empty()) return std::nullopt;
        const ExecResult t = run_capture({xdotool_, "getwindowname", id}, {}, 1000);
        WindowInfo w;
        w.handle = std::strtoull(id.c_str(), nullptr, 10);
        w.title = trim(t.stdout_text);
        const ExecResult pidv = run_capture({xdotool_, "getwindowpid", id}, {}, 1000);
        w.pid = uint32_t(std::strtoul(trim(pidv.stdout_text).c_str(), nullptr, 10));
        if (w.pid) w.process = trim(read_small("/proc/" + std::to_string(w.pid) + "/comm"));
        return w;
    }

    bool activate_window(uint64_t handle) override {
        if (xdotool_.empty() || !handle) return false;
        const std::string id = std::to_string(handle);
        const ExecResult w = run_capture({xdotool_, "windowactivate", "--sync", id}, {}, 3000);
        return w.started && !w.timed_out && w.exit_code == 0;
    }

    bool close_window(uint64_t handle) override {
        if (xdotool_.empty() || !handle) return false;
        const ExecResult w = run_capture({xdotool_, "windowkill", std::to_string(handle)}, {}, 3000);
        return w.started && w.exit_code == 0;
    }

    // -------------------------------------------------------------------- ввод
    bool mouse_move(int x, int y) override {
        if (xdotool_.empty()) return set_virtual("mouse_move");
        return run_capture({xdotool_, "mousemove", std::to_string(x), std::to_string(y)}, {}, 2000)
                   .ok();
    }

    bool mouse_click(int x, int y, int button, int clicks) override {
        if (xdotool_.empty()) return set_virtual("mouse_click");
        if (!mouse_move(x, y)) return false;
        ExecResult r = run_capture({xdotool_, "click", "--repeat", std::to_string(clicks), "--delay",
                                    "40", std::to_string(button)},
                                   {}, 3000);
        if (clicks == 2 && !r.ok()) r = run_capture({xdotool_, "click", "--repeat", "2"}, {}, 3000);
        return r.ok();
    }

    bool key_press(std::string_view key) override {
        if (xdotool_.empty()) return set_virtual("key_press");
        return run_capture({xdotool_, "key", "--clearmodifiers", std::string(to_x_key(key))}, {}, 3000)
            .ok();
    }

    bool hotkey(std::string_view keys) override {
        if (xdotool_.empty()) return set_virtual("hotkey");
        std::string combo;
        for (char c : keys) {
            if (c == '+') combo += '+';
            else combo += c;
        }
        combo = lower(combo);
        return run_capture({xdotool_, "key", "--clearmodifiers", combo}, {}, 3000).ok();
    }

    bool type_text(std::string_view text) override {
        // как и на Windows: длинный текст — через буфер обмена + Ctrl+V
        if (text.size() > 40 || text.find('\n') != std::string_view::npos) {
            if (!clipboard_set(text)) return false;
            return xdotool_.empty() ? set_virtual("paste") : hotkey("ctrl+v");
        }
        if (xdotool_.empty()) return set_virtual("type_text");
        return run_capture({xdotool_, "type", "--clearmodifiers", "--delay", "5", std::string(text)},
                           {}, 5000)
            .ok();
    }

    // ------------------------------------------------------------ буфер обмена
    std::string clipboard_get() override {
        if (!xclip_.empty()) {
            const ExecResult r = run_capture({xclip_, "-selection", "clipboard", "-o"}, {}, 2000);
            if (r.started && r.exit_code == 0) return r.stdout_text;
        }
        std::ifstream f(clip_store_path_);
        if (!f) return {};
        std::stringstream ss;
        ss << f.rdbuf();
        return ss.str();
    }

    bool clipboard_set(std::string_view text) override {
        {
            std::ofstream f(clip_store_path_, std::ios::binary | std::ios::trunc);
            if (f) f.write(text.data(), std::streamsize(text.size()));
        }
        if (!xclip_.empty()) {
            const std::string tmp = clip_store_path_;
            const ExecResult r =
                run_capture({"/bin/sh", "-c", xclip_ + " -selection clipboard -i < '" + tmp + "'"},
                            {}, 3000);
            return r.started && !r.timed_out;
        }
        return true;  // виртуальный буфер обмена (headless)
    }

    // ------------------------------------------------------------------ файлы
    bool file_exists(std::string_view path) const override {
        std::error_code ec;
        return fs::exists(std::string(path), ec);
    }
    bool is_dir(std::string_view path) const override {
        std::error_code ec;
        return fs::is_directory(std::string(path), ec);
    }
    bool mkdir(std::string_view path, bool recursive) override {
        std::error_code ec;
        if (recursive) return fs::create_directories(std::string(path), ec) || !ec;
        return fs::create_directory(std::string(path), ec);
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
        return read_small(std::string(path), max_bytes);
    }
    bool remove_path(std::string_view path, bool recursive, bool to_trash) override {
        std::error_code ec;
        const std::string p(path);
        if (to_trash && std::system(nullptr)) {
            const std::string q = shell_quote(p);
            if (std::system(("gio trash -- " + q + " >/dev/null 2>&1").c_str()) == 0) return true;
        }
        if (recursive) return fs::remove_all(p, ec) > 0;
        return fs::remove(p, ec);
    }
    bool move_path(std::string_view src, std::string_view dst) override {
        std::error_code ec;
        // перезапись поверх существующего — тоже осознанное действие инструмента
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
            if (!fe.is_dir) fe.size = e.is_regular_file(ec) ? e.file_size(ec) : 0;
            out.push_back(std::move(fe));
        }
        std::sort(out.begin(), out.end(), [](const FileEntry& a, const FileEntry& b) {
            if (a.is_dir != b.is_dir) return a.is_dir;
            return lower(a.name) < lower(b.name);
        });
        return out;
    }
    std::vector<FileEntry> search_files(std::string_view root, std::string_view pattern,
                                        size_t limit) override {
        std::vector<FileEntry> out;
        if (limit == 0) limit = 50;
        std::error_code ec;
        const std::string needle = lower(std::string(pattern));
        for (fs::recursive_directory_iterator it(std::string(root), ec), end; it != end && !ec;
             it.increment(ec)) {
            const std::string nm = lower(it->path().filename().string());
            if (needle.empty() || nm.find(needle) != std::string::npos) {
                FileEntry fe;
                fe.path = it->path().string();
                fe.name = it->path().filename().string();
                fe.is_dir = it->is_directory(ec);
                out.push_back(std::move(fe));
                if (out.size() >= limit) break;
            }
        }
        return out;
    }

    // --------------------------------------------------------------- оболочка
    ExecResult run_command(std::string_view command, std::string_view cwd, int timeout_ms) override {
        return run_capture({"/bin/bash", "-lc", std::string(command)},
                           std::string(cwd), timeout_ms > 0 ? timeout_ms : 30000);
    }
    ExecResult run_powershell(std::string_view script, std::string_view cwd,
                              int timeout_ms) override {
        // на Linux нет PowerShell по умолчанию — понятная ошибка вместо тишины
        ExecResult r;
        r.error = "PowerShell недоступен: платформа linux";
        (void)script;
        (void)cwd;
        (void)timeout_ms;
        return r;
    }
    bool open_uri(std::string_view uri) override {
        // Нет xdg-open — нет открытия: честный false вместо «виртуального успеха»,
        // иначе агент считает, что браузер открыт, и падает в проверке результата.
        if (xdg_open_.empty()) {
            last_error_ = "xdg-open не найден: нечем открыть ссылку";
            return false;
        }
        std::error_code ec;
        const int rc = std::system((xdg_open_ + " " + shell_quote(std::string(uri)) +
                                    " >/dev/null 2>&1 &")
                                       .c_str());
        return rc == 0 || !ec;
    }
    bool open_path(std::string_view path) override {
        std::error_code ec;
        if (!fs::exists(std::string(path), ec)) {
            last_error_ = "путь не существует: " + std::string(path);
            return false;
        }
        return open_uri(path);
    }

    // -------------------------------------------------- приложения / обнаружение
    int discover_apps(AppRegistry& reg) override {
        int found = 0;
        std::vector<std::string> dirs = {
            "/usr/share/applications",
            "/usr/local/share/applications",
        };
        const std::string home = env("HOME");
        if (!home.empty()) {
            dirs.push_back(home + "/.local/share/applications");
            dirs.push_back(home + "/.local/share/flatpak/exports/share/applications");
        }
        std::set<std::string> seen;
        for (const std::string& dir : dirs) {
            std::error_code ec;
            for (const fs::directory_entry& e : fs::directory_iterator(dir, ec)) {
                if (ec) break;
                if (e.path().extension() != ".desktop") continue;
                const std::string body = read_small(e.path().string(), 8192);
                if (body.empty()) continue;
                const std::string nm = desktop_value(body, "Name");
                if (nm.empty() || !seen.insert(lower(nm)).second) continue;
                const std::string bin = first_word(strip_field_codes(desktop_value(body, "Exec")));
                if (bin.empty()) continue;
                // сначала ищем известное приложение по имени/алиасу, потом заводим новое
                AppRegistry::Lookup hit = reg.find(nm);
                std::string key;
                if (hit.app && hit.score >= 0.85f) {
                    key = hit.app->key;
                } else {
                    key = normalize_key(nm);
                    if (key.empty() || reg.get(key)) continue;
                    AppInfo app;
                    app.key = key;
                    app.display_name = nm;
                    app.kind = "app";
                    reg.add(app);
                    if (!reg.get(key)) continue;
                }
                std::string resolved;
                const bool exists = which(bin, &resolved);
                reg.set_path(key, exists ? resolved : bin, exists);
                if (exists) ++found;
            }
        }
        return found;
    }
    std::string resolve_command(std::string_view command) const override {
        std::string p;
        return which(std::string(command), &p) ? p : std::string();
    }
    std::string default_browser() const override {
        const std::string preferred =
            env("BROWSER").empty() ? std::string("xdg-open") : env("BROWSER");
        (void)preferred;
        for (const char* b : {"google-chrome", "chromium", "chromium-browser", "firefox", "xdg-open"})
            if (which(b)) return b;
        return {};
    }

    // ------------------------------------------------------------------ экран
    std::vector<MonitorInfo> monitors() const override {
        MonitorInfo m;
        m.index = 1;
        m.name = "display";
        m.primary = true;
        m.dpi_scale = 1.0;
        // разрешение из xrandr, если есть; иначе виртуальный экран для тестов
        if (!has_display()) {
            m.width = 1920;
            m.height = 1080;
            return {m};
        }
        if (str_tool_empty()) return {m};
        const ExecResult r = run_capture({"sh", "-c", "xrandr 2>/dev/null | grep '\\*'"}, {}, 2000);
        const std::string line = trim(r.stdout_text);
        const size_t x = line.find('x');
        if (x != std::string::npos) {
            m.width = std::atoi(line.c_str());
            m.height = std::atoi(line.c_str() + x + 1);
        }
        if (m.width <= 0) m.width = 1920;
        if (m.height <= 0) m.height = 1080;
        return {m};
    }

    Frame capture(int monitor, const int* region_xywh) override {
        Frame f;
        const std::vector<MonitorInfo> ms = monitors();
        MonitorInfo m = ms.empty() ? MonitorInfo{} : ms.front();
        for (const MonitorInfo& cand : ms)
            if (cand.index == monitor) m = cand;
        int x = m.x, y = m.y, w = m.width, h = m.height;
        if (region_xywh) {
            x = region_xywh[0];
            y = region_xywh[1];
            w = region_xywh[2];
            h = region_xywh[3];
        }
        if (w <= 0 || h <= 0) {
            f.width = 0;
            f.height = 0;
            return f;
        }
        // без дисплея (headless) — синтетический градиент: тесты и dev-режим
        f.width = w;
        f.height = h;
        f.origin_x = x;
        f.origin_y = y;
        f.stride = w * 4;
        f.headless = !has_display();
        f.backend = f.headless ? "synthetic" : "gdi";
        f.pixels.assign(size_t(w) * size_t(h) * 4, 0);
        for (int row = 0; row < h; ++row) {
            for (int col = 0; col < w; ++col) {
                const size_t off = (size_t(row) * size_t(w) + size_t(col)) * 4;
                f.pixels[off + 0] = uint8_t((col * 255) / (w > 1 ? w - 1 : 1));      // B
                f.pixels[off + 1] = uint8_t((row * 255) / (h > 1 ? h - 1 : 1));      // G
                f.pixels[off + 2] = uint8_t(((col + row) * 255) / (w + h - 2 > 0 ? w + h - 2 : 1));
                f.pixels[off + 3] = 255;
            }
        }
        timespec ts{};
        clock_gettime(CLOCK_REALTIME, &ts);
        f.captured_ms = int64_t(ts.tv_sec) * 1000 + ts.tv_nsec / 1000000;
        return f;
    }

    // ---------------------------------------------------------------- система
    bool set_wallpaper(std::string_view path) override {
        const std::string p = tool("xfconf-query");
        std::string cmd;
        if (!p.empty()) cmd = "xfconf-query -c xfce4-desktop -p /backdrop/screen0/monitor0/workspace0/last-image -s " + shell_quote(std::string(path));
        else cmd = "gsettings set org.gnome.desktop.background picture-uri file://" + shell_quote(std::string(path));
        return std::system((cmd + " >/dev/null 2>&1").c_str()) == 0;
    }
    bool open_settings(std::string_view page) override {
        // на Windows это ms-settings:..., здесь — вежливая ошибка
        (void)page;
        return false;
    }
    bool set_volume(int percent) override {
        std::string p = tool("pactl");
        if (!p.empty()) {
            const float v = float(percent < 0 ? 0 : (percent > 100 ? 100 : percent)) / 100.0f;
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.2f", v);
            return std::system((p + " set-sink-volume @DEFAULT_SINK@ " + buf + " >/dev/null 2>&1")
                                   .c_str()) == 0;
        }
        p = tool("amixer");
        if (!p.empty()) {
            return std::system((p + " set Master " + std::to_string(percent) + "% >/dev/null 2>&1")
                                   .c_str()) == 0;
        }
        return false;
    }
    int get_volume() const override { return -1; }
    std::string cwd() const override { return cwd_; }
    std::string env(std::string_view name) const override {
        const char* v = ::getenv(std::string(name).c_str());
        return v ? std::string(v) : std::string();
    }

private:
    static std::vector<std::string> split_lines(const std::string& s) {
        std::vector<std::string> out;
        std::stringstream ss(s);
        std::string line;
        while (std::getline(ss, line)) {
            const std::string t = trim(line);
            if (!t.empty()) out.push_back(t);
        }
        return out;
    }

    static std::string strip_field_codes(const std::string& exec) {
        std::string out;
        for (size_t i = 0; i < exec.size(); ++i) {
            if (exec[i] == '%' && i + 1 < exec.size()) {
                ++i;  // %U, %F, %i → мусор для нас
                continue;
            }
            out.push_back(exec[i]);
        }
        return trim(out);
    }

    static std::string first_word(const std::string& s) {
        std::stringstream ss(s);
        std::string w;
        ss >> w;
        return w;
    }

    static std::string desktop_value(const std::string& body, const std::string& key) {
        std::stringstream ss(body);
        std::string line;
        const std::string prefix = key + "=";
        while (std::getline(ss, line)) {
            const std::string t = trim(line);
            if (t.rfind(prefix, 0) == 0) return trim(t.substr(prefix.size()));
        }
        return {};
    }

    static std::string to_x_key(std::string_view key) {
        const std::string k = lower(std::string(key));
        if (k == "enter" || k == "return") return "Return";
        if (k == "esc" || k == "escape") return "Escape";
        if (k == "win" || k == "super" || k == "meta" || k == "cmd") return "Super_L";
        if (k == "ctrl") return "ctrl";
        if (k == "alt") return "alt";
        if (k == "shift") return "shift";
        if (k == "tab") return "Tab";
        if (k == "space") return "space";
        if (k == "backspace") return "BackSpace";
        if (k == "delete" || k == "del") return "Delete";
        if (k == "up") return "Up";
        if (k == "down") return "Down";
        if (k == "left") return "Left";
        if (k == "right") return "Right";
        return std::string(key);
    }

    static std::string shell_quote(const std::string& s) {
        std::string out = "'";
        for (char c : s) {
            if (c == '\'') out += "'\\''";
            else out.push_back(c);
        }
        out += "'";
        return out;
    }

    bool str_tool_empty() const { return !has_display(); }
    bool set_virtual(const char* what) {
        last_virtual_ = what;
        return true;  // виртуальный режим: ввод «принят», GUI отсутствует
    }

    std::string display_;
    std::string cwd_;
    std::string xdotool_;
    std::string xclip_;
    std::string xdg_open_;
    std::string clip_store_path_;
    std::string last_virtual_;
    std::string last_error_;
};

std::unique_ptr<IPlatform> make_platform() { return std::make_unique<LinuxPlatform>(); }

}  // namespace agent
