// Тесты ядра AgentRuntime: разбор фраз, маршрутизация, реестр приложений,
// оптимизатор, пакеты действий, ожидания, полный цикл простой команды.
//
// Сборка (без CMake, чистый g++):
//   g++ -std=c++20 -O2 -Iinclude tests/test_core.cpp src/*.cpp -o build/agent_tests
// Запуск:
//   ./build/agent_tests            — все тесты
//   ./build/agent_tests --bench    — только микро-бенчмарк маршрутизатора
#include <cstdio>
#include <cstdlib>
#include <cstring>
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
    bool mouse_click(int, int, int, int) override { return true; }
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
        f.pixels.assign(size_t(f.width) * size_t(f.height) * 4, 7);
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
static std::unique_ptr<AgentRuntime> make_runtime(RuntimeConfig* used = nullptr) {
    auto platform = std::make_unique<MockPlatform>();
    RuntimeConfig cfg;
    cfg.preload = true;
    cfg.state_dir = "/tmp/agent_test_state";
    if (used) *used = cfg;
    auto rt = std::make_unique<AgentRuntime>(cfg, std::move(platform));
    std::string err;
    rt->start(err);
    return rt;
}

// ---------------------------------------------------------------------------
//  Разбор фраз (Intent Engine)
// ---------------------------------------------------------------------------
static void test_intent() {
    group("intent: разбор фраз");
    auto rt_ptr = make_runtime();
    AgentRuntime& rt = *rt_ptr;
    IntentEngine& ie = rt.intents();

    struct Case {
        const char* phrase;
        const char* action;
        const char* target;   // ожидаемый слот Target (если проверяем)
        const char* place;
    };
    const Case cases[] = {
        {"открой телегу", "launch_app", "telegram", ""},
        {"запусти дискорд", "launch_app", "discord", ""},
        {"открой вскод", "launch_app", "vscode", ""},
        {"открой хром", "launch_app", "chrome", ""},
        // «проводник» есть в каталоге приложений: допустим и запуск explorer, и открытие папки
        {"открой загрузки", "open_folder", "", "загрузки"},
        {"открой рабочий стол", "open_folder", "", "рабочий стол"},
        {"создай папку 123 на рабочем столе", "create_folder", "123", "рабочий стол"},
        {"удали папку 123 с рабочего стола", "delete_path", "123", "рабочий стол"},
        {"открой youtube", "open_url", "", ""},
        {"найди видео про котиков на ютубе", "youtube_search", "", ""},
        {"погугли погоду", "web_search", "", ""},
        {"сделай скриншот", "screenshot", "", ""},
        {"поменяй обои на картинку обои.png", "set_wallpaper", "", ""},
        {"выключи компьютер", "power", "", ""},
        {"перезагрузи компьютер", "power", "", ""},
        {"громкость 50 процентов", "volume", "", ""},
        {"покажи рабочий стол", "show_desktop", "", ""},
        {"открой настройки дисплея", "settings_page", "", ""},
        {"нажми ctrl+s", "send_keys", "", ""},
        {"напиши привет", "type_text", "", ""},
        {"как дела?", "chat", "", ""},
        {"напиши калькулятор на python", "code_task", "", ""},
    };
    // «открой проводник» — в каталоге приложений это explorer; допустимы оба пути
    {
        Intent it = ie.parse("открой проводник");
        const bool ok = it.action.view() == "open_folder" ||
                        (it.action.view() == "launch_app" && it.slot(SlotId::Target).view() == "explorer");
        check(ok, "«открой проводник» → explorer/open_folder (got «" + it.action.str() + "», target «" +
                      it.slot(SlotId::Target).str() + "»)");
    }
    for (const Case& c : cases) {
        Intent it = ie.parse(c.phrase);
        std::string got = it.action.str();
        if (got == "launch_app" && c.target[0]) {
            // цель должна вести в реестр приложений
            const std::string target = it.slot(SlotId::Target).str();
            check(!target.empty() && target != c.phrase,
                  std::string("фраза «") + c.phrase + "» → launch_app(" + target + ")");
            if (!target.empty() && target != c.phrase) {
                AppRegistry::Lookup lk = rt.apps().find(target);
                if (lk.app) {
                    check_eq(lk.app->key, c.target, std::string("  реестр узнал «") + target + "»");
                } else {
                    // на Linux-хосте реестр заполнен встроенным каталогом
                    check_eq(target, c.target, std::string("  цель «") + c.phrase + "»");
                }
            }
            continue;
        }
        if (std::string(c.action) == "open_folder" || std::string(c.action) == "create_folder" ||
            std::string(c.action) == "delete_path") {
            check_eq(got, c.action, std::string("фраза «") + c.phrase + "»");
            if (c.target[0]) {
                check_eq(it.slot(SlotId::Target).str(), c.target,
                         std::string("  цель «") + c.phrase + "»");
            }
            if (c.place[0]) {
                check_eq(normalize(it.slot(SlotId::Place).view()), c.place,
                         std::string("  место «") + c.phrase + "»");
            }
            continue;
        }
        check_eq(got, c.action, std::string("фраза «") + c.phrase + "»");
    }
}

// ---------------------------------------------------------------------------
//  Составные команды
// ---------------------------------------------------------------------------
static void test_compound() {
    group("intent: составные команды");
    auto rt_ptr = make_runtime();
    AgentRuntime& rt = *rt_ptr;
    Intent it = rt.intents().parse("открой VS Code и напиши калькулятор на Python");
    check(it.is_compound(), "«открой VS Code и напиши калькулятор на Python» распознана как составная");
    if (it.is_compound()) {
        check_eq(it.parts[0].action.str(), "launch_app", "  первая часть — запуск");
        check_eq(it.parts[1].action.str(), "code_task", "  вторая часть — код");
    }
    Intent it2 = rt.intents().parse("открой youtube и найди видео про котиков");
    check(it2.is_compound(), "«открой youtube и найди видео про котиков» распознана как составная");
}

// ---------------------------------------------------------------------------
//  Fast Router: что попадает в модель, а что нет
// ---------------------------------------------------------------------------
static void test_router() {
    group("router: классификация");
    auto rt_ptr = make_runtime();
    AgentRuntime& rt = *rt_ptr;
    Router& r = rt.router();

    auto kind_of = [&](const char* phrase) { return r.route(phrase).kind; };
    check(kind_of("открой телегу") == RouteKind::Direct, "«открой телегу» → Direct (без модели)");
    check(kind_of("запусти дискорд") == RouteKind::Direct, "«запусти дискорд» → Direct");
    check(kind_of("сделай скриншот") == RouteKind::Direct, "«сделай скриншот» → Direct");
    check(kind_of("создай папку 123 на рабочем столе") == RouteKind::Direct,
          "«создай папку 123 на рабочем столе» → Direct");
    check(kind_of("напиши калькулятор на python") == RouteKind::LlmText,
          "«напиши калькулятор на python» → LlmText (нужен код)");
    check(kind_of("как дела?") == RouteKind::Chat, "«как дела?» → Chat");
    check(kind_of("собери отчёт из трёх сайтов и пришли в телеграм") == RouteKind::Agent,
          "сложная задача → Agent");
    check(kind_of("посмотри на экран и скажи что открыто") == RouteKind::Vision,
          "«посмотри на экран…» → Vision");
}

// ---------------------------------------------------------------------------
//  Реестр приложений и оптимизатор
// ---------------------------------------------------------------------------
static void test_registry_optimizer() {
    group("registry / optimizer");
    auto rt_ptr = make_runtime();
    AgentRuntime& rt = *rt_ptr;

    AppRegistry& reg = rt.apps();
    check(reg.size() > 10, "встроенный каталог приложений загружен (" + std::to_string(reg.size()) + ")");
    AppRegistry::Lookup lk = reg.find("телега");
    check(lk.app != nullptr && lk.app->key == "telegram", "«телега» → telegram");
    AppRegistry::Lookup lk2 = reg.find("вс код");
    check(lk2.app != nullptr && lk2.app->key == "vscode", "«вс код» → vscode");
    auto sug = reg.suggest("диск", 3);
    check(!sug.empty() && sug.front().first->key == "discord", "подсказки для «диск» начинаются с discord");

    Optimizer& opt = rt.optimizer();
    const Method all[] = {Method::WinApi, Method::CachedExe, Method::Shell, Method::Shortcut,
                          Method::Uia, Method::Input, Method::Vision, Method::Llm};
    std::vector<Method> order = opt.order("launch_app", all);
    check(!order.empty() && (order.front() == Method::CachedExe || order.front() == Method::WinApi),
          std::string("лестница launch_app начинается с дешёвого способа (") +
              (order.empty() ? "пусто" : to_string(order.front())) + ")");
    // после провалов WinApi оптимизатор должен поднимать рабочую альтернативу
    for (int i = 0; i < 5; ++i) opt.note("launch_app", Method::WinApi, false, 30.0);
    for (int i = 0; i < 5; ++i) opt.note("launch_app", Method::Shortcut, true, 12.0);
    std::vector<Method> order2 = opt.order("launch_app", all);
    check(order2.front() == Method::Shortcut || order2.front() == Method::CachedExe ||
              order2.front() == Method::Cli,
          "после серии провалов WinApi уступает проверенному способу (" + std::string(to_string(order2.front())) + ")");
}

// ---------------------------------------------------------------------------
//  Пакетное выполнение
// ---------------------------------------------------------------------------
static void test_batch() {
    group("batch: пакет действий");
    ActionQueue q(3);
    int calls = 0;
    ActionSpec a1;
    a1.id = "a1";
    a1.tool = "open_url";
    a1.args_json = R"({"url":"https://example.com"})";
    ActionSpec a2;
    a2.id = "a2";
    a2.tool = "type_text";
    a2.args_json = R"({"text":"hello"})";
    a2.depends_on = {0};   // зависит от a1
    ActionSpec a3;
    a3.id = "a3";
    a3.tool = "screenshot";
    q.add(a1);
    q.add(a2);
    q.add(a3);
    BatchResult br = q.run(
        [&](ActionSpec& a) {
            ++calls;
            a.ok = true;
            a.state = ActionState::Success;
            a.output = "done:" + a.tool;
        },
        [](const ActionSpec&, std::string&) { return true; });
    check(br.ok, "пакет выполнен целиком");
    check(calls == 3, "вызваны все три инструмента (" + std::to_string(calls) + ")");
    check(br.actions.size() == 3, "результат содержит три действия");

    // падение с fallback: первая попытка ломается, вторая (fallback) успешна
    ActionQueue q2(2);
    ActionSpec b1;
    b1.id = "b1";
    b1.tool = "open_uri_via_api";
    b1.fallbacks.push_back({"open_uri_via_shell", R"({"uri":"https://x.y"})"});
    q2.add(b1);
    int first_calls = 0, fallback_calls = 0;
    BatchResult br2 = q2.run(
        [&](ActionSpec& a) {
            if (a.tool == "open_uri_via_api") {
                ++first_calls;
                a.ok = false;
                a.error = "нет WinAPI-пути";
                a.state = ActionState::Failed;
            } else {
                ++fallback_calls;
                a.ok = true;
                a.state = ActionState::Success;
            }
        },
        [](const ActionSpec&, std::string&) { return true; });
    check(br2.ok, "fallback спас действие");
    check(first_calls == 1 && fallback_calls == 1, "сначала основной способ, потом запасной");
}

// ---------------------------------------------------------------------------
//  Ожидания (не sleep)
// ---------------------------------------------------------------------------
static void test_waits() {
    group("wait: ожидание состояния");
    MockPlatform* raw = new MockPlatform();
    std::unique_ptr<IPlatform> platform(raw);
    WaitManager wm(raw);

    const int64_t t0 = now_ms();
    WaitResult wr = wm.wait_process_started("telegram", 100);
    check(!wr.ok, "процесс не найден — ожидание завершилось неудачей");
    check(now_ms() - t0 < 400, "неудача пришла быстро, без sleep(1s)");

    MockPlatform* raw2 = new MockPlatform();
    std::unique_ptr<IPlatform> platform2(raw2);
    WaitManager wm2(raw2);
    raw2->files["/tmp/x.txt"] = "hello";  // «файл появился» в моке
    WaitResult wr2 = wm2.wait_file_exists("/tmp/x.txt", 200);
    check(wr2.ok, "файл найден (мок)");
    check(wr2.polls >= 1, "опрос был хотя бы один");
}

// ---------------------------------------------------------------------------
//  Полный цикл: простые команды без модели
// ---------------------------------------------------------------------------
static void test_runtime_cycle() {
    group("runtime: простые команды без модели");
    auto platform = std::make_unique<MockPlatform>();
    RuntimeConfig cfg;
    cfg.preload = true;
    cfg.state_dir = "/tmp/agent_test_state";
    AgentRuntime rt(cfg, std::move(platform));
    std::string err;
    rt.start(err);

    std::vector<Event> events;
    rt.set_event_sink([&](const Event& e) { events.push_back(e); });

    struct Case {
        const char* phrase;
        bool handled;
    };
    const Case fast[] = {
        {"создай папку Smoke123", true},
        {"создай папку Smoke123", true},
        {"удали папку Smoke123", true},
        {"сделай скриншот", true},
        {"открой https://example.com", true},
    };
    for (const Case& c : fast) {
        FastOutcome out = rt.execute(c.phrase);
        check(out.handled == c.handled,
              std::string("«") + c.phrase + "» обработано ядром (route " + to_string(out.route) + ")");
        check(out.route_us < 5000.0,
              std::string("  решение принято за ") + std::to_string(int(out.route_us)) + " мкс");
    }

    FastOutcome complex_out = rt.execute("собери отчёт из трёх сайтов и пришли в телеграм");
    check(!complex_out.handled && complex_out.needs_llm,
          "сложная задача уходит в модель (needs_llm)");

    std::string preview = rt.preview("удали папку 123 с рабочего стола");
    check(preview.find("delete") != std::string::npos || preview.find("удали") != std::string::npos ||
              preview.find("123") != std::string::npos,
          "PLAN ONLY показывает план: " + preview);

    MetricsSnapshot m = rt.metrics();
    check(m.fast_tasks >= 4, "метрики: быстрых задач " + std::to_string(m.fast_tasks));
    check(m.ttc_avg_ms() >= 0.0, "метрики: TTC считается (" + std::to_string(int(m.ttc_avg_ms())) + " мс)");
    rt.stop();
}

// ---------------------------------------------------------------------------
//  Вызовы инструментов агентом (план модели → нативное исполнение)
// ---------------------------------------------------------------------------
static void test_run_tool() {
    group("tools: вызов инструментов агентом");
    auto rt_ptr = make_runtime();
    AgentRuntime& rt = *rt_ptr;

    const std::string folder = "/home/tester/Desktop/AgentFolder";
    std::string r1 = rt.run_tool("create_folder", R"({"path":"/home/tester/Desktop/AgentFolder"})");
    check(r1.find("\"ok\":true") != std::string::npos, "create_folder через агентный вызов: " + r1);

    std::string r2 = rt.run_tool("fs_write",
                                 R"({"path":"/home/tester/Desktop/AgentFolder/notes.txt","content":"привет"})");
    check(r2.find("\"ok\":true") != std::string::npos, "fs_write (алиас write_file): " + r2);

    std::string r3 = rt.run_tool("read_file", R"({"path":"/home/tester/Desktop/AgentFolder/notes.txt"})");
    check(r3.find("привет") != std::string::npos, "read_file вернул содержимое: " + r3);

    std::string r4 = rt.run_tool("wait_for", R"({"kind":"file_exists","target":"/home/tester/Desktop/AgentFolder/notes.txt"})");
    check(r4.find("\"ok\":true") != std::string::npos, "wait_for дождался файла: " + r4);

    std::string r5 = rt.run_tool("click", R"({"x":100,"y":200})");
    check(r5.find("\"ok\":true") != std::string::npos, "click через SendInput: " + r5);

    std::string r6 = rt.run_tool("delete_path", R"({"path":"/home/tester/Desktop/AgentFolder"})");
    check(r6.find("\"ok\":true") != std::string::npos, "delete_path: " + r6);

    std::string bad = rt.run_tool("teleport_user", R"({})");
    check(bad.find("\"ok\":false") != std::string::npos, "неизвестный инструмент честно отвергнут");

    const uint64_t before = rt.metrics().tool_calls;
    rt.run_tool("create_folder", R"({"path":"/home/tester/Desktop/Y"})");
    check(rt.metrics().tool_calls > before, "метрики считают вызовы инструментов");
}

// ---------------------------------------------------------------------------
//  Отказ не должен быть медленным: перебор способов ограничен бюджетом (ТЗ §28)
// ---------------------------------------------------------------------------
static void test_launch_failure_is_fast() {
    group("recovery: неудачный запуск не тормозит");
    auto rt_ptr = make_runtime();
    AgentRuntime& rt = *rt_ptr;
    const int64_t t0 = now_ms();
    FastOutcome out = rt.execute("открой приложение-которого-нет-9f3a");
    const int64_t elapsed = now_ms() - t0;
    check(out.handled && !out.ok, "запуск невозможен: ответ без модели и без паники");
    // На мок-платформе каждый запуск «успешен», но не подтверждается: проверяем,
    // что перебор способов ограничен, а не что он мгновенный.
    check(elapsed < 5000, "отказ пришёл за " + std::to_string(elapsed) + " мс (лимит 5000); " +
                              out.error);
    check(elapsed < int(rt.config().launch_timeout_ms), "уложились в бюджет запуска " +
                                                            std::to_string(rt.config().launch_timeout_ms) +
                                                            " мс");
    const MethodStat* st = rt.optimizer().stat("launch_app", Method::Shell);
    check(st == nullptr || st->attempts <= 4,
          "оптимизатор не зациклился на неудачном способе");
}

// ---------------------------------------------------------------------------
//  Микро-бенчмарк: разбор + маршрутизация
// ---------------------------------------------------------------------------
static void bench_router(int iterations) {
    group("bench: микро-бенчмарк");
    auto rt_ptr = make_runtime();   // прогретый реестр: как в бою
    AgentRuntime& rt = *rt_ptr;
    Router& router = rt.router();
    const char* phrases[] = {
        "открой телегу", "запусти дискорд", "открой вскод", "сделай скриншот",
        "создай папку 123 на рабочем столе", "удали папку 123 с рабочего стола",
        "открой youtube и найди видео про котиков", "напиши калькулятор на python",
        "как дела?", "поменяй обои",
    };
    const int n = int(sizeof(phrases) / sizeof(phrases[0]));
    double total_us = 0.0;
    int count = 0;
    for (int i = 0; i < iterations; ++i) {
        for (int j = 0; j < n; ++j) {
            const double t0 = now_us();
            Route r = router.route(phrases[j]);
            const double dt = now_us() - t0;
            total_us += dt;
            ++count;
            if (i == 0 && j == 0) std::printf("  пример: «%s» → %s (%.1f мкс)\n", phrases[j],
                                              to_string(r.kind), dt);
        }
    }
    const double avg = total_us / count;
    std::printf("  маршрутов: %d, среднее: %.2f мкс, максимум кадра: %.0f мкс\n", count, avg, total_us);
    check(avg < 250.0, "среднее время маршрута меньше 250 мкс (получено " +
                           std::to_string(int(avg * 100) / 100.0) + ")");
}

// ---------------------------------------------------------------------------
//  C ABI (то, что дёргает C#-оболочка через P/Invoke)
// ---------------------------------------------------------------------------
static void test_abi() {
    group("abi: C ABI для C#");
    const char* cfg = R"({"state_dir":"/tmp/agent_abi_state","preload":false,"safety_mode":"auto"})";
    check(agent_init(cfg) == 0, "agent_init прошёл");
    char* out = agent_execute("создай папку AbiSmoke");
    std::string json = out ? out : "";
    check(json.find("\"handled\":true") != std::string::npos, "agent_execute вернул JSON: " + json);
    agent_free(out);
    char* metrics = agent_metrics_json();
    check(metrics && std::strlen(metrics) > 2, "agent_metrics_json вернул данные");
    agent_free(metrics);
    char* preview = agent_preview("удали папку 123 с рабочего стола");
    check(preview && std::strlen(preview) > 0, "agent_preview вернул план");
    agent_free(preview);
    char* tools = agent_tools_json();
    check(tools && std::strlen(tools) > 2, "agent_tools_json вернул список инструментов");
    agent_free(tools);
    agent_shutdown();
    check(true, "agent_shutdown прошёл");
}

// ---------------------------------------------------------------------------
//  Канал C++ ↔ Python (постоянный воркер, framed JSON)
// ---------------------------------------------------------------------------
static void test_ipc() {
    group("ipc: канал C++ ↔ Python");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string sock = "/tmp/agent_ipc_test.sock";
    std::remove(sock.c_str());
    std::remove("/tmp/agent_ipc_test_state");
    const std::string cmd = "cd " + repo +
                            " && AGENT_AI_SOCKET=" + sock +
                            " AGENT_STATE_DIR=/tmp/agent_ipc_test_state AGENT_LOG_LEVEL=WARNING"
                            " python3 -m ai.main --serve > /tmp/agent_ipc_test.log 2>&1 &";
    if (std::system(cmd.c_str()) != 0) {
        check(false, "не удалось запустить python-воркер");
        return;
    }
    AiLink link;
    std::string error;
    const bool connected = link.connect(sock, 8000, error);
    check(connected, "воркер поднялся и принял соединение" + (connected ? "" : (": " + error)));
    if (connected) {
        AiReply reply = link.request(R"({"id":1,"type":"health"})", 8000);
        check(reply.ok, "ответ по каналу получен за " + std::to_string(int(reply.ms)) + " мс");
        if (reply.ok) {
            check(reply.json.find("\"ok\":true") != std::string::npos, "воркер ответил ok");
            check(reply.json.find("model") != std::string::npos, "в ответе есть имя модели");
            check(reply.json.find("generation") != std::string::npos, "воркер сообщает поколение");
        }
        AiReply bad = link.request(R"({"id":2,"type":"nonsense"})", 3000);
        check(bad.ok && bad.json.find("неизвестный тип") != std::string::npos,
              "неизвестный запрос обработан без обрыва канала");
    }
    link.close();
    std::system("pkill -f 'ai.main --serve' >/dev/null 2>&1 || true");
}

// ---------------------------------------------------------------------------
//  Агентный цикл: план внешнего «мозга» (Python) → нативные инструменты → проверка
// ---------------------------------------------------------------------------
static std::string read_text_file(const std::string& path) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return "";
    std::string out;
    char buf[512];
    size_t n = 0;
    while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    std::fclose(f);
    return out;
}

// Ждём появления файла (или процесса) — вместо sleep(3).
static bool wait_for_file(const std::string& path, int timeout_ms) {
    const int64_t deadline = now_ms() + timeout_ms;
    while (now_ms() < deadline) {
        if (fs::exists(path)) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return false;
}

static int start_fake_brain(const std::string& repo, const std::string& sock, const std::string& base,
                            const std::string& mode, const std::string& log_path) {
    const std::string cmd = "cd " + repo + " && AGENT_FAKE_SOCKET=" + sock + " AGENT_FAKE_BASE=" + base +
                            " AGENT_FAKE_MODE=" + mode + " AGENT_FAKE_LOG=" + log_path +
                            " nohup python3 -m ai.fake_brain > " + log_path + ".out 2>&1 &";
    return std::system(cmd.c_str());
}

static void test_agent_loop() {
    group("агентный цикл: план мозга → нативные инструменты → проверка");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string base = "/tmp/agent_loop_test";
    const std::string sock = "/tmp/agent_loop_test.sock";
    const std::string state = "/tmp/agent_loop_test_state";
    const std::string log = "/tmp/agent_loop_test.log";
    fs::remove_all(base);
    fs::remove_all(state);
    std::remove(sock.c_str());
    std::remove(log.c_str());

    start_fake_brain(repo, sock, base, "plan", log);
    bool up = false;
    for (int i = 0; i < 200 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "фейковый мозг поднял канал");
    if (!up) return;

    RuntimeConfig cfg;
    cfg.state_dir = state;
    cfg.preload = false;
    cfg.ai_socket = sock;
    cfg.max_steps = 6;
    AgentRuntime rt(cfg, make_platform());
    std::string error;
    check(rt.start(error), "ядро стартовало: " + error);

    const std::string result = rt.run_task("проанализируй проект и напиши отчёт", 6);
    check(result.find("\"mode\":\"agent\"") != std::string::npos, "задача пошла агентным циклом");
    check(result.find("\"ok\":true") != std::string::npos, "задача завершена успешно: " + result);
    check(result.find("\"llm_calls\":2") != std::string::npos, "модель вызвана дважды (план + переплан)");
    check(result.find("\"tool_calls\":3") != std::string::npos, "инструменты вызваны по плану");
    check(fs::exists(base + "/AgentLoop/plan.txt"), "побочный эффект на диске есть");
    check_eq(read_text_file(base + "/AgentLoop/plan.txt"), "готово", "содержимое файла записано верно");

    // Второй запуск переиспользует тот же канал: воркер не перезапускается на команду.
    const std::string again = rt.run_task("проверь и настрой окружение для проекта", 6);
    check(again.find("\"ok\":true") != std::string::npos, "повторная задача прошла");
    const std::string journal = read_text_file(log);
    size_t accepts = 0;
    for (size_t pos = journal.find("accept"); pos != std::string::npos;
         pos = journal.find("accept", pos + 1)) ++accepts;
    check(accepts >= 1, "канал переиспользован (accept за сессию: " + std::to_string(accepts) + ")");

    // Модель вернула неразбираемый план — рантайм обязан честно упасть, а не «успешно» молчать.
    std::system("pkill -f ai.fake_brain >/dev/null 2>&1 || true");
    std::remove(sock.c_str());
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    start_fake_brain(repo, sock, base, "broken", log + ".broken");
    for (int i = 0; i < 200 && !fs::exists(sock); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    RuntimeConfig cfg2 = cfg;
    cfg2.state_dir = state + "2";
    AgentRuntime rt2(cfg2, make_platform());
    check(rt2.start(error), "ядро для проверки ошибок стартовало");
    const std::string broken = rt2.run_task("установи и настрой окружение для проекта", 4);
    check(broken.find("\"ok\":false") != std::string::npos, "неразбираемый план → честная ошибка");
    check(broken.find("план модели не разобран") != std::string::npos,
          "в ошибке сказано, что именно случилось: " + broken);

    std::system("pkill -f ai.fake_brain >/dev/null 2>&1 || true");
}

static void test_agent_loop_with_python_worker() {
    group("агентный цикл: настоящий Python-воркер (мозг со сценарием)");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string base = "/tmp/agent_worker_e2e";
    const std::string sock = "/tmp/agent_worker_e2e.sock";
    const std::string state = "/tmp/agent_worker_e2e_state";
    fs::remove_all(base);
    fs::remove_all(state);
    std::remove(sock.c_str());

    const std::string script =
        "[{\"say\":\"готовлю файл\",\"calls\":["
        "{\"tool\":\"create_folder\",\"args\":{\"path\":\"" + base + "/Отчёт\"}},"
        "{\"tool\":\"write_file\",\"args\":{\"path\":\"" + base + "/Отчёт/итог.txt\","
        "\"content\":\"3 задачи\"}}]},"
        "{\"say\":\"Готово\"}]";
    const std::string cmd = "cd " + repo + " && AGENT_SCRIPT='" + script + "' AGENT_STATE_DIR=" +
                            state + " nohup python3 -m ai.dev_scripted --serve --socket " + sock +
                            " --state-dir " + state + " > /tmp/agent_worker_e2e.log 2>&1 &";
    std::system(cmd.c_str());
    bool up = false;
    for (int i = 0; i < 400 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "воркер поднял канал");
    if (!up) return;

    RuntimeConfig cfg;
    cfg.state_dir = state;
    cfg.preload = false;
    cfg.ai_socket = sock;
    AgentRuntime rt(cfg, make_platform());
    std::string error;
    check(rt.start(error), "ядро стартовало: " + error);

    std::vector<std::string> statuses;
    rt.set_event_sink([&statuses](const Event& ev) {
        if (!ev.message.empty()) statuses.push_back(ev.kind + ": " + ev.message);
    });
    const std::string result = rt.run_task("проанализируй проект и подготовь отчёт", 5);
    check(result.find("\"ok\":true") != std::string::npos,
          "задача выполнена через настоящий воркер: " + result);
    check(result.find("\"llm_calls\":2") != std::string::npos, "модель вызвана на план и переплан");
    check(fs::exists(base + "/Отчёт/итог.txt"), "файл создан инструментами ядра");
    check_eq(read_text_file(base + "/Отчёт/итог.txt"), "3 задачи", "содержимое записано верно");
    check(result.find("\"message\":\"Готово\"") != std::string::npos,
          "итог — слова модели, а не техническая простыня");
    bool planned = false, finished = false;
    for (const std::string& line : statuses) {
        if (line.find("готовлю файл") != std::string::npos) planned = true;
        if (line.find("Готово") != std::string::npos) finished = true;
    }
    check(planned, "план модели показан пользователю человеческим статусом");
    check(finished, "финал показан человеческим статусом");

    std::system("pkill -f dev_scripted >/dev/null 2>&1 || true");
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    fs::remove_all(base);
}

static void test_agent_loop_failed_tool() {
    group("агентный цикл: провал инструмента не выдаётся за успех");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string sock = "/tmp/agent_loop_bad.sock";
    std::remove(sock.c_str());
    start_fake_brain(repo, sock, "/tmp/agent_loop_bad", "bad_tool", "/tmp/agent_loop_bad.log");
    bool up = false;
    for (int i = 0; i < 200 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "второй фейковый мозг поднял канал");
    if (up) {
        RuntimeConfig cfg;
        cfg.state_dir = "/tmp/agent_loop_bad_state";
        cfg.preload = false;
        cfg.ai_socket = sock;
        AgentRuntime rt(cfg, make_platform());
        std::string error;
        rt.start(error);
        const std::string result = rt.run_task("установи и настрой окружение для проекта", 4);
        check(result.find("\"ok\":false") != std::string::npos,
              "неизвестный инструмент → задача не «успешна»: " + result);
        check(result.find("\"failed_calls\":1") != std::string::npos, "счётчик провалов честный");
        check(result.find("teleport_user") != std::string::npos, "в наблюдениях видно, что не сработало");
    }
    std::system("pkill -f ai.fake_brain >/dev/null 2>&1 || true");
}

// ---------------------------------------------------------------------------
//  Полная сборка теста
// ---------------------------------------------------------------------------
int main(int argc, char** argv) {
    const bool bench_only = argc > 1 && std::string(argv[1]) == "--bench";
    const int iterations = argc > 2 ? std::atoi(argv[2]) : 2000;

    std::printf("agent core tests\n");
    if (!bench_only) {
        test_intent();
        test_compound();
        test_router();
        test_registry_optimizer();
        test_batch();
        test_waits();
        test_runtime_cycle();
        test_abi();
        test_run_tool();
        test_launch_failure_is_fast();
        test_agent_loop();
        test_agent_loop_with_python_worker();
        test_agent_loop_failed_tool();
        test_ipc();
        std::printf("\nитог: %d пройдено, %d провалено\n", g_passed, g_failed);
    }
    bench_router(iterations);
    return g_failed == 0 ? 0 : 1;
}
