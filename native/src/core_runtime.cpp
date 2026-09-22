// AgentRuntime: сборка быстрого пути в один объект.
#include <algorithm>
#include <cstring>
#include <sstream>

#include "agent/intent.h"
#include "agent/runtime.h"
#include "agent/util.h"

namespace agent {
namespace {

struct PlaceEnv {
    const char* label;
    const char* win_env;      // %VAR% на Windows
    const char* linux_xdg;    // xdg-user-dir / стандартная папка
};

// Места, которые понимает агент («с рабочего стола», «в загрузки»).
const PlaceEnv kPlaces[] = {
    {"рабочий стол", "USERPROFILE\\Desktop", "Desktop"},
    {"desktop", "USERPROFILE\\Desktop", "Desktop"},
    {"загрузки", "USERPROFILE\\Downloads", "Downloads"},
    {"downloads", "USERPROFILE\\Downloads", "Downloads"},
    {"документы", "USERPROFILE\\Documents", "Documents"},
    {"документ", "USERPROFILE\\Documents", "Documents"},
    {"картинки", "USERPROFILE\\Pictures", "Pictures"},
    {"изображения", "USERPROFILE\\Pictures", "Pictures"},
    {"музыка", "USERPROFILE\\Music", "Music"},
    {"видео", "USERPROFILE\\Videos", "Videos"},
    {"домашняя папка", "USERPROFILE", "HOME"},
    {"домашний каталог", "USERPROFILE", "HOME"},
    {"корзина", "USERPROFILE", "HOME"},
};

}  // namespace

// ---------------------------------------------------------------------------
//  Events / metrics
// ---------------------------------------------------------------------------
std::string Event::to_json() const {
    std::string out = "{\"kind\":\"" + json_escape(kind) + "\"";
    if (!tool.empty()) out += ",\"tool\":\"" + json_escape(tool) + "\"";
    if (!status.empty()) out += ",\"status\":\"" + json_escape(status) + "\"";
    if (!message.empty()) out += ",\"message\":\"" + json_escape(message) + "\"";
    if (!data_json.empty()) out += ",\"data\":" + data_json;
    out += ",\"ms\":" + std::to_string(int(ms));
    out += ",\"ts_ms\":" + std::to_string(ts_ms == 0 ? now_ms() : ts_ms);
    out += "}";
    return out;
}

std::string MetricsSnapshot::to_json() const {
    std::ostringstream oss;
    oss << "{\"tasks\":" << tasks << ",\"tasks_ok\":" << tasks_ok
        << ",\"fast_tasks\":" << fast_tasks << ",\"agent_tasks\":" << agent_tasks
        << ",\"llm_calls\":" << llm_calls << ",\"tool_calls\":" << tool_calls
        << ",\"vision_calls\":" << vision_calls << ",\"fallbacks\":" << fallbacks
        << ",\"ttc_avg_ms\":" << ttc_avg_ms() << ",\"route_avg_us\":" << route_avg_us() << "}";
    return oss.str();
}

std::string FastOutcome::to_json() const {
    std::string out = "{\"handled\":" + std::string(handled ? "true" : "false");
    out += ",\"ok\":" + std::string(ok ? "true" : "false");
    out += ",\"route\":\"" + std::string(to_string(route)) + "\"";
    out += ",\"action\":\"" + json_escape(action) + "\"";
    out += ",\"message\":\"" + json_escape(message) + "\"";
    out += ",\"error\":\"" + json_escape(error) + "\"";
    out += ",\"ms\":" + std::to_string(int(total_ms));
    out += ",\"route_us\":" + std::to_string(int(route_us));
    out += ",\"actions\":" + std::to_string(actions);
    out += ",\"needs_llm\":" + std::string(needs_llm ? "true" : "false");
    if (!llm_prompt.empty()) out += ",\"llm_prompt\":\"" + json_escape(llm_prompt) + "\"";
    if (!data_json.empty()) out += ",\"data\":" + data_json;
    out += "}";
    return out;
}

// ---------------------------------------------------------------------------
//  Жизненный цикл
// ---------------------------------------------------------------------------
AgentRuntime::AgentRuntime(RuntimeConfig cfg, std::unique_ptr<IPlatform> platform)
    : cfg_(std::move(cfg)), platform_(std::move(platform)), intents_(&apps_),
      queue_(3) {
    wait_ = std::make_unique<WaitManager>(platform_.get(), cfg_.process_timeout_ms);
    router_ = new Router(intents_, tools_);
    intents_.set_places(&places_);
}

AgentRuntime::~AgentRuntime() {
    delete router_;
}

void AgentRuntime::set_event_sink(EventFn fn) {
    std::lock_guard<std::mutex> lock(mu_);
    sink_ = std::move(fn);
}

void AgentRuntime::set_cancel(std::function<bool()> fn) {
    // ВАЖНО: копия, а не перемещение — иначе WaitManager получил бы уже пустую
    // функцию и «Стоп» не прерывал бы ожидания.
    cancel_ = fn;
    if (wait_) wait_->set_cancel(fn);
}

void AgentRuntime::emit(Event ev) {
    if (ev.ts_ms == 0) ev.ts_ms = now_ms();
    EventFn sink;
    {
        std::lock_guard<std::mutex> lock(mu_);
        sink = sink_;
    }
    if (sink) {
        try {
            sink(ev);
        } catch (...) {
            // UI не должен ронять исполнителя
        }
    }
}

void AgentRuntime::register_builtin_tools() {
    auto add = [&](const char* name, const char* desc, const char* category, Risk risk,
                   Method method, int timeout_ms, VerifyKind verify,
                   std::initializer_list<const char*> actions,
                   std::initializer_list<Method> fallbacks = {}, bool gui = false,
                   const char* params = "{\"type\":\"object\",\"properties\":{}}") {
        ToolSpec t;
        t.name = name;
        t.description = desc;
        t.category = category;
        t.risk = risk;
        t.preferred = method;
        t.timeout_ms = timeout_ms;
        t.verify = verify;
        t.gui = gui;
        t.parameters_json = params;
        t.fallbacks.assign(fallbacks.begin(), fallbacks.end());
        for (const char* a : actions) t.intent_actions.push_back(a);
        tools_.add(std::move(t));
    };

    add("launch_application", "Запустить приложение по имени (реестр приложений, PATH, оболочка)",
        "apps", Risk::Low, Method::CachedExe, cfg_.launch_timeout_ms, VerifyKind::ProcessStarted,
        {"launch_app"}, {Method::Shell, Method::Cli, Method::Uia},
        false, "{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"}},\"required\":[\"name\"]}");
    add("browser_task", "Сложная страница: клик/ввод/данные/скачивание через Playwright (Chromium)",
        "browser", Risk::Medium, Method::Uia, cfg_.browser_timeout_ms, VerifyKind::None,
        {"browser_task"}, {Method::Cli}, false,
        "{\"type\":\"object\",\"properties\":{\"action\":{\"type\":\"string\"},"
        "\"url\":{\"type\":\"string\"},\"selector\":{\"type\":\"string\"}},"
        "\"required\":[\"action\"]}");
    add("focus_window", "Переключиться на окно приложения (по заголовку/процессу; при отсутствии — запуск)",
        "windows", Risk::Low, Method::WinApi, cfg_.window_timeout_ms, VerifyKind::WindowActive,
        {"focus_window"}, {Method::Shell, Method::Uia});
    add("open_url", "Открыть ссылку в браузере по умолчанию", "browser", Risk::Low, Method::WinApi,
        cfg_.browser_timeout_ms, VerifyKind::None, {"open_url"},
        {Method::Shell}, false,
        "{\"type\":\"object\",\"properties\":{\"url\":{\"type\":\"string\"}},\"required\":[\"url\"]}");
    add("search_web", "Поиск в интернете прямой ссылкой (без набора в адресной строке)", "browser",
        Risk::None, Method::WinApi, cfg_.browser_timeout_ms, VerifyKind::None, {"web_search"},
        {Method::Shell});
    add("search_youtube", "Поиск видео на YouTube прямой ссылкой results?search_query=", "browser",
        Risk::None, Method::WinApi, cfg_.browser_timeout_ms, VerifyKind::None, {"youtube_search"},
        {Method::Shell});
    add("open_folder", "Открыть папку в проводнике", "fs", Risk::Low, Method::WinApi,
        cfg_.launch_timeout_ms, VerifyKind::FileExists, {"open_folder", "open_path"});
    add("create_folder", "Создать папку (вложенные — по необходимости)", "fs", Risk::Low,
        Method::WinApi, cfg_.file_timeout_ms, VerifyKind::FileExists, {"create_folder"});
    add("write_file", "Создать/перезаписать текстовый файл", "fs", Risk::Low, Method::WinApi,
        cfg_.file_timeout_ms, VerifyKind::FileExists, {"create_file"});
    add("read_file", "Прочитать текстовый файл", "fs", Risk::None, Method::WinApi,
        cfg_.file_timeout_ms, VerifyKind::None, {"read_file"});
    add("list_dir", "Показать содержимое папки", "fs", Risk::None, Method::WinApi,
        cfg_.file_timeout_ms, VerifyKind::None, {"list_dir"});
    add("search_files", "Найти файлы по имени/маске", "fs", Risk::None, Method::WinApi,
        cfg_.file_timeout_ms, VerifyKind::None, {"find_files"});
    add("move_file", "Переместить/переименовать файл или папку", "fs", Risk::Medium, Method::WinApi,
        cfg_.file_timeout_ms, VerifyKind::FileExists, {"move_path"});
    add("copy_file", "Скопировать файл или папку", "fs", Risk::Low, Method::WinApi,
        cfg_.file_timeout_ms, VerifyKind::FileExists, {"copy_path"});
    add("delete_path", "Удалить файл или папку (в корзину, если возможно)", "fs", Risk::Medium,
        Method::WinApi, cfg_.file_timeout_ms, VerifyKind::FileGone, {"delete_path"},
        {Method::Shell});
    add("screenshot", "Снимок экрана (DXGI Desktop Duplication, без лишних копий)", "screen",
        Risk::None, Method::WinApi, cfg_.vision_timeout_ms, VerifyKind::None, {"screenshot"});
    add("find_element", "Найти элемент интерфейса на экране (UI Automation, затем модель-зрение)",
        "vision", Risk::None, Method::Uia, cfg_.vision_timeout_ms, VerifyKind::ScreenChanged,
        {"click_element", "find_element"}, {Method::Vision}, true);
    add("send_keys", "Нажать клавишу или сочетание (SendInput)", "input", Risk::Low, Method::WinApi,
        3000, VerifyKind::None, {"send_keys"}, {}, true);
    add("type_text", "Ввести текст (длинный — через буфер обмена + Ctrl+V)", "input", Risk::Low,
        Method::WinApi, 5000, VerifyKind::None, {"type_text"}, {Method::Uia}, true);
    add("set_wallpaper", "Сменить обои рабочего стола (SystemParametersInfo)", "system",
        Risk::Low, Method::WinApi, 8000, VerifyKind::None, {"set_wallpaper"});
    add("set_volume", "Громкость: получить/установить/mute", "system", Risk::Low, Method::WinApi,
        5000, VerifyKind::None, {"volume"});
    add("system_power", "Питание: выключение, перезагрузка, сон, блокировка (требует подтверждения)",
        "system", Risk::Critical, Method::WinApi, 15000, VerifyKind::None, {"power"});
    add("show_desktop", "Показать рабочий стол (Win+D)", "system", Risk::None, Method::WinApi,
        3000, VerifyKind::None, {"show_desktop"}, {}, true);
    add("run_command", "Выполнить команду в оболочке (PowerShell/CMD/bash) и вернуть вывод",
        "system", Risk::High, Method::WinApi, cfg_.shell_timeout_ms, VerifyKind::ExitCodeZero,
        {"run_command"}, {Method::Shell});
    add("kill_process", "Завершить процесс по имени или по приложению из реестра", "system",
        Risk::Medium, Method::WinApi, cfg_.process_timeout_ms, VerifyKind::ProcessFinished,
        {"kill_process"}, {Method::Cli});
    add("open_settings", "Открыть страницу настроек ОС (ms-settings:...)", "system", Risk::Low,
        Method::WinApi, cfg_.launch_timeout_ms, VerifyKind::WindowCreated, {"settings_page"},
        {Method::Shell});
    add("clipboard", "Прочитать/записать буфер обмена", "input", Risk::Low, Method::WinApi, 3000,
        VerifyKind::None, {"clipboard_get", "clipboard_set"});
    add("computer_state", "Состояние ПК: активное окно, процессы, курсор, мониторы, буфер обмена",
        "system", Risk::None, Method::WinApi, 2000, VerifyKind::None, {"computer_state"});
    add("wait_for", "Ждать состояние, а не время: процесс, окно, файл, порт", "system", Risk::None,
        Method::WinApi, cfg_.process_timeout_ms, VerifyKind::None, {"wait_for"},
        {}, false,
        "{\"type\":\"object\",\"properties\":{\"kind\":{\"type\":\"string\"},"
        "\"target\":{\"type\":\"string\"}},\"required\":[\"kind\",\"target\"]}");
    add("execute_powershell", "Выполнить скрипт PowerShell и вернуть вывод", "system", Risk::High,
        Method::WinApi, cfg_.shell_timeout_ms, VerifyKind::ExitCodeZero, {"execute_powershell"});
    add("click", "Клик мышью по координатам экрана (SendInput)", "input", Risk::Low, Method::WinApi,
        3000, VerifyKind::None, {"click"}, {}, true,
        "{\"type\":\"object\",\"properties\":{\"x\":{\"type\":\"integer\"},"
        "\"y\":{\"type\":\"integer\"}},\"required\":[\"x\",\"y\"]}");
    add("mouse_move", "Переместить курсор мыши", "input", Risk::None, Method::WinApi, 2000,
        VerifyKind::None, {"mouse_move"});
}

bool AgentRuntime::start(std::string& error) {
    if (!platform_) {
        error = "платформа не создана";
        return false;
    }
    register_builtin_tools();

    // Каталог известных приложений — всегда: это таблица в памяти, микросекунды,
    // зато «открой телегу» работает даже без прогрева.
    discover_apps_into(apps_);

    // Места (рабочий стол/загрузки/...) — из окружения, без опроса диска.
    for (const PlaceEnv& p : kPlaces) {
        std::string path;
        if (platform_->name() == std::string_view("windows")) {
            const std::string profile = platform_->env("USERPROFILE");
            std::string sub(p.win_env);
            const size_t slash = sub.find('\\');
            if (slash != std::string::npos) sub = sub.substr(slash + 1);
            if (!profile.empty()) path = join_path(profile, sub);
        } else {
            const std::string home = platform_->env("HOME");
            if (!home.empty()) {
                const char* sub = p.linux_xdg;
                if (std::strcmp(sub, "HOME") == 0) {
                    path = home;
                } else {
                    path = join_path(home, sub);
                }
            }
        }
        if (!path.empty()) places_.emplace_back(p.label, path);
    }

    if (cfg_.preload) {
        // Реальное обнаружение установленных приложений делает платформа.
        const int found = platform_->discover_apps(apps_);
        // Прогрев снимка экрана: первый кадр самый дорогой (инициализация DXGI).
        if (platform_->has_display()) {
            Frame warm = platform_->capture(0, nullptr);
            emit(Event{"log", "", "info",
                       "Прогрев: приложений " + std::to_string(found) + ", экран " +
                           std::to_string(warm.width) + "x" + std::to_string(warm.height) +
                           " (" + warm.backend + ")",
                       "", 0.0, now_ms()});
        } else {
            emit(Event{"log", "", "warn",
                       "Дисплей не обнаружен: инструменты экрана/ввода работают в режиме журнала",
                       "", 0.0, now_ms()});
        }
    }
    running_.store(true);
    return true;
}

void AgentRuntime::stop() { running_.store(false); }

// ---------------------------------------------------------------------------
//  Метрики
// ---------------------------------------------------------------------------
MetricsSnapshot AgentRuntime::metrics() const {
    MetricsSnapshot m;
    m.tasks = c_.tasks.load();
    m.tasks_ok = c_.tasks_ok.load();
    m.fast_tasks = c_.fast_tasks.load();
    m.agent_tasks = c_.agent_tasks.load();
    m.llm_calls = c_.llm_calls.load();
    m.tool_calls = c_.tool_calls.load();
    m.vision_calls = c_.vision_calls.load();
    m.fallbacks = c_.fallbacks.load();
    m.total_ms = double(c_.total_ms.load());
    m.route_us_total = double(c_.route_us.load());
    return m;
}

std::string AgentRuntime::metrics_json() const {
    return metrics().to_json();
}

void AgentRuntime::count_task(bool ok, bool fast, double ms, double route_us) {
    c_.tasks.fetch_add(1);
    if (ok) c_.tasks_ok.fetch_add(1);
    if (fast) c_.fast_tasks.fetch_add(1);
    else c_.agent_tasks.fetch_add(1);
    c_.total_ms.fetch_add(uint64_t(ms > 0 ? ms : 0));
    c_.route_us.fetch_add(uint64_t(route_us > 0 ? route_us : 0));
}

void AgentRuntime::note_tool_call(std::string_view tool, bool ok, double ms) {
    c_.tool_calls.fetch_add(1);
    if (!ok) c_.fallbacks.fetch_add(1);
    (void)tool;
    (void)ms;
}

void AgentRuntime::note_result(std::string_view action, bool ok, double ms, Method m) {
    optimizer_.note(action, m, ok, ms);
    c_.tool_calls.fetch_add(1);
    if (!ok) c_.fallbacks.fetch_add(1);
}

// ---------------------------------------------------------------------------
//  Места и пути
// ---------------------------------------------------------------------------
std::string AgentRuntime::resolve_place(std::string_view place) const {
    const std::string p = normalize(place);
    if (p.empty()) return {};
    for (const auto& [label, path] : places_) {
        if (p == label || starts_with_word(p, label)) return path;
    }
    return {};
}

std::string AgentRuntime::resolve_target_path(std::string_view target, std::string_view place,
                                              bool must_exist, bool& found) const {
    found = true;
    std::string t(target);
    while (!t.empty() && t.back() == ' ') t.pop_back();
    while (!t.empty() && t.front() == ' ') t.erase(0, 1);
    if (t.empty()) {
        found = false;
        return {};
    }
    std::string path;
    const bool absolute = (t.size() > 1 && (t[0] == '/' || t[1] == ':')) || t.rfind("~/", 0) == 0;
    if (absolute) {
        path = t;
    } else {
        std::string base = resolve_place(place);
        if (base.empty()) {
            base = platform_->cwd();
        }
        path = join_path(base, t);
    }
    if (!must_exist) return path;
    if (platform_->file_exists(path)) return path;
    // Нечёткий поиск в папке (например, «папка отчёт» → «Отчёт за август»).
    std::string dir = path;
    const size_t slash = dir.find_last_of("/\\");
    std::string name = slash == std::string::npos ? dir : dir.substr(slash + 1);
    std::string parent = slash == std::string::npos ? platform_->cwd() : dir.substr(0, slash);
    double best = 0.0;
    std::string best_path;
    for (const FileEntry& e : platform_->list_dir(parent)) {
        const double s = similarity(normalize(name), normalize(e.name));
        if (s > best) {
            best = s;
            best_path = e.path;
        }
    }
    if (best >= 0.72) {
        found = true;
        return best_path;
    }
    found = false;
    return path;
}

// ---------------------------------------------------------------------------
//  Проверка и подтверждения
// ---------------------------------------------------------------------------
bool AgentRuntime::verify_action(const ActionSpec& a, std::string& detail) {
    if (!wait_) return true;
    // a.timeout_ms позволяет ограничить ожидание на одну попытку: при переборе
    // способов запуска нельзя тратить полный таймаут на каждый неудачный вариант.
    const int launch_budget = a.timeout_ms > 0 ? a.timeout_ms : cfg_.launch_timeout_ms;
    const int window_budget = a.timeout_ms > 0 ? a.timeout_ms : cfg_.window_timeout_ms;
    const int file_budget = a.timeout_ms > 0 ? a.timeout_ms : cfg_.file_timeout_ms;
    switch (a.verify) {
        case VerifyKind::None:
            return true;
        case VerifyKind::FileExists: {
            const std::string path = a.verify_path.empty() ? a.args_json : a.verify_path;
            WaitResult r = wait_->wait_file_exists(path, file_budget);
            detail = r.ok ? r.detail : "файл не появился";
            return r.ok;
        }
        case VerifyKind::FileGone: {
            WaitResult r = wait_->wait_file_gone(a.verify_path, file_budget);
            detail = r.ok ? r.detail : "объект на месте";
            return r.ok;
        }
        case VerifyKind::ProcessStarted: {
            WaitResult r = wait_->wait_process_started(a.verify_path, launch_budget);
            detail = r.ok ? r.detail : "процесс не запустился";
            return r.ok;
        }
        case VerifyKind::ProcessFinished: {
            WaitResult r = wait_->wait_process_finished(a.verify_path, cfg_.process_timeout_ms);
            detail = r.ok ? r.detail : "процесс ещё работает";
            return r.ok;
        }
        case VerifyKind::WindowCreated:
        case VerifyKind::WindowActive: {
            WaitResult r = a.verify == VerifyKind::WindowActive
                               ? wait_->wait_window_active(a.verify_path, window_budget)
                               : wait_->wait_window_created(a.verify_path, window_budget);
            detail = r.ok ? r.detail : "окно не найдено";
            return r.ok;
        }
        case VerifyKind::ExitCodeZero:
        case VerifyKind::UrlLoaded:
        case VerifyKind::FileChanged:
        case VerifyKind::PortOpen:
        case VerifyKind::ScreenChanged:
            // Проверки, которые исполняются в конкретном обработчике (там есть данные).
            return true;
    }
    return true;
}

void AgentRuntime::set_confirm_handler(ConfirmFn fn) { confirm_fn_ = std::move(fn); }

void AgentRuntime::answer_confirmation(bool approved) {
    {
        std::lock_guard<std::mutex> lock(confirm_mu_);
        if (!confirm_pending_) return;
        confirm_answer_ = approved;
        confirm_pending_ = false;
    }
    confirm_cv_.notify_all();
}

bool AgentRuntime::confirmation_pending() const {
    std::lock_guard<std::mutex> lock(confirm_mu_);
    return confirm_pending_;
}

// Подтверждение опасного действия. Три случая:
//   * UI подставил обработчик — решает он (диалог в оболочке);
//   * обработчика нет, но задан confirm_timeout_ms — ждём ответа через
//     answer_confirmation() (оболочка показывает диалог по событию «confirm»);
//   * ждать не настроено — опасное действие НЕ выполняется.
// Молчание всегда трактуется как отказ: это единственное безопасное поведение.
bool AgentRuntime::ask_user(std::string_view question, std::string& reason) {
    const std::string text(question);
    emit(Event{"confirm", "", "pending", text, "", 0.0, now_ms()});
    if (confirm_fn_) {
        if (confirm_fn_(text, cfg_.confirm_timeout_ms)) return true;
        reason = "пользователь отказался";
        emit(Event{"confirm", "", "denied", "Действие отменено пользователем", "", 0.0, now_ms()});
        return false;
    }
    if (cfg_.confirm_timeout_ms <= 0) {
        reason = "требуется подтверждение пользователя";
        return false;
    }
    std::unique_lock<std::mutex> lock(confirm_mu_);
    confirm_pending_ = true;
    confirm_answer_ = false;
    const bool answered = confirm_cv_.wait_for(lock, std::chrono::milliseconds(cfg_.confirm_timeout_ms),
                                               [this] { return !confirm_pending_; });
    const bool approved = answered && confirm_answer_;
    confirm_pending_ = false;
    if (!approved) {
        reason = answered ? "пользователь отказался" : "ответа на подтверждение не было";
        emit(Event{"confirm", "", "denied", reason, "", 0.0, now_ms()});
        return false;
    }
    emit(Event{"confirm", "", "approved", "Подтверждено пользователем", "", 0.0, now_ms()});
    return true;
}

bool AgentRuntime::needs_confirmation(const ToolSpec& tool, const Intent& it,
                                      std::string& reason) const {
    const std::string& mode = cfg_.safety_mode;
    if (mode == "full") return false;
    if (mode == "step") {
        reason = "пошаговый режим: подтверждается каждое действие";
        return true;
    }
    if (mode == "observe") {
        reason = "режим наблюдения: действия не выполняются";
        return true;
    }
    if (mode == "confirm") {
        if (tool.risk >= Risk::Medium) {
            reason = "режим подтверждения: риск " + std::string(to_string(tool.risk));
            return true;
        }
        return false;
    }
    // auto: только критичное и массовое
    if (tool.risk >= Risk::Critical) {
        reason = "критичное действие";
        return true;
    }
    if (it.action.view() == "delete_path") {
        const std::string path = it.slot(SlotId::Target).str();
        if (path.find('*') != std::string::npos || path.find('?') != std::string::npos) {
            reason = "массовое удаление";
            return true;
        }
    }
    return false;
}

}  // namespace agent
