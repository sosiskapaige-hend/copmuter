"""Фоновые задачи и наблюдение: «скачивай и сообщи когда закончится»,
«следи за программой/папкой и скажи когда...».

Reальный «watch» строится из готовых кирпичей: background-задача +
терминал/процессы/триггеры. Здесь — обёртка для запуска произвольных
асинхронных функций в фоне с уведомлением.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

from ..events import EventBus


class BackgroundRunner:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit(self, name: str, coro: Awaitable[Any]) -> str:
        jid = uuid.uuid4().hex[:8]
        self.jobs[jid] = {"name": name, "status": "running",
                          "started": time.time(), "result": None, "error": None}
        self.bus.emit("log", level="bg", message=f"Фоновая задача запущена: {name}")
        task = asyncio.create_task(self._run(jid, coro))
        self._tasks[jid] = task
        return jid

    async def _run(self, jid: str, coro: Awaitable[Any]) -> None:
        job = self.jobs[jid]
        try:
            job["result"] = await coro
            job["status"] = "done"
            self.bus.emit("log", level="bg_done",
                          message=f"Фоновая задача завершена: {job['name']}. "
                                  f"Результат: {str(job['result'])[:200]}")
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(e)
            self.bus.emit("log", level="bg_error",
                          message=f"Фоновая задача завершилась ошибкой: {job['name']}: {e}")
        finally:
            job["finished"] = time.time()

    def status(self, jid: str) -> dict | None:
        return self.jobs.get(jid)

    def all(self) -> list[dict]:
        return list(self.jobs.values())

    async def cancel(self, jid: str) -> bool:
        t = self._tasks.get(jid)
        if t:
            t.cancel()
            self.jobs[jid]["status"] = "cancelled"
            return True
        return False
