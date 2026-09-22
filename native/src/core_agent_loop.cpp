// Агентный цикл сложных задач: PLAN → ACT → VERIFY → REPLAN.
//
// Роли разделены строго по ТЗ:
//   * планирует Python-мозг (LM Studio + Qwen3-VL) — за каналом Named Pipes;
//   * исполняет и проверяет C++ рантайм (нативные инструменты, ожидания, восстановление);
//   * UI получает поток событий (plan / tool_call / observation / task_done).
//
// Рантайм ничего не «додумывает» за модель: не пришло планa — задача честно завершается
// с ошибкой, а не имитацией успеха.
#include <string>
#include <vector>

#include "agent/ipc.h"
#include "agent/runtime.h"
#include "agent/util.h"

namespace agent {
namespace {

std::string keep_short(std::string text, size_t limit) {
    if (text.size() <= limit) return text;
    return text.substr(0, limit) + "…";
}

}  // namespace

// Задача целиком: маршрут → (быстрый путь | агентный цикл) → проверка → итог.
std::string AgentRuntime::run_task(std::string_view task, int max_steps) {
    const double t0 = now_us();
    const std::string text(task);
    if (text.empty()) return "{\"ok\":false,\"error\":\"пустая задача\"}";

    // 1) Сначала «рефлексы»: простую команду делает само ядро, модель не нужна.
    FastOutcome fast = execute(text);
    if (fast.handled) {
        std::string out = "{\"ok\":";
        out += fast.ok ? "true" : "false";
        out += ",\"mode\":\"fast\",\"steps\":1,\"llm_calls\":0,\"tool_calls\":";
        out += std::to_string(c_.tool_calls.load() ? 1 : 0);
        out += ",\"ms\":" + std::to_string(int((now_us() - t0) / 1000.0));
        out += ",\"route_us\":" + std::to_string(int(fast.route_us));
        out += ",\"message\":\"" + json_escape(fast.message) + "\"";
        if (!fast.error.empty()) out += ",\"error\":\"" + json_escape(fast.error) + "\"";
        if (!fast.data_json.empty()) out += ",\"data\":" + fast.data_json;
        out += "}";
        emit(Event{"task_done", "", fast.ok ? "success" : "failed", fast.message, out,
                   (now_us() - t0) / 1000.0, now_ms()});
        return out;
    }

    // 1.5) Путь зрения: «нажми кнопку X» — снимок → модель → клик ядра → проверка.
    //      Без круга планирования: модель здесь нужна как глаза, а не как мозг.
    if (fast.route == RouteKind::Vision) {
        const Intent intent = intents_.parse(text);
        const std::string target = intent.slot(SlotId::Target).str();
        const std::string vision = vision_call("find", target, "{}");
        const bool ok = vision.find("\"ok\":true") != std::string::npos;
        std::string out = "{\"ok\":" + std::string(ok ? "true" : "false") +
                          ",\"mode\":\"vision\",\"steps\":1";
        out += ",\"tool_calls\":1,\"ms\":" + std::to_string(int((now_us() - t0) / 1000.0));
        out += ",\"result\":" + vision;
        out += ",\"message\":\"" + json_escape(ok ? ("Нашёл и нажал: " + target)
                                                    : ("Не нашёл на экране: " + target)) + "\"";
        if (!ok) {
            const std::string why = json_get_str(vision, "error");
            if (!why.empty()) out += ",\"error\":\"" + json_escape(why) + "\"";
        }
        out += "}";
        count_task(ok, false, (now_us() - t0) / 1000.0, fast.route_us);
        emit(Event{"task_done", "", ok ? "success" : "failed", target, out,
                   (now_us() - t0) / 1000.0, now_ms()});
        return out;
    }

    // 2) Сложная задача: спрашиваем план у мозга (Python + Qwen3-VL).
    if (cfg_.ai_socket.empty()) {
        count_task(false, false, (now_us() - t0) / 1000.0, fast.route_us);
        return "{\"ok\":false,\"mode\":\"agent\",\"error\":\"канал к AI-воркеру не настроен "
               "(ai_socket)\",\"needs_llm\":true,\"route\":\"" +
               std::string(to_string(fast.route)) + "\"}";
    }
    if (max_steps <= 0) max_steps = cfg_.max_steps;

    std::string error;
    if (!ai_link_) ai_link_ = std::make_unique<AiLink>();
    if (!ai_link_->connected() && !ai_link_->connect(cfg_.ai_socket, 5000, error)) {
        count_task(false, false, (now_us() - t0) / 1000.0, fast.route_us);
        return "{\"ok\":false,\"mode\":\"agent\",\"error\":\"" + json_escape(error) + "\"}";
    }

    const std::string tools_json = tools_.schemas_json({});
    std::vector<std::string> observations;
    int llm_calls = 0;
    int tool_calls = 0;
    int failed_calls = 0;
    // Восстановился ли цикл после последней ошибки: успешный шаг после сбоя = «да».
    int last_fail_step = -1;
    int last_ok_step = -1;
    int step = 0;
    bool finished = false;
    bool ok = false;
    std::string summary;

    for (step = 1; step <= max_steps; ++step) {
        if (cancel_ && cancel_()) {
            summary = "остановлено пользователем";
            break;
        }
        // 2.1 План/переплан у модели. Наблюдения передаём полностью — модель должна
        //     видеть реальные результаты, а не догадываться о них.
        std::string obs_json = "[";
        for (size_t i = 0; i < observations.size(); ++i) {
            if (i) obs_json += ",";
            obs_json += "\"" + json_escape(observations[i]) + "\"";
        }
        obs_json += "]";
        const std::string request =
            "{\"id\":" + std::to_string(step) + ",\"type\":\"" +
            std::string(step == 1 ? "plan" : "replan") + "\",\"task\":\"" + json_escape(text) +
            "\",\"step\":" + std::to_string(step) + ",\"max_steps\":" + std::to_string(max_steps) +
            ",\"tools\":" + tools_json + ",\"state\":" + state_json() +
            // Реестр приложений: модель должна называть приложения так, как они есть
            // на этой машине, а не угадывать имена и пути.
            ",\"apps\":" + apps_json_for(text) +
            ",\"observations\":" + obs_json + "}";
        emit(Event{"plan", "", "running", step == 1 ? "Думаю, как выполнить задачу"
                                                    : "Уточняю план по результатам",
                   "", 0.0, now_ms()});
        const double plan_t0 = now_us();
        // «Стоп» действует и здесь: ожидание плана модели прерывается, а не висит
        // до llm_timeout_ms (у локальной модели это десятки секунд).
        AiReply reply = ai_link_->request(request, cfg_.llm_timeout_ms,
                                          [this] { return cancel_ && cancel_(); });
        const double plan_ms = (now_us() - plan_t0) / 1000.0;
        debug("plan", "шаг " + std::to_string(step) + " | " + std::to_string(int(plan_ms)) + " мс");
        if (!reply.ok) {
            error = reply.error;
            if (reply.cancelled) {
                summary = "остановлено пользователем";
                debug("plan.cancel", "план модели прерван по «Стоп»");
                break;
            }
            if (!ai_link_->ensure_connected(error)) break;
            continue;                       // один честный повтор на обрыв канала
        }
        ++llm_calls;

        std::vector<std::string> calls;
        std::string say;
        bool model_finished = false;
        if (!parse_plan(reply.json, calls, say, model_finished)) {
            error = "план модели не разобран";
            debug("plan.error", error);
            break;
        }
        if (!say.empty())
            emit(Event{"thought", "", "info", keep_short(say, 400), "", plan_ms, now_ms()});
        if (model_finished && calls.empty()) {
            finished = true;
            ok = true;
            summary = say.empty() ? "Задача выполнена" : keep_short(say, 400);
            break;
        }
        if (calls.empty()) {
            error = say.empty() ? "модель не предложила действий" : keep_short(say, 300);
            break;
        }

        // 2.2 Исполнение плана: последовательно (порядок важен для UI и для проверок),
        //     каждый вызов — нативный инструмент, наблюдение — его реальный результат.
        for (const std::string& call_json : calls) {
            if (cancel_ && cancel_()) break;
            const std::string tool = json_get_str(call_json, "tool");
            const std::string args = json_get_str(call_json, "args_json", "{}");
            const std::string note = json_get_str(call_json, "note");
            if (tool.empty()) continue;
            emit(Event{"tool_call", tool, "running",
                       note.empty() ? ("Выполняю " + tool) : note, "", 0.0, now_ms()});
            const std::string result = run_tool(tool, args);
            ++tool_calls;
            const bool call_ok = json_get_bool(result, "ok", false);
            if (call_ok) last_ok_step = step;
            else { ++failed_calls; last_fail_step = step; }
            std::string observation = tool + ": " + (call_ok ? "ok" : "ошибка");
            const std::string output = json_get_str(result, "output");
            const std::string call_error = json_get_str(result, "error");
            if (!output.empty()) observation += " — " + keep_short(output, 300);
            // Проверка результата — часть наблюдения: модель и пользователь видят одно и то же.
            if (call_ok && (json_get_bool(result, "verified", false) ||
                            json_get_bool(result, "changed", false)))
                observation += " (проверено: результат подтверждён)";
            if (!call_ok && !call_error.empty()) observation += " (" + keep_short(call_error, 200) + ")";
            observations.push_back(observation);
            emit(Event{"observation", tool, call_ok ? "success" : "failed", observation, "",
                       0.0, now_ms()});
            // Явно не удалось — отдаём модели на переплан, не ломая остаток шага.
            if (!call_ok) break;
        }
    }

    if (step > max_steps && !finished) {
        error = "исчерпан лимит шагов (" + std::to_string(max_steps) + ")";
    }
    // Победа не объявляется там, где последнее действие провалилось: контроль результатa —
    // часть рантайма, а не модели.
    if (ok && failed_calls > 0 && last_fail_step > last_ok_step) {
        ok = false;
        error = "модель завершила задачу, но последние действия не сработали";
    }
    const double total_ms = (now_us() - t0) / 1000.0;
    count_task(ok, false, total_ms, fast.route_us);
    c_.llm_calls.fetch_add(uint64_t(llm_calls));

    std::string out = "{\"ok\":";
    out += ok ? "true" : "false";
    out += ",\"mode\":\"agent\",\"steps\":" + std::to_string(step);
    out += ",\"llm_calls\":" + std::to_string(llm_calls);
    out += ",\"tool_calls\":" + std::to_string(tool_calls);
    out += ",\"failed_calls\":" + std::to_string(failed_calls);
    if (ok && failed_calls > 0) {
        // Задача дошла до конца, но часть действий провалилась: об этом нужно сказать,
        // иначе «готово» звучит как обещание, которого нет.
        out += ",\"warnings\":[\"не сработало действий: " + std::to_string(failed_calls) + "\"]";
    }
    out += ",\"ms\":" + std::to_string(int(total_ms));
    out += ",\"finished\":" + std::string(finished ? "true" : "false");
    if (!summary.empty()) out += ",\"message\":\"" + json_escape(summary) + "\"";
    if (!error.empty()) out += ",\"error\":\"" + json_escape(error) + "\"";
    out += ",\"observations\":[";
    for (size_t i = 0; i < observations.size(); ++i) {
        if (i) out += ",";
        out += "\"" + json_escape(observations[i]) + "\"";
    }
    out += "]}";
    emit(Event{"task_done", "", ok ? "success" : "failed",
               summary.empty() ? (ok ? "Задача выполнена" : error) : summary, out, total_ms,
               now_ms()});
    return out;
}

// Разбор ответа мозга: {"calls":[{"tool":"…","args":{…},"note":"…"}],"say":"…","finished":bool}
bool AgentRuntime::parse_plan(std::string_view json, std::vector<std::string>& calls, std::string& say,
                              bool& finished) {
    say = json_get_str(json, "say");
    finished = json_get_bool(json, "finished", false);
    // Ответ модели обязан быть планом: иначе честная ошибка, а не пустой шаг.
    if (json.find("\"calls\"") == std::string_view::npos &&
        json.find("\"say\"") == std::string_view::npos &&
        json.find("\"finished\"") == std::string_view::npos)
        return false;
    const size_t calls_at = json.find("\"calls\"");
    if (calls_at == std::string_view::npos) return true;   // ответ без плана — это нормально
    const size_t array_at = json.find('[', calls_at);
    if (array_at == std::string_view::npos) return false;
    size_t i = array_at + 1;
    int depth = 0;
    size_t item_start = std::string_view::npos;
    for (; i < json.size(); ++i) {
        const char c = json[i];
        if (c == '{') {
            if (depth == 0) item_start = i;
            ++depth;
        } else if (c == '}') {
            --depth;
            if (depth == 0 && item_start != std::string_view::npos) {
                const std::string_view item = json.substr(item_start, i - item_start + 1);
                const std::string tool = json_get_str(item, "tool");
                if (!tool.empty()) {
                    const size_t args_at = item.find("\"args\"");
                    std::string args = "{}";
                    if (args_at != std::string_view::npos) {
                        const size_t begin = item.find('{', args_at);
                        if (begin != std::string_view::npos) {
                            int d = 0;
                            for (size_t k = begin; k < item.size(); ++k) {
                                if (item[k] == '{') ++d;
                                else if (item[k] == '}') {
                                    --d;
                                    if (d == 0) {
                                        args = std::string(item.substr(begin, k - begin + 1));
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    std::string entry = "{\"tool\":\"" + json_escape(tool) + "\",\"args_json\":\"" +
                                        json_escape(args) + "\",\"note\":\"" +
                                        json_escape(json_get_str(item, "note")) + "\"}";
                    calls.push_back(std::move(entry));
                }
                item_start = std::string_view::npos;
            }
        } else if (c == ']' && depth == 0) {
            break;
        }
    }
    return true;
}

}  // namespace agent
