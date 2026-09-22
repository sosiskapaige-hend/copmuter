// Исполнение одного вызова инструмента: то, чем пользуется агентный цикл (Python/Qwen)
// после планирования. Ядро остаётся «руками»: модель просит — рантайм исполняет
// нативным способом, проверяет результат и возвращает наблюдение.
#include <cstdlib>
#include <string>

#include "agent/intent.h"
#include "agent/runtime.h"
#include "agent/util.h"

namespace agent {
namespace {

struct ToolCall {
    std::string tool;
    std::string args;
};

// Общие имена инструментов (как их зовёт модель) → обработчики намерений.
const char* normalize_tool(std::string_view tool) {
    static const std::pair<const char*, const char*> kAliases[] = {
        {"launch_app", "launch_application"},   {"open_application", "launch_application"},
        {"run_app", "launch_application"},      {"open_uri", "open_url"},
        {"open_link", "open_url"},              {"browse", "open_url"},
        {"search", "search_web"},               {"google", "search_web"},
        {"youtube", "search_youtube"},          {"open_dir", "open_folder"},
        {"mkdir", "create_folder"},             {"fs_mkdir", "create_folder"},
        {"fs_write", "write_file"},             {"create_file", "write_file"},
        {"fs_read", "read_file"},               {"fs_list", "list_dir"},
        {"fs_delete", "delete_path"},           {"remove", "delete_path"},
        {"fs_move", "move_file"},               {"rename", "move_file"},
        {"fs_copy", "copy_file"},               {"take_screenshot", "screenshot"},
        {"screen", "screenshot"},               {"press_key", "send_keys"},
        {"hotkey", "send_keys"},                {"set_volume", "set_volume"},
        {"wallpaper", "set_wallpaper"},         {"power", "system_power"},
        {"settings", "open_settings"},          {"run_powershell", "execute_powershell"},
        {"powershell", "execute_powershell"},   {"shell", "run_command"},
        {"kill", "kill_process"},               {"focus_window", "focus_window"},        {"activate_window", "focus_window"},
        {"switch_to_window", "focus_window"},    {"switch_window", "focus_window"},
        {"clipboard_get", "clipboard"},
        {"clipboard_set", "clipboard"},
    };
    for (const auto& [alias, canonical] : kAliases)
        if (tool == alias) return canonical;
    return nullptr;
}

}  // namespace

// Возвращает JSON-наблюдение: {"ok":true,"output":"…","error":"…","ms":…,"method":"…"}
std::string AgentRuntime::run_tool(std::string_view tool_in, std::string_view args_json) {
    const double w0 = now_us();
    std::string json = run_tool_impl(tool_in, args_json);
    // Наблюдение всегда несёт имя инструмента и время: по ним модель и метрики понимают,
    // что именно произошло.
    if (json.size() > 1 && json[0] == '{') {
        std::string prefix;
        if (json.find("\"tool\"") == std::string::npos)
            prefix += "\"tool\":\"" + json_escape(std::string(tool_in)) + "\",";
        if (json.find("\"ms\"") == std::string::npos)
            prefix += "\"ms\":" + std::to_string(int((now_us() - w0) / 1000.0)) + ",";
        if (!prefix.empty()) json.insert(1, prefix);
    }
    return json;
}

std::string AgentRuntime::run_tool_impl(std::string_view tool_in, std::string_view args_json) {
    const double t0 = now_us();
    std::string tool(tool_in);
    if (const char* canonical = normalize_tool(tool)) tool = canonical;

    FastOutcome out;
    out.handled = true;
    Intent it;
    it.raw = tool;
    bool known = true;
    std::string output;

    auto fail = [&](const std::string& error) {
        return std::string("{\"ok\":false,\"error\":\"") + json_escape(error) + "\",\"tool\":\"" +
               json_escape(tool) + "\",\"ms\":" +
               std::to_string(int((now_us() - t0) / 1000.0)) + "}";
    };

    if (tool == "click" || tool == "double_click" || tool == "right_click" || tool == "mouse_click") {
        const int x = json_get_int(args_json, "x", -1);
        const int y = json_get_int(args_json, "y", -1);
        int button = json_get_int(args_json, "button", 1);
        const std::string button_name = json_get_str(args_json, "button_name");
        if (!button_name.empty()) {
            if (button_name == "right") button = 2;
            else if (button_name == "middle") button = 3;
        }
        int clicks = json_get_int(args_json, "clicks", tool == "double_click" ? 2 : 1);
        if (x < 0 || y < 0) return fail("не переданы координаты клика");
        const bool ok = platform_->mouse_click(x, y, button, clicks);
        note_tool_call("click", ok, (now_us() - t0) / 1000.0);
        output = ok ? "Кликнул в " + std::to_string(x) + "," + std::to_string(y)
                    : "Клик не прошёл";
        if (!ok) return fail(output);
        return std::string("{\"ok\":true,\"output\":\"") + json_escape(output) + "\",\"tool\":\"click\"}";
    }
    if (tool == "mouse_move") {
        const int x = json_get_int(args_json, "x", -1);
        const int y = json_get_int(args_json, "y", -1);
        if (x < 0 || y < 0) return fail("не переданы координаты");
        const bool ok = platform_->mouse_move(x, y);
        return std::string("{\"ok\":") + (ok ? "true" : "false") + ",\"output\":\"" + (ok ? "Курсор перемещён" : "Не удалось переместить курсор") + "\"}";
    }
    if (tool == "execute_powershell") {
        const std::string script = json_get_str(args_json, "script",
                                                json_get_str(args_json, "command"));
        if (script.empty()) return fail("пустой скрипт PowerShell");
        const int timeout = json_get_int(args_json, "timeout_ms", cfg_.shell_timeout_ms);
        const ExecResult res = platform_->run_powershell(script, json_get_str(args_json, "cwd"), timeout);
        note_tool_call("execute_powershell", res.ok(), res.ms);
        if (!res.started)
            return fail(res.error.empty() ? "PowerShell не запустился" : res.error);
        return std::string("{\"ok\":") + (res.ok() ? "true" : "false") + ",\"output\":\"" +
               json_escape(res.stdout_text) + "\",\"error\":\"" + json_escape(res.stderr_text) +
               "\",\"exit_code\":" + std::to_string(res.exit_code) +
               ",\"timed_out\":" + (res.timed_out ? "true" : "false") + ",\"ms\":" +
               std::to_string(int(res.ms)) + "}";
    }
    if (tool == "computer_state" || tool == "state") {
        return std::string("{\"ok\":true,\"output\":") + state_json() + "}";
    }
    if (tool == "wait_for") {
        const std::string kind = json_get_str(args_json, "kind");
        const std::string target = json_get_str(args_json, "target");
        const int timeout = json_get_int(args_json, "timeout_ms", cfg_.process_timeout_ms);
        WaitResult wr;
        if (kind == "process_started" || kind == "process_start") {
            wr = wait_->wait_process_started(target, timeout);
        } else if (kind == "process_finished" || kind == "process_exit") {
            wr = wait_->wait_process_finished(target, timeout);
        } else if (kind == "window_created" || kind == "window") {
            wr = wait_->wait_window_created(target, timeout);
        } else if (kind == "window_active") {
            wr = wait_->wait_window_active(target, timeout);
        } else if (kind == "file_exists" || kind == "file") {
            wr = wait_->wait_file_exists(target, timeout, 0, json_get_num(args_json, "stable_ms", 0.0));
        } else if (kind == "file_gone") {
            wr = wait_->wait_file_gone(target, timeout);
        } else if (kind == "port" || kind == "port_open") {
            wr = wait_->wait_port_open(target, timeout);
        } else if (kind == "url_loaded" || kind == "url") {
            wr = wait_->wait_url_loaded(target, json_get_str(args_json, "title"), timeout);
        } else {
            return fail("неизвестное условие ожидания: " + kind);
        }
        return std::string("{\"ok\":") + (wr.ok ? "true" : "false") + ",\"output\":\"" +
               json_escape(wr.detail) + "\",\"ms\":" + std::to_string(int(wr.elapsed_ms)) +
               ",\"polls\":" + std::to_string(wr.polls) + "}";
    }
    if (tool == "clipboard") {
        const std::string mode = json_get_str(args_json, "mode", json_get_str(args_json, "action", "get"));
        if (mode == "set" || mode == "write") {
            const std::string text = json_get_str(args_json, "text");
            const bool ok = platform_->clipboard_set(text);
            return std::string("{\"ok\":") + (ok ? "true" : "false") + ",\"output\":\"" +
                   (ok ? "Записал в буфер обмена" : "Не удалось записать в буфер") + "\"}";
        }
        const std::string text = platform_->clipboard_get();
        return std::string("{\"ok\":true,\"output\":\"") + json_escape(text) + "\",\"chars\":" +
               std::to_string(text.size()) + "}";
    }

    // ------------------------------------------------------------------ намерения
    if (tool == "launch_application") {
        it.action = "launch_app";
        it.set(SlotId::Target, json_get_str(args_json, "name",
                                            json_get_str(args_json, "app", json_get_str(args_json, "target"))));
        it.set(SlotId::Args, json_get_str(args_json, "args"));
    } else if (tool == "open_url") {
        it.action = "open_url";
        it.set(SlotId::Url, json_get_str(args_json, "url", json_get_str(args_json, "target")));
    } else if (tool == "search_web") {
        it.action = "web_search";
        it.set(SlotId::Query, json_get_str(args_json, "query", json_get_str(args_json, "text")));
    } else if (tool == "search_youtube") {
        it.action = "youtube_search";
        it.set(SlotId::Query, json_get_str(args_json, "query", json_get_str(args_json, "text")));
    } else if (tool == "focus_window") {
        it.action = "focus_window";
        it.set(SlotId::Target, json_get_str(args_json, "title",
                                            json_get_str(args_json, "name",
                                                         json_get_str(args_json, "app",
                                                                      json_get_str(args_json, "target")))));
    } else if (tool == "open_folder") {
        it.action = "open_folder";
        it.set(SlotId::Target, json_get_str(args_json, "path", json_get_str(args_json, "folder")));
        it.set(SlotId::Place, json_get_str(args_json, "place"));
    } else if (tool == "create_folder") {
        it.action = "create_folder";
        it.set(SlotId::Target, json_get_str(args_json, "path", json_get_str(args_json, "name")));
        it.set(SlotId::Place, json_get_str(args_json, "place"));
    } else if (tool == "write_file") {
        it.action = "create_file";
        it.set(SlotId::Target, json_get_str(args_json, "path",
                                            json_get_str(args_json, "file", json_get_str(args_json, "name"))));
        it.set(SlotId::Content, json_get_str(args_json, "content", json_get_str(args_json, "text")));
        it.set(SlotId::Place, json_get_str(args_json, "place"));
    } else if (tool == "read_file") {
        it.action = "read_file";
        it.set(SlotId::Target, json_get_str(args_json, "path", json_get_str(args_json, "file")));
    } else if (tool == "list_dir") {
        it.action = "list_dir";
        it.set(SlotId::Target, json_get_str(args_json, "path", json_get_str(args_json, "dir")));
    } else if (tool == "search_files") {
        it.action = "find_files";
        it.set(SlotId::Target, json_get_str(args_json, "pattern", json_get_str(args_json, "name")));
        it.set(SlotId::Place, json_get_str(args_json, "place"));
    } else if (tool == "delete_path") {
        it.action = "delete_path";
        it.set(SlotId::Target, json_get_str(args_json, "path", json_get_str(args_json, "target")));
        it.set(SlotId::Place, json_get_str(args_json, "place"));
        if (json_get_bool(args_json, "permanent")) it.set(SlotId::Args, "permanent");
    } else if (tool == "move_file") {
        it.action = "move_path";
        it.set(SlotId::Target, json_get_str(args_json, "src", json_get_str(args_json, "from")));
        it.set(SlotId::Place, json_get_str(args_json, "dst", json_get_str(args_json, "to")));
    } else if (tool == "copy_file") {
        it.action = "copy_path";
        it.set(SlotId::Target, json_get_str(args_json, "src", json_get_str(args_json, "from")));
        it.set(SlotId::Place, json_get_str(args_json, "dst", json_get_str(args_json, "to")));
    } else if (tool == "screenshot") {
        it.action = "screenshot";
    } else if (tool == "send_keys") {
        it.action = "send_keys";
        it.set(SlotId::Key, json_get_str(args_json, "keys",
                                         json_get_str(args_json, "key", json_get_str(args_json, "hotkey"))));
    } else if (tool == "type_text") {
        it.action = "type_text";
        it.set(SlotId::Content, json_get_str(args_json, "text", json_get_str(args_json, "content")));
    } else if (tool == "set_wallpaper") {
        it.action = "set_wallpaper";
        it.set(SlotId::Target, json_get_str(args_json, "path", json_get_str(args_json, "image")));
    } else if (tool == "set_volume") {
        it.action = "volume";
        const std::string action = json_get_str(args_json, "action", "set");
        it.set(SlotId::Value, std::to_string(json_get_int(args_json, "percent",
                                                           json_get_int(args_json, "value", -1))));
        it.set(SlotId::Args, action);
    } else if (tool == "system_power") {
        it.action = "power";
        it.set(SlotId::Args, json_get_str(args_json, "action", "shutdown"));
    } else if (tool == "show_desktop") {
        it.action = "show_desktop";
    } else if (tool == "run_command") {
        it.action = "run_command";
        it.set(SlotId::Content, json_get_str(args_json, "command", json_get_str(args_json, "cmd")));
    } else if (tool == "kill_process") {
        it.action = "kill_process";
        it.set(SlotId::Target, json_get_str(args_json, "name",
                                            json_get_str(args_json, "process", json_get_str(args_json, "app"))));
    } else if (tool == "open_settings") {
        it.action = "settings_page";
        it.set(SlotId::Args, json_get_str(args_json, "page", json_get_str(args_json, "target")));
    } else if (tool == "find_element" || tool == "read_screen" || tool == "analyze_screen") {
        // Элементы интерфейса ищет Python-зрение: у ядра тут нет детерминированного пути.
        out.handled = false;
        out.needs_llm = true;
        out.llm_prompt = tool;
        out.message = "Поиск элемента на экране выполняет зрение (Python + Qwen3-VL)";
        known = false;
    } else {
        known = false;
    }

    if (!known) {
        note_tool_call(tool, false, (now_us() - t0) / 1000.0);
        return std::string("{\"ok\":false,\"needs_llm\":") + (out.needs_llm ? "true" : "false") +
               ",\"error\":\"" + json_escape(out.message.empty() ? ("неизвестный инструмент: " + tool)
                                                                 : out.message) +
               "\",\"tool\":\"" + json_escape(tool) + "\"}";
    }

    bool ok = execute_intent(it, out);
    const double ms = (now_us() - t0) / 1000.0;
    note_tool_call(tool, ok, ms);
    output = out.message;
    std::string result = "{\"ok\":";
    result += ok ? "true" : "false";
    result += ",\"tool\":\"" + json_escape(tool) + "\"";
    if (!output.empty()) result += ",\"output\":\"" + json_escape(output) + "\"";
    if (!out.error.empty()) result += ",\"error\":\"" + json_escape(out.error) + "\"";
    if (!out.data_json.empty()) result += ",\"data\":" + out.data_json;
    result += ",\"ms\":" + std::to_string(int(ms));
    result += "}";
    return result;
}

}  // namespace agent
