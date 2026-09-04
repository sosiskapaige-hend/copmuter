"""Очередь задач: последовательное/параллельное выполнение, фоновые задачи,
состояние каждой сохраняется (можно продолжить после перезапуска).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from ..agent.core import Agent
from ..events import EventBus
from ..memory.session import SessionStore, TaskState


@dataclass
class QueueItem:
    goal: str
    mode: str = ""
    parallel_ok: bool = False
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"      # queued | running | done | failed | cancelled
    background: bool = False


class TaskManager:
    """Работает внутри asyncio-лупа рантайма."""

    def __init__(self, agent: Agent, bus: EventBus, sessions: SessionStore) -> None:
        self.agent = agent
        self.bus = bus
        self.sessions = sessions
        self.queue: list[QueueItem] = []
        self._cond = asyncio.Condition()
        self._workers: list[asyncio.Task] = []
        self._started = False
        self.max_parallel = 2

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self.max_parallel):
            self._workers.append(asyncio.create_task(self._worker(i)))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._started = False

    async def enqueue(self, goal: str, mode: str = "", parallel_ok: bool = False,
                      background: bool = False) -> QueueItem:
        item = QueueItem(goal=goal, mode=mode, parallel_ok=parallel_ok,
                         background=background)
        async with self._cond:
            self.queue.append(item)
            self._cond.notify_all()
        self.bus.emit("task_state", task_id=item.task_id, state="queued", goal=goal)
        return item

    async def _worker(self, wid: int) -> None:
        while True:
            async with self._cond:
                while not self.queue:
                    await self._cond.wait()
                item = self.queue.pop(0)
            item.status = "running"
            self.bus.emit("task_state", task_id=item.task_id, state="running", goal=item.goal)
            try:
                st = await self.agent.run_task(item.goal, mode=item.mode, task_id=item.task_id)
                item.status = "done" if st.status == "done" else (
                    "cancelled" if st.status == "cancelled" else "failed")
            except asyncio.CancelledError:
                item.status = "cancelled"
                raise
            except Exception as e:  # noqa: BLE001
                item.status = "failed"
                self.bus.emit("log", level="error", message=f"worker {wid}: {e}")
            self.bus.emit("task_state", task_id=item.task_id, state=item.status,
                          goal=item.goal)
            if item.background:
                self.bus.emit("log", level="bg_done",
                              message=f"Фоновая задача завершена: {item.goal[:80]}")

    def queued_goals(self) -> list[str]:
        return [f"[{i.status}] {i.goal[:90]}" for i in self.queue]

    async def cancel_next(self) -> bool:
        async with self._cond:
            if self.queue:
                item = self.queue.pop(0)
                item.status = "cancelled"
                self.bus.emit("task_state", task_id=item.task_id, state="cancelled",
                              goal=item.goal)
                return True
        return False

    def pause_all(self) -> int:
        n = 0
        for tid in self.agent.list_running():
            if self.agent.pause_task(tid):
                n += 1
        return n

    def resume_all(self) -> int:
        n = 0
        for tid in self.agent.list_running():
            if self.agent.resume_task(tid):
                n += 1
        return n

    def stop_all(self) -> int:
        n = 0
        for tid in self.agent.list_running():
            if self.agent.stop_task(tid):
                n += 1
        return n
