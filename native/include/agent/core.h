// AgentRuntime — portable core of the native execution layer.
//
// Здесь живёт «рефлекторная» часть агента: Fast Router, Intent Engine,
// Tool Registry, Execution Optimizer, Batch/Queue, Verification и Recovery.
// Всё это не зависит от ОС и не обращается к модели: конкретные системные
// вызовы (Win32, DXGI, SendInput, Named Pipes) скрыты за интерфейсом IPlatform.
//
// Требования к производительности, из которых выведены решения этого файла:
//   * никаких аллокаций в горячем пути разбора фразы (SSO-строки + арена);
//   * никаких regex (std::regex медленный) — таблицы глаголов/алиасов + хеши;
//   * результат «фраза → маршрут» должен укладываться в единицы микросекунд,
//     чтобы простые команды выполнялись заметно быстрее одного кадра экрана.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace agent {

// Экранирование строк для JSON (используется в отчётах и IPC).
std::string json_escape(std::string_view s);

// ---------------------------------------------------------------------------
//  Time
// ---------------------------------------------------------------------------
using Clock = std::chrono::steady_clock;
inline int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               Clock::now().time_since_epoch())
        .count();
}
// Время суток в миллисекундах: журнал отладки читают глазами, а не только скриптом.
std::string wall_clock_text();

inline double now_us() {
    return std::chrono::duration_cast<std::chrono::duration<double, std::micro>>(
               Clock::now().time_since_epoch())
        .count();
}

// ---------------------------------------------------------------------------
//  Small string: 32 байта инлайн (имена приложений, пути, ключи, тексты кода —
//  всё, что встречается в горячем пути — не трогает кучу).
// ---------------------------------------------------------------------------
class smstr {
public:
    static constexpr size_t kInline = 32;
    smstr() = default;
    smstr(std::string_view v) { assign(v); }
    smstr(const char* v) { assign(v ? std::string_view(v) : std::string_view()); }

    smstr& operator=(std::string_view v) {
        assign(v);
        return *this;
    }
    smstr& operator=(const char* v) {
        assign(v ? std::string_view(v) : std::string_view());
        return *this;
    }
    void assign(std::string_view v) {
        if (v.size() <= kInline) {
            heap_.clear();
            size_ = static_cast<uint8_t>(v.size());
            std::memcpy(inline_, v.data(), v.size());
        } else {
            heap_.assign(v.data(), v.size());
            size_ = kHeap;
        }
    }
    std::string_view view() const {
        return size_ == kHeap ? std::string_view(heap_) : std::string_view(inline_, size_);
    }
    const std::string& str() const {
        if (size_ != kHeap) {
            tmp_.assign(inline_, size_);
            return tmp_;
        }
        return heap_;
    }
    size_t size() const { return size_ == kHeap ? heap_.size() : size_; }
    bool empty() const { return size() == 0; }
    void clear() {
        size_ = 0;
        heap_.clear();
    }
    friend bool operator==(const smstr& a, std::string_view b) { return a.view() == b; }

private:
    static constexpr uint8_t kHeap = 255;
    char inline_[kInline]{};
    uint8_t size_ = 0;
    std::string heap_;
    mutable std::string tmp_;
};

// ---------------------------------------------------------------------------
//  Risk / Method / Route
// ---------------------------------------------------------------------------
enum class Risk : uint8_t { None = 0, Low, Medium, High, Critical };

// Способы выполнения в порядке лестницы Execution Optimizer.
enum class Method : uint8_t {
    WinApi = 0,      // нативный системный вызов
    CachedExe,       // сохранённый/подтверждённый путь к программе
    Cli,             // командная строка инструмента (code.exe, git.exe ...)
    Shell,           // оболочка ОС / протокол / ярлык
    Shortcut,        // .lnk, .desktop
    Uia,             // UI Automation / accessibility
    Input,           // SendInput по координатам
    Vision,          // модель-зрение
    Llm,             // полноценный агентный цикл
};

const char* to_string(Method m);
const char* to_string(Risk r);

// Куда идёт команда.
enum class RouteKind : uint8_t {
    Direct = 0,   // детерминированные инструменты, модель не нужна
    Chat,         // вопрос/беседа — быстрый ответ модели
    LlmText,      // один вызов модели за текстом/кодом
    Vision,       // нужен снимок экрана и модель-зрение
    Agent,        // сложная задача: планирование + агентный цикл
};
const char* to_string(RouteKind k);

// Как проверять результат действия.
enum class VerifyKind : uint8_t {
    None = 0,
    ProcessStarted,
    ProcessFinished,
    WindowCreated,
    WindowActive,
    FileExists,
    FileGone,
    FileChanged,
    UrlLoaded,
    PortOpen,
    ScreenChanged,
    ExitCodeZero,
};
const char* to_string(VerifyKind v);

// ---------------------------------------------------------------------------
//  Слоты намерения
// ---------------------------------------------------------------------------
enum class SlotId : uint8_t {
    None = 0,
    Target,    // «телега», «папка 123», «Discord»
    Place,     // «рабочий стол», «загрузки»
    Args,      // аргументы запуска / страница настроек
    Url,
    Query,
    Key,       // клавиша/хоткей
    Content,   // текст/код
    Value,     // число (громкость, яркость)
    Count,
};

struct Intent {
    smstr action;                       // каноническое действие: launch_app, open_url, ...
    smstr raw;                          // исходная фраза (для логов и обучения)
    std::array<smstr, static_cast<size_t>(SlotId::Count)> slots;
    std::vector<Intent> parts;          // составная команда
    float confidence = 0.0f;
    uint8_t source = 0;                 // 0 rule, 1 registry, 2 memory, 3 fallback
    int32_t app_index = -1;             // индекс приложения в реестре приложений (-1 нет)

    const smstr& slot(SlotId id) const {
        return slots[static_cast<size_t>(id)];
    }
    void set(SlotId id, std::string_view v) { slots[static_cast<size_t>(id)] = v; }
    bool is_compound() const { return !parts.empty(); }
    std::string label() const;          // человекочитаемая подпись для UI
};

// ---------------------------------------------------------------------------
//  Реестр инструментов
// ---------------------------------------------------------------------------
struct ToolSpec {
    std::string name;                  // launch_application, open_url, fs_mkdir ...
    std::string description;
    std::string category;              // apps | browser | fs | input | system | vision | code
    std::string parameters_json;       // JSON-схема аргументов (отдаётся модели/Python)
    Risk risk = Risk::Low;
    Method preferred = Method::WinApi;
    int timeout_ms = 5000;
    VerifyKind verify = VerifyKind::None;
    std::vector<Method> fallbacks;
    bool gui = false;                  // требует дисплей/ввод → сериализуется
    bool needs_llm = false;            // сам инструмент обращается к модели
    std::vector<std::string> intent_actions;  // какие намерения умеет обслуживать
};

class ToolRegistry {
public:
    void add(ToolSpec spec);
    const ToolSpec* find(std::string_view name) const;
    // Первый инструмент, обслуживающий намерение (с учётом предпочтительного способа).
    const ToolSpec* for_action(std::string_view action, Method want) const;
    const std::vector<std::string>& intent_actions(std::string_view action) const;
    std::vector<const ToolSpec*> all() const;
    std::string names_json() const;
    std::string schemas_json(const std::vector<std::string>& names) const;
    size_t size() const { return specs_.size(); }

private:
    std::unordered_map<std::string, ToolSpec> specs_;
    std::unordered_map<std::string, std::vector<std::string>> by_action_;
    mutable std::vector<std::string> empty_;
};

// ---------------------------------------------------------------------------
//  Execution Optimizer: лестница способов + обучение на статистике
// ---------------------------------------------------------------------------
struct MethodStat {
    uint32_t attempts = 0;
    uint32_t ok = 0;
    uint32_t fallbacks = 0;
    double total_ms = 0.0;
    double ema_ms = 0.0;      // экспоненциальное среднее времени
    int64_t last_ok_ms = 0;
    double score() const;     // больше — лучше
    double success_rate() const { return attempts ? double(ok) / attempts : 0.0; }
    double avg_ms() const { return attempts ? total_ms / attempts : 0.0; }
};

class Optimizer {
public:
    explicit Optimizer(size_t ladder_hint = 64);
    // Упорядочить способы для конкретной возможности ("launch_app", "open_url", ...).
    std::vector<Method> order(std::string_view capability, std::span<const Method> available) const;
    Method best(std::string_view capability, std::span<const Method> available) const;
    void note(std::string_view capability, Method m, bool ok, double ms);
    const MethodStat* stat(std::string_view capability, Method m) const;
    std::string benchmark_json(std::string_view capability) const;
    std::string stats_json() const;
    void load_json(std::string_view json);
    std::string save_json() const;

private:
    struct Key {
        std::string cap;
        Method m;
        bool operator==(const Key& o) const { return m == o.m && cap == o.cap; }
    };
    struct KeyHash {
        size_t operator()(const Key& k) const {
            return std::hash<std::string>{}(k.cap) * 131 + static_cast<size_t>(k.m);
        }
    };
    mutable std::unordered_map<Key, MethodStat, KeyHash> stats_;
};

// ---------------------------------------------------------------------------
//  Действия и пакетное выполнение
// ---------------------------------------------------------------------------
enum class ActionState : uint8_t { Queued = 0, Running, Success, Failed, Timeout, Cancelled, Skipped };
const char* to_string(ActionState s);

struct ActionSpec {
    std::string id;
    std::string tool;                  // имя инструмента
    std::string args_json;             // аргументы (JSON)
    std::string title;                 // человекочитаемо («Открываю Telegram»)
    Risk risk = Risk::Low;
    int timeout_ms = 30000;
    VerifyKind verify = VerifyKind::None;
    std::string verify_path;           // файл/URL/порт для проверки
    std::vector<std::pair<std::string, std::string>> fallbacks;  // (tool, args_json)
    std::vector<int> depends_on;       // индексы в списке
    bool parallel = true;
    int retries = 1;
    bool soft = false;                 // ошибка не срывает пакет
    // runtime
    ActionState state = ActionState::Queued;
    bool ok = false;
    double ms = 0.0;
    std::string output;
    std::string error;
    Method method_used = Method::WinApi;
};

struct BatchResult {
    bool ok = false;
    std::vector<ActionSpec> actions;
    double ms = 0.0;
    int stopped_at = -1;
    std::string error;
    std::string to_json() const;
};

class ActionQueue {
public:
    using CallFn = std::function<void(ActionSpec&)>;   // вызов инструмента
    using VerifyFn = std::function<bool(const ActionSpec&, std::string& detail)>;
    explicit ActionQueue(int max_parallel = 3) : max_parallel_(max_parallel) {}
    void add(ActionSpec a) { actions_.push_back(std::move(a)); }
    void clear() { actions_.clear(); }
    const std::vector<ActionSpec>& actions() const { return actions_; }
    BatchResult run(const CallFn& call, const VerifyFn& verify, bool stop_on_error = true);
    void cancel();

private:
    std::vector<std::vector<int>> groups() const;
    std::atomic<bool> cancelled_{false};
    std::vector<ActionSpec> actions_;
    int max_parallel_ = 3;
};

// ---------------------------------------------------------------------------
//  Fast Router
// ---------------------------------------------------------------------------
struct Route {
    RouteKind kind = RouteKind::Agent;
    Intent intent;
    Method method = Method::WinApi;
    const char* reason = "";
    float confidence = 0.0f;
    std::string to_json() const;
};

class IntentEngine;
class ToolRegistry;

class Router {
public:
    Router(const IntentEngine& engine, const ToolRegistry& registry) : engine_(engine), registry_(registry) {}
    Route route(std::string_view phrase) const;

private:
    const IntentEngine& engine_;
    const ToolRegistry& registry_;
};

}  // namespace agent
