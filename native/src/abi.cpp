// C ABI: то, что видит C#-оболочка через P/Invoke (AgentRuntime.dll).
//
// Здесь нет логики — только маршалинг: JSON-конфиг → RuntimeConfig,
// глобальный рантайм, строки, которые C# обязан освободить через agent_free.
#include <atomic>
#include <cstring>
#include <mutex>
#include <string>

#include "agent/platform.h"
#include "agent/runtime.h"

#ifdef _WIN32
#define AGENT_EXPORT extern "C" __declspec(dllexport)
#else
#define AGENT_EXPORT extern "C" __attribute__((visibility("default")))
#endif

namespace {

std::mutex g_mu;
agent::AgentRuntime* g_runtime = nullptr;
std::atomic<bool> g_cancel{false};   // «Стоп» из UI: виден исполнителю между шагами
std::string g_last_error;
void (*g_sink)(const char*, void*) = nullptr;
void* g_sink_user = nullptr;

// Плоский JSON-конфиг: {"state_dir":"…","safety_mode":"auto","preload":true,…}.
// Полноценный парсер здесь не нужен — ключи известны и не вложены.
std::string json_string(const std::string& json, const std::string& key, const std::string& def = {}) {
    const std::string pattern = "\"" + key + "\"";
    size_t pos = json.find(pattern);
    if (pos == std::string::npos) return def;
    pos = json.find(':', pos + pattern.size());
    if (pos == std::string::npos) return def;
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) ++pos;
    if (pos >= json.size()) return def;
    if (json[pos] == '"') {
        const size_t end = json.find('"', pos + 1);
        if (end == std::string::npos) return def;
        return json.substr(pos + 1, end - pos - 1);
    }
    const size_t end = json.find_first_of(",}\n", pos);
    return json.substr(pos, end == std::string::npos ? std::string::npos : end - pos);
}

bool json_bool(const std::string& json, const std::string& key, bool def) {
    const std::string v = json_string(json, key);
    if (v.empty()) return def;
    return v == "true" || v == "1" || v == "yes";
}

int json_int(const std::string& json, const std::string& key, int def) {
    const std::string v = json_string(json, key);
    if (v.empty()) return def;
    try {
        return std::stoi(v);
    } catch (...) {
        return def;
    }
}

char* dup_string(const std::string& s) {
    char* out = new char[s.size() + 1];
    std::memcpy(out, s.c_str(), s.size() + 1);
    return out;
}

agent::RuntimeConfig parse_config(const std::string& json) {
    agent::RuntimeConfig cfg;
    cfg.state_dir = json_string(json, "state_dir");
    cfg.safety_mode = json_string(json, "safety_mode", cfg.safety_mode);
    cfg.default_browser = json_string(json, "default_browser");
    cfg.language = json_string(json, "language", cfg.language);
    cfg.preload = json_bool(json, "preload", cfg.preload);
    cfg.dry_run = json_bool(json, "dry_run", cfg.dry_run);
    cfg.fast_path = json_bool(json, "fast_path", cfg.fast_path);
    cfg.launch_timeout_ms = json_int(json, "launch_timeout_ms", cfg.launch_timeout_ms);
    cfg.file_timeout_ms = json_int(json, "file_timeout_ms", cfg.file_timeout_ms);
    cfg.browser_timeout_ms = json_int(json, "browser_timeout_ms", cfg.browser_timeout_ms);
    cfg.vision_timeout_ms = json_int(json, "vision_timeout_ms", cfg.vision_timeout_ms);
    cfg.process_timeout_ms = json_int(json, "process_timeout_ms", cfg.process_timeout_ms);
    cfg.window_timeout_ms = json_int(json, "window_timeout_ms", cfg.window_timeout_ms);
    cfg.shell_timeout_ms = json_int(json, "shell_timeout_ms", cfg.shell_timeout_ms);
    cfg.max_retries = json_int(json, "max_retries", cfg.max_retries);
    cfg.screenshot_max_pixels = json_int(json, "screenshot_max_pixels", cfg.screenshot_max_pixels);
    cfg.ai_socket = json_string(json, "ai_socket", cfg.ai_socket);
    cfg.llm_timeout_ms = json_int(json, "llm_timeout_ms", cfg.llm_timeout_ms);
    cfg.max_steps = json_int(json, "max_steps", cfg.max_steps);
    cfg.confirm_timeout_ms = json_int(json, "confirm_timeout_ms", cfg.confirm_timeout_ms);
    cfg.debug_log = json_bool(json, "debug_log", cfg.debug_log);
    cfg.debug_log_path = json_string(json, "debug_log_path", cfg.debug_log_path);
    return cfg;
}

}  // namespace

AGENT_EXPORT int32_t agent_init(const char* config_json) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (g_runtime) return 0;   // уже запущен: рантайм живёт всё время работы приложения
    const std::string json = config_json ? config_json : "{}";
    const agent::RuntimeConfig cfg = parse_config(json);
    try {
        g_runtime = new agent::AgentRuntime(cfg, agent::make_platform());
    } catch (const std::exception& e) {
        g_last_error = std::string("не удалось создать рантайм: ") + e.what();
        g_runtime = nullptr;
        return 10;
    }
    g_runtime->set_cancel([] { return g_cancel.load(); });
    if (g_sink) {
        void (*fn)(const char*, void*) = g_sink;
        void* user = g_sink_user;
        g_runtime->set_event_sink([fn, user](const agent::Event& ev) {
            const std::string json_ev = ev.to_json();
            fn(json_ev.c_str(), user);
        });
    }
    std::string error;
    if (!g_runtime->start(error)) {
        g_last_error = error.empty() ? "рантайм не запустился" : error;
        delete g_runtime;
        g_runtime = nullptr;
        return 11;
    }
    return 0;
}

AGENT_EXPORT void agent_shutdown() {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) return;
    g_runtime->stop();
    delete g_runtime;
    g_runtime = nullptr;
}

AGENT_EXPORT char* agent_execute(const char* phrase) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) {
        g_last_error = "рантайм не инициализирован: вызовите agent_init";
        return dup_string("{\"handled\":false,\"ok\":false,\"error\":\"not_initialized\"}");
    }
    if (!phrase) return dup_string("{\"handled\":false,\"ok\":false,\"error\":\"empty\"}");
    g_cancel.store(false);   // новая команда отменяет предыдущий «Стоп»
    try {
        return dup_string(g_runtime->execute(phrase).to_json());
    } catch (const std::exception& e) {
        g_last_error = e.what();
        return dup_string(std::string("{\"handled\":false,\"ok\":false,\"error\":\"") + e.what() +
                          "\"}");
    }
}

AGENT_EXPORT char* agent_preview(const char* phrase) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime || !phrase) return dup_string("");
    try {
        return dup_string(g_runtime->preview(phrase));
    } catch (...) {
        return dup_string("");
    }
}

AGENT_EXPORT char* agent_metrics_json() {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) return dup_string("{}");
    return dup_string(g_runtime->metrics_json());
}

AGENT_EXPORT char* agent_state_json() {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) return dup_string("{}");
    return dup_string(g_runtime->state_json());
}

AGENT_EXPORT char* agent_tools_json() {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) return dup_string("[]");
    return dup_string(g_runtime->tools_json());
}

AGENT_EXPORT char* agent_run_tool(const char* tool, const char* args_json) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) {
        g_last_error = "рантайм не инициализирован: вызовите agent_init";
        return dup_string("{\"ok\":false,\"error\":\"not_initialized\"}");
    }
    if (!tool) return dup_string("{\"ok\":false,\"error\":\"empty_tool\"}");
    try {
        return dup_string(g_runtime->run_tool(tool, args_json ? args_json : "{}"));
    } catch (const std::exception& e) {
        g_last_error = e.what();
        return dup_string(std::string("{\"ok\":false,\"error\":\"") + e.what() + "\"}");
    }
}

AGENT_EXPORT char* agent_run_task(const char* task, int32_t max_steps) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) {
        g_last_error = "рантайм не инициализирован: вызовите agent_init";
        return dup_string("{\"ok\":false,\"error\":\"not_initialized\"}");
    }
    if (!task) return dup_string("{\"ok\":false,\"error\":\"empty_task\"}");
    g_cancel.store(false);
    try {
        return dup_string(g_runtime->run_task(task, max_steps));
    } catch (const std::exception& e) {
        g_last_error = e.what();
        return dup_string(std::string("{\"ok\":false,\"error\":\"") + e.what() + "\"}");
    }
}

AGENT_EXPORT const char* agent_last_error() { return g_last_error.c_str(); }

AGENT_EXPORT void agent_free(char* ptr) { delete[] ptr; }

AGENT_EXPORT void agent_set_event_sink(void (*fn)(const char*, void*), void* user) {
    std::lock_guard<std::mutex> lock(g_mu);
    g_sink = fn;
    g_sink_user = user;
    if (!g_runtime) return;
    if (!fn) {
        g_runtime->set_event_sink(nullptr);
        return;
    }
    g_runtime->set_event_sink([fn, user](const agent::Event& ev) {
        const std::string json_ev = ev.to_json();
        fn(json_ev.c_str(), user);
    });
}

AGENT_EXPORT void agent_answer_confirmation(int32_t approved) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (g_runtime) g_runtime->answer_confirmation(approved != 0);
}

AGENT_EXPORT void agent_set_confirm_timeout(int32_t timeout_ms) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (g_runtime) g_runtime->config().confirm_timeout_ms = timeout_ms;
}

AGENT_EXPORT void agent_cancel_current() {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_runtime) return;
    // «Стоп» должен прерывать работу на уровне исполнителя, а не UI (ТЗ §33).
    g_cancel.store(true);
    g_runtime->queue().cancel();
}
