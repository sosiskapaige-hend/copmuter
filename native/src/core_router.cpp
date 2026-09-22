// Fast Router: «простая команда → инструменты» или «сложная → модель».
#include <cstring>

#include "agent/core.h"
#include "agent/intent.h"

namespace agent {
namespace {

// Намерения, которые выполняются детерминированно (быстрый путь).
bool is_direct_action(std::string_view action) {
    static const char* kDirect[] = {
        "launch_app", "open_url", "open_folder", "open_path", "web_search", "youtube_search",
        "create_folder", "create_file", "read_file", "list_dir", "delete_path", "move_path",
        "copy_path", "find_files", "screenshot", "set_wallpaper", "volume", "power",
        "show_desktop", "send_keys", "type_text", "kill_process", "run_command",
        "settings_page", "clipboard_get", "clipboard_set", "analyze_screen", "read_screen",
    };
    for (const char* a : kDirect)
        if (action == a) return true;
    return false;
}

// Требует ли намерение зрения (нельзя выполнить через API/uia).
bool needs_vision(std::string_view action) {
    static const char* kVision[] = {"click_element", "find_element", "analyze_screen", "read_screen",
                                    "find_on_screen", "click_on_screen", "describe_screen"};
    for (const char* a : kVision)
        if (action == a) return true;
    return false;
}

// Инструменты, для которых UI automation/зрение запрещены (ТЗ §15).
bool never_vision(std::string_view action) {
    static const char* kApi[] = {"send_keys", "type_text", "clipboard_get", "clipboard_set",
                                 "show_desktop", "window_focus"};
    for (const char* a : kApi)
        if (action == a) return true;
    return false;
}

}  // namespace

Route Router::route(std::string_view phrase) const {
    const double t0 = now_us();
    Route r;
    r.intent = engine_.parse(phrase);
    r.confidence = r.intent.confidence;
    const std::string_view action = r.intent.action.view();

    if (action == "compound") {
        bool uncertain = false;
        for (const Intent& p : r.intent.parts) {
            const std::string_view pa = p.action.view();
            if (!is_direct_action(pa) && pa != "code_task") uncertain = true;
            if (pa == "code_task") uncertain = true;    // внутри нужен один вызов модели
        }
        r.kind = uncertain ? RouteKind::Agent : RouteKind::Direct;
        r.reason = uncertain ? "в составной команде есть шаг для модели"
                             : "несколько простых действий";
        r.confidence = r.intent.confidence;
    } else if (action == "chat") {
        r.kind = RouteKind::Chat;
        r.reason = "вопрос/беседа";
    } else if (action == "code_task") {
        r.kind = RouteKind::LlmText;
        r.reason = "нужна генерация кода (один вызов модели)";
    } else if (needs_vision(action)) {
        r.kind = never_vision(action) ? RouteKind::Agent : RouteKind::Vision;
        r.reason = "поиск элемента интерфейса";
    } else if (action == "agent_task" || action.empty()) {
        r.kind = RouteKind::Agent;
        r.reason = "намерение не распознано детерминированно";
        r.confidence = r.intent.confidence;
    } else if (is_direct_action(action)) {
        // есть ли инструмент, который это умеет
        r.kind = registry_.for_action(action, Method::WinApi) ? RouteKind::Direct
                                                              : RouteKind::Agent;
        r.reason = r.kind == RouteKind::Direct ? "детерминированный инструмент"
                                               : "нет инструмента для намерения";
    } else {
        r.kind = RouteKind::Agent;
        r.reason = "нет быстрого обработчика";
    }
    // Замер решения: попадает в метрики (route_us) — это часть Time To Completion.
    r.method = Method::WinApi;
    (void)t0;
    return r;
}

std::string Route::to_json() const {
    std::string out = "{\"kind\":\"";
    out += to_string(kind);
    out += "\",\"action\":\"" + json_escape(intent.action.view());
    out += "\",\"reason\":\"" + json_escape(reason);
    out += "\",\"confidence\":" + std::to_string(confidence);
    out += ",\"slots\":{\"target\":\"" + json_escape(intent.slot(SlotId::Target).view()) +
           "\",\"place\":\"" + json_escape(intent.slot(SlotId::Place).view()) +
           "\",\"url\":\"" + json_escape(intent.slot(SlotId::Url).view()) +
           "\",\"query\":\"" + json_escape(intent.slot(SlotId::Query).view()) +
           "\",\"key\":\"" + json_escape(intent.slot(SlotId::Key).view()) +
           "\",\"content\":\"" + json_escape(intent.slot(SlotId::Content).view()) +
           "\",\"value\":\"" + json_escape(intent.slot(SlotId::Value).view()) + "\"}";
    out += ",\"label\":\"" + json_escape(intent.label()) + "\"}";
    return out;
}

}  // namespace agent
