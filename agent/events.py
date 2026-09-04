"""Система событий агента: единая шина для UI (CLI, Web), лога и тестов.

Всё, что происходит в агенте (план, вызовы инструментов, наблюдения, ошибки,
просьбы подтверждения), приходит сюда как AgentEvent. Интерфейсы подписываются
на шину и решают, как это показывать.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


@dataclass
class AgentEvent:
    type: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "type": self.type, "ts": self.ts,
                           "data": self.data}, ensure_ascii=False, default=str)


class EventBus:
    """Тред-безопасная шина. Подписчики могут быть синхронными (вызываются
    в своем потоке) — поэтому callback обязан быть неблокирующим (например,
    put в очередь)."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[AgentEvent], None]] = []
        self._lock = threading.Lock()
        self._history: list[AgentEvent] = []
        self._history_limit = 500

    def subscribe(self, cb: Callable[[AgentEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(cb)

        def _unsub() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)
        return _unsub

    def emit(self, type_: str, **data: Any) -> AgentEvent:
        ev = AgentEvent(type=type_, data=data)
        with self._lock:
            self._history.append(ev)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit:]
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(ev)
            except Exception:  # подписчик не должен ломать агента
                pass
        return ev

    def history(self, last: int = 100) -> list[AgentEvent]:
        with self._lock:
            return list(self._history[-last:])

    def jsonl(self, last: int = 100) -> str:
        return "\n".join(e.to_json() for e in self.history(last))


# Типовые типы событий (контракт для UI)
T_PLAN = "plan"                  # план создан: data={task_id, steps:[{title,detail}]}
T_STEP = "step"                  # шаг: data={task_id, index, title, status}
T_THOUGHT = "thought"            # рассуждение LLM: data={text}
T_TOOL_CALL = "tool_call"        # data={name, args}
T_OBSERVATION = "observation"    # data={name, ok, output, data}
T_ERROR = "error"                # data={tool, error, analysis}
T_RETRY = "retry"                # data={tool, attempt, reason}
T_CONFIRM = "confirm_request"    # data={confirm_id, tool, args, reason, risk}
T_CONFIRM_RESULT = "confirm_result"  # data={confirm_id, approved, comment}
T_USER_ASK = "user_ask"          # data={ask_id, question, options}
T_USER_ANSWER = "user_answer"    # data={ask_id, answer}
T_PROGRESS = "progress"          # data={task_id, progress, remaining, message}
T_DONE = "task_done"             # data={task_id, ok, summary, details}
T_FAILED = "task_failed"         # data={task_id, error}
T_TASK_STATE = "task_state"      # data={task_id, state}
T_LOG = "log"                    # data={level, message}
T_SCREEN = "screen"              # data={path, monitor}
T_SYSTEM = "system"              # data={...статистика...}


class EventStream:
    """Асинхронная очередь событий для одного потребителя (напр., SSE-клиент)."""

    def __init__(self, bus: EventBus, maxsize: int = 1000) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._unsub: Callable[[], None] | None = None

    def _on_event(self, ev: AgentEvent) -> None:
        q = self._queue
        loop = getattr(self, "_loop", None)
        try:
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._put, ev)
            else:
                q.put_nowait(ev)
        except Exception:
            pass

    def _put(self, ev: AgentEvent) -> None:
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(ev)
            except Exception:
                pass

    async def start(self) -> None:
        import asyncio as _a
        self._loop = _a.get_running_loop()
        self._unsub = self._bus.subscribe(self._on_event)
        for ev in self._bus.history(50):
            await self._queue.put(ev)

    def close(self) -> None:
        if self._unsub:
            self._unsub()

    async def get(self, timeout: float = 15.0) -> AgentEvent | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None


# ---------- ожидания пользовательских решений ----------

class _Pending:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.future: asyncio.Future | None = None
        self.description: str = ""
        self.data: dict = {}


class InteractionGateway:
    """Остановка агента на вопросов/подтверждениях пользователя.

    Агент вызывает await gateway.ask(...) / gateway.confirm(...) и получает
    ответ, когда пользователь решит (через CLI, Web UI или API). Таймаут
    приводит к отказу/отмене.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def _register(self, kind: str, description: str, data: dict) -> tuple[str, _Pending]:
        """Создаёт pending-запись С будущей в текущем event loop (confirm/ask
        вызываются из агентского кода, который живёт в лупе)."""
        pid = f"{kind}_{uuid.uuid4().hex[:8]}"
        p = _Pending()
        p.description = description
        p.data = data
        try:
            loop = asyncio.get_running_loop()
            p.loop = loop
            p.future = loop.create_future()
        except RuntimeError:
            pass  # луп не найден — future создаст resolve() через submit
        with self._lock:
            self._pending[pid] = p
        return pid, p

    async def confirm(self, description: str, data: dict, timeout: float) -> tuple[bool, str]:
        pid, p = self._register("confirm", description, data)
        if p.future is None:
            return False, "нет event loop для подтверждения"
        self.bus.emit(T_CONFIRM, confirm_id=pid, description=description, **data)
        try:
            approved, comment = await asyncio.wait_for(asyncio.shield(p.future), timeout)
        except asyncio.TimeoutError:
            self.bus.emit(T_CONFIRM_RESULT, confirm_id=pid, approved=False,
                          comment="timeout — действие отклонено автоматически")
            return False, "timeout"
        finally:
            with self._lock:
                self._pending.pop(pid, None)
        self.bus.emit(T_CONFIRM_RESULT, confirm_id=pid, approved=approved, comment=comment)
        return approved, comment

    async def ask(self, question: str, options: list[str] | None = None,
                  timeout: float = 600.0) -> str:
        pid, p = self._register("ask", question, {"options": options or []})
        if p.future is None:
            return "(нет event loop для вопроса)"
        self.bus.emit(T_USER_ASK, ask_id=pid, question=question, options=options or [])
        try:
            answer = await asyncio.wait_for(asyncio.shield(p.future), timeout)
        except asyncio.TimeoutError:
            self.bus.emit(T_USER_ANSWER, ask_id=pid, answer="(нет ответа — таймаут)")
            return "(нет ответа от пользователя)"
        finally:
            with self._lock:
                self._pending.pop(pid, None)
        self.bus.emit(T_USER_ANSWER, ask_id=pid, answer=answer)
        return str(answer)

    def resolve(self, pid: str, value: Any) -> bool:
        with self._lock:
            p = self._pending.get(pid)
        if p is None:
            return False
        if p.future is None and p.loop is not None:
            p.future = p.loop.create_future()
        if p.future is None or p.future.done():
            return False
        if p.loop and not p.loop.is_running():
            return False
        p.future.set_result(value)
        return True

    def pending_list(self) -> list[dict]:
        with self._lock:
            return [{"id": k, "description": v.description, "data": v.data}
                    for k, v in self._pending.items()]
