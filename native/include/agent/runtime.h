// AgentRuntime: сборка ядра в один объект + C ABI для P/Invoke из C#.
//
// Поток выполнения простой команды («открой телегу»):
//
//   pharse ──► IntentEngine ──► Router ──► ToolRegistry ──► Optimizer
//                                                             │
//                        ┌────────────────────────────────────┘
//                        ▼
//                 ActionQueue (batch, параллельно независимые шаги)
//                        ▼
//                  IPlatform (Win32 / SendInput / DXGI / Shell)
//                        ▼
//                  Verification ──► Recovery (fallback) ──► Event / IPC
//
// Модель (Python worker + Qwen3-VL) подключается только когда Router вернул
// Agent / Vision / LlmText. Простые команды не доходят до неё вообще.
#pragma once

#include <atomic>
#include <condition_variable>
#include <fstream>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "core.h"
#include "intent.h"
#include "ipc.h"
#include "platform.h"

namespace agent {

struct RuntimeConfig {
    // Таймауты шагов (мс) — ТЗ §28.
    int launch_timeout_ms = 12000;
    int file_timeout_ms = 5000;
    int browser_timeout_ms = 15000;
    int vision_timeout_ms = 20000;
    int process_timeout_ms = 10000;
    int window_timeout_ms = 8000;
    int shell_timeout_ms = 60000;
    int max_retries = 2;
    // Экран
    int screenshot_max_pixels = 1474560;
    // Режим безопасности: full | auto | confirm | step | observe | plan_only
    std::string safety_mode = "auto";
    std::string default_browser;
    std::string state_dir;             // где хранить кеши/метрики/реестр
    bool preload = true;               // прогрев при старте
    bool fast_path = true;             // простые команды без модели
    bool dry_run = false;              // PLAN ONLY
    std::string language = "ru";
    // Агентный цикл (сложные задачи): канал к Python-мозгу и его лимиты.
    std::string ai_socket;             // \\.\pipe\agent_ai_v1 | /tmp/agent_ai.sock
    int llm_timeout_ms = 120000;       // сколько ждём план модели
    int max_steps = 12;                // лимит шагов агентного цикла
    // Сколько ждать ответа пользователя на «подтвердите опасное действие».
    // 0 — не ждать: без ответа опасное действие НЕ выполняется (безопасно по умолчанию).
    int confirm_timeout_ms = 0;
    // Журнал отладки (ТЗ): рефлексы, инструменты, откаты, ошибки — с временами.
    // Пишется в state_dir/agent_debug.log, если включён (в UI не показывается).
    bool debug_log = false;
    std::string debug_log_path;
};

// Событие для UI/IPC: то же, что видит пользователь («Открываю Telegram», ошибка...).
struct Event {
    std::string kind;        // plan | tool_call | observation | task_done | task_failed | log | metrics
    std::string tool;
    std::string status;      // queued | running | success | failed | timeout | cancelled
    std::string message;     // человекочитаемо
    std::string data_json;   // подробности
    double ms = 0.0;
    int64_t ts_ms = 0;
    std::string to_json() const;
};

using EventFn = std::function<void(const Event&)>;

struct FastOutcome {
    bool handled = false;      // true → модель не нужна, задача выполнена ядром
    bool ok = false;
    RouteKind route = RouteKind::Direct;
    std::string action;
    std::string message;
    std::string error;
    std::string data_json;
    double total_ms = 0.0;
    double route_us = 0.0;     // время принятия решения
    size_t actions = 0;
    bool needs_llm = false;    // ядро признало, что нужна модель (аргумент для Python)
    std::string llm_prompt;    // что попросить у модели (код/план/vision)
    std::string to_json() const;
};

// Метрики (ТЗ §22): главная — Time To Completion.
struct MetricsSnapshot {
    uint64_t tasks = 0, tasks_ok = 0, fast_tasks = 0, agent_tasks = 0;
    uint64_t llm_calls = 0, tool_calls = 0, vision_calls = 0, fallbacks = 0;
    double total_ms = 0.0, route_us_total = 0.0;
    double ttc_avg_ms() const { return tasks ? total_ms / double(tasks) : 0.0; }
    double route_avg_us() const { return tasks ? route_us_total / double(tasks) : 0.0; }
    std::string to_json() const;
};

class AgentRuntime {
public:
    AgentRuntime(RuntimeConfig cfg, std::unique_ptr<IPlatform> platform);
    ~AgentRuntime();

    // --- жизненный цикл ---
    bool start(std::string& error);              // прогрев: приложения, экран, первые слоты
    void stop();
    bool running() const { return running_.load(); }

    // --- основной вход ---
    // Разбирает фразу и, если это простое действие, выполняет его сам.
    FastOutcome execute(std::string_view phrase);
    // Выполнить уже разобранный маршрут (используется тестами и Python-воркером).
    FastOutcome execute_route(const Route& route);
    // Только план (PLAN ONLY / Dry Run): что будет сделано.
    std::string preview(std::string_view phrase);

    // Полный проход по задаче: быстрый путь, а для сложной — агентный цикл
    // (Python-мозг за каналом + нативные инструменты + проверка). Возвращает JSON-итог.
    std::string run_task(std::string_view task, int max_steps = 0);

    // Выполнить один вызов инструмента (агентный цикл: план модели → нативное исполнение).
    // Возвращает JSON-наблюдение: {"ok":…,"output":…,"error":…,"ms":…}.
    std::string run_tool(std::string_view tool, std::string_view args_json);

    // --- доступ для Python-воркера / UI ---
    Router& router() { return *router_; }
    IntentEngine& intents() { return intents_; }
    AppRegistry& apps() { return apps_; }
    ToolRegistry& tools() { return tools_; }
    Optimizer& optimizer() { return optimizer_; }
    WaitManager& wait() { return *wait_; }
    IPlatform& platform() { return *platform_; }
    ActionQueue& queue() { return queue_; }
    const RuntimeConfig& config() const { return cfg_; }
    // Изменяемая конфигурация: режим безопасности, бюджеты, канал — меняются из UI.
    RuntimeConfig& config() { return cfg_; }

    void set_event_sink(EventFn fn);             // UI/IPC
    void emit(Event ev);
    MetricsSnapshot metrics() const;
    std::string metrics_json() const;
    std::string state_json() const;              // computer state (окно/процессы/курсор/экраны)
    std::string tools_json() const;
    void set_cancel(std::function<bool()> fn);   // «Стоп» на уровне исполнителя
    // Подтверждение опасных действий. Если обработчик задан (UI), он и решает;
    // иначе рантайм ждёт ответа через answer_confirmation() до confirm_timeout_ms.
    using ConfirmFn = std::function<bool(const std::string& question, int timeout_ms)>;
    void set_confirm_handler(ConfirmFn fn);
    // Ответ на «подтвердите»: вызывается из UI-потока, когда пользователь нажал кнопку.
    void answer_confirmation(bool approved);
    // Есть ли сейчас ожидающий вопрос (для UI: показать диалог).
    bool confirmation_pending() const;
    // Строка в отладочный журнал: [время][раздел] текст. Дёшево при выключенном журнале.
    void debug(std::string_view area, std::string_view text);
    std::string debug_log_path() const;

private:
    // --- выполнение отдельных намерений (быстрый путь) ---
    bool do_launch_app(const Intent& it, FastOutcome& out);
    bool do_open_url(const Intent& it, FastOutcome& out);
    bool do_focus_window(const Intent& it, FastOutcome& out);
    bool do_open_folder(const Intent& it, FastOutcome& out);
    bool do_web_search(const Intent& it, FastOutcome& out, bool youtube);
    bool do_create_folder(const Intent& it, FastOutcome& out);
    bool do_create_file(const Intent& it, FastOutcome& out);
    bool do_read_file(const Intent& it, FastOutcome& out);
    bool do_delete_path(const Intent& it, FastOutcome& out);
    bool do_move_or_copy(const Intent& it, FastOutcome& out, bool move);
    bool do_screenshot(const Intent& it, FastOutcome& out);
    bool do_set_wallpaper(const Intent& it, FastOutcome& out);
    bool do_volume(const Intent& it, FastOutcome& out);
    bool do_power(const Intent& it, FastOutcome& out);
    bool do_show_desktop(const Intent& it, FastOutcome& out);
    bool do_send_keys(const Intent& it, FastOutcome& out);
    bool do_type_text(const Intent& it, FastOutcome& out);
    bool do_kill_process(const Intent& it, FastOutcome& out);
    bool do_run_command(const Intent& it, FastOutcome& out);
    bool do_find_files(const Intent& it, FastOutcome& out);
    bool do_settings_page(const Intent& it, FastOutcome& out);
    bool do_compound(const Intent& it, FastOutcome& out);

    // --- служебное ---
    bool execute_intent(const Intent& it, FastOutcome& out);
    std::string run_tool_impl(std::string_view tool, std::string_view args_json);
    // Глаза: снимок → Python/Qwen3-VL → координаты → клик ядра → проверка изменения экрана.
    std::string vision_call(const std::string& mode, const std::string& target,
                            std::string_view args_json);
    // Сложные страницы: Playwright в Python-воркере (прямые ссылки — нативный open_url).
    std::string browser_call(std::string_view args_json);
    // Разбор ответа мозга: {"calls":[{"tool","args","note"}],"say","finished"}.
    static bool parse_plan(std::string_view json, std::vector<std::string>& calls, std::string& say,
                           bool& finished);
    bool needs_confirmation(const ToolSpec& tool, const Intent& it, std::string& reason) const;
    bool verify_action(const ActionSpec& a, std::string& detail);
    bool ask_user(std::string_view question, std::string& reason);   // через UI/IPC
    std::string resolve_place(std::string_view place) const;
    std::string resolve_target_path(std::string_view target, std::string_view place,
                                    bool must_exist, bool& found) const;
    void note_result(std::string_view action, bool ok, double ms, Method m);
    // Учёт вызова инструмента агентом: метрики + событие для UI.
    void note_tool_call(std::string_view tool, bool ok, double ms);
    void count_task(bool ok, bool fast, double ms, double route_us);
    void register_builtin_tools();
    std::string describe_app(std::string_view target) const;   // для PLAN ONLY
    // Компактный список приложений для модели: сначала те, что упомянуты в задаче,
    // затем установленные с подтверждённым путём. Полный каталог модели не отправляем.
    std::string apps_json_for(std::string_view task, size_t limit = 12) const;

    RuntimeConfig cfg_;
    std::unique_ptr<IPlatform> platform_;
    AppRegistry apps_;
    IntentEngine intents_;
    ToolRegistry tools_;
    Optimizer optimizer_;
    std::unique_ptr<WaitManager> wait_;
    Router* router_ = nullptr;
    ActionQueue queue_;
    mutable std::mutex mu_;
    EventFn sink_;
    std::atomic<bool> running_{false};
    std::function<bool()> cancel_;
    ConfirmFn confirm_fn_;
    mutable std::mutex confirm_mu_;
    mutable std::condition_variable confirm_cv_;
    mutable bool confirm_pending_ = false;
    mutable bool confirm_answer_ = false;
    mutable std::mutex debug_mu_;
    std::ofstream debug_out_;
    mutable bool debug_disabled_ = false;
    std::unique_ptr<AiLink> ai_link_;   // постоянное соединение с Python-мозгом
    struct Counters {
        std::atomic<uint64_t> tasks{0}, tasks_ok{0}, fast_tasks{0}, agent_tasks{0};
        std::atomic<uint64_t> llm_calls{0}, tool_calls{0}, vision_calls{0}, fallbacks{0};
        std::atomic<uint64_t> route_us{0};
        std::atomic<uint64_t> total_ms{0};
    } c_;
    std::vector<std::pair<std::string, std::string>> places_;   // «рабочий стол» → путь
};

}  // namespace agent

// ---------------------------------------------------------------------------
//  C ABI для C# (P/Invoke) — минимальная поверхность, всё остальное через IPC.
// ---------------------------------------------------------------------------
// Export decoration must agree on declarations and definitions under MSVC.
#if defined(_WIN32) && defined(AGENT_RUNTIME_EXPORTS)
#define AGENT_API __declspec(dllexport)
#else
#define AGENT_API
#endif
extern "C" {
// Возвращает 0 при успехе; иначе код ошибки (см. agent_last_error).
AGENT_API int32_t agent_init(const char* config_json);
AGENT_API void agent_shutdown();
// Выполнить фразу: возвращает JSON FastOutcome (память освобождать agent_free).
AGENT_API char* agent_execute(const char* phrase);
AGENT_API char* agent_preview(const char* phrase);
AGENT_API char* agent_metrics_json();
AGENT_API char* agent_state_json();
AGENT_API char* agent_tools_json();
// Исполнить один инструмент: args — JSON-объект, ответ — JSON-наблюдение.
AGENT_API char* agent_run_tool(const char* tool, const char* args_json);
// Пройти задачу целиком: быстрые команды ядром, сложные — агентным циклом.
AGENT_API char* agent_run_task(const char* task, int max_steps);
AGENT_API const char* agent_last_error();
AGENT_API void agent_free(char* ptr);
// Подписка на события: callback вызывается из рабочих потоков (нужен C#-маршаллинг).
typedef void (*agent_event_fn)(const char* event_json, void* user);
AGENT_API void agent_set_event_sink(agent_event_fn fn, void* user);
// Ответ пользователя на «подтвердите опасное действие» (1 — да, 0 — нет).
AGENT_API void agent_answer_confirmation(int32_t approved);
// Сколько миллисекунд рантайм ждёт ответа (0 — не ждать и не выполнять).
AGENT_API void agent_set_confirm_timeout(int32_t timeout_ms);
AGENT_API void agent_cancel_current();
}
