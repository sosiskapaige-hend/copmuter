#!/usr/bin/env python3
"""AI Computer Agent — DESKTOP-ПРИЛОЖЕНИЕ (запуск в 1 клик).

  python desktop.py                # нативное окно (pywebview) или браузер
  python desktop.py --headless     # только сервер (для тестов/сервера)
  python desktop.py --config c.json

Окно содержит весь функционал: задачи, очередь, режимы, голос (Ctrl+Shift+M),
экран агента, память, расписание, триггеры, настройки модели (⚙).
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI Computer Agent — desktop")
    ap.add_argument("--config", help="путь к config.json")
    ap.add_argument("--headless", action="store_true",
                    help="без окна: поднять сервер и работать (для Linux-серверов)")
    ap.add_argument("--width", type=int, default=1340)
    ap.add_argument("--height", type=int, default=860)
    args = ap.parse_args(argv)

    from agent.config import Config
    from agent.runtime import AgentRuntime

    cfg = Config.load(args.config)
    rt = AgentRuntime(cfg, workdir=".")

    from agent.ui.desktop import run_desktop
    try:
        ui, url = run_desktop(rt, headless=args.headless,
                              width=args.width, height=args.height)
        if args.headless:
            import time
            print(f"AI Computer Agent работает: {url}")
            print("Ctrl+C — выход")
            while True:
                time.sleep(1)
        return 0
    except KeyboardInterrupt:
        print("\nЗавершение...")
        return 0
    finally:
        rt.stop()


if __name__ == "__main__":
    sys.exit(main())
