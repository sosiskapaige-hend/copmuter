// Реестр инструментов + строковые имена перечислений + JSON-сериализация.
#include <algorithm>
#include <cstdio>

#include "agent/core.h"

namespace agent {
namespace {
std::string escape(std::string_view s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(c);
                }
        }
    }
    return out;
}
}  // namespace

std::string json_escape(std::string_view s) { return escape(s); }

const char* to_string(Method m) {
    switch (m) {
        case Method::WinApi: return "winapi";
        case Method::CachedExe: return "cached_exe";
        case Method::Cli: return "cli";
        case Method::Shell: return "shell";
        case Method::Shortcut: return "shortcut";
        case Method::Uia: return "uia";
        case Method::Input: return "input";
        case Method::Vision: return "vision";
        case Method::Llm: return "llm";
    }
    return "?";
}

const char* to_string(Risk r) {
    switch (r) {
        case Risk::None: return "none";
        case Risk::Low: return "low";
        case Risk::Medium: return "medium";
        case Risk::High: return "high";
        case Risk::Critical: return "critical";
    }
    return "?";
}

const char* to_string(RouteKind k) {
    switch (k) {
        case RouteKind::Direct: return "direct";
        case RouteKind::Chat: return "chat";
        case RouteKind::LlmText: return "llm_text";
        case RouteKind::Vision: return "vision";
        case RouteKind::Agent: return "agent";
    }
    return "?";
}

const char* to_string(VerifyKind v) {
    switch (v) {
        case VerifyKind::None: return "none";
        case VerifyKind::ProcessStarted: return "process_started";
        case VerifyKind::ProcessFinished: return "process_finished";
        case VerifyKind::WindowCreated: return "window_created";
        case VerifyKind::WindowActive: return "window_active";
        case VerifyKind::FileExists: return "file_exists";
        case VerifyKind::FileGone: return "file_gone";
        case VerifyKind::FileChanged: return "file_changed";
        case VerifyKind::UrlLoaded: return "url_loaded";
        case VerifyKind::PortOpen: return "port_open";
        case VerifyKind::ScreenChanged: return "screen_changed";
        case VerifyKind::ExitCodeZero: return "exit_code_zero";
    }
    return "?";
}

const char* to_string(ActionState s) {
    switch (s) {
        case ActionState::Queued: return "queued";
        case ActionState::Running: return "running";
        case ActionState::Success: return "success";
        case ActionState::Failed: return "failed";
        case ActionState::Timeout: return "timeout";
        case ActionState::Cancelled: return "cancelled";
        case ActionState::Skipped: return "skipped";
    }
    return "?";
}

// ---------------------------------------------------------------------------
//  ToolRegistry
// ---------------------------------------------------------------------------
void ToolRegistry::add(ToolSpec spec) {
    const std::string name = spec.name;
    for (const std::string& action : spec.intent_actions) {
        auto& v = by_action_[action];
        if (std::find(v.begin(), v.end(), name) == v.end()) v.push_back(name);
    }
    specs_[name] = std::move(spec);
}

const ToolSpec* ToolRegistry::find(std::string_view name) const {
    auto it = specs_.find(std::string(name));
    return it == specs_.end() ? nullptr : &it->second;
}

const std::vector<std::string>& ToolRegistry::intent_actions(std::string_view action) const {
    auto it = by_action_.find(std::string(action));
    return it == by_action_.end() ? empty_ : it->second;
}

const ToolSpec* ToolRegistry::for_action(std::string_view action, Method want) const {
    const std::vector<std::string>* names = nullptr;
    auto it = by_action_.find(std::string(action));
    if (it != by_action_.end()) names = &it->second;
    if (!names) return nullptr;
    const ToolSpec* best = nullptr;
    for (const std::string& n : *names) {
        const ToolSpec* t = find(n);
        if (!t) continue;
        if (t->preferred == want) return t;
        if (!best) best = t;
    }
    return best;
}

std::vector<const ToolSpec*> ToolRegistry::all() const {
    std::vector<const ToolSpec*> out;
    out.reserve(specs_.size());
    for (const auto& [name, spec] : specs_) out.push_back(&spec);
    std::sort(out.begin(), out.end(),
              [](const ToolSpec* a, const ToolSpec* b) { return a->name < b->name; });
    return out;
}

std::string ToolRegistry::names_json() const {
    std::string out = "[";
    bool first = true;
    for (const ToolSpec* t : all()) {
        if (!first) out += ", ";
        first = false;
        out += "\"";
        out += escape(t->name);
        out += "\"";
    }
    out += "]";
    return out;
}

std::string ToolRegistry::schemas_json(const std::vector<std::string>& names) const {
    std::string out = "[";
    bool first = true;
    for (const ToolSpec* t : all()) {
        if (!names.empty() && std::find(names.begin(), names.end(), t->name) == names.end())
            continue;
        if (!first) out += ",";
        first = false;
        out += "{\"name\":\"";
        out += escape(t->name);
        out += "\",\"description\":\"";
        out += escape(t->description);
        out += "\",\"category\":\"";
        out += escape(t->category);
        out += "\",\"risk\":\"";
        out += to_string(t->risk);
        out += "\",\"method\":\"";
        out += to_string(t->preferred);
        out += "\",\"timeout_ms\":";
        out += std::to_string(t->timeout_ms);
        out += ",\"verify\":\"";
        out += to_string(t->verify);
        out += "\",\"parameters\":";
        out += t->parameters_json.empty() ? "{\"type\":\"object\",\"properties\":{}}"
                                          : t->parameters_json;
        out += "}";
    }
    out += "]";
    return out;
}

// ---------------------------------------------------------------------------
//  JSON для результатов действий
// ---------------------------------------------------------------------------
std::string BatchResult::to_json() const {
    std::string out = "{\"ok\":";
    out += ok ? "true" : "false";
    out += ",\"ms\":" + std::to_string(int(ms));
    out += ",\"stopped_at\":" + std::to_string(stopped_at);
    out += ",\"error\":\"" + escape(error) + "\",\"actions\":[";
    bool first = true;
    for (const ActionSpec& a : actions) {
        if (!first) out += ",";
        first = false;
        out += "{\"id\":\"" + escape(a.id) + "\",\"tool\":\"" + escape(a.tool) +
               "\",\"title\":\"" + escape(a.title) + "\",\"status\":\"" + to_string(a.state) +
               "\",\"ok\":" + (a.ok ? "true" : "false") + ",\"ms\":" + std::to_string(int(a.ms)) +
               ",\"method\":\"" + to_string(a.method_used) + "\",\"output\":\"" +
               escape(a.output.substr(0, 500)) + "\",\"error\":\"" + escape(a.error.substr(0, 300)) +
               "\"}";
    }
    out += "]}";
    return out;
}

}  // namespace agent
