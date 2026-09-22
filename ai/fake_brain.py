"""Фейковый «мозг» для тестов агентного цикла (без LM Studio).

Отвечает на plan/replan по каналу Named Pipes/Unix-сокету заранее заданным сценарием.
Нужен, чтобы проверять именно связку C++ рантайма с внешним планировщиком: реальные
инструменты, реальные наблюдения, реальная верификация — но без модели.

    AGENT_FAKE_SOCKET=/tmp/fake.sock AGENT_FAKE_MODE=plan python3 -m ai.fake_brain

Режимы:
    plan        — шаг 1: создать папку и файл, дождаться файла; шаг 2: finished
    plan_calculator — сценарий 5 ТЗ: редактор → файл с кодом → буфер → запуск → готово
    ui          — сценарий 10 ТЗ: launch_application → find_element (зрение) → готово
    bad_tool    — шаг 1: неизвестный инструмент, шаг 2: finished
    broken      — неразбираемый ответ (проверка честной ошибки)
    slow        — шаг 1: долгое ожидание (проверка «Стоп» на уровне исполнителя)
    slow_answer — модель отвечает не сразу (проверка «Стоп» во время ожидания ответа)
    vision      — на запрос зрения отвечает кликом по AGENT_FAKE_CLICK (по умолчанию 640,360)
    vision_none — на запрос зрения честно отвечает «не вижу»
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


def build_vision_reply(request: dict) -> dict:
    """Ответ зрения: клик по координатам (или честное «не вижу»)."""
    mode = os.environ.get("AGENT_FAKE_MODE", "plan")
    request_id = request.get("id", 0)
    if mode == "vision_none":
        return {"id": request_id, "ok": True, "kind": "vision", "say": "Не вижу на экране то, что нужно",
                "calls": [], "finished": True}
    click = os.environ.get("AGENT_FAKE_CLICK", "640,360")
    x_str, _, y_str = click.partition(",")
    return {
        "id": request_id,
        "ok": True,
        "kind": "vision",
        "say": "Нашёл кнопку",
        "calls": [{"tool": "click", "args": {"x": int(x_str or 640), "y": int(y_str or 360),
                                             "button": 1, "clicks": 1}, "note": "Кликаю по кнопке"}],
        "found": True,
        "finished": False,
    }


def build_browser_reply(request: dict) -> dict:
    """Ответ браузерного пути: без настоящего Chromium, для проверки связки."""
    request_id = request.get("id", 0)
    action = request.get("action", "open")
    if action not in ("open", "click", "fill", "press", "text", "links", "eval", "wait",
                      "screenshot", "back", "download", "info"):
        from ai import protocol as _protocol

        return _protocol.error_reply(request_id, f"неизвестное действие браузера: {action}")
    if action == "text":
        data = {"text": "Котики: 10 видео", "chars": 16, "url": "https://youtube.com/results"}
    elif action == "open":
        data = {"url": request.get("url", ""), "title": "YouTube", "status": 200}
    else:
        data = {"action": action, "url": "https://youtube.com/results"}
    return {"id": request_id, "ok": True, "kind": "browser",
            "say": f"браузер: {action} выполнен", "calls": [], "finished": True, "data": data}


def build_reply(request: dict) -> dict:
    step = int(request.get("step") or 1)
    mode = os.environ.get("AGENT_FAKE_MODE", "plan")
    base = os.environ.get("AGENT_FAKE_BASE", "/tmp")
    request_id = request.get("id", 0)

    if mode == "broken":
        return {"id": request_id, "ok": True, "kind": "plan", "raw": "это не json"}

    if mode == "plan_calculator":
        # Сценарий 5 ТЗ: открыть редактор → записать код (его «сочинил» мозг) →
        # провести длинный текст через буфер → запустить → проверить код возврата.
        base = os.environ.get("AGENT_FAKE_BASE", "/tmp/agent_calc")
        code = ("def calc(expr):\n"
                "    return eval(expr)\n\n"
                "if __name__ == '__main__':\n"
                "    print('2 + 2 =', calc('2 + 2'))\n")
        steps = {
            1: [{"tool": "launch_application", "args": {"name": "vscode"},
                 "note": "Открываю VS Code"}],
            2: [{"tool": "write_file", "args": {"path": f"{base}/calculator.py", "content": code},
                 "note": "Сохраняю код"},
                {"tool": "clipboard", "args": {"mode": "set", "text": code},
                 "note": "Кладу код в буфер для вставки"}],
            3: [{"tool": "run_command", "args": {"command": "python calculator.py"},
                 "note": "Запускаю калькулятор"}],
        }
        calls = steps.get(step)
        if calls:
            return {"id": request_id, "ok": True, "kind": "plan", "say": "Работаю по шагам",
                    "calls": calls, "finished": False}
        return {"id": request_id, "ok": True, "kind": "plan", "say": "Готово: калькулятор работает",
                "calls": [], "finished": True}

    if mode == "ui":
        # Сценарий 10 ТЗ: открыть программу и нажать кнопку, которой ядро не знает.
        if step == 1:
            return {"id": request_id, "ok": True, "kind": "plan", "say": "Открываю программу",
                    "calls": [{"tool": "launch_application", "args": {"name": "mockapp"},
                               "note": "Открываю программу"}], "finished": False}
        if step == 2:
            return {"id": request_id, "ok": True, "kind": "plan", "say": "Нажимаю кнопку ОК",
                    "calls": [{"tool": "find_element", "args": {"target": "кнопка ОК"},
                               "note": "Ищу кнопку на экране"}], "finished": False}
        return {"id": request_id, "ok": True, "kind": "plan", "say": "Готово", "calls": [],
                "finished": True}

    if mode == "slow_answer":
        # Медленный ответ модели: ядро должно прервать ожидание по «Стоп», а не
        # висеть до llm_timeout_ms.
        delay_ms = int(float(os.environ.get("AGENT_FAKE_DELAY_S", "5")) * 1000)
        time.sleep(delay_ms / 1000.0)
        return {"id": request_id, "ok": True, "kind": "plan", "say": "наконец-то ответил",
                "calls": [], "finished": True}

    if mode == "slow":
        if step == 1:
            return {"id": request_id, "ok": True, "kind": "plan", "say": "жду долго",
                    "calls": [{"tool": "wait_for",
                               "args": {"kind": "process_started", "target": "never_appears",
                                        "timeout_ms": 30000}}],
                    "finished": False}
        return {"id": request_id, "ok": True, "kind": "plan", "say": "Готово", "calls": [],
                "finished": True}

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
                    if request.get("type") == "browser":
                        protocol.write_frame(conn, build_browser_reply(request))
                        continue
                    if request.get("type") == "vision":
                        protocol.write_frame(conn, build_vision_reply(request))
                        continue
                    if request.get("type") == "health":
                        protocol.write_frame(conn, {"id": request.get("id", 0), "ok": True,
                                                    "kind": "health", "say": "жив"})
                        continue
                    if request.get("type") == "shutdown":
                        protocol.write_frame(conn, protocol.text_reply(request.get("id", 0), "выхожу"))
                        return
                    log_line(request.get("type", "?"))
                    if os.environ.get("AGENT_FAKE_DUMP"):
                        # Для тестов: видно, что именно ядро положило в запрос.
                        log_line("request=" + json.dumps(request, ensure_ascii=False))
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
