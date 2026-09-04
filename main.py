#!/usr/bin/env python3
"""AI Computer Agent — точка входа.

Использование:
  python main.py                  # интерактивный CLI (режим по умолчанию — confirm)
  python main.py "Создай папку Test"   # одна задача и выход
  python main.py --web            # Web-дашборд (CLI тоже доступен в терминале)
  python main.py --web --port 9000
  python main.py --task "..." --mode auto   # задача сразу с заданным режимом
  python main.py --plan "..."   # только план, без выполнения
  python main.py --config config.json
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI Computer Agent — универсальный оператор ПК")
    ap.add_argument("task", nargs="*", help="Задача для агента (одна задача и выход, если нет --web)")
    ap.add_argument("--config", help="Путь к config.json")
    ap.add_argument("--mode", default="", help="auto|confirm|step|observe|plan_only")
    ap.add_argument("--web", action="store_true", help="Запустить Web-дашборд")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="")
    ap.add_argument("--workdir", default=".", help="Рабочая папка агента")
    ap.add_argument("--task", dest="task_flag", help="Альтернативная передача задачи")
    ap.add_argument("--plan", action="store_true", help="Только план (plan_only)")
    args = ap.parse_args(argv)

    from agent.config import Config
    from agent.runtime import AgentRuntime

    cfg = Config.load(args.config)
    workdir = args.workdir
    rt = AgentRuntime(cfg, workdir=workdir)
    rt.start()

    goal = args.task_flag or " ".join(args.task).strip()

    try:
        if args.web:
            from agent.ui.web import serve
            w = cfg.web_ui
            ui = serve(rt, host=args.host or w.get("host", "0.0.0.0"),
                       port=args.port or int(w.get("port", 8710)), open_browser=False)
            print(f"Web UI: http://{ui.host}:{ui.port}")
            print("Агент работает. Для интерактивного ввода откройте отдельный терминал:\n"
                  "    python main.py\n"
                  "Нажмите Ctrl+C для выхода.\n")
            if goal:
                rt.submit_task(goal, mode=args.mode or cfg.safety.mode)
            try:
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nОстанавливаю...")
            return 0

        if args.plan and goal:
            cfg.safety.mode = "plan_only"
        if goal:
            # одна задача: запускаем и ждём
            from agent.ui.cli import CliPrinter, _wait_done
            CliPrinter(rt)  # живой поток событий в терминал
            tid = rt.submit_task(goal, mode=args.mode or cfg.safety.mode)
            import time
            print(f"(задача {tid} — работаем, Ctrl+C для стопа)\n")
            try:
                while True:
                    st = rt.sessions.active.get(tid)
                    if st and st.status in ("done", "failed", "cancelled"):
                        break
                    time.sleep(0.3)
            except KeyboardInterrupt:
                rt.control("stop_all")
                while True:
                    st = rt.sessions.active.get(tid)
                    if st and st.status in ("done", "failed", "cancelled"):
                        break
                    time.sleep(0.3)
            st = rt.sessions.active.get(tid)
            if st:
                print("\n=== ИТОГ (" + st.status + ", " + str(st.progress) + "%) ===")
                print(st.summary or "(без резюме)")
            return 0

        # интерактивный CLI
        from agent.ui.cli import run_cli
        run_cli(rt)
        return 0
    finally:
        rt.stop()


if __name__ == "__main__":
    sys.exit(main())
