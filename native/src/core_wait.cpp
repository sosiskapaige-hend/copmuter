// WaitManager: ожидание состояния, а не sleep (ТЗ §17).
//
// Шаг опроса адаптивный: 2 мс → 25 мс → 100 мс → 250 мс. Первые миллисекунды
// проверяем часто (процессы/окна появляются мгновенно), затем реже — чтобы не жечь CPU.
#include "agent/platform.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <thread>

#include "agent/util.h"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace fs = std::filesystem;

namespace agent {
namespace {

int64_t mono_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

std::string lower_ascii(std::string_view in) {
    std::string out(in);
    for (char& c : out)
        if (c >= 'A' && c <= 'Z') c = char(c - 'A' + 'a');
    return out;
}

uint64_t file_size_of(const std::string& path) {
    std::error_code ec;
    const fs::path p(path);
    if (!fs::exists(p, ec)) return 0;
    if (fs::is_directory(p, ec)) return 1;
    return uint64_t(fs::file_size(p, ec));
}

int64_t file_mtime_ms(const std::string& path) {
    std::error_code ec;
    const fs::path p(path);
    if (!fs::exists(p, ec)) return 0;
    const fs::file_time_type t = fs::last_write_time(p, ec);
    if (ec) return 0;
    const auto sys = std::chrono::time_point_cast<std::chrono::milliseconds>(
        t - fs::file_time_type::clock::now() + std::chrono::system_clock::now());
    return int64_t(sys.time_since_epoch().count());
}

bool port_open(const std::string& host, int port) {
    if (host.empty() || port <= 0) return false;
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    const std::string service = std::to_string(port);
    if (getaddrinfo(host.c_str(), service.c_str(), &hints, &res) != 0) return false;
    bool ok = false;
    for (addrinfo* ai = res; ai && !ok; ai = ai->ai_next) {
#ifdef _WIN32
        SOCKET s = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (s == INVALID_SOCKET) continue;
#else
        int s = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (s < 0) continue;
#endif
        ok = (::connect(s, ai->ai_addr, int(ai->ai_addrlen)) == 0);
#ifdef _WIN32
        ::closesocket(s);
#else
        ::close(s);
#endif
    }
    freeaddrinfo(res);
    return ok;
}

}  // namespace

int WaitManager::next_interval_us(int current_us) {
    if (current_us < 2000) return 2000;
    if (current_us < 25000) return 25000;
    if (current_us < 100000) return 100000;
    return 250000;
}

WaitResult WaitManager::wait_condition(const std::function<bool()>& predicate, int timeout_ms,
                                       const std::string& description) {
    WaitResult r;
    if (!predicate) {
        r.detail = "нет условия ожидания";
        return r;
    }
    const int64_t t0 = mono_ms();
    int interval = 2000;
    if (timeout_ms <= 0) timeout_ms = default_timeout_ms_;
    while (true) {
        ++r.polls;
        bool ok = false;
        try {
            ok = predicate();
        } catch (...) {
            ok = false;
        }
        if (ok) {
            r.ok = true;
            r.detail = description.empty() ? "готово" : description;
            break;
        }
        if (cancel_ && cancel_()) {
            r.detail = "отменено пользователем";
            break;
        }
        if (mono_ms() - t0 >= timeout_ms) {
            r.detail = description.empty() ? "таймаут ожидания" : ("таймаут: " + description);
            break;
        }
        std::this_thread::sleep_for(std::chrono::microseconds(interval));
        interval = next_interval_us(interval);
    }
    r.elapsed_ms = double(mono_ms() - t0);
    return r;
}

WaitResult WaitManager::wait_process_started(std::string_view name, int timeout_ms) {
    if (platform_->process_running(name))
        return WaitResult{true, 0.0, 1, "процесс уже запущен: " + std::string(name)};
    return wait_condition([&] { return platform_->process_running(name); }, timeout_ms,
                          "запуск процесса " + std::string(name));
}

WaitResult WaitManager::wait_process_finished(std::string_view name, int timeout_ms) {
    return wait_condition([&] { return !platform_->process_running(name); }, timeout_ms,
                          "завершение процесса " + std::string(name));
}

WaitResult WaitManager::wait_window_created(std::string_view title, int timeout_ms) {
    return wait_condition([&] { return platform_->find_window(title).has_value(); }, timeout_ms,
                          "появление окна " + std::string(title));
}

WaitResult WaitManager::wait_window_active(std::string_view title, int timeout_ms) {
    const std::string want = lower_ascii(title);
    return wait_condition(
        [&] {
            const std::optional<WindowInfo> w = platform_->active_window();
            if (!w) return false;
            if (want.empty()) return true;
            return lower_ascii(w->title).find(want) != std::string::npos ||
                   lower_ascii(w->process).find(want) != std::string::npos;
        },
        timeout_ms, "активное окно " + std::string(title));
}

WaitResult WaitManager::wait_file_exists(std::string_view path, int timeout_ms, uint64_t min_size,
                                         double stable_ms) {
    const std::string p(path);
    const int64_t t0 = mono_ms();
    uint64_t last_size = 0;
    int64_t last_change = t0;
    return wait_condition(
        [&] {
            if (!platform_->file_exists(p)) return false;
            const uint64_t size = file_size_of(p);
            if (size < min_size) return false;
            const int64_t now = mono_ms();
            // stable_ms: файл дописан и больше не меняется (не читаем полупустой файл)
            if (size != last_size) {
                last_size = size;
                last_change = now;
            }
            if (stable_ms > 0.0 && double(now - last_change) < stable_ms) return false;
            return true;
        },
        timeout_ms, "появление файла " + p);
}

WaitResult WaitManager::wait_file_gone(std::string_view path, int timeout_ms) {
    return wait_condition([&] { return !platform_->file_exists(path); }, timeout_ms,
                          "исчезновение " + std::string(path));
}

WaitResult WaitManager::wait_file_changed(std::string_view path, int64_t since_ms, int timeout_ms) {
    const std::string p(path);
    int64_t reference = file_mtime_ms(p);
    if (since_ms > 0 && since_ms > reference) reference = since_ms;
    return wait_condition([&] { return file_mtime_ms(p) > reference; }, timeout_ms,
                          "изменение файла " + p);
}

WaitResult WaitManager::wait_port_open(std::string_view host_port, int timeout_ms) {
    const std::string hp(host_port);
    const size_t colon = hp.rfind(':');
    if (colon == std::string::npos)
        return WaitResult{false, 0.0, 0, "нет порта в «" + hp + "»"};
    std::string host = hp.substr(0, colon);
    if (host.empty() || host == "localhost") host = "127.0.0.1";
    const int port = std::atoi(hp.c_str() + colon + 1);
    return wait_condition([&] { return port_open(host, port); }, timeout_ms, "порт " + hp);
}

WaitResult WaitManager::wait_url_loaded(std::string_view url, std::string_view title, int timeout_ms) {
    (void)url;
    if (!title.empty()) {
        const WaitResult w = wait_window_created(title, timeout_ms);
        if (w.ok) return w;
    }
    return wait_condition(
        [&] {
            for (const WindowInfo& w : platform_->windows()) {
                const std::string t = lower_ascii(w.title);
                const std::string pr = lower_ascii(w.process);
                if (t.find(".com") != std::string::npos) return true;
                if (pr.find("chrome") != std::string::npos || pr.find("msedge") != std::string::npos ||
                    pr.find("firefox") != std::string::npos || pr.find("browser") != std::string::npos ||
                    pr.find("yandex") != std::string::npos)
                    return true;
            }
            return false;
        },
        timeout_ms, "загрузка " + std::string(url));
}

}  // namespace agent
