"""Batch Actions и Action Queue (ТЗ §24, §25, §45, §46).

Правило: агент не должен дёргать модель между двумя шагами одной задачи.
«Открой VS Code, создай файл, вставь код, сохрани» — это один пакет действий,
который выполняется одним махом; модель (и пользователь) получает управление
только когда пакет завершён или упал.

Внутри:

  * `Action` — одно действие: инструмент, аргументы, риск, проверка результата,
    срок, зависимости. Есть `status`: queued/running/success/failed/timeout/
    cancelled — те же состояния показывает UI (ТЗ §46).
  * `ActionPlan` — список действий + способ проверки всей цепочки.
  * `ActionQueue` — исполнитель: последовательно, параллельно (только для
    независимых и не-GUI действий), с остановкой по ошибке, отменой,
    паузой и живым состоянием для интерфейса.

Очередь полностью локальная и не знает про LLM — её можно использовать и в
быстром пути, и в агентном цикле (например, для группировки вызовов модели).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

STATUSES = ("queued", "running", "success", "failed", "cancelled", "timeout")


@dataclass
class Action:
    tool: str
    args: dict = field(default_factory=dict)
    title: str = ""
    risk: str = "low"
    timeout: float = 30.0
    is_gui: bool = False
    parallel: bool = True          # можно ли запускать параллельно с другими
    depends_on: list[int] = field(default_factory=list)   # индексы действий
    verify: str = ""               # имя проверки: file|process|window|url|screen|none
    verify_args: dict = field(default_factory=dict)
    retries: int = 1
    fallbacks: list[tuple[str, dict]] = field(default_factory=list)  # (tool, args)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "queued"
    ok: bool = False
    ms: float = 0.0
    output: str = ""
    error: str = ""
    verified: bool | None = None
    method: str = ""
    attempts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": _brief(self.args),
                "title": self.title, "risk": self.risk, "status": self.status,
                "ok": self.ok, "ms": round(self.ms, 1), "output": (self.output or "")[:300],
                "error": (self.error or "")[:200], "verified": self.verified,
                "method": self.method, "attempts": self.attempts[-4:]}

    @property
    def sign(self) -> str:
        return f"{self.tool}({', '.join(f'{k}={v}' for k, v in list(self.args.items())[:2])})"


@dataclass
class BatchResult:
    ok: bool
    actions: list[Action] = field(default_factory=list)
    ms: float = 0.0
    stopped_at: int | None = None
    error: str = ""
    parallel_saved_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "ms": round(self.ms, 1), "stopped_at": self.stopped_at,
                "error": self.error, "parallel_saved_ms": round(self.parallel_saved_ms, 1),
                "actions": [a.to_dict() for a in self.actions]}

    def summary(self) -> str:
        lines = []
        for a in self.actions:
            mark = "✓" if a.ok else ("✗" if a.status in ("failed", "timeout") else "—")
            lines.append(f"{mark} {a.title or a.sign}")
        return "\n".join(lines)


def _brief(args: dict, limit: int = 120) -> dict:
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[k] = s if len(s) <= limit else s[:limit] + "…"
    return out


class ActionQueue:
    """Очередь действий с проверкой результатов и понятным состоянием."""

    def __init__(self, max_parallel: int = 3, log: Any = None, metrics: Any = None,
                 bus: Any = None, task_id: str = "") -> None:
        self.actions: list[Action] = []
        self.max_parallel = max(1, max_parallel)
        self.log = log
        self.metrics = metrics
        self.bus = bus
        self.task_id = task_id
        self.cancelled = False
        self.current: str = ""

    # ------------------------------------------------------------------ состав
    def add(self, action: Action) -> Action:
        action.status = "queued"
        self.actions.append(action)
        return action

    def extend(self, actions: Iterable[Action]) -> None:
        for a in actions:
            self.add(a)

    def cancel(self, action_id: str | None = None) -> None:
        if action_id is None:
            self.cancelled = True
            for a in self.actions:
                if a.status == "queued":
                    a.status = "cancelled"
            return
        for a in self.actions:
            if a.id == action_id and a.status == "queued":
                a.status = "cancelled"

    def state(self) -> dict:
        done = sum(1 for a in self.actions if a.status in ("success", "failed", "timeout"))
        return {"task_id": self.task_id, "total": len(self.actions), "done": done,
                "cancelled": self.cancelled,
                "progress": int(done / len(self.actions) * 100) if self.actions else 0,
                "actions": [a.to_dict() for a in self.actions]}

    # ------------------------------------------------------------------ запуск
    async def run(self, call: Callable[[Action], Awaitable[Any]],
                  stop_on_error: bool = True,
                  verify: Callable[[Action], Awaitable[tuple[bool | None, str]]] | None = None,
                  control: Any = None, max_parallel_gui: int = 1) -> BatchResult:
        """Выполнить пакет.

        `call(action)` — как вызвать инструмент (обычно `registry.call`).
        Действия с `parallel=True`, не-GUI и без зависимостей группируются и
        запускаются одновременно: независимые операции не должны ждать друг друга.
        """
        t0 = time.perf_counter()
        for a in self.actions:
            a.status = "queued"
            a.ok = False
            a.error = ""
        self.cancelled = False
        serial_ms = 0.0
        parallel_saved = 0.0
        stopped_at: int | None = None

        groups = self._groups()
        for gi, group in enumerate(groups):
            if self.cancelled or (control is not None and getattr(control, "mode", "") == "stop"):
                for a in self.actions:
                    if a.status == "queued":
                        a.status = "cancelled"
                break
            if control is not None:
                try:
                    await control.wait_if_paused()
                except Exception:
                    pass
            if len(group) == 1:
                a = group[0]
                await self._run_one(a, call, verify)
                serial_ms += a.ms
            else:
                results = await asyncio.gather(*(self._run_one(a, call, verify) for a in group),
                                               return_exceptions=True)
                for a in group:
                    serial_ms += a.ms
                dur = max((a.ms for a in group), default=0.0)
                parallel_saved += max(0.0, sum(a.ms for a in group) - dur)
            # ошибка в группе → останавливаемся (LLM должен узнать)
            bad = [a for a in group if a.status in ("failed", "timeout") and a.verify != "soft"]
            if bad and stop_on_error:
                stopped_at = self.actions.index(bad[0])
                for a in self.actions:
                    if a.status == "queued":
                        a.status = "cancelled"
                break

        ms = (time.perf_counter() - t0) * 1000
        hard_failed = [a for a in self.actions
                       if a.status in ("failed", "timeout") and a.verify != "soft"]
        ok = not hard_failed and not self.cancelled
        first_error = next((a.error for a in self.actions if a.error), "")
        res = BatchResult(ok=ok, actions=list(self.actions), ms=ms, stopped_at=stopped_at,
                          error=first_error, parallel_saved_ms=parallel_saved)
        if self.log is not None:
            try:
                self.log.event("batch", task_id=self.task_id, ok=res.ok,
                               ms=round(ms, 1), actions=len(self.actions),
                               stopped_at=stopped_at, error=first_error[:160])
            except Exception:
                pass
        return res

    # ------------------------------------------------------------------ группы
    def _groups(self) -> list[list[Action]]:
        """Разбиение на последовательные/параллельные группы с учётом зависимостей."""
        groups: list[list[Action]] = []
        pending = list(self.actions)
        done_idx: set[int] = set()
        while pending:
            ready = []
            for a in pending:
                idx = self.actions.index(a)
                if any(d not in done_idx for d in a.depends_on):
                    continue
                parallel_ok = (a.parallel and not a.is_gui and not self._gui_conflict(a))
                ready.append((a, idx, parallel_ok))
            if not ready:
                # циклическая зависимость — выполняем как есть
                ready = [(pending[0], self.actions.index(pending[0]), False)]
            seq = [r for r in ready if not r[2]]
            par = [r for r in ready if r[2]]
            if seq:
                first = seq[0]
                groups.append([first[0]])
                done_idx.add(first[1])
                pending.remove(first[0])
            elif par:
                batch = [r for r in par][: self.max_parallel]
                groups.append([b[0] for b in batch])
                for b in batch:
                    done_idx.add(b[1])
                    pending.remove(b[0])
        return groups

    def _gui_conflict(self, action: Action) -> bool:
        """GUI-действия нельзя параллелить: фокус окна один на всех."""
        gui_tools = {"mouse_click", "keyboard_type", "keyboard_hotkey", "window_focus",
                     "click_element", "find_element", "screen_capture", "take_screenshot"}
        return action.tool in gui_tools or action.is_gui

    # ------------------------------------------------------------------ одно действие
    async def _run_one(self, a: Action, call: Callable[[Action], Awaitable[Any]],
                       verify: Callable[[Action], Awaitable[tuple[bool | None, str]]] | None) -> None:
        if a.status != "queued":
            return
        a.status = "running"
        self.current = a.id
        self._emit("tool_call", action=a, status="running")
        attempts = max(1, a.retries + 1)
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                res = await asyncio.wait_for(call(a), timeout=a.timeout)
            except asyncio.TimeoutError:
                a.ms = (time.perf_counter() - t0) * 1000
                a.status = "timeout"
                a.error = f"таймаут {a.timeout:.0f} с"
                a.attempts.append({"try": attempt + 1, "ok": False, "error": "timeout"})
                continue
            except asyncio.CancelledError:
                a.status = "cancelled"
                raise
            except Exception as exc:                      # noqa: BLE001
                a.ms = (time.perf_counter() - t0) * 1000
                a.status = "failed"
                a.error = f"{type(exc).__name__}: {exc}"
                a.attempts.append({"try": attempt + 1, "ok": False, "error": a.error})
                continue

            a.ms = (time.perf_counter() - t0) * 1000
            a.output = _output_of(res)
            a.ok = _ok_of(res)
            a.method = getattr(res, "data", {}).get("method", "") if isinstance(getattr(res, "data", None), dict) else ""
            if not a.ok:
                a.error = _error_of(res)
                a.attempts.append({"try": attempt + 1, "ok": False, "error": a.error[:160]})
                # пробуем запасной способ внутри этого же действия
                if a.fallbacks and attempt == 0:
                    fb_tool, fb_args = a.fallbacks.pop(0)
                    a.attempts.append({"try": attempt + 1, "fallback": fb_tool})
                    a.tool, a.args = fb_tool, fb_args
                    continue
                continue
            # проверка результата (ТЗ §21)
            if verify is not None and a.verify and a.verify != "none":
                try:
                    v_ok, v_detail = await asyncio.wait_for(verify(a), timeout=min(a.timeout, 12.0))
                except Exception as exc:                  # noqa: BLE001
                    v_ok, v_detail = None, f"проверка не удалась: {exc}"
                a.verified = v_ok
                if v_ok is False:
                    a.ok = False
                    a.status = "failed"
                    a.error = f"проверка не прошла: {v_detail}"
                    a.attempts.append({"try": attempt + 1, "ok": False, "verify": v_detail[:120]})
                    continue
                if v_detail:
                    a.output = f"{a.output}\nПроверка: {v_detail}".strip()
            a.status = "success"
            a.attempts.append({"try": attempt + 1, "ok": True, "ms": round(a.ms, 1)})
            break
        if a.status == "running":
            a.status = "failed" if not a.ok else "success"
        if self.metrics is not None:
            try:
                self.metrics.tool_call(a.tool, a.ms, a.ok, timeout=a.status == "timeout")
            except Exception:
                pass
        self._emit("observation", action=a, status=a.status)
        self.current = ""

    def _emit(self, kind: str, action: Action, status: str = "") -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit(kind, task_id=self.task_id, tool=action.tool,
                          args=_brief(action.args), status=status or action.status,
                          title=action.title, id=action.id)
        except Exception:
            pass


def _ok_of(res: Any) -> bool:
    if res is None:
        return False
    ok = getattr(res, "ok", None)
    if ok is None and isinstance(res, dict):
        ok = res.get("ok")
    return bool(ok)


def _output_of(res: Any) -> str:
    for name in ("output", "message", "summary"):
        v = getattr(res, name, None)
        if v is None and isinstance(res, dict):
            v = res.get(name)
        if v:
            return str(v)
    data = getattr(res, "data", None)
    return str(data)[:400] if data else ""


def _error_of(res: Any) -> str:
    for name in ("error", "message"):
        v = getattr(res, name, None)
        if v is None and isinstance(res, dict):
            v = res.get(name)
        if v:
            return str(v)
    return "не удалось"


__all__ = ["Action", "ActionQueue", "BatchResult", "STATUSES"]
