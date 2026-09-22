// Канал C++ ↔ Python (мозг). Постоянное соединение, без запуска процесса на вызов.
//
// Windows: Named Pipes (\\.\pipe\agent_ai_v1) — CreateFileW + ReadFile/WriteFile.
// Linux (dev/тесты): AF_UNIX + SOCK_STREAM по тому же протоколу.
//
// Протокол — framed JSON: [uint32 BE длина][UTF-8 JSON]. Одновременно обрабатывается
// один запрос (модель всё равно одна), поэтому корреляция по id не нужна; поле "id"
// в JSON остаётся для логов и для будущего мультиплексирования.
#pragma once

#include <cstdint>
#include <memory>
#include <string>

namespace agent {

struct AiReply {
    bool ok = false;
    std::string json;        // ответ воркера как есть
    std::string error;
    double ms = 0.0;
};

class AiLink {
public:
    AiLink();
    ~AiLink();
    AiLink(const AiLink&) = delete;
    AiLink& operator=(const AiLink&) = delete;

    // address: "\\\\.\\pipe\\agent_ai_v1" (Windows) или "/tmp/agent_ai_v1.sock" (Linux).
    // Ждёт появления сервера до timeout_ms (воркер поднимается параллельно).
    bool connect(const std::string& address, int timeout_ms, std::string& error);
    bool connected() const;
    void close();

    // Запрос к модели/планировщику. Возвращает ответ или ошибку (таймаут/обрыв).
    AiReply request(const std::string& json, int timeout_ms);

    // Сколько раз соединение поднималось заново (диагностика «мигающих» воркеров).
    int reconnects() const { return reconnects_; }
    const std::string& address() const { return address_; }

    // Переподключиться, если воркер перезапустился (модель не должна терять связь).
    bool ensure_connected(std::string& error);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    std::string address_;
    int reconnects_ = 0;
};

}  // namespace agent
