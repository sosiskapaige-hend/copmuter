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
        {"kill", "kill_process"},               {"click_element", "find_element"},       {"find_and_click", "find_element"},
        {"find_on_screen", "find_element"},      {"ocr_screen", "read_screen"},
        {"describe_screen", "analyze_screen"},   {"screen_text", "read_screen"},
        {"browser", "browser_task"},             {"playwright", "browser_task"},
        {"web_automation", "browser_task"},      {"browser_step", "browser_task"},
        {"focus_window", "focus_window"},        {"activate_window", "focus_window"},
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

    // Разрушительное действие модель не выполняет сама: спрашиваем пользователя.
    // (Политика — в needs_confirmation: режим, риск инструмента, массовость.)
    if (const ToolSpec* spec = tools_.find(tool)) {
        Intent probe;
        probe.action = tool;
        probe.set(SlotId::Target, json_get_str(args_json, "path",
                                               json_get_str(args_json, "target",
                                                             json_get_str(args_json, "name"))));
        std::string reason;
        if (needs_confirmation(*spec, probe, reason)) {
            note_tool_call(tool, false, (now_us() - t0) / 1000.0);
            return std::string("{\"ok\":false,\"needs_confirmation\":true,\"tool\":\"") +
                   json_escape(tool) + "\",\"error\":\"требуется подтверждение: " +
                   json_escape(reason) + "\"}";
        }
    }

    // Сложная страница: Playwright в Python-воркере. Прямые ссылки сюда не доходят —
    // их открывает нативный open_url, потому что это на порядок быстрее.
    if (tool == "browser_task" || tool == "browser" || tool == "playwright") {
        return browser_call(args_json);
    }

    // Зрение: кадр → модель → координаты → клик ядра → проверка изменения экрана.
    if (tool == "find_element" || tool == "read_screen" || tool == "analyze_screen") {
        const std::string mode =
            tool == "read_screen" ? "read" : (tool == "analyze_screen" ? "describe" : "find");
        const std::string target = json_get_str(args_json, "target",
                                                json_get_str(args_json, "description",
                                                             json_get_str(args_json, "text",
                                                                          json_get_str(args_json, "question"))));
        return vision_call(mode, target, args_json);
    }

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

// ---------------------------------------------------------------------------
//  Глаза: снимок → Python/Qwen3-VL → координаты → клик ядра → проверка
// ---------------------------------------------------------------------------
std::string AgentRuntime::vision_call(const std::string& mode, const std::string& target,
                                     std::string_view args_json) {
    const double t0 = now_us();
    auto fail = [&](const std::string& error, const char* extra = "") {
        std::string out = std::string("{\"ok\":false,\"tool\":\"") + (mode == "find" ? "find_element"
                                                                            : (mode == "read" ? "read_screen"
                                                                                              : "analyze_screen")) +
                          "\",\"error\":\"" + json_escape(error) + "\"";
        if (*extra) out += std::string(",") + extra;
        out += ",\"ms\":" + std::to_string(int((now_us() - t0) / 1000.0)) + "}";
        return out;
    };
    if (cfg_.ai_socket.empty())
        return fail("зрение недоступно: канал к мозгу не настроен", "\"needs_llm\":true");

    // Область интереса: region:[x,y,w,h] или x1/y1/x2/y2 — чтобы не гнать весь экран.
    int region[4] = {0, 0, 0, 0};
    bool has_region = false;
    if (args_json.find("\"region\"") != std::string_view::npos) {
        const size_t at = args_json.find('[');
        if (at != std::string_view::npos) {
            int values[4] = {0, 0, 0, 0};
            size_t i = at + 1;
            int got = 0;
            while (i < args_json.size() && got < 4) {
                if (args_json[i] >= '0' && args_json[i] <= '9') {
                    int value = 0;
                    while (i < args_json.size() && args_json[i] >= '0' && args_json[i] <= '9') {
                        value = value * 10 + (args_json[i] - '0');
                        ++i;
                    }
                    values[got++] = value;
                } else {
                    ++i;
                }
            }
            if (got == 4 && values[2] > 0 && values[3] > 0) {
                region[0] = values[0];
                region[1] = values[1];
                region[2] = values[2];
                region[3] = values[3];
                has_region = true;
            }
        }
    }
    const int monitor = json_get_int(args_json, "monitor", 0);
    const Frame before = platform_->capture(monitor, has_region ? region : nullptr);
    if (before.width <= 0 || before.pixels.empty()) return fail("нет кадра: экран недоступен");
    const Frame small = frame_shrink(before, cfg_.screenshot_max_pixels, 2200);

    std::vector<uint8_t> png;
    if (!png_encode(small, png)) return fail("не удалось закодировать кадр");
    const std::string b64 = base64_encode(png.data(), png.size());
    const double scale = small.width > 0 ? double(before.width) / double(small.width) : 1.0;

    if (!ai_link_) ai_link_ = std::make_unique<AiLink>();
    std::string error;
    if (!ai_link_->connected() && !ai_link_->connect(cfg_.ai_socket, 3000, error))
        return fail(error);

    const std::string request =
        "{\"id\":1,\"type\":\"vision\",\"mode\":\"" + mode + "\",\"target\":\"" +
        json_escape(target) + "\",\"frame_b64\":\"" + b64 + "\",\"frame\":{\"width\":" +
        std::to_string(small.width) + ",\"height\":" + std::to_string(small.height) +
        ",\"origin_x\":" + std::to_string(before.origin_x) + ",\"origin_y\":" +
        std::to_string(before.origin_y) + ",\"scale\":" + std::to_string(scale) + "}}";
    AiReply reply = ai_link_->request(request, cfg_.vision_timeout_ms);
    if (!reply.ok && ai_link_->ensure_connected(error))
        reply = ai_link_->request(request, cfg_.vision_timeout_ms);
    c_.vision_calls.fetch_add(1);
    if (!reply.ok) return fail("зрение не ответило: " + reply.error);
    if (!json_get_bool(reply.json, "ok", true)) {
        const std::string why = json_get_str(reply.json, "error");
        return fail(why.empty() ? "зрение не справилось" : why, "\"found\":false");
    }
    note_tool_call(mode == "find" ? "find_element" : (mode == "read" ? "read_screen"
                                                                     : "analyze_screen"),
                   true, (now_us() - t0) / 1000.0);

    const std::string say = json_get_str(reply.json, "say");
    if (mode != "find") {
        // Прочитать текст или описать экран — ответ модели и есть результат.
        const std::string text = say.empty() ? json_get_str(reply.json, "output") : say;
        return std::string("{\"ok\":true,\"output\":\"") + json_escape(text) +
               "\",\"mode\":\"" + mode + "\",\"ms\":" +
               std::to_string(int((now_us() - t0) / 1000.0)) + "}";
    }

    // Клик: берём координаты из плана зрения и исполняем их нативно.
    int click_x = -1, click_y = -1;
    int button = 1, clicks = 1;
    const size_t calls_at = reply.json.find("\"calls\"");
    if (calls_at != std::string::npos) {
        const size_t brace = reply.json.find('{', calls_at);
        if (brace != std::string::npos) {
            const int depth_end = int(reply.json.find('}', brace));
            const std::string_view call =
                std::string_view(reply.json).substr(brace, size_t(std::max(0, depth_end - int(brace) + 1)));
            click_x = json_get_int(call, "x", -1);
            click_y = json_get_int(call, "y", -1);
            button = json_get_int(call, "button", 1);
            clicks = json_get_int(call, "clicks", 1);
        }
    }
    if (click_x < 0 || click_y < 0)
        return fail(say.empty() ? "не вижу на экране то, что нужно" : say, "\"found\":false");

    const bool clicked = platform_->mouse_click(click_x, click_y, button, clicks);
    if (!clicked) return fail("клик не прошёл", "\"found\":true");
    // Проверка: экран должен измениться. Не изменился — говорим об этом честно,
    // чтобы мозг перепланировал, а не считал действие успешным.
    const Frame after = platform_->capture(monitor, has_region ? region : nullptr);
    const double diff = frame_difference(before, after);
    const bool changed = diff > 0.002;
    return std::string("{\"ok\":true,\"output\":\"") +
           json_escape(say.empty() ? ("Кликнул по цели в " + std::to_string(click_x) + "," +
                                      std::to_string(click_y))
                                   : say) +
           "\",\"found\":true,\"verified\":" + (changed ? "true" : "false") +
           ",\"changed\":" + (changed ? "true" : "false") + ",\"diff\":" +
           std::to_string(diff).substr(0, 5) + ",\"click\":{\"x\":" + std::to_string(click_x) +
           ",\"y\":" + std::to_string(click_y) + "},\"ms\":" +
           std::to_string(int((now_us() - t0) / 1000.0)) + "}";
}

// ---------------------------------------------------------------------------
//  Сложная страница: запрос к браузерному пути (Playwright) в Python-воркере
// ---------------------------------------------------------------------------
std::string AgentRuntime::browser_call(std::string_view args_json) {
    const double t0 = now_us();
    auto fail = [&](const std::string& error) {
        return std::string("{\"ok\":false,\"tool\":\"browser_task\",\"error\":\"") +
               json_escape(error) + "\",\"ms\":" +
               std::to_string(int((now_us() - t0) / 1000.0)) + "}";
    };
    const std::string action = json_get_str(args_json, "action", "open");
    std::string body(args_json);
    if (body.empty() || body == "{}") body = "{\"action\":\"open\"}";
    if (cfg_.ai_socket.empty())
        return fail("браузерный путь недоступен: канал к мозгу не настроен");
    if (!ai_link_) ai_link_ = std::make_unique<AiLink>();
    std::string error;
    if (!ai_link_->connected() && !ai_link_->connect(cfg_.ai_socket, 3000, error))
        return fail(error);
    // Запрос формируем сами: тело — исходные аргументы плюс тип запроса.
    std::string request = "{\"id\":1,\"type\":\"browser\",";
    request += "\"action\":\"" + json_escape(action) + "\",";
    for (const char* key : {"url", "selector", "text", "keys", "script", "folder", "path"}) {
        const std::string value = json_get_str(args_json, key);
        if (!value.empty()) request += "\"" + std::string(key) + "\":\"" + json_escape(value) + "\",";
    }
    request += "\"timeout_ms\":" +
               std::to_string(json_get_int(args_json, "timeout_ms", cfg_.browser_timeout_ms)) + "}";
    AiReply reply = ai_link_->request(request, cfg_.browser_timeout_ms);
    if (!reply.ok && ai_link_->ensure_connected(error))
        reply = ai_link_->request(request, cfg_.browser_timeout_ms);
    const bool payload_ok = json_get_bool(reply.json, "ok", true);
    note_tool_call("browser_task", reply.ok && payload_ok, (now_us() - t0) / 1000.0);
    if (!reply.ok) return fail("браузерный путь не ответил: " + reply.error);
    if (!payload_ok) {
        // Мозг ответил по каналу, но само действие не удалось: причина — в его ответе.
        const std::string why = json_get_str(reply.json, "error");
        return fail(why.empty() ? "браузер не справился с действием" : why);
    }
    const std::string say = json_get_str(reply.json, "say");
    std::string data = "{}";
    const size_t data_at = reply.json.find("\"data\":");
    if (data_at != std::string::npos) {
        const size_t begin = reply.json.find('{', data_at);
        if (begin != std::string::npos) {
            int depth = 0;
            for (size_t i = begin; i < reply.json.size(); ++i) {
                if (reply.json[i] == '{') ++depth;
                else if (reply.json[i] == '}') {
                    --depth;
                    if (depth == 0) {
                        data = reply.json.substr(begin, i - begin + 1);
                        break;
                    }
                }
            }
        }
    }
    return std::string("{\"ok\":true,\"tool\":\"browser_task\",\"output\":\"") +
           json_escape(say.empty() ? ("браузер: " + action) : say) + "\",\"data\":" + data +
           ",\"ms\":" + std::to_string(int((now_us() - t0) / 1000.0)) + "}";
}

}  // namespace agent
