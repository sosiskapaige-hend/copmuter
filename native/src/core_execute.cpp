// Быстрый путь: исполнение намерений через нативные инструменты.
#include <algorithm>
#include <sstream>

#include "agent/intent.h"
#include "agent/runtime.h"
#include "agent/util.h"

namespace agent {
namespace {

// Ответ на «солидный» вопрос подтверждения: причина нужна в тексте.
const char* kConfirmHint = "Требуется подтверждение пользователя (UI/IPC).";

}  // namespace

// ---------------------------------------------------------------------------
//  Вход
// ---------------------------------------------------------------------------
FastOutcome AgentRuntime::execute(std::string_view phrase) {
    FastOutcome out;
    const double t0 = now_us();
    if (!running_.load()) {
        out.message = "ядро не запущено";
        out.error = "not started";
        return out;
    }
    if (!cfg_.fast_path) {
        out.needs_llm = true;
        out.llm_prompt = std::string(phrase);
        return out;
    }
    Route route = router_->route(phrase);
    out.route_us = now_us() - t0;
    out.route = route.kind;
    out.action = std::string(route.intent.action.view());

    if (route.kind != RouteKind::Direct) {
        out.handled = false;
        out.needs_llm = true;
        out.llm_prompt = std::string(phrase);
        out.message = std::string("Требуется модель: ") + route.reason;
        emit(Event{"route", out.action, "agent", out.message, route.to_json(),
                   out.route_us / 1000.0, now_ms()});
        c_.agent_tasks.fetch_add(1);
        c_.route_us.fetch_add(uint64_t(out.route_us));
        out.total_ms = (now_us() - t0) / 1000.0;
        return out;
    }
    if (cfg_.dry_run) {
        out.handled = true;
        out.ok = true;
        out.message = preview(phrase);
        out.data_json = route.to_json();
        out.total_ms = (now_us() - t0) / 1000.0;
        count_task(true, true, out.total_ms, out.route_us);
        return out;
    }

    FastOutcome res = execute_route(route);
    res.route_us = out.route_us;
    res.total_ms = (now_us() - t0) / 1000.0;
    count_task(res.ok, true, res.total_ms, res.route_us);
    return res;
}

// Один вызов инструмента по уже разобранному намерению (используется агентным циклом).
bool AgentRuntime::execute_intent(const Intent& it, FastOutcome& out) {
    Route r;
    r.intent = it;
    r.kind = RouteKind::Direct;
    r.reason = "вызов инструмента";
    FastOutcome res = execute_route(r);
    out.action = res.action;
    out.message = res.message;
    out.error = res.error;
    out.data_json = res.data_json;
    out.total_ms = res.total_ms;
    out.handled = res.handled;
    out.needs_llm = res.needs_llm;
    out.llm_prompt = res.llm_prompt;
    out.ok = res.ok;
    return res.ok;
}

FastOutcome AgentRuntime::execute_route(const Route& route) {
    FastOutcome out;
    out.handled = true;
    out.route = route.kind;
    const Intent& it = route.intent;
    const std::string action = std::string(it.action.view());
    out.action = action;

    if (action == "launch_app") out.ok = do_launch_app(it, out);
    else if (action == "open_url") out.ok = do_open_url(it, out);
    else if (action == "open_folder") out.ok = do_open_folder(it, out);
    else if (action == "open_path") out.ok = do_open_folder(it, out);
    else if (action == "web_search") out.ok = do_web_search(it, out, false);
    else if (action == "youtube_search") out.ok = do_web_search(it, out, true);
    else if (action == "create_folder") out.ok = do_create_folder(it, out);
    else if (action == "create_file") out.ok = do_create_file(it, out);
    else if (action == "read_file") out.ok = do_read_file(it, out);
    else if (action == "list_dir") out.ok = do_open_folder(it, out);
    else if (action == "find_files") out.ok = do_find_files(it, out);
    else if (action == "delete_path") out.ok = do_delete_path(it, out);
    else if (action == "move_path") out.ok = do_move_or_copy(it, out, true);
    else if (action == "copy_path") out.ok = do_move_or_copy(it, out, false);
    else if (action == "screenshot") out.ok = do_screenshot(it, out);
    else if (action == "set_wallpaper") out.ok = do_set_wallpaper(it, out);
    else if (action == "volume") out.ok = do_volume(it, out);
    else if (action == "power") out.ok = do_power(it, out);
    else if (action == "show_desktop") out.ok = do_show_desktop(it, out);
    else if (action == "send_keys") out.ok = do_send_keys(it, out);
    else if (action == "type_text") out.ok = do_type_text(it, out);
    else if (action == "focus_window") out.ok = do_focus_window(it, out);
    else if (action == "kill_process") out.ok = do_kill_process(it, out);
    else if (action == "run_command") out.ok = do_run_command(it, out);
    else if (action == "settings_page") out.ok = do_settings_page(it, out);
    else if (action == "compound") out.ok = do_compound(it, out);
    else {
        out.handled = false;
        out.needs_llm = true;
        out.llm_prompt = std::string(it.raw.view());
        out.message = "Нет быстрого обработчика для «" + action + "»";
        out.error = "no handler";
        return out;
    }

    if (!out.message.empty())
        emit(Event{"observation", action, out.ok ? "success" : "failed", out.message,
                   out.data_json, out.total_ms, now_ms()});
    return out;
}

// ---------------------------------------------------------------------------
//  Приложения
// ---------------------------------------------------------------------------
bool AgentRuntime::do_launch_app(const Intent& it, FastOutcome& out) {
    const std::string target = it.slot(SlotId::Target).str();
    AppInfo* app = nullptr;
    AppRegistry::Lookup hit = apps_.find(target);
    if (hit.app && hit.score >= 0.72f) app = hit.app;

    const double t0 = now_us();
    std::vector<Method> available;
    if (app) {
        if (!app->path.empty() && platform_->file_exists(app->path)) available.push_back(Method::CachedExe);
        if (!app->protocol.empty() || app->kind == "settings" || app->kind == "folder")
            available.push_back(Method::WinApi);
        if (!app->appid.empty()) available.push_back(Method::Shell);
        if (!app->path.empty()) available.push_back(Method::Cli);
        available.push_back(Method::Shell);
    } else {
        available.push_back(Method::Shell);
    }

    const std::vector<Method> order = optimizer_.order("launch_app", available);
    std::string last_error = "приложение не найдено";
    // Бюджет на весь перебор: попытка не должна съедать полный таймаут (ТЗ §28).
    const double budget_ms = double(cfg_.launch_timeout_ms);
    // Реальный запуск подтверждается за сотни миллисекунд: не ждём секундами.
    // Процесс — основной сигнал; окно проверяем коротким «добором».
    const double probe_ms = std::min(900.0, budget_ms);
    const double window_probe_ms = std::min(400.0, budget_ms);
    // Число попыток ограничено max_retries: перебор способов — не бесконечный цикл.
    const size_t attempt_cap = static_cast<size_t>(std::max(1, cfg_.max_retries) + 1);
    for (size_t attempt_index = 0; attempt_index < order.size() && attempt_index < attempt_cap;
         ++attempt_index) {
        const Method m = order[attempt_index];
        const double spent_ms = (now_us() - t0) / 1000.0;
        if (spent_ms >= budget_ms) {
            last_error = "превышен бюджет запуска (" + std::to_string(int(budget_ms)) + " мс)";
            break;
        }
        double attempt_ms = 0.0;
        const double at0 = now_us();
        bool ok = false;
        std::string detail;
        if (m == Method::CachedExe && app) {
            LaunchResult lr = platform_->spawn_detached(app->path, app->args);
            ok = lr.ok;
            detail = lr.error.empty() ? lr.message : lr.error;
        } else if (m == Method::WinApi && app) {
            if (!app->protocol.empty()) {
                ok = platform_->open_uri(app->protocol);
            } else if (app->kind == "settings") {
                ok = platform_->open_settings(app->args.empty() ? std::string() : app->args);
            } else if (app->kind == "folder") {
                ok = platform_->open_path(platform_->env("USERPROFILE"));
            }
            detail = ok ? "протокол/системный вызов" : "системный вызов не сработал";
        } else if (m == Method::Shell && app && !app->appid.empty()) {
            ok = platform_->open_uri("shell:AppsFolder\\" + app->appid);
            detail = ok ? "Store-приложение" : "Store-приложение не открылось";
        } else if ((m == Method::Cli || m == Method::Shell) && app) {
            for (const std::string& exe : app->exe) {
                const std::string resolved = platform_->resolve_command(exe);
                if (!resolved.empty()) {
                    LaunchResult lr = platform_->spawn_detached(resolved, app->args);
                    ok = lr.ok;
                    detail = lr.error.empty() ? lr.message : lr.error;
                    if (ok && app->path.empty()) apps_.set_path(app->key, resolved);
                    break;
                }
            }
            if (!ok && detail.empty()) detail = "команда не найдена в PATH";
        } else if (m == Method::Shell && !app) {
            // неизвестное имя: пробуем как путь или как команду оболочки
            const std::string resolved = platform_->resolve_command(target);
            if (!resolved.empty()) {
                LaunchResult lr = platform_->spawn_detached(resolved);
                ok = lr.ok;
                detail = lr.error.empty() ? lr.message : lr.error;
            } else {
                detail = "не найдено ни в реестре, ни в PATH";
            }
        }
        attempt_ms = (now_us() - at0) / 1000.0;
        note_result("launch_app", ok, attempt_ms, m);
        if (ok) {
            // Проверка результата (ТЗ §18).
            bool verified = true;
            if (app) {
                std::string vdetail;
                ActionSpec probe;
                probe.timeout_ms = int(probe_ms);
                probe.verify = VerifyKind::ProcessStarted;
                probe.verify_path = app->exe.empty() ? app->key : app->exe.front();
                verified = verify_action(probe, vdetail);
                // Окно «добираем» только у системных вызовов (протокол/Store): у обычных
                // процессов сигналом запуска служит сам факт процесса в списке.
                if (!verified && (m == Method::WinApi || m == Method::Shell || m == Method::Shortcut)) {
                    probe.verify = VerifyKind::WindowCreated;
                    probe.verify_path = app->display_name;
                    probe.timeout_ms = int(window_probe_ms);
                    verified = verify_action(probe, vdetail);
                }
                apps_.note_use(app->key, verified, attempt_ms, m);
                if (verified) apps_.learn_alias(target, app->key);
            }
            if (app && !verified) {
                last_error = "запуск не подтвердился";
                emit(Event{"fallback", "launch_application", "failed", last_error, "",
                           attempt_ms, now_ms()});
                continue;                       // пробуем следующий способ
            }
            out.message = app ? ("Открыл " + app->display_name)
                              : ("Запустил " + target);
            out.data_json = "{\"method\":\"" + std::string(to_string(m)) + "\",\"app\":\"" +
                            (app ? app->key : target) + "\",\"verified\":" +
                            (verified ? "true" : "false") + "}";
            out.total_ms = (now_us() - t0) / 1000.0;
            return true;
        }
        last_error = detail;
        // Неудачная попытка не тратит время на ожидание: сразу следующий способ.
        emit(Event{"fallback", "launch_application", "failed", detail, "", attempt_ms, now_ms()});
    }
    out.ok = false;
    out.error = last_error;
    out.message = "Не удалось открыть «" + target + "»: " + last_error;
    out.data_json = "{\"app\":\"" + target + "\"}";
    out.total_ms = (now_us() - t0) / 1000.0;
    return false;
}

// ---------------------------------------------------------------------------
//  Ссылки и поиск
// ---------------------------------------------------------------------------
// Переключение на уже открытое окно: ищем по заголовку и процессу, при отсутствии —
// поднимаем приложение и ждём окно. Никакого слепого Alt+Tab.
bool AgentRuntime::do_focus_window(const Intent& it, FastOutcome& out) {
    const double t0 = now_us();
    const std::string target = it.slot(SlotId::Target).str();
    if (target.empty()) {
        out.message = "Не понял, на что переключаться";
        out.error = "не указано окно";
        return false;
    }
    AppRegistry::Lookup hit = apps_.find(target);
    std::vector<std::string> titles{target};
    std::string process;
    if (hit.app) {
        if (!hit.app->display_name.empty()) titles.push_back(hit.app->display_name);
        if (!hit.app->exe.empty()) process = hit.app->exe.front();
        if (!hit.app->key.empty()) titles.push_back(hit.app->key);
    }
    auto find_any = [&]() -> std::optional<WindowInfo> {
        for (const std::string& t : titles) {
            if (t.empty()) continue;
            if (std::optional<WindowInfo> w = platform_->find_window(t)) return w;
        }
        if (!process.empty()) {
            for (const WindowInfo& w : platform_->windows()) {
                if (w.process.find(process) != std::string::npos) return w;
            }
        }
        return std::nullopt;
    };

    std::optional<WindowInfo> win = find_any();
    bool launched = false;
    if (!win) {
        // Приложение не запущено: поднимаем и ждём появления окна (состояние, а не sleep).
        Intent launch = it;
        launch.action = "launch_app";
        FastOutcome lout;
        launched = do_launch_app(launch, lout);
        if (launched) {
            const WaitResult wr = wait_->wait_condition(
                [&]() { return find_any().has_value(); }, cfg_.window_timeout_ms,
                "окно «" + target + "»");
            if (wr.ok) win = find_any();
        } else if (!lout.error.empty()) {
            out.error = lout.error;
        }
    }

    bool ok = false;
    if (win) {
        ok = platform_->activate_window(win->handle);
        if (ok) {
            const std::string title = win->title.empty() ? target : win->title;
            wait_->wait_window_active(title, cfg_.window_timeout_ms / 2);
        }
    }
    const double ms = (now_us() - t0) / 1000.0;
    note_result("focus_window", ok, ms, Method::WinApi);
    out.total_ms = ms;
    out.ok = ok;
    out.message = ok ? ("Переключился на «" + std::string(win && !win->title.empty() ? win->title : target) + "»")
                     : (launched ? ("Запустил, но окно «" + target + "» не появилось")
                                 : ("Окно «" + target + "» не найдено"));
    if (!ok && !launched) out.error = "окно не найдено";
    out.data_json = std::string("{\"target\":\"") + json_escape(target) + "\",\"launched\":" +
                    (launched ? "true" : "false") + ",\"hwnd\":" +
                    std::to_string(win ? win->handle : 0) + ",\"pid\":" +
                    std::to_string(win ? win->pid : 0) + "}";
    return ok;
}

bool AgentRuntime::do_open_url(const Intent& it, FastOutcome& out) {
    std::string url = std::string(it.slot(SlotId::Url).view());
    if (url.empty()) url = std::string(it.slot(SlotId::Target).view());
    if (url.empty()) {
        out.error = "пустая ссылка";
        out.message = "Не понял, что открыть";
        return false;
    }
    url = normalize_url(url);
    const double t0 = now_us();
    bool ok = platform_->open_uri(url);
    double ms = (now_us() - t0) / 1000.0;
    note_result("open_url", ok, ms, Method::WinApi);
    if (!ok) {
        // Запасной путь: браузер по умолчанию как процесс.
        const std::string browser = cfg_.default_browser.empty() ? platform_->default_browser()
                                                                 : cfg_.default_browser;
        if (!browser.empty()) {
            LaunchResult lr = platform_->spawn_detached(browser, url);
            ok = lr.ok;
            ms = (now_us() - t0) / 1000.0;
            note_result("open_url", ok, ms, Method::Shell);
        }
    }
    out.message = ok ? ("Открыл: " + url) : ("Не удалось открыть: " + url);
    out.error = ok ? "" : "оболочка не открыла ссылку";
    out.data_json = "{\"url\":\"" + json_escape(url) + "\",\"ok\":" + (ok ? "true" : "false") + "}";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_web_search(const Intent& it, FastOutcome& out, bool youtube) {
    const std::string query = std::string(it.slot(SlotId::Query).view());
    const std::string url = search_url_for(query, youtube ? "youtube" : "google");
    Intent copy = it;
    copy.set(SlotId::Url, url);
    copy.action = "open_url";
    const bool ok = do_open_url(copy, out);
    out.message = ok ? (youtube ? ("Открыл поиск видео: " + query) : ("Открыл поиск: " + query))
                     : out.message;
    out.data_json = "{\"url\":\"" + json_escape(url) + "\",\"query\":\"" + json_escape(query) + "\"}";
    return ok;
}

// ---------------------------------------------------------------------------
//  Файлы
// ---------------------------------------------------------------------------
bool AgentRuntime::do_open_folder(const Intent& it, FastOutcome& out) {
    const std::string target = it.slot(SlotId::Target).str();
    const std::string place = it.slot(SlotId::Place).str();
    bool found = false;
    const std::string path = resolve_target_path(target, place, true, found);
    const double t0 = now_us();
    bool ok = false;
    if (!path.empty() && platform_->file_exists(path)) {
        ok = platform_->open_path(path);
    } else if (!place.empty()) {
        const std::string base = resolve_place(place);
        if (!base.empty()) ok = platform_->open_path(base);
    }
    const double ms = (now_us() - t0) / 1000.0;
    note_result("open_folder", ok, ms, Method::WinApi);
    out.message = ok ? ("Открыл: " + path) : ("Не нашёл: " + (target.empty() ? place : target));
    out.error = ok ? "" : "объект не найден";
    out.data_json = "{\"path\":\"" + json_escape(path) + "\",\"place\":\"" + json_escape(place) + "\"}";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_create_folder(const Intent& it, FastOutcome& out) {
    bool found = false;
    const std::string path = resolve_target_path(it.slot(SlotId::Target).view(),
                                                 it.slot(SlotId::Place).view(), false, found);
    const double t0 = now_us();
    const bool created = platform_->mkdir(path, true);
    const bool exists = platform_->is_dir(path);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("file_op", created && exists, ms, Method::WinApi);
    out.ok = created && exists;
    out.message = out.ok ? ("Создал папку: " + path) : ("Не удалось создать папку: " + path);
    out.error = out.ok ? "" : "файловая система отказала";
    out.data_json = "{\"path\":\"" + json_escape(path) + "\",\"verified\":" +
                    (exists ? "true" : "false") + "}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_create_file(const Intent& it, FastOutcome& out) {
    bool found = false;
    const std::string path = resolve_target_path(it.slot(SlotId::Target).view(),
                                                 it.slot(SlotId::Place).view(), false, found);
    const std::string content = std::string(it.slot(SlotId::Content).view());
    const double t0 = now_us();
    const bool written = platform_->write_file(path, content, false);
    const bool exists = platform_->file_exists(path);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("file_op", written && exists, ms, Method::WinApi);
    out.ok = written && exists;
    out.message = out.ok ? ("Создал файл: " + path + (content.empty() ? "" : " (" +
                                                      std::to_string(content.size()) + " симв.)"))
                         : ("Не удалось записать файл: " + path);
    out.error = out.ok ? "" : "запись не удалась";
    out.data_json = "{\"path\":\"" + json_escape(path) + "\"}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_read_file(const Intent& it, FastOutcome& out) {
    bool found = false;
    const std::string path = resolve_target_path(it.slot(SlotId::Target).view(), {}, true, found);
    if (!found) {
        out.message = "Файл не найден: " + path;
        out.error = "not found";
        return false;
    }
    const double t0 = now_us();
    const std::string text = platform_->read_file(path, 20000);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("file_op", !text.empty(), ms, Method::WinApi);
    out.ok = !text.empty();
    out.message = text.substr(0, 4000);
    out.error = out.ok ? "" : "файл пуст или недоступен";
    out.data_json = "{\"path\":\"" + json_escape(path) + "\",\"bytes\":" +
                    std::to_string(text.size()) + "}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_find_files(const Intent& it, FastOutcome& out) {
    const std::string pattern = it.slot(SlotId::Target).str();
    const std::string place = it.slot(SlotId::Place).str();
    std::string root = resolve_place(place);
    if (root.empty()) root = platform_->cwd();
    const double t0 = now_us();
    const std::vector<FileEntry> hits = platform_->search_files(root, pattern, 40);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("file_op", true, ms, Method::WinApi);
    std::string text = "Нашёл " + std::to_string(hits.size()) + " объектов по «" + pattern + "»";
    std::string items = "[";
    for (size_t i = 0; i < hits.size(); ++i) {
        if (i) items += ",";
        items += "{\"path\":\"" + json_escape(hits[i].path) + "\",\"dir\":" +
                 (hits[i].is_dir ? "true" : "false") + "}";
        if (i < 10) text += "\n- " + hits[i].path;
    }
    items += "]";
    out.ok = true;
    out.message = text;
    out.data_json = "{\"root\":\"" + json_escape(root) + "\",\"items\":" + items + "}";
    out.total_ms = ms;
    return true;
}

bool AgentRuntime::do_delete_path(const Intent& it, FastOutcome& out) {
    const std::string target = it.slot(SlotId::Target).str();
    const std::string place = it.slot(SlotId::Place).str();
    bool found = false;
    const std::string path = resolve_target_path(target, place, true, found);
    if (!found) {
        out.message = "Не нашёл: " + (target.empty() ? place : target);
        out.error = "объект не существует";
        return false;
    }
    const bool is_dir = platform_->is_dir(path);
    const int approx = is_dir ? count_items(*platform_, path) : 1;
    Intent copy = it;
    copy.set(SlotId::Target, path);
    std::string reason;
    const ToolSpec* tool = tools_.find("delete_path");
    if (tool && needs_confirmation(*tool, copy, reason)) {
        std::string ask = "Удалить " + std::string(is_dir ? "папку" : "файл") + " «" + path +
                          "»" + (approx > 1 ? (" (~" + std::to_string(approx) + " объектов)") : "") +
                          "? Причина: " + reason;
        std::string deny;
        if (!ask_user(ask, deny)) {
            out.ok = false;
            out.message = "Удаление отменено (нужно подтверждение).";
            out.error = deny;
            return false;
        }
    }
    const double t0 = now_us();
    const bool removed = platform_->remove_path(path, is_dir, true);
    WaitResult gone = wait_ ? wait_->wait_file_gone(path, cfg_.file_timeout_ms)
                            : WaitResult{removed, 0, 0, ""};
    const double ms = (now_us() - t0) / 1000.0;
    note_result("delete_op", removed && gone.ok, ms, Method::WinApi);
    out.ok = removed && gone.ok;
    out.message = out.ok ? ("Удалил " + std::string(is_dir ? "папку: " : "файл: ") + path)
                         : ("Не удалось удалить: " + path);
    out.error = out.ok ? "" : "объект остался на месте";
    out.data_json = "{\"path\":\"" + json_escape(path) + "\",\"verified\":" +
                    (gone.ok ? "true" : "false") + "}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_move_or_copy(const Intent& it, FastOutcome& out, bool move) {
    bool found_src = false, found_dst = false;
    const std::string src = resolve_target_path(it.slot(SlotId::Target).view(), {}, true, found_src);
    const std::string dst = resolve_target_path(it.slot(SlotId::Args).view(),
                                               it.slot(SlotId::Place).view(), false, found_dst);
    if (!found_src || src.empty() || dst.empty()) {
        out.message = "Не понял источник или назначение";
        out.error = "нужны оба пути";
        return false;
    }
    const double t0 = now_us();
    const bool ok = move ? platform_->move_path(src, dst) : platform_->copy_path(src, dst);
    const bool exists = platform_->file_exists(dst);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("file_op", ok && exists, ms, Method::WinApi);
    out.ok = ok && exists;
    out.message = out.ok ? ((move ? "Переместил: " : "Скопировал: ") + src + " → " + dst)
                         : ("Не удалось " + std::string(move ? "переместить: " : "скопировать: ") + src);
    out.error = out.ok ? "" : "операция не выполнена";
    out.data_json = "{\"src\":\"" + json_escape(src) + "\",\"dst\":\"" + json_escape(dst) + "\"}";
    out.total_ms = ms;
    return out.ok;
}

// ---------------------------------------------------------------------------
//  Система: экран, обои, звук, питание, клавиши
// ---------------------------------------------------------------------------
bool AgentRuntime::do_screenshot(const Intent& it, FastOutcome& out) {
    const double t0 = now_us();
    Frame f = platform_->capture(0, nullptr);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("screenshot", f.width > 0, ms, Method::WinApi);
    out.ok = f.width > 0;
    out.message = "Снимок экрана: " + std::to_string(f.width) + "x" + std::to_string(f.height) +
                  " (" + f.backend + (f.headless ? ", без дисплея" : "") + ")";
    out.data_json = "{\"width\":" + std::to_string(f.width) + ",\"height\":" +
                    std::to_string(f.height) + ",\"backend\":\"" + f.backend +
                    "\",\"headless\":" + (f.headless ? "true" : "false") + "}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_set_wallpaper(const Intent& it, FastOutcome& out) {
    std::string path = it.slot(SlotId::Target).str();
    if (path.empty()) {
        out.message = "Не понял, какие обои поставить";
        out.error = "нет картинки";
        return false;
    }
    const double t0 = now_us();
    const bool ok = platform_->set_wallpaper(path);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("wallpaper", ok, ms, Method::WinApi);
    out.ok = ok;
    out.message = ok ? ("Поставил обои: " + path) : ("Не удалось поставить обои (нет картинки): " + path);
    out.error = ok ? "" : "картинка не найдена";
    out.data_json = "{\"path\":\"" + json_escape(path) + "\"}";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_volume(const Intent& it, FastOutcome& out) {
    const std::string action = it.slot(SlotId::Args).str();
    const std::string value = it.slot(SlotId::Value).str();
    const double t0 = now_us();
    bool ok = false;
    if (action == "set" && !value.empty()) {
        ok = platform_->set_volume(std::atoi(value.c_str()));
    } else if (action == "mute") {
        ok = platform_->set_volume(0);
    } else if (action == "up") {
        ok = platform_->hotkey("volumeup");
    } else if (action == "down") {
        ok = platform_->hotkey("volumedown");
    } else {
        const int v = platform_->get_volume();
        ok = v >= 0;
        out.message = ok ? ("Громкость: " + std::to_string(v) + "%") : "Не удалось узнать громкость";
    }
    const double ms = (now_us() - t0) / 1000.0;
    note_result("volume", ok, ms, Method::WinApi);
    out.ok = ok;
    if (out.message.empty())
        out.message = ok ? ("Громкость: " + (action == "set" ? value + "%" : action)) : "Не удалось";
    out.error = ok ? "" : "управление звуком недоступно";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_power(const Intent& it, FastOutcome& out) {
    const std::string action = it.slot(SlotId::Args).str();
    const std::string label = action == "shutdown"      ? "выключить компьютер"
                              : action == "restart"     ? "перезагрузить компьютер"
                              : action == "lock"        ? "заблокировать экран"
                              : action == "sleep"       ? "перевести в спящий режим"
                              : action == "logoff"      ? "завершить сеанс"
                              : action == "monitor-off" ? "выключить монитор"
                                                        : action;
    Intent copy = it;
    copy.set(SlotId::Target, label);
    std::string reason;
    const ToolSpec* tool = tools_.find("system_power");
    if (tool && needs_confirmation(*tool, copy, reason)) {
        std::string deny;
        if (!ask_user("Подтвердите: " + label + "?", deny)) {
            out.message = "Действие отменено (нужно подтверждение).";
            out.error = deny;
            return false;
        }
    }
    const double t0 = now_us();
    bool ok = false;
    if (action == "shutdown") ok = platform_->run_command("shutdown /s /t 0", "", 5000).started ||
                                   platform_->run_command("systemctl poweroff", "", 5000).started;
    else if (action == "restart") ok = platform_->run_command("shutdown /r /t 0", "", 5000).started ||
                                      platform_->run_command("systemctl reboot", "", 5000).started;
    else if (action == "lock") ok = platform_->run_command("rundll32.exe user32.dll,LockWorkStation", "", 5000).started ||
                                   platform_->hotkey("win+l");
    else if (action == "sleep") ok = platform_->run_command("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", "", 5000).started ||
                                   platform_->run_command("systemctl suspend", "", 5000).started;
    else if (action == "logoff") ok = platform_->run_command("logoff", "", 5000).started;
    else if (action == "monitor-off") ok = platform_->hotkey("monitor-off");
    const double ms = (now_us() - t0) / 1000.0;
    note_result("power", ok, ms, Method::WinApi);
    out.ok = ok;
    out.message = ok ? ("Выполняю: " + label) : ("Не удалось: " + label);
    out.error = ok ? "" : "системная команда не поддерживается";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_show_desktop(const Intent& it, FastOutcome& out) {
    (void)it;
    (void)kConfirmHint;
    const double t0 = now_us();
    const bool ok = platform_->hotkey("win+d");
    const double ms = (now_us() - t0) / 1000.0;
    note_result("show_desktop", ok, ms, Method::WinApi);
    out.ok = ok;
    out.message = ok ? "Показал рабочий стол" : "Не удалось показать рабочий стол";
    out.error = ok ? "" : "нет доступа к SendInput";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_send_keys(const Intent& it, FastOutcome& out) {
    std::string keys = it.slot(SlotId::Key).str();
    if (keys.empty()) keys = it.slot(SlotId::Target).str();
    if (keys.empty()) {
        out.message = "Не понял сочетание клавиш";
        out.error = "нет клавиш";
        return false;
    }
    const double t0 = now_us();
    const bool ok = keys.find('+') != std::string::npos ? platform_->hotkey(keys)
                                                        : platform_->key_press(keys);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("press_key", ok, ms, Method::WinApi);
    out.ok = ok;
    out.message = ok ? ("Нажал " + keys) : ("Не удалось нажать " + keys);
    out.error = ok ? "" : "SendInput недоступен";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_type_text(const Intent& it, FastOutcome& out) {
    const std::string text = it.slot(SlotId::Content).str();
    if (text.empty()) {
        out.message = "Нечего печатать";
        out.error = "пустой текст";
        return false;
    }
    const double t0 = now_us();
    const bool ok = platform_->type_text(text);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("type_text", ok, ms, Method::WinApi);
    out.ok = ok;
    out.message = ok ? ("Ввёл текст (" + std::to_string(text.size()) + " симв.)")
                     : "Не удалось ввести текст";
    out.error = ok ? "" : "ввод недоступен";
    out.total_ms = ms;
    return ok;
}

bool AgentRuntime::do_kill_process(const Intent& it, FastOutcome& out) {
    const std::string target = it.slot(SlotId::Target).str();
    std::string name = target;
    AppRegistry::Lookup hit = apps_.find(target);
    if (hit.app && hit.score >= 0.72f && !hit.app->exe.empty()) name = hit.app->exe.front();
    Intent copy = it;
    copy.set(SlotId::Target, name);
    std::string reason;
    const ToolSpec* tool = tools_.find("kill_process");
    if (tool && needs_confirmation(*tool, copy, reason)) {
        std::string deny;
        if (!ask_user("Завершить процесс «" + name + "»?", deny)) {
            out.message = "Отменено (нужно подтверждение).";
            out.error = deny;
            return false;
        }
    }
    const double t0 = now_us();
    bool ok = platform_->kill_process(name, false);
    if (!ok) ok = platform_->kill_process(name, true);
    double ms = (now_us() - t0) / 1000.0;
    bool verified = false;
    if (ok && wait_) {
        verified = wait_->wait_process_finished(name, cfg_.process_timeout_ms).ok;
        ms = (now_us() - t0) / 1000.0;
    } else if (ok) {
        verified = true;
    }
    note_result("kill_process", ok && verified, ms, Method::WinApi);
    if (hit.app) apps_.note_use(hit.app->key, ok && verified, ms, Method::WinApi);
    out.ok = ok && verified;
    out.message = out.ok ? ("Закрыл " + name) : ("Не удалось закрыть " + name);
    out.error = out.ok ? "" : "процесс не найден или защищён";
    out.data_json = "{\"process\":\"" + json_escape(name) + "\",\"verified\":" +
                    (verified ? "true" : "false") + "}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_run_command(const Intent& it, FastOutcome& out) {
    const std::string command = it.slot(SlotId::Content).str();
    if (command.empty()) {
        out.message = "Пустая команда";
        out.error = "нет команды";
        return false;
    }
    Intent copy = it;
    std::string reason;
    const ToolSpec* tool = tools_.find("run_command");
    if (tool && needs_confirmation(*tool, copy, reason)) {
        std::string deny;
        if (!ask_user("Выполнить команду: " + command + "?", deny)) {
            out.message = "Команда отменена (нужно подтверждение).";
            out.error = deny;
            return false;
        }
    }
    const double t0 = now_us();
    ExecResult r = platform_->run_powershell(command, platform_->cwd(), cfg_.shell_timeout_ms);
    if (!r.started) r = platform_->run_command(command, platform_->cwd(), cfg_.shell_timeout_ms);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("run_command", r.ok(), ms, Method::WinApi);
    out.ok = r.ok();
    out.message = r.ok() ? (!r.stdout_text.empty() ? r.stdout_text.substr(0, 4000)
                                                   : "Команда выполнена без вывода")
                         : ("Команда завершилась с ошибкой: " + r.stderr_text.substr(0, 1000));
    out.error = r.ok() ? "" : (r.error.empty() ? "ненулевой код возврата" : r.error);
    out.data_json = "{\"exit_code\":" + std::to_string(r.exit_code) + ",\"ms\":" +
                    std::to_string(int(ms)) + "}";
    out.total_ms = ms;
    return out.ok;
}

bool AgentRuntime::do_settings_page(const Intent& it, FastOutcome& out) {
    std::string page = it.slot(SlotId::Args).str();
    const double t0 = now_us();
    const bool ok = platform_->open_settings(page);
    const double ms = (now_us() - t0) / 1000.0;
    note_result("settings_page", ok, ms, Method::WinApi);
    out.ok = ok;
    out.message = ok ? ("Открыл настройки: " + (page.empty() ? std::string("все") : page))
                     : "Не удалось открыть настройки";
    out.error = ok ? "" : "страница настроек недоступна";
    out.total_ms = ms;
    return ok;
}

// ---------------------------------------------------------------------------
//  Составная команда: инструменты работают пачкой, без обращений к модели
// ---------------------------------------------------------------------------
bool AgentRuntime::do_compound(const Intent& it, FastOutcome& out) {
    const double t0 = now_us();
    std::string text;
    std::string data = "[";
    bool all_ok = true;
    size_t done = 0;
    for (size_t i = 0; i < it.parts.size(); ++i) {
        if (cancel_ && cancel_()) break;
        FastOutcome sub = execute_route(Route{RouteKind::Direct, it.parts[i]});
        if (i) data += ",";
        data += sub.to_json();
        if (!sub.message.empty()) text += (text.empty() ? "" : "\n") + sub.message;
        all_ok = all_ok && sub.ok && sub.handled;
        ++done;
    }
    data += "]";
    out.ok = all_ok;
    out.message = text.empty() ? "Выполнил действия" : text;
    out.error = all_ok ? "" : "часть действий не выполнена";
    out.data_json = "{\"parts\":" + data + "}";
    out.actions = done;
    out.total_ms = (now_us() - t0) / 1000.0;
    return out.ok;
}

// ---------------------------------------------------------------------------
//  Предпросмотр (PLAN ONLY) и состояние
// ---------------------------------------------------------------------------
std::string AgentRuntime::preview(std::string_view phrase) {
    Route route = router_->route(phrase);
    std::string text = "План (ничего не выполнялось): «" + std::string(phrase) + "»\n";
    text += "Маршрут: " + std::string(to_string(route.kind)) + " — " + route.reason + "\n";
    if (route.kind != RouteKind::Direct) {
        text += "Нужна модель: план построит агентный цикл (Qwen3-VL).";
        return text;
    }
    int index = 1;
    std::function<void(const Intent&)> walk = [&](const Intent& in) {
        if (in.action.view() == "compound") {
            for (const Intent& p : in.parts) walk(p);
            return;
        }
        text += std::to_string(index++) + ". " + in.label();
        const std::string target = in.slot(SlotId::Target).str();
        const std::string place = in.slot(SlotId::Place).str();
        if (!target.empty() || !place.empty()) {
            bool found = true;
            const std::string path = resolve_target_path(target, place, false, found);
            if (!path.empty()) text += " → " + path;
        }
        const std::string url = in.slot(SlotId::Url).str();
        if (!url.empty()) text += " → " + url;
        text += "\n";
    };
    walk(route.intent);
    const ToolSpec* tool = tools_.for_action(route.intent.action.view(), Method::WinApi);
    if (tool) {
        std::string reason;
        const bool confirm = needs_confirmation(*tool, route.intent, reason);
        text += "Инструмент: " + tool->name + " (риск " + to_string(tool->risk) +
                ", способ " + to_string(tool->preferred) + ")\n";
        text += confirm ? ("Потребуется подтверждение: " + reason) : "Подтверждение не требуется";
    }
    return text;
}

std::string AgentRuntime::state_json() const {
    std::string out = "{\"platform\":\"" + std::string(platform_->name()) + "\"";
    out += ",\"cwd\":\"" + json_escape(platform_->cwd()) + "\"";
    const auto active = platform_->active_window();
    if (active) {
        out += ",\"active_window\":{\"title\":\"" + json_escape(active->title) +
               "\",\"process\":\"" + json_escape(active->process) + "\",\"pid\":" +
               std::to_string(active->pid) + "}";
    }
    const auto monitors = platform_->monitors();
    out += ",\"monitors\":[";
    for (size_t i = 0; i < monitors.size(); ++i) {
        if (i) out += ",";
        out += "{\"index\":" + std::to_string(monitors[i].index) + ",\"width\":" +
               std::to_string(monitors[i].width) + ",\"height\":" +
               std::to_string(monitors[i].height) + ",\"dpi\":" +
               std::to_string(monitors[i].dpi_scale) + "}";
    }
    out += "]";
    const auto procs = platform_->processes();
    out += ",\"process_count\":" + std::to_string(procs.size());
    out += ",\"has_display\":" + std::string(platform_->has_display() ? "true" : "false");
    out += ",\"metrics\":" + metrics_json();
    out += "}";
    return out;
}

std::string AgentRuntime::tools_json() const { return tools_.schemas_json({}); }

}  // namespace agent
