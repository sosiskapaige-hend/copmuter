// Общая часть тестов: мини-фреймворк и мок-платформа.
//
// Вынесено в заголовок, потому что этим пользуются два набора: тесты ядра
// (test_core.cpp) и прогон обязательных сценариев ТЗ (test_scenarios.cpp).
#pragma once
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include "agent/intent.h"
#include "agent/ipc.h"
#include "agent/platform.h"
#include "agent/runtime.h"
#include "agent/util.h"

namespace fs = std::filesystem;
using namespace agent;

// ---------------------------------------------------------------------------
//  Мини-фреймворк: без внешних зависимостей, чтобы собиралось одним g++.
// ---------------------------------------------------------------------------
static int g_failed = 0;
static int g_passed = 0;
static const char* g_group = "";

static void group(const char* name) {
    g_group = name;
    std::printf("\n== %s\n", name);
}

static void check(bool cond, const std::string& what) {
    if (cond) {
        ++g_passed;
        std::printf("  ok   %s\n", what.c_str());
    } else {
        ++g_failed;
        std::printf("  FAIL %s\n", what.c_str());
    }
}

static void check_eq(const std::string& got, const std::string& want, const std::string& what) {
    check(got == want, what + " (got «" + got + "», want «" + want + "»)");
}

// ---------------------------------------------------------------------------
//  Мок-платформа: используется там, где нельзя трогать реальную систему.
// ---------------------------------------------------------------------------
class MockPlatform : public IPlatform {
public:
    std::vector<ProcessInfo> procs;
    std::vector<WindowInfo> wins;
    std::vector<MonitorInfo> mons;
    std::map<std::string, std::string> files;
    std::string clip;
    std::vector<std::string> spawned;
    std::vector<std::string> commands;
    std::vector<std::pair<int, int>> clicks;   // куда кликали (проверка зрения)
    // Регистрировать ли процесс при запуске: по умолчанию нет (тесты отказов),
    // в сценариях включаем — как в жизни, запуск означает появившийся процесс.
    bool spawn_registers_process = false;
    int frame_phase = 0;                       // экран «реагирует» на клик
    ExecResult command_result;
    bool fail_spawn = false;

    const char* name() const override { return "mock"; }
    bool has_display() const override { return true; }

    std::vector<ProcessInfo> processes() const override { return procs; }
    bool process_running(std::string_view n) const override {
        for (const ProcessInfo& p : procs)
            if (p.name == n) return true;
        return false;
    }
    bool kill_process(std::string_view n, bool) override {
        for (size_t i = 0; i < procs.size(); ++i)
            if (procs[i].name == n) {
                procs.erase(procs.begin() + long(i));
                return true;
            }
        return false;
    }
    LaunchResult spawn_detached(const std::string& command, std::string_view args) override {
        LaunchResult r;
        if (fail_spawn) {
            r.error = "spawn отключён в тесте";
            return r;
        }
        spawned.push_back(command + (args.empty() ? "" : " " + std::string(args)));
        r.ok = true;
        r.method = "mock";
        r.ms = 0.1;
        if (spawn_registers_process) {
            ProcessInfo p;
            p.pid = uint32_t(1000 + procs.size());
            const size_t slash = command.find_last_of("/\\");
            p.name = slash == std::string::npos ? command : command.substr(slash + 1);
            p.path = command;
            procs.push_back(p);
            WindowInfo w;
            w.handle = 100 + procs.size();
            w.pid = p.pid;
            w.title = p.name;
            w.process = p.name;
            wins.push_back(w);
        }
        return r;
    }
    std::vector<WindowInfo> windows() const override { return wins; }
    std::optional<WindowInfo> find_window(std::string_view title) const override {
        for (const WindowInfo& w : wins)
            if (w.title.find(title) != std::string::npos) return w;
        return std::nullopt;
    }
    std::optional<WindowInfo> active_window() const override {
        return wins.empty() ? std::nullopt : std::optional<WindowInfo>(wins.front());
    }
    bool activate_window(uint64_t) override { return true; }
    bool close_window(uint64_t) override { return true; }
    bool mouse_move(int, int) override { return true; }
    bool mouse_click(int x, int y, int, int) override {
        clicks.emplace_back(x, y);
        ++frame_phase;      // после клика картинка меняется — проверка это увидит
        return true;
    }
    bool key_press(std::string_view) override { return true; }
    bool hotkey(std::string_view) override { return true; }
    bool type_text(std::string_view) override { return true; }
    std::string clipboard_get() override { return clip; }
    bool clipboard_set(std::string_view text) override {
        clip = std::string(text);
        return true;
    }
    bool file_exists(std::string_view p) const override { return files.count(std::string(p)) > 0; }
    bool is_dir(std::string_view p) const override {
        auto it = files.find(std::string(p));
        return it != files.end() && it->second == "<dir>";
    }
    bool mkdir(std::string_view p, bool) override {
        files[std::string(p)] = "<dir>";
        return true;
    }
    bool write_file(std::string_view p, std::string_view c, bool) override {
        files[std::string(p)] = std::string(c);
        return true;
    }
    std::string read_file(std::string_view p, size_t) override { return files[std::string(p)]; }
    bool remove_path(std::string_view p, bool, bool) override { return files.erase(std::string(p)) > 0; }
    bool move_path(std::string_view s, std::string_view d) override {
        auto it = files.find(std::string(s));
        if (it == files.end()) return false;
        files[std::string(d)] = it->second;
        files.erase(it);
        return true;
    }
    bool copy_path(std::string_view s, std::string_view d) override {
        auto it = files.find(std::string(s));
        if (it == files.end()) return false;
        files[std::string(d)] = it->second;
        return true;
    }
    std::vector<FileEntry> list_dir(std::string_view) override { return {}; }
    std::vector<FileEntry> search_files(std::string_view, std::string_view, size_t) override {
        return {};
    }
    ExecResult run_command(std::string_view cmd, std::string_view, int) override {
        commands.push_back(std::string(cmd));
        return command_result;
    }
    ExecResult run_powershell(std::string_view cmd, std::string_view, int) override {
        commands.push_back("ps:" + std::string(cmd));
        return command_result;
    }
    bool open_uri(std::string_view uri) override {
        spawned.push_back("uri:" + std::string(uri));
        return !fail_spawn;
    }
    bool open_path(std::string_view path) override {
        spawned.push_back("path:" + std::string(path));
        return !fail_spawn;
    }
    int discover_apps(AppRegistry& reg) override {
        AppInfo a;
        a.key = "mockapp";
        a.display_name = "MockApp";
        a.installed = true;
        a.path = "mockapp.exe";
        a.exe = {"mockapp.exe"};
        reg.add(a);
        return 1;
    }
    std::string resolve_command(std::string_view c) const override { return std::string(c); }
    std::string default_browser() const override { return "chrome"; }
    std::vector<MonitorInfo> monitors() const override {
        if (!mons.empty()) return mons;
        MonitorInfo m;
        m.width = 1920;
        m.height = 1080;
        return {m};
    }
    Frame capture(int, const int* region) override {
        Frame f;
        f.width = region ? region[2] : 1920;
        f.height = region ? region[3] : 1080;
        f.stride = f.width * 4;
        f.pixels.assign(size_t(f.width) * size_t(f.height) * 4,
                        uint8_t(7 + frame_phase * 40));
        f.headless = true;
        f.backend = "mock";
        return f;
    }
    bool set_wallpaper(std::string_view p) override {
        spawned.push_back("wall:" + std::string(p));
        return !fail_spawn;
    }
    bool open_settings(std::string_view p) override {
        spawned.push_back("settings:" + std::string(p));
        return !fail_spawn;
    }
    bool set_volume(int) override { return true; }
    int get_volume() const override { return 40; }
    std::string cwd() const override { return "/mock"; }
    std::string env(std::string_view n) const override {
        if (n == "USERPROFILE") return "C:\\Users\\tester";
        if (n == "HOME") return "/home/tester";
        if (n == "USERNAME") return "tester";
        if (n == "TEMP") return "/tmp";
        return {};
    }
};

// Прогретый рантайм на мок-платформе: разбор фраз, реестр приложений, места.

// Итог для наборов, у которых свой main().
[[maybe_unused]] static int scenario_report() {
    std::printf("\nитог: %d пройдено, %d провалено\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
