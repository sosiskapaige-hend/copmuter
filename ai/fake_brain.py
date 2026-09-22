"""Фейковый «мозг» для тестов агентного цикла (без LM Studio).

Отвечает на plan/replan по каналу Named Pipes/Unix-сокету заранее заданным сценарием.
Нужен, чтобы проверять именно связку C++ рантайма с внешним планировщиком: реальные
инструменты, реальные наблюдения, реальная верификация — но без модели.

    AGENT_FAKE_SOCKET=/tmp/fake.sock AGENT_FAKE_MODE=plan python3 -m ai.fake_brain

Режимы:
    plan     — шаг 1: создать папку и файл, дождаться файла; шаг 2: finished
    bad_tool — шаг 1: неизвестный инструмент, шаг 2: finished
    broken   — неразбираемый ответ (проверка честной ошибки)
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai import protocol  # noqa: E402


def log_line(text: str) -> None:
    path = os.environ.get("AGENT_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def build_reply(request: dict) -> dict:
    step = int(request.get("step") or 1)
    mode = os.environ.get("AGENT_FAKE_MODE", "plan")
    base = os.environ.get("AGENT_FAKE_BASE", "/tmp")
    request_id = request.get("id", 0)

    if mode == "broken":
        return {"id": request_id, "ok": True, "kind": "plan", "raw": "это не json"}

    if mode == "bad_tool":
        if step == 1:
            return {"id": request_id, "ok": True, "kind": "plan", "say": "пробую неизвестное",
                    "calls": [{"tool": "teleport_user", "args": {}}], "finished": False}
        return {"id": request_id, "ok": True, "kind": "plan", "say": "сдаюсь", "calls": [],
                "finished": True}

    if step == 1:
        return {
            "id": request_id,
            "ok": True,
            "kind": "plan",
            "say": "готовлю папку и файл",
            "calls": [
                {"tool": "create_folder", "args": {"path": f"{base}/AgentLoop"},
                 "note": "Создаю папку"},
                {"tool": "fs_write",
                 "args": {"path": f"{base}/AgentLoop/plan.txt", "content": "готово"},
                 "note": "Пишу файл"},
                {"tool": "wait_for",
                 "args": {"kind": "file_exists", "target": f"{base}/AgentLoop/plan.txt"},
                 "note": "Жду файл"},
            ],
            "finished": False,
        }
    return {"id": request_id, "ok": True, "kind": "plan", "say": "Готово", "calls": [],
            "finished": True}


def serve(path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    try:
        while True:
            conn, _ = server.accept()
            log_line("accept")
            try:
                while True:
                    payload = protocol.read_frame(conn)
                    if payload is None:
                        break
                    try:
                        request = protocol.decode(payload)
                    except Exception as exc:                      # noqa: BLE001
                        protocol.write_frame(conn, protocol.error_reply(0, f"плохой запрос: {exc}"))
                        continue
                    if request.get("type") == "health":
                        protocol.write_frame(conn, {"id": request.get("id", 0), "ok": True,
                                                    "kind": "health", "say": "жив"})
                        continue
                    if request.get("type") == "shutdown":
                        protocol.write_frame(conn, protocol.text_reply(request.get("id", 0), "выхожу"))
                        return
                    log_line(request.get("type", "?"))
                    protocol.write_frame(conn, build_reply(request))
            except (protocol.ProtocolError, ConnectionError):
                pass
            finally:
                conn.close()
    finally:
        server.close()
        if os.path.exists(path):
            os.unlink(path)


def main() -> int:
    path = os.environ.get("AGENT_FAKE_SOCKET")
    if not path:
        print("нужен AGENT_FAKE_SOCKET", file=sys.stderr)
        return 2
    print(f"фейковый мозг слушает {path}", flush=True)
    try:
        serve(path)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
