"""Точка входа AI-воркера.

    python -m ai.main --serve                 # постоянный воркер (обычный режим)
    python -m ai.main --preflight             # проверка LM Studio/модели/vision/tools
    python -m ai.main --health                # состояние воркера и метрики
    python -m ai.main --once '<json запроса>' # один запрос (отладка, тесты)

Воркер не перезапускается на каждый запрос: он поднимается при старте приложения
вместе с рантаймом и держит соединение с LM Studio тёплым.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from .config import WorkerConfig
from .worker import AiWorker

DEFAULT_PIPE = r"\\.\pipe\agent_ai_v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-ai", description="AI-воркер (мозг) агента")
    parser.add_argument("--serve", action="store_true", help="работать постоянно (по умолчанию)")
    parser.add_argument("--once", metavar="JSON", help="обработать один запрос и выйти")
    parser.add_argument("--preflight", action="store_true", help="проверить модель и канал")
    parser.add_argument("--health", action="store_true", help="показать состояние и метрики")
    parser.add_argument("--socket", help="адрес канала (AF_UNIX сокет или \\\\.\\pipe\\имя)")
    parser.add_argument("--endpoint", help="адрес LM Studio, например http://127.0.0.1:1234/v1")
    parser.add_argument("--model", help="имя модели в LM Studio")
    parser.add_argument("--state-dir", help="каталог для SQLite и кэшей")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = WorkerConfig()
    if args.socket:
        cfg.socket = args.socket
    if args.endpoint:
        cfg.endpoint = args.endpoint
    if args.model:
        cfg.model = args.model
    if args.state_dir:
        cfg.state_dir = args.state_dir
    configure_logging(args.log_level or cfg.log_level)

    worker = AiWorker(cfg)
    if args.preflight:
        report = worker.client.preflight()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        worker.close()
        return 0 if report.get("ready") else 1
    if args.health:
        print(json.dumps(worker.health(), ensure_ascii=False, indent=2))
        worker.close()
        return 0
    if args.once:
        print(worker.serve_once(args.once))
        worker.close()
        return 0

    # постоянный режим
    socket_path = args.socket or cfg.socket_path or (DEFAULT_PIPE if os.name == "nt" else "")
    if not socket_path:
        print("не задан адрес канала: --socket или AGENT_AI_SOCKET", file=sys.stderr)
        return 2
    stop = {"flag": False}

    def _stop(signum, frame):  # pragma: no cover - сигналы в тестах не нужны
        stop["flag"] = True
        logging.getLogger("ai.main").info("получен сигнал %s — завершаюсь", signum)
        raise KeyboardInterrupt

    if os.name != "nt":
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    try:
        if os.name == "nt" and socket_path.startswith("\\\\.\\pipe\\"):
            from .pipe_win import run_pipe_worker

            run_pipe_worker(worker, socket_path)
        else:
            worker.serve(socket_path)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        worker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
