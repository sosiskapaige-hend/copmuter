// Тесты ядра AgentRuntime: разбор фраз, маршрутизация, реестр приложений,
// оптимизатор, пакеты действий, ожидания, полный цикл простой команды,
// агентный цикл, зрение, безопасность, ABI и IPC.
//
// Сборка (без CMake, чистый g++): native/build_linux.sh
// Запуск:
//   ./build/agent_tests            — все тесты
//   ./build/agent_tests --bench    — только микро-бенчмарк маршрутизатора
#include "test_support.h"

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
                            " AGENT_FAKE_DUMP=1" +
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
    // Реестр приложений этой машины: модель должна получать реальные имена и пути,
    // а не выдумывать их. Путь подтверждаем так же, как это делает первый запуск.
    rt.apps().set_path("vscode", "/usr/bin/code");

    const std::string result = rt.run_task("проанализируй проект и напиши отчёт", 6);
    check(result.find("\"mode\":\"agent\"") != std::string::npos, "задача пошла агентным циклом");
    check(result.find("\"ok\":true") != std::string::npos, "задача завершена успешно: " + result);
    check(result.find("\"llm_calls\":2") != std::string::npos, "модель вызвана дважды (план + переплан)");
    check(result.find("\"tool_calls\":3") != std::string::npos, "инструменты вызваны по плану");
    check(fs::exists(base + "/AgentLoop/plan.txt"), "побочный эффект на диске есть");
    check_eq(read_text_file(base + "/AgentLoop/plan.txt"), "готово", "содержимое файла записано верно");

    // Что именно ядро положило в запрос к мозгу: состояние ПК и реестр приложений.
    const std::string journal0 = read_text_file(log);
    check(journal0.find("request=") != std::string::npos, "запросы к мозгу записаны");
    check(journal0.find("\"apps\": [") != std::string::npos, "в запросе плана есть реестр приложений");
    check(journal0.find("\"key\": \"vscode\"") != std::string::npos &&
              journal0.find("/usr/bin/code") != std::string::npos,
          "подтверждённый путь приложения уходит в план");
    check(journal0.find("\"state\": {") != std::string::npos, "в запросе плана есть состояние ПК");
    check(journal0.find("\"tools\": [") != std::string::npos, "в запросе плана есть инструменты");
    check(journal0.find("\"observations\":") != std::string::npos,
          "наблюдения предыдущих шагов передаются модели");

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
//  Зрение: снимок → мозг → координаты → клик ядра → проверка изменения экрана
// ---------------------------------------------------------------------------
static void test_vision_roundtrip() {
    group("зрение: снимок → мозг → клик → проверка");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string sock = "/tmp/agent_vision.sock";
    std::remove(sock.c_str());
    std::system("pkill -f 'ai[.]fake_brain' >/dev/null 2>&1 || true");
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    const std::string cmd = "cd " + repo +
                            " && AGENT_FAKE_MODE=vision AGENT_FAKE_CLICK=640,360"
                            " AGENT_FAKE_SOCKET=" + sock +
                            " nohup python3 -m ai.fake_brain > /tmp/agent_vision.log 2>&1 &";
    std::system(cmd.c_str());
    bool up = false;
    for (int i = 0; i < 300 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "канал к зрению поднят");
    if (!up) return;

    auto platform = std::make_unique<MockPlatform>();
    MockPlatform* plat = platform.get();
    RuntimeConfig cfg;
    cfg.preload = false;
    cfg.state_dir = "/tmp/agent_vision_state";
    cfg.ai_socket = sock;
    AgentRuntime rt(cfg, std::move(platform));
    std::string error;
    rt.start(error);

    const std::string result = rt.run_tool("find_element", R"({"target":"кнопка ОК"})");
    check(result.find("\"ok\":true") != std::string::npos, "зрение нашло элемент: " + result);
    check(plat->clicks.size() == 1, "клик выполнен ядром (SendInput), а не моделью");
    if (!plat->clicks.empty()) {
        check(plat->clicks[0].first == 640 && plat->clicks[0].second == 360,
              "координаты взяты из ответа зрения: " + std::to_string(plat->clicks[0].first) + "," +
                  std::to_string(plat->clicks[0].second));
    }
    check(result.find("\"changed\":true") != std::string::npos,
          "экран изменился — действие проверено, а не объявлено успешным");

    // Кадра нет — честная ошибка, а не выдуманный клик.
    std::system("pkill -f 'ai[.]fake_brain' >/dev/null 2>&1 || true");
    std::this_thread::sleep_for(std::chrono::milliseconds(120));
}

static void test_vision_honest_failure() {
    group("зрение: «не вижу» — это честный отказ");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string sock = "/tmp/agent_vision_none.sock";
    std::remove(sock.c_str());
    const std::string cmd = "cd " + repo +
                            " && AGENT_FAKE_MODE=vision_none AGENT_FAKE_SOCKET=" + sock +
                            " nohup python3 -m ai.fake_brain > /tmp/agent_vision_none.log 2>&1 &";
    std::system(cmd.c_str());
    bool up = false;
    for (int i = 0; i < 300 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "канал поднят");
    if (up) {
        auto platform = std::make_unique<MockPlatform>();
        MockPlatform* plat = platform.get();
        RuntimeConfig cfg;
        cfg.preload = false;
        cfg.state_dir = "/tmp/agent_vision_none_state";
        cfg.ai_socket = sock;
        AgentRuntime rt(cfg, std::move(platform));
        std::string error;
        rt.start(error);
        const std::string result = rt.run_tool("find_element", R"({"target":"несуществующая кнопка"})");
        check(result.find("\"ok\":false") != std::string::npos, "клика нет: " + result);
        check(result.find("вижу") != std::string::npos, "причина названа словами пользователя");
        check(plat->clicks.empty(), "ядро не кликало наугад");
    }
    std::system("pkill -f 'ai[.]fake_brain' >/dev/null 2>&1 || true");
}

// ---------------------------------------------------------------------------
//  Подтверждение опасных действий и «Стоп» на уровне исполнителя
// ---------------------------------------------------------------------------
static void test_confirmation_gate() {
    group("безопасность: модель не выполняет опасное без подтверждения");
    {
        auto rt_ptr = make_runtime();
        AgentRuntime& rt = *rt_ptr;
        rt.config().safety_mode = "confirm";
        const std::string answer = rt.run_tool("delete_path", R"({"path":"C:/Work/123"})");
        check(answer.find("\"needs_confirmation\":true") != std::string::npos,
              "в режиме подтверждения действие остановлено: " + answer);
        check(answer.find("требуется подтверждение") != std::string::npos,
              "пользователю сказано, что именно нужно подтвердить");
    }
    {
        auto rt_ptr = make_runtime();
        AgentRuntime& rt = *rt_ptr;
        rt.config().safety_mode = "auto";
        const std::string answer = rt.run_tool("delete_path", R"({"path":"C:/Temp/*"})");
        check(answer.find("\"needs_confirmation\":true") != std::string::npos,
              "массовое удаление (маска) требует подтверждения и в auto: " + answer);
    }
    {
        auto rt_ptr = make_runtime();
        AgentRuntime& rt = *rt_ptr;
        rt.config().safety_mode = "full";
        const std::string answer = rt.run_tool("delete_path", R"({"path":"C:/Temp/one.txt"})");
        check(answer.find("\"needs_confirmation\"") == std::string::npos,
              "в полном режиме явное удаление одного файла не переспрашивает");
    }
}

static void test_cancel_stops_agent_loop() {
    group("«Стоп»: отмена доходит до исполнителя");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string sock = "/tmp/agent_cancel.sock";
    std::remove(sock.c_str());
    const std::string cmd = "cd " + repo +
                            " && AGENT_FAKE_MODE=slow AGENT_FAKE_SOCKET=" + sock +
                            " nohup python3 -m ai.fake_brain > /tmp/agent_cancel.log 2>&1 &";
    std::system(cmd.c_str());
    bool up = false;
    for (int i = 0; i < 300 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "канал поднят");
    if (up) {
        auto platform = std::make_unique<MockPlatform>();
        RuntimeConfig cfg;
        cfg.preload = false;
        cfg.state_dir = "/tmp/agent_cancel_state";
        cfg.ai_socket = sock;
        AgentRuntime rt(cfg, std::move(platform));
        std::string error;
        rt.start(error);
        std::atomic<bool> cancelled{false};
        rt.set_cancel([&cancelled]() { return cancelled.load(); });
        std::thread stopper([&cancelled]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(300));
            cancelled.store(true);
        });
        const int64_t t0 = now_ms();
        const std::string result = rt.run_task("дождись того, чего не будет", 3);
        const int64_t ms = now_ms() - t0;
        stopper.join();
        check(ms < 2000, "ожидание прервано сразу, а не через 30 секунд (получено " +
                             std::to_string(ms) + " мс)");
        check(result.find("не сработал") != std::string::npos ||
                  result.find("\"ok\":false") != std::string::npos,
              "прерванная задача не объявлена успешной: " + result);
    }
    std::system("pkill -f 'ai[.]fake_brain' >/dev/null 2>&1 || true");
}

static void test_browser_path() {
    group("браузерный путь: сложная страница через Playwright-мозг");
    const char* repo_env = std::getenv("AGENT_REPO_ROOT");
    const std::string repo = repo_env ? repo_env : ".";
    const std::string sock = "/tmp/agent_browser.sock";
    std::remove(sock.c_str());
    const std::string cmd = "cd " + repo +
                            " && AGENT_FAKE_SOCKET=" + sock +
                            " nohup python3 -m ai.fake_brain > /tmp/agent_browser.log 2>&1 &";
    std::system(cmd.c_str());
    bool up = false;
    for (int i = 0; i < 300 && !up; ++i) {
        up = fs::exists(sock);
        if (!up) std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    check(up, "канал поднят");
    if (!up) return;

    auto platform = std::make_unique<MockPlatform>();
    RuntimeConfig cfg;
    cfg.preload = false;
    cfg.state_dir = "/tmp/agent_browser_state";
    cfg.ai_socket = sock;
    AgentRuntime rt(cfg, std::move(platform));
    std::string error;
    rt.start(error);

    const std::string opened = rt.run_tool(
        "browser_task", R"({"action":"open","url":"https://youtube.com/results?search_query=котики"})");
    check(opened.find("\"ok\":true") != std::string::npos, "страница открыта браузерным путём: " + opened);
    check(opened.find("Кошки") != std::string::npos || opened.find("youtube") != std::string::npos,
          "данные страницы попали в наблюдение");

    const std::string text = rt.run_tool("playwright", R"({"action":"text","selector":"#results"})");
    check(text.find("Котики") != std::string::npos || text.find("\"text\"") != std::string::npos,
          "текст страницы извлечён: " + text);

    const std::string bad = rt.run_tool("web_automation", R"({"action":"teleport"})");
    check(bad.find("\"ok\":false") != std::string::npos &&
              bad.find("неизвестное действие браузера") != std::string::npos,
          "непонятное действие — честный отказ, а не «получилось»: " + bad);

    // Без канала инструмент честно отказывает, а не молчит.
    RuntimeConfig lonely;
    lonely.preload = false;
    lonely.state_dir = "/tmp/agent_browser_state2";
    AgentRuntime rt2(lonely, std::make_unique<MockPlatform>());
    rt2.start(error);
    const std::string no_channel = rt2.run_tool("browser_task", R"({"action":"open","url":"x"})");
    check(no_channel.find("канал к мозгу не настроен") != std::string::npos,
          "без канала — понятная ошибка: " + no_channel);

    std::system("pkill -f 'ai[.]fake_brain' >/dev/null 2>&1 || true");
}

// ---------------------------------------------------------------------------
//  Подтверждение опасных действий: ответ приходит от пользователя
// ---------------------------------------------------------------------------
static void test_confirmation_flow() {
    group("подтверждение: опасное действие выполняется только с согласия");
    // В режиме auto (по умолчанию) одиночная явно названная папка удаляется без вопроса,
    // а массовое удаление и критичные операции — только с подтверждением.
    const std::string target = "/home/tester/Desktop/123";
    const std::string state = "/tmp/agent_confirm_state";

    // 1) Ждём ответа: пока пользователь не ответил, действие не выполняется.
    {
        auto platform = std::make_unique<MockPlatform>();
        MockPlatform* plat = platform.get();
        plat->files[target] = "<dir>";
        RuntimeConfig cfg;
        cfg.preload = false;
        cfg.state_dir = state;
        cfg.safety_mode = "confirm";
        cfg.confirm_timeout_ms = 5000;
        AgentRuntime rt(cfg, std::move(platform));
        std::string error;
        rt.start(error);

        std::vector<std::string> events;
        rt.set_event_sink([&events](const Event& ev) {
            if (ev.kind == "confirm") events.push_back(ev.status);
        });
        std::thread answerer([&rt]() {
            // Даём рантайму время дойти до вопроса, затем «нажимаем Да».
            for (int i = 0; i < 200 && !rt.confirmation_pending(); ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            rt.answer_confirmation(true);
        });
        const int64_t t0 = now_ms();
        const FastOutcome outcome = rt.execute("удали папку 123 с рабочего стола");
        const int64_t ms = now_ms() - t0;
        answerer.join();
        check(outcome.ok, "после подтверждения удаление выполнено: " + outcome.to_json());
        check(plat->files.count(target) == 0, "папка действительно удалена");
        check(ms < 3000, "ожидания ответа не растянулись: " + std::to_string(ms) + " мс");
        bool asked = false, approved = false;
        for (const std::string& status : events) {
            if (status == "pending") asked = true;
            if (status == "approved") approved = true;
        }
        check(asked, "пользователю задан вопрос (событие confirm/pending)");
        check(approved, "подтверждение отражено в событиях для UI");
    }

    // 2) Отказ: действие не выполняется, папка остаётся.
    {
        auto platform = std::make_unique<MockPlatform>();
        MockPlatform* plat = platform.get();
        plat->files[target] = "<dir>";
        RuntimeConfig cfg;
        cfg.preload = false;
        cfg.state_dir = state + "2";
        cfg.safety_mode = "confirm";
        cfg.confirm_timeout_ms = 5000;
        AgentRuntime rt(cfg, std::move(platform));
        std::string error;
        rt.start(error);
        std::thread answerer([&rt]() {
            for (int i = 0; i < 200 && !rt.confirmation_pending(); ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            rt.answer_confirmation(false);
        });
        const FastOutcome outcome = rt.execute("удали папку 123 с рабочего стола");
        answerer.join();
        check(!outcome.ok, "без согласия действие не выполнено");
        check(plat->files.count(target) == 1, "папка на месте — ничего не удалено");
        check(outcome.message.find("отменено") != std::string::npos ||
                  outcome.message.find("подтверждение") != std::string::npos,
              "пользователю объяснили, что нужно подтверждение: " + outcome.message);
    }

    // 3) Подтверждение требуется, но ждать ответа не настроено — отказ, а не «на удачу».
    {
        auto platform = std::make_unique<MockPlatform>();
        MockPlatform* plat = platform.get();
        plat->files[target] = "<dir>";
        RuntimeConfig cfg;
        cfg.preload = false;
        cfg.state_dir = state + "3";
        cfg.safety_mode = "confirm";
        cfg.confirm_timeout_ms = 0;
        AgentRuntime rt(cfg, std::move(platform));
        std::string error;
        rt.start(error);
        const FastOutcome outcome = rt.execute("удали папку 123 с рабочего стола");
        check(!outcome.ok && plat->files.count(target) == 1,
              "молчание = отказ, данные целы: " + outcome.to_json());
    }

    // 4) Обработчик из UI (диалог) — решает он, ожидание не нужно.
    {
        auto platform = std::make_unique<MockPlatform>();
        MockPlatform* plat = platform.get();
        plat->files[target] = "<dir>";
        RuntimeConfig cfg;
        cfg.preload = false;
        cfg.state_dir = state + "4";
        cfg.safety_mode = "confirm";
        AgentRuntime rt(cfg, std::move(platform));
        std::string error;
        rt.start(error);
        std::string asked_question;
        rt.set_confirm_handler([&asked_question](const std::string& question, int) {
            asked_question = question;
            return true;      // пользователь нажал «Да» в диалоге
        });
        const FastOutcome outcome = rt.execute("удали папку 123 с рабочего стола");
        check(outcome.ok, "обработчик подтвердил — действие выполнено: " + outcome.to_json());
        check(asked_question.find("Удалить папку") != std::string::npos,
              "в диалоге понятный вопрос: " + asked_question);
        check(plat->files.count(target) == 0, "папка удалена после подтверждения");
    }
}

// ---------------------------------------------------------------------------
//  PLAN ONLY: честный путь приложения (не склейка с рабочим каталогом)
// ---------------------------------------------------------------------------
static void test_preview_paths() {
    group("PLAN ONLY показывает путь из реестра, а не выдуманный");
    auto platform = std::make_unique<MockPlatform>();
    MockPlatform* plat = platform.get();   // cwd мока — /mock
    RuntimeConfig cfg;
    cfg.preload = false;
    cfg.state_dir = "/tmp/agent_preview_state";
    AgentRuntime rt(cfg, std::move(platform));
    std::string error;
    rt.start(error);

    // Приложение вне каталога: обещаем поиск, но не показываем /mock/ZzzApp.
    const std::string unknown = rt.preview("открой ZzzApp и Telegram");
    check(unknown.find("ZzzApp") != std::string::npos && unknown.find("elegram") != std::string::npos,
          "в плане обе команды: " + unknown);
    check(unknown.find("/mock/") == std::string::npos,
          "нет пути, склеенного с рабочим каталогом");
    check(unknown.find("при запуске") != std::string::npos,
          "сказано, что путь определится при запуске: " + unknown);
    // Известное приложение: видно, чем именно его откроют (протокол/путь), без выдумок.
    const std::string pair = rt.preview("открой Discord и Telegram");
    check(pair.find("discord://") != std::string::npos || pair.find("tg://") != std::string::npos,
          "для известных приложений виден способ запуска: " + pair);

    // Как только путь подтверждён — он и показывается (кеш реестра приложений).
    rt.apps().set_path("discord", "C:/Program Files/Discord/Discord.exe");
    const std::string known = rt.preview("открой Discord");
    check(known.find("C:/Program Files/Discord/Discord.exe") != std::string::npos,
          "подтверждённый путь виден в плане: " + known);

    // Файловая операция по-прежнему показывает реальный путь места.
    plat->files["/home/tester/Desktop/123"] = "<dir>";
    const std::string del = rt.preview("удали папку 123 с рабочего стола");
    check(del.find("/home/tester/Desktop/123") != std::string::npos,
          "для файловой операции путь на месте: " + del);
    check(del.find("одтвержд") != std::string::npos,
          "видно, потребуется ли подтверждение: " + del);
}

// ---------------------------------------------------------------------------
//  Кадр и координаты: монитор не всегда начинается с (0,0)
// ---------------------------------------------------------------------------
static void test_frame_crop() {
    group("снимок и клик в одной системе координат (мультимонитор)");
    Frame primary;
    primary.width = 1920;
    primary.height = 1080;
    primary.origin_x = 0;
    primary.origin_y = 0;

    FrameCrop whole = crop_into_frame(primary, 0, 0, 1920, 1080);
    check(whole.covered && whole.x == 0 && whole.y == 0 && whole.width == 1920,
          "весь основной монитор покрыт кадром");

    FrameCrop region = crop_into_frame(primary, 100, 50, 400, 300);
    check(region.covered && region.x == 100 && region.y == 50, "область вырезается со смещением");

    // Второй монитор слева (отрицательные координаты) — как в реальной раскладке Windows.
    Frame left;
    left.width = 1280;
    left.height = 1024;
    left.origin_x = -1280;
    left.origin_y = 0;
    FrameCrop on_left = crop_into_frame(left, -1200, 100, 200, 200);
    check(on_left.covered && on_left.x == 80 && on_left.y == 100,
          "отрицательные координаты монитора пересчитаны верно");
    check(!crop_into_frame(primary, -1200, 100, 200, 200).covered,
          "область левого монитора не считается частью основного");
    check(!crop_into_frame(primary, 1800, 1000, 400, 400).covered,
          "область, выходящая за кадр, честно отклонена");
    check(!crop_into_frame(Frame{}, 0, 0, 10, 10).covered, "пустой кадр ничего не покрывает");

    // Координаты кадра + смещение = координаты экрана (этим живёт путь зрения).
    check(left.origin_x + on_left.x == -1200 && left.origin_y + on_left.y == 100,
          "смещение кадра переводит пиксель обратно в координаты экрана");
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
        test_vision_roundtrip();
        test_vision_honest_failure();
        test_confirmation_gate();
        test_confirmation_flow();
        test_preview_paths();
        test_frame_crop();
        test_browser_path();
        test_cancel_stops_agent_loop();
        test_ipc();
        std::printf("\nитог: %d пройдено, %d провалено\n", g_passed, g_failed);
    }
    bench_router(iterations);
    return g_failed == 0 ? 0 : 1;
}
