"""Мозг со сценарием вместо модели: отладка и CI без LM Studio.

Тот же самый воркер (``AiWorker``: контекст, память, протокол, разбор вызовов), но
клиент модели подменён — он отдаёт заранее записанные шаги. Нужен, чтобы проверять
связку «C++ рантайм ↔ мозг» целиком, когда модели рядом нет.

    AGENT_SCRIPT='[{"calls":[{"tool":"create_folder","args":{"path":"/tmp/x"}}]},{"say":"готово"}]' \\
    python3 -m ai.dev_scripted --serve --socket /tmp/agent_ai.sock --state-dir /tmp/agent_state

Шаг ответа = номер обращения к модели внутри задачи: первый plan → шаг 1, replan → шаг 2 и т. д.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from typing import Any

from .config import WorkerConfig
from .llm import LLMError, LLMReply
from .memory import Memory
from .worker import AiWorker


class ScriptedClient:
    """Клиент модели, который читает сценарий из AGENT_SCRIPT / файла."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = steps
        self.index = 0
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> LLMReply:
        step = self.steps[self.index] if self.index < len(self.steps) else {"say": "Готово"}
        self.index += 1
        calls = step.get("calls") or []
        tool_calls = [
            {"function": {"name": call["tool"],
                          "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}}
            for call in calls
        ]
        return LLMReply(content=str(step.get("say") or ""), tool_calls=tool_calls,
                        usage={"total_tokens": 0}, ms=1.0)

    def chat(self, messages, **kwargs) -> LLMReply:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._next()

    def vision(self, prompt, image_bytes=None, **kwargs) -> LLMReply:
        step = self.steps[self.index] if self.index < len(self.steps) else {}
        self.index += 1
        return LLMReply(content=str(step.get("vision") or ""), ms=1.0)

    def preflight(self, **kwargs) -> dict:
        return {"ready": True, "model_present": True, "tools_ok": True, "vision_ok": True,
                "scripted": True}

    def models(self) -> list[str]:
        return ["scripted"]

    @property
    def stats(self) -> dict:
        return {"calls": len(self.calls), "errors": 0, "last_ms": 1.0, "scripted": True}

    def close(self) -> None:
        pass


def load_script() -> list[dict[str, Any]]:
    raw = os.environ.get("AGENT_SCRIPT", "")
    path = os.environ.get("AGENT_SCRIPT_FILE", "")
    if path:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    if not raw:
        # По умолчанию: создать папку в каталоге состояния и завершиться.
        state = os.environ.get("AGENT_STATE_DIR", "/tmp")
        raw = json.dumps([
            {"say": "создаю папку и завершаю",
             "calls": [{"tool": "create_folder", "args": {"path": f"{state}/scripted"}}]},
            {"say": "Готово"},
        ], ensure_ascii=False)
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai.dev_scripted", description="мозг со сценарием")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--once", metavar="JSON")
    parser.add_argument("--socket")
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)

    cfg = WorkerConfig()
    if args.socket:
        cfg.socket = args.socket
    if args.state_dir:
        cfg.state_dir = args.state_dir
    cfg.ensure_dirs()

    client = ScriptedClient(load_script())
    worker = AiWorker(cfg, client=client, memory=Memory(cfg.db_path))
    if args.once:
        print(worker.serve_once(args.once))
        worker.close()
        return 0

    path = args.socket or cfg.socket_path
    if not path:
        print("нужен --socket", file=sys.stderr)
        return 2

    def _stop(signum, frame):  # pragma: no cover
        raise KeyboardInterrupt

    if os.name != "nt":
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    print(f"[dev_scripted] слушаю {path}", flush=True)
    try:
        worker.serve(path)
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
