"""Расписание задач: «каждый день в 9 утра проверь сайт», «каждый час».

Поддерживаемые выражения:
  daily HH:MM        — каждый день в HH:MM
  weekly <dow> HH:MM — по дням недели (mon..sun, можно несколько через запятую)
  hourly             — каждый час
  every <N>m         — каждые N минут
  every <N>h         — каждые N часов
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass
class Schedule:
    id: str
    expr: str
    instruction: str
    mode: str = ""
    last_run: float = 0
    enabled: bool = True
    next_run_hint: str = ""


def parse_expr(expr: str) -> dict:
    e = expr.strip().lower()
    m = re.match(r"^daily\s+(\d{1,2}):(\d{2})$", e)
    if m:
        return {"kind": "daily", "hour": int(m.group(1)), "minute": int(m.group(2))}
    m = re.match(r"^weekly\s+([a-z,]+)\s+(\d{1,2}):(\d{2})$", e)
    if m:
        days = [_WEEKDAYS[d] for d in m.group(1).split(",") if d in _WEEKDAYS]
        return {"kind": "weekly", "days": days, "hour": int(m.group(2)), "minute": int(m.group(3))}
    if e == "hourly":
        return {"kind": "hourly"}
    m = re.match(r"^every\s+(\d+)(m|h|d)$", e)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        secs = n * (60 if unit == "m" else 3600 if unit == "h" else 86400)
        return {"kind": "interval", "seconds": secs}
    raise ValueError(f"не понял выражение расписания: '{expr}'. "
                     f"Форматы: daily 09:00 | weekly mon,wed 10:30 | hourly | every 30m | every 2h")


def is_due(s: Schedule, now: datetime) -> bool:
    spec = parse_expr(s.expr)
    k = spec["kind"]
    if k == "daily":
        return (now.hour, now.minute) == (spec["hour"], spec["minute"]) \
            and (now.timestamp() - s.last_run) > 3600
    if k == "weekly":
        if now.weekday() in spec["days"] and (now.hour, now.minute) == (spec["hour"], spec["minute"]):
            return (now.timestamp() - s.last_run) > 3600
        return False
    if k == "hourly":
        return now.minute == 0 and (now.timestamp() - s.last_run) > 3500
    if k == "interval":
        return (now.timestamp() - s.last_run) >= spec["seconds"]
    return False


class Scheduler:
    def __init__(self, state_dir: Path, on_fire: Callable[[Schedule], Awaitable[None]],
                 check_interval: float = 30.0) -> None:
        self.path = Path(state_dir) / "schedules.json"
        self.on_fire = on_fire
        self.check_interval = check_interval
        self.items: list[Schedule] = []
        self._task: asyncio.Task | None = None
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                for d in json.loads(self.path.read_text(encoding="utf-8")):
                    self.items.append(Schedule(**d))
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                [s.__dict__ for s in self.items], ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def add(self, expr: str, instruction: str, mode: str = "") -> Schedule:
        parse_expr(expr)  # валидация
        s = Schedule(id=uuid.uuid4().hex[:8], expr=expr.strip().lower(),
                     instruction=instruction, mode=mode)
        self.items.append(s)
        self._save()
        return s

    def remove(self, sid: str) -> bool:
        before = len(self.items)
        self.items = [s for s in self.items if s.id != sid]
        if len(self.items) != before:
            self._save()
            return True
        return False

    def list(self) -> list[Schedule]:
        return list(self.items)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        last_min: tuple[int, int] | None = None
        while True:
            try:
                now = datetime.now()
                for s in list(self.items):
                    if not s.enabled:
                        continue
                    if is_due(s, now):
                        s.last_run = time.time()
                        self._save()
                        await self.on_fire(s)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.check_interval)
