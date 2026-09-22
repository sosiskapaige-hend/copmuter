// Канал C++ ↔ Python worker на POSIX (Linux/macOS): AF_UNIX + framed JSON.
// Используется в dev-режиме и в тестах — тот же протокол, что и Named Pipes на Windows.
#include "agent/ipc.h"

#ifndef _WIN32

#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <functional>
#include <cstring>
#include <mutex>
#include <thread>

#include "agent/core.h"

namespace agent {
namespace {

int64_t mono_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

uint32_t be32(const char* p) {
    return (uint32_t(uint8_t(p[0])) << 24) | (uint32_t(uint8_t(p[1])) << 16) |
           (uint32_t(uint8_t(p[2])) << 8) | uint32_t(uint8_t(p[3]));
}

void put_be32(char* p, uint32_t v) {
    p[0] = char(v >> 24);
    p[1] = char((v >> 16) & 0xFF);
    p[2] = char((v >> 8) & 0xFF);
    p[3] = char(v & 0xFF);
}

}  // namespace

struct AiLink::Impl {
    int fd = -1;
    std::mutex mu;

    bool write_all(const char* data, size_t size) {
        size_t sent = 0;
        while (sent < size) {
            const ssize_t n = ::send(fd, data + sent, size - sent, MSG_NOSIGNAL);
            if (n <= 0) {
                if (errno == EINTR) continue;
                return false;
            }
            sent += size_t(n);
        }
        return true;
    }

    // cancelled выставляется, если сработал опрос «Стоп».
    bool cancelled = false;

    bool read_all(char* data, size_t size, int timeout_ms,
                  const std::function<bool()>& cancel = {}) {
        size_t got = 0;
        const int64_t deadline = mono_ms() + (timeout_ms > 0 ? timeout_ms : 30000);
        while (got < size) {
            // Ждём короткими шагами: так «Стоп» замечается за десятки миллисекунд,
            // а не только между запросами к модели.
            const int slice = cancel ? 50 : 1000000;
            const int left = int(deadline - mono_ms());
            if (left <= 0) return false;
            pollfd pfd{fd, POLLIN, 0};
            const int pr = ::poll(&pfd, 1, std::min(left, slice));
            if (pr == 0) {
                if (cancel && cancel()) {
                    cancelled = true;
                    return false;
                }
                continue;                       // просто истёк срез ожидания
            }
            if (pr < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            const ssize_t n = ::recv(fd, data + got, size - got, 0);
            if (n <= 0) {
                if (n < 0 && (errno == EINTR)) continue;
                return false;
            }
            got += size_t(n);
        }
        return true;
    }
};

AiLink::AiLink() : impl_(std::make_unique<Impl>()) {}
AiLink::~AiLink() { close(); }

bool AiLink::connect(const std::string& address, int timeout_ms, std::string& error) {
    close();
    address_ = address;
    const int64_t deadline = mono_ms() + (timeout_ms > 0 ? timeout_ms : 5000);
    int last_errno = 0;
    while (true) {
        const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) {
            error = "socket(): " + std::string(std::strerror(errno));
            return false;
        }
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        if (address.size() >= sizeof(addr.sun_path)) {
            ::close(fd);
            error = "адрес сокета слишком длинный";
            return false;
        }
        std::strncpy(addr.sun_path, address.c_str(), sizeof(addr.sun_path) - 1);
        if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0) {
            impl_->fd = fd;
            ++reconnects_;
            return true;
        }
        last_errno = errno;
        ::close(fd);
        if (mono_ms() >= deadline) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    error = "воркер не отвечает на " + address + ": " + std::strerror(last_errno);
    return false;
}

bool AiLink::connected() const {
    if (!impl_ || impl_->fd < 0) return false;
    pollfd pfd{impl_->fd, POLLIN, 0};
    const int pr = ::poll(&pfd, 1, 0);
    if (pr < 0) return false;
    if (pr == 0) return true;                       // жив, данных нет
    if (pfd.revents & (POLLHUP | POLLERR)) return false;
    return true;
}

void AiLink::close() {
    if (!impl_) return;
    if (impl_->fd >= 0) {
        ::shutdown(impl_->fd, SHUT_RDWR);
        ::close(impl_->fd);
        impl_->fd = -1;
    }
}

bool AiLink::ensure_connected(std::string& error) {
    if (connected()) return true;
    if (address_.empty()) {
        error = "канал не настроен";
        return false;
    }
    return connect(address_, 3000, error);
}

AiReply AiLink::request(const std::string& json, int timeout_ms,
                        const std::function<bool()>& cancel) {
    AiReply reply;
    if (!impl_) {
        reply.error = "канал не инициализирован";
        return reply;
    }
    std::lock_guard<std::mutex> lock(impl_->mu);
    if (impl_->fd < 0) {
        reply.error = "нет соединения с воркером";
        return reply;
    }
    const int64_t t0 = mono_ms();
    char header[4];
    put_be32(header, uint32_t(json.size()));
    if (!impl_->write_all(header, 4) || !impl_->write_all(json.data(), json.size())) {
        reply.error = "не удалось отправить запрос воркеру";
        close();
        return reply;
    }
    impl_->cancelled = false;
    char len_buf[4];
    if (!impl_->read_all(len_buf, 4, timeout_ms, cancel)) {
        reply.ms = double(mono_ms() - t0);
        if (impl_->cancelled) {
            reply.cancelled = true;
            reply.error = "остановлено пользователем";
        } else {
            reply.error = "воркер не ответил вовремя";
        }
        close();                                // ответ в пути не должен достаться следующему запросу
        return reply;
    }
    const uint32_t size = be32(len_buf);
    if (size > (64u << 20)) {
        reply.error = "слишком большой ответ воркера";
        close();
        return reply;
    }
    reply.json.resize(size);
    if (size && !impl_->read_all(reply.json.data(), size, timeout_ms, cancel)) {
        reply.ms = double(mono_ms() - t0);
        if (impl_->cancelled) {
            reply.cancelled = true;
            reply.error = "остановлено пользователем";
        } else {
            reply.error = "ответ воркера оборвался";
        }
        close();
        return reply;
    }
    reply.ok = true;
    reply.ms = double(mono_ms() - t0);
    return reply;
}

}  // namespace agent

#endif  // !_WIN32
