// Обязательные сценарии ТЗ: 10 команд, на которых проверяется вся архитектура.
//
// Запуск (любая ОС, платформа подменена):
//     ./build/agent_scenarios
//     ./build/agent_scenarios --direct-only    — только то, что делает рантайм без модели
//     ./build/agent_scenarios --agent-only     — только агентные сценарии (нужен python3)
//
// Что проверяем: маршрут (модель вызвана или нет), какие инструменты ушли, есть ли
// проверка результата, честность отказа. На Windows те же сценарии прогоняются
// agent_host'ом на живом железе: там мок заменяется настоящей платформой.
#include "test_support.h"

#include <csignal>
#include <functional>

using namespace agent;

namespace {

bool contains(const std::string& haystack, const std::string& needle) {
    return haystack.find(needle) != std::string::npos;
}

// Мозг со сценарием: поднимает `ai.fake_brain` отдельным процессом (как настоящий воркер),
// поэтому проверяется реальная связка «ядро ↔ внешний планировщик», а не заглушка в памяти.
class ScenarioBrain {
public:
    ScenarioBrain(const std::string& repo, const std::string& name, const std::string& mode)
        : repo_(repo), socket_("/tmp/agent_scenario_" + name + ".sock"),
          log_("/tmp/agent_scenario_" + name + ".log"),
          mode_(mode) {}

    ~ScenarioBrain() { stop(); }

    bool start() {
        std::system("pkill -f 'python3 -m ai[.]fake_brain' >/dev/null 2>&1 || true");
        std::remove(socket_.c_str());
        std::this_thread::sleep_for(std::chrono::milliseconds(120));
        const std::string cmd = "cd " + repo_ + " && AGENT_FAKE_MODE=" + mode_ +
                                " AGENT_FAKE_CLICK=640,360 AGENT_FAKE_SOCKET=" + socket_ +
                                " nohup python3 -m ai.fake_brain > " + log_ + " 2>&1 &";
        std::system(cmd.c_str());
        for (int i = 0; i < 400; ++i) {
            if (fs::exists(socket_)) return true;
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
        }
        return false;
    }

    void stop() {
        std::system("pkill -f 'python3 -m ai[.]fake_brain' >/dev/null 2>&1 || true");
    }

    const std::string& socket() const { return socket_; }

private:
    std::string repo_;
    std::string socket_;
    std::string log_;
    std::string mode_;
};

struct DirectRun {
    std::unique_ptr<AgentRuntime> runtime;
    MockPlatform* platform = nullptr;   // владелец — runtime, указатель живёт столько же
    FastOutcome outcome;
    std::string json;
};

DirectRun run_direct(const std::string& phrase, const std::string& state_dir,
                     const std::function<void(MockPlatform&)>& setup = {}) {
    DirectRun run;
    auto platform = std::make_unique<MockPlatform>();
    run.platform = platform.get();
    if (setup) setup(*platform);
    RuntimeConfig cfg;
    cfg.preload = true;
    cfg.state_dir = state_dir;
    run.runtime = std::make_unique<AgentRuntime>(cfg, std::move(platform));
    std::string error;
    run.runtime->start(error);
    run.outcome = run.runtime->execute(phrase);
    run.json = run.outcome.to_json();
    return run;
}

// ---------------------------------------------------------------------------
//  1–4: запуск приложений — модель не участвует
// ---------------------------------------------------------------------------
void scenario_launch_apps() {
    struct Case {
        const char* phrase;
        const char* app;
    };
    const Case cases[] = {
        {"открой браузер", "chrome"},
        {"открой Discord", "discord"},
        {"открой Telegram", "telegram"},
        {"открой VS Code", "vscode"},
    };
    for (const Case& c : cases) {
        DirectRun run = run_direct(c.phrase, "/tmp/agent_scenario_state",
                                   [](MockPlatform& p) { p.spawn_registers_process = true; });
        const std::string label = std::string("«") + c.phrase + "»";
        check(run.outcome.handled, label + " обработана рантаймом");
        check(run.outcome.action == std::string("launch_app"),
              label + " — это запуск приложения (получено «" + std::string(run.outcome.action) + "»)");
        check(contains(run.json, std::string("\"app\":\"") + c.app + "\""),
              label + " — правильное приложение (" + c.app + "): " + run.json);
        check(run.runtime->metrics().llm_calls == 0, label + ": модель не вызывалась");
        check(!run.platform->spawned.empty(), label + ": запуск дошёл до платформы");
        check(run.outcome.total_ms < 2000, label + ": уложились в 2 секунды (" +
                                               std::to_string(int(run.outcome.total_ms)) + " мс)");
    }
}

// ---------------------------------------------------------------------------
//  5: VS Code + «напиши калькулятор на Python» — агентный цикл
// ---------------------------------------------------------------------------
void scenario_calculator(ScenarioBrain& brain) {
    auto platform = std::make_unique<MockPlatform>();
    MockPlatform* plat = platform.get();
    plat->command_result.started = true;
    plat->command_result.exit_code = 0;
    plat->command_result.stdout_text = "2 + 2 = 4\n";
    plat->spawn_registers_process = true;   // запуск редактора подтверждается процессом и окном

    RuntimeConfig cfg;
    cfg.preload = true;
    cfg.state_dir = "/tmp/agent_scenario_state5";
    cfg.ai_socket = brain.socket();
    AgentRuntime rt(cfg, std::move(platform));
    std::string error;
    rt.start(error);

    std::vector<std::string> tools_called;
    rt.set_event_sink([&tools_called](const Event& ev) {
        if (ev.kind == "tool_call" && !ev.tool.empty()) tools_called.push_back(ev.tool);
    });

    const std::string result = rt.run_task("напиши калькулятор на python и запусти его", 8);
    check(contains(result, "\"ok\":true"), "задача завершена: " + result);
    check(contains(result, "\"mode\":\"agent\""), "пошла агентным циклом: нужен план и код");
    check(contains(result, "\"llm_calls\":") && rt.metrics().llm_calls >= 2,
          "модель участвовала в планировании");
    check(rt.metrics().tool_calls >= 4, "инструменты вызваны по шагам плана");

    const auto called = [&tools_called](const char* name) {
        for (const std::string& t : tools_called)
            if (t == name) return true;
        return false;
    };
    check(called("launch_application"), "редактор открыт инструментом ядра");
    check(contains(result, "launch_application: ok"),
          "запуск подтверждён процессом/окном, а не объявлен: " + result);
    check(called("write_file"), "код записан на диск инструментом ядра");
    check(called("clipboard"), "длинный текст проходил через буфер обмена");
    check(called("run_command"), "скрипт запущен");
    const auto files = plat->files;
    bool code_written = false;
    for (const auto& [path, content] : files) {
        if (contains(path, "calculator") && contains(content, "print")) code_written = true;
    }
    check(code_written, "на диске лежит код калькулятора, а не пустой файл");
}

// ---------------------------------------------------------------------------
//  6: YouTube + «найди видео про котиков»
// ---------------------------------------------------------------------------
void scenario_youtube() {
    DirectRun run = run_direct("открой ютуб и найди видео про котиков", "/tmp/agent_scenario_state6");
    check(run.outcome.handled, "составная команда разобрана рантаймом");
    check(run.runtime->metrics().llm_calls == 0, "модель не нужна: путь детерминированный");
    bool searched = false, query_ok = false;
    for (const std::string& uri : run.platform->spawned) {
        if (contains(uri, "youtube.com/results?search_query=")) {
            searched = true;
            // Запрос уходит в percent-кодировке (utf8) либо как есть.
            if (contains(uri, "%D0%BA%D0%BE%D1%82") || contains(uri, "котик")) query_ok = true;
        }
    }
    check(searched, "поиск ушёл прямой ссылкой results?search_query=…");
    check(query_ok, "в ссылке именно запрос пользователя (котики)");
    check(contains(run.json, "\"route\":\"compound\"") || run.outcome.actions >= 1,
          "открытие сайта и поиск — в одном проходе, без лишних кругов модели: " + run.json);
}

// ---------------------------------------------------------------------------
//  7: поиск «123»
// ---------------------------------------------------------------------------
void scenario_search_123() {
    DirectRun run = run_direct("найди 123", "/tmp/agent_scenario_state7");
    check(run.outcome.handled, "«найди 123» разобрана рантаймом");
    check(run.outcome.action == std::string("web_search"), "это поиск в интернете");
    check(run.runtime->metrics().llm_calls == 0, "модель не вызывалась");
    bool searched = false;
    for (const std::string& uri : run.platform->spawned) {
        if (contains(uri, "123") && (contains(uri, "search") || contains(uri, "google") ||
                                     contains(uri, "yandex") || contains(uri, "duck")))
            searched = true;
    }
    check(searched, "запрос «123» ушёл поисковику прямой ссылкой");
}

// ---------------------------------------------------------------------------
//  8: «Поменяй обои» — системный вызов
// ---------------------------------------------------------------------------
void scenario_wallpaper() {
    DirectRun run = run_direct("поменяй обои на картинку /tmp/wall.png",
                               "/tmp/agent_scenario_state8",
                               [](MockPlatform& p) { p.files["/tmp/wall.png"] = "картинка"; });
    check(run.outcome.handled, "команда разобрана рантаймом");
    check(run.outcome.action == std::string("set_wallpaper"), "это смена обоев");
    check(run.runtime->metrics().llm_calls == 0, "модель не вызывалась");
    bool called = false;
    for (const std::string& call : run.platform->spawned)
        if (contains(call, "wall:/tmp/wall.png")) called = true;
    check(called, "обои поставлены системным вызовом, а не эмуляцией кликов");
}

// ---------------------------------------------------------------------------
//  9: «Удали папку 123 с рабочего стола»
// ---------------------------------------------------------------------------
void scenario_delete_folder() {
    auto platform = std::make_unique<MockPlatform>();
    MockPlatform* plat = platform.get();
    const std::string target = "/home/tester/Desktop/123";
    plat->files[target] = "<dir>";
    plat->files["/home/tester/Desktop/важное.txt"] = "не трогать";

    RuntimeConfig cfg;
    cfg.preload = true;
    cfg.state_dir = "/tmp/agent_scenario_state9";
    AgentRuntime rt(cfg, std::move(platform));
    std::string error;
    rt.start(error);

    const std::string preview = rt.preview("удали папку 123 с рабочего стола");
    check(contains(preview, target), "план показывает точный путь: " + preview);

    const FastOutcome outcome = rt.execute("удали папку 123 с рабочего стола");
    check(outcome.ok, "удаление выполнено: " + outcome.to_json());
    check(plat->files.count(target) == 0, "папки больше нет — результат проверен");
    check(plat->files.count("/home/tester/Desktop/важное.txt") == 1, "соседний файл не тронут");
    check(rt.metrics().llm_calls == 0, "нашёл, удалил, проверил — без модели");
}

// ---------------------------------------------------------------------------
//  10: «Открой программу X и нажми кнопку Y»
// ---------------------------------------------------------------------------
void scenario_click_button(ScenarioBrain& brain) {
    auto platform = std::make_unique<MockPlatform>();
    MockPlatform* plat = platform.get();
    RuntimeConfig cfg;
    cfg.preload = true;
    cfg.state_dir = "/tmp/agent_scenario_state10";
    cfg.ai_socket = brain.socket();
    AgentRuntime rt(cfg, std::move(platform));
    std::string error;
    rt.start(error);

    const std::string result = rt.run_task("открой программу mockapp и нажми кнопку ОК", 8);
    check(contains(result, "\"ok\":true"), "задача выполнена: " + result);
    check(contains(result, "launch_application"), "программа открыта инструментом ядра");
    check(contains(result, "find_element"), "кнопку искали зрением: интерфейс неизвестен");
    check(plat->clicks.size() == 1, "клик сделан ядром по координатам зрения");
    if (!plat->clicks.empty()) {
        check(plat->clicks[0].first == 640 && plat->clicks[0].second == 360,
              "координаты пришли из модели зрения");
    }
    check(contains(result, "проверено: результат подтверждён"),
          "после клика экран проверен, и это видно в наблюдении: " + result);
}

}  // namespace

int main(int argc, char** argv) {
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const bool only_agent = argc > 1 && std::string(argv[1]) == "--agent-only";
    const bool only_direct = argc > 1 && std::string(argv[1]) == "--direct-only";

    std::printf("Обязательные сценарии ТЗ (10 команд)\\n");

    if (!only_agent) {
        group("1–4: браузер, Discord, Telegram, VS Code (без модели)");
        scenario_launch_apps();
        group("6: YouTube + «найди видео про котиков» (без модели)");
        scenario_youtube();
        group("7: поиск «123» (без модели)");
        scenario_search_123();
        group("8: «поменяй обои» (системный вызов)");
        scenario_wallpaper();
        group("9: «удали папку 123 с рабочего стола» (найти → удалить → проверить)");
        scenario_delete_folder();
    }

    if (!only_direct) {
        {
            ScenarioBrain brain(repo, "calc", "plan_calculator");
            if (brain.start()) {
                group("5: VS Code + «напиши калькулятор на Python» (агентный цикл)");
                scenario_calculator(brain);
            } else {
                check(false, "сценарный мозг не поднялся — сценарий 5 не проверен");
            }
        }
        {
            ScenarioBrain brain(repo, "ui", "ui");
            if (brain.start()) {
                group("10: «открой программу X и нажми кнопку Y» (зрение)");
                scenario_click_button(brain);
            } else {
                check(false, "сценарный мозг не поднялся — сценарий 10 не проверен");
            }
        }
    }

    return scenario_report();
}
