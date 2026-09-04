"""Интерактивный CLI: REPL с живым потоком событий, режимами, очередью.

Команды (список — /help). Любая обычная фраза — задача для агента.
Особые фразы: «стоп», «пауза», «продолжай», «отмени»/«undo» — управление.
"""
from __future__ import annotations

import sys
import time

from ..events import AgentEvent
from ..runtime import AgentRuntime

C = {"reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
     "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
     "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m"}


def _c(color: str, s: str) -> str:
    return f"{C[color]}{s}{C['reset']}"


class CliPrinter:
    def __init__(self, rt: AgentRuntime, verbose: bool = True) -> None:
        self.rt = rt
        self.verbose = verbose
        rt.bus.subscribe(self._on)

    def _on(self, ev: AgentEvent) -> None:
        t = ev.type
        d = ev.data
        ts = time.strftime("%H:%M:%S")
        if t == "thought" and self.verbose:
            print(f"  {_c('dim', ts)} {_c('magenta', '💭')} {d.get('text','')[:200]}")
        elif t == "plan":
            print(_c("cyan", "\n── ПЛАН " + ("(пересобран)" if d.get("replan") else "") + " " + "─" * 30))
            for i, s in enumerate(d.get("steps", []), 1):
                print(f"  {i}. {_c('bold', s['title'])} {_c('dim', '— ' + s.get('detail',''))}")
        elif t == "tool_call":
            print(f"  {_c('dim', ts)} {_c('blue', '🔧')} {d.get('name')}({_short(d.get('args', {}))})")
        elif t == "observation":
            mark = _c("green", "✔") if d.get("ok") else _c("red", "✘")
            line = d.get("output") or d.get("error") or ""
            print(f"     {mark} {_c('dim', line[:220].splitlines()[0] if line else '')}")
        elif t == "error":
            print(f"  {_c('dim', ts)} {_c('red', '⚠ ошибка')} {d.get('error','')[:200]}")
        elif t == "progress":
            bar = "█" * int(d.get("progress", 0) // 5) + "░" * (20 - int(d.get("progress", 0) // 5))
            print(f"  {_c('dim', ts)} [{bar}] {d.get('progress',0)}% {_c('dim', d.get('message','')[:100])}")
        elif t == "confirm_request":
            print(_c("yellow", f"\n  ⏸ ПОДТВЕРЖДЕНИЕ ({d.get('risk','?')}): {d.get('description','')[:300]}"))
            print(_c("yellow", "  (Ответьте в Web UI или дождитесь таймаута; в CLI: подтверждение через /confirm)"))
        elif t == "user_ask":
            print(_c("magenta", f"\n  ❓ ВОПРОС АГЕНТА: {d.get('question','')}"))
            print(_c("magenta", "  (Ответ: /answer <текст> или Web UI)"))
        elif t == "task_done":
            print(_c("green", f"\n══ Готово ══ {d.get('summary','')[:400]}\n"))
        elif t == "task_failed":
            print(_c("red", f"\n══ Сбой ══ {d.get('error','')[:300]}\n"))
        elif t == "log" and d.get("level") in ("bg", "bg_done", "bg_error", "trigger", "scheduler"):
            print(_c("dim", f"  ℹ [{d.get('level')}] {d.get('message','')}"))
        elif t == "screen":
            p = d.get("path")
            if p and self.verbose:
                print(_c("dim", f"  🖥 скриншот: {p}"))


def _short(args: dict) -> str:
    parts = []
    for k, v in list(args.items())[:4]:
        s = str(v)
        parts.append(f"{k}={s[:40]}")
    return ", ".join(parts)


HELP = """\
Команды:
  /help              эта справка
  /mode <режим>      auto | confirm | step | observe | plan_only
  /tasks             история задач
  /running           запущенные задачи
  /queue             очередь
  /stop              остановить текущие задачи
  /pause | /resume   пауза / продолжить
  /undo [n]          отменить последние n действий (undo из журнала)
  /cancel            убрать следующую из очереди
  /confirm <id>      подтвердить ожидание (id из потока событий)
  /answer <текст>    ответить на вопрос агента
  /memory            показать память
  /memory add <текст> / memory rm <id>
  /sched             расписания
  /sched add <expr> <текст>   (daily 09:00 | weekly mon 10:00 | hourly | every 30m)
  /sched rm <id>
  /trig              триггеры
  /trig add <папка> <pattern> <инструкция>   (например: ~/Downloads "*.pdf" Сложи PDF в Documents)
  /trig rm <id>
  /journal           последние действия (undo-журнал)
  /screen            открыть последний скриншот
  /stats             состояние системы
  /web               адрес Web UI
  /exit              выход
Любая другая фраза — задача для агента (Ctrl+C во время выполнения — стоп).
"""


def run_cli(rt: AgentRuntime, initial_task: str = "") -> int:
    printer = CliPrinter(rt)
    print(_c("bold", "\n╔══════════════════════════════════════════════════╗"))
    print(_c("bold", "║   AI COMPUTER AGENT — оператор вашего ПК        ║"))
    print(_c("bold", "╚══════════════════════════════════════════════════╝\n"))
    print(_c("dim", rt.platform.summary()))
    print(f"\n{_c('cyan','LLM:')} {rt.llm.name}   {_c('cyan','режим:')} {rt.cfg.safety.mode}   {_c('cyan','инструментов:')} {len(rt.registry.names())}")
    print(_c("dim", "Особые фразы: «стоп», «пауза», «продолжай», «отмени». Справка: /help\n"))

    def _wait_task(task_id: str):
        try:
            return _wait_done(rt, task_id)
        except KeyboardInterrupt:
            print(_c("yellow", "\n(останавливаю...)"))
            rt.control("stop_all")
            _wait_done(rt, task_id)
            return None

    if initial_task:
        tid = rt.submit_task(initial_task)
        _wait_task(tid)

    while True:
        try:
            line = input(_c("green", "вы> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if line == "/exit" or line == "/quit":
            break
        if line == "/help":
            print(HELP)
            continue
        if line.startswith("/mode"):
            parts = line.split()
            if len(parts) > 1:
                print(_c("cyan", f"режим: {rt.set_mode(parts[1])}"))
            else:
                print(f"текущий: {rt.cfg.safety.mode} (auto|confirm|step|observe|plan_only)")
            continue
        if line.startswith("/confirm"):
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[1] != "<id>":
                ok = rt.confirm(parts[1], True, parts[2] if len(parts) > 2 else "")
                print(ok and _c("green", "подтверждено") or "id не найден")
            else:
                pend = rt.gateway.pending_list()
                for p in pend:
                    print(f"  [{p['id']}] {p['description'][:120]}")
                if not pend:
                    print("нет ожиданий")
            continue
        if line.startswith("/answer"):
            parts = line.split(None, 1)
            pend = rt.gateway.pending_list()
            if len(parts) > 1 and pend:
                ok = rt.answer(pend[0]["id"], parts[1])
                print(ok and _c("green", "ответ передан агенту") or "нет ожидания")
            else:
                print("формат: /answer <текст>")
            continue
        if line == "/stop":
            n = rt.control("stop_all")
            print(_c("yellow", f"остановлено: {n.get('affected', 0)}"))
            continue
        if line == "/pause":
            print(f"пауза: {rt.control('pause_all')}")
            continue
        if line == "/resume":
            print(f"продолжено: {rt.control('resume_all')}")
            continue
        if line == "/running":
            print(rt.agent.list_running() or "(ничего не запущено)")
            continue
        if line == "/queue":
            print(rt.tasks.queued_goals() or "(очередь пуста)")
            continue
        if line == "/cancel":
            rt.submit(rt.tasks.cancel_next())
            print("следующая в очереди отменена")
            continue
        if line == "/tasks":
            for t in rt.sessions.history_brief(15):
                print(f"  [{t['status']}] {t['goal'][:80]} {t['summary'][:60]}")
            continue
        if low.startswith("/undo"):
            parts = line.split()
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            r = rt.undo_last(n)
            print(_c("green" if r.get("ok") else "red", r.get("output") or r.get("error", "")))
            continue
        if low == "стоп" or low == "stop":
            rt.control("stop_all")
            print(_c("yellow", "все задачи остановлены"))
            continue
        if low in ("пауза", "pause"):
            rt.control("pause_all")
            continue
        if low in ("продолжай", "продолжи", "resume", "continue"):
            rt.control("resume_all")
            continue
        if low in ("отмени", "undo", "верни как было", "отмени последнее действие"):
            r = rt.undo_last(1)
            print(_c("green" if r.get("ok") else "red", r.get("output") or r.get("error", "")))
            continue
        if line.startswith("/memory"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 3 and parts[1] == "add":
                it = rt.memory_add(parts[2])
                print(f"запомнено ({it['id']}): {parts[2]}")
            elif len(parts) >= 3 and parts[1] == "rm":
                print(rt.memory_remove(parts[2]) and "удалено" or "не найдено")
            else:
                for it in rt.memory_list():
                    print(f"  [{it['id']}] ({it['kind']}) {it['text']}")
            continue
        if line.startswith("/sched"):
            parts = line.split(None, 2)
            if len(parts) >= 3 and parts[1] == "add":
                rest = parts[2]
                import re
                m = re.match(r"(daily\s+\d{1,2}:\d{2}|weekly\s+[a-z,]+\s+\d{1,2}:\d{2}|hourly|every\s+\d+[mhd])\s+(.*)", rest)
                if m:
                    r = rt.schedule_add(m.group(1), m.group(2))
                    print(r.get("error") or f"добавлено: {r.get('id')} «{m.group(1)}»")
                else:
                    print("формат: /sched add <daily 09:00|weekly mon 10:00|hourly|every 30m> <инструкция>")
            elif len(parts) >= 3 and parts[1] == "rm":
                print(rt.schedule_remove(parts[2]) and "удалено" or "не найдено")
            else:
                for s in rt.schedules.list():
                    print(f"  [{s.id}] {s.expr}: {s.instruction}")
            continue
        if line.startswith("/trig"):
            parts = line.split(None, 3)
            if len(parts) >= 4 and parts[1] == "add":
                r = rt.trigger_add(parts[2], parts[3] if len(parts) > 3 else "*",
                                   parts[4] if len(parts) > 4 else "Обработай новый файл {file}")
                print(r.get("error") or f"добавлен: {r.get('id')}")
            elif len(parts) >= 3 and parts[1] == "rm":
                print(rt.trigger_remove(parts[2]) and "удалено" or "не найдено")
            else:
                for t in rt.triggers.list():
                    print(f"  [{t.id}] {t.path} ({t.pattern}): {t.instruction}")
            continue
        if line == "/journal":
            for e in rt.journal.recent(15):
                print(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['tool']} "
                      f"{'✔' if e['ok'] else '✘'} undo={'да' if e.get('undo') else '—'}")
            continue
        if line == "/screen":
            from pathlib import Path
            import os
            p = Path(rt.cfg.state_dir / "screens")
            shots = sorted(p.glob("shot_*.png"), key=lambda x: x.stat().st_mtime, reverse=True) \
                if p.exists() else []
            if shots:
                print(shots[0])
                try:
                    if sys.platform == "darwin":
                        os.system(f"open '{shots[0]}'")
                    elif sys.platform == "win32":
                        os.startfile(shots[0])  # type: ignore[attr-defined]
                    else:
                        os.system(f"xdg-open '{shots[0]}' &")
                except Exception:
                    pass
            else:
                print("скриншотов пока нет")
            continue
        if line == "/stats":
            import json as _j
            print(_j.dumps(rt.system_stats(), ensure_ascii=False, indent=1))
            continue
        if line == "/web":
            w = rt.cfg.web_ui
            print(f"http://{w.get('host','0.0.0.0')}:{w.get('port',8710)}")
            continue
        if line.startswith("/"):
            print(_c("yellow", f"неизвестная команда: {line} (/help)"))
            continue
        # обычная задача
        tid = rt.submit_task(line, mode=rt.cfg.safety.mode)
        print(_c("dim", f"(задача {tid} отправлена, работаем...)"))
        _wait_task(tid)


def _wait_done(rt: AgentRuntime, task_id: str, prompt_user: bool = True) -> None:
    """Ждёт завершения конкретной задачи (опрос статуса в главном потоке —
    event-loop агента при этом не блокируется). Если агент ждёт
    подтверждения/ответа — спрашивает пользователя прямо здесь."""
    terminal = {"done", "failed", "cancelled"}
    asked: set[str] = set()
    while True:
        st = rt.sessions.active.get(task_id)
        if st is not None and st.status in terminal:
            print(_c("dim", f"статус: {st.status}, прогресс: {st.progress}%"))
            return
        if prompt_user:
            for p in rt.gateway.pending_list():
                if p["id"] in asked:
                    continue
                asked.add(p["id"])
                try:
                    if p["data"] and "risk" in p["data"]:  # подтверждение
                        print(_c("yellow", f"\n  ⏸ [{p['id']}] {p['description'][:300]}"))
                        ans = input(_c("yellow", "  Подтвердить? [y/N/комментарий] ")).strip()
                        ok = ans.lower() in ("y", "yes", "да", "д")
                        rt.confirm(p["id"], ok, ans if ok else ans if ans else "")
                    else:  # вопрос
                        print(_c("magenta", f"\n  ❓ [{p['id']}] {p['description']}"))
                        ans = input(_c("magenta", "  Ваш ответ: ")).strip()
                        if ans:
                            rt.answer(p["id"], ans)
                except EOFError:
                    break
        time.sleep(0.3)


def main() -> None:
    from ..config import Config
    cfg = Config.load()
    rt = AgentRuntime(cfg)
    rt.start()
    try:
        run_cli(rt)
    finally:
        rt.stop()
