"""Триггеры: реакция на изменения. «Когда в Downloads появится PDF — сделай X».

Реализация: polling (каждые 2-3 с) + watchdog, если установлен (быстрее).
Состояние «уже обработанных» файлов хранится, чтобы не дублировать.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable


@dataclass
class FolderTrigger:
    id: str
    path: str
    pattern: str = "*"
    instruction: str = ""
    enabled: bool = True
    cooldown: float = 5.0
    processed: list[str] = field(default_factory=list)
    last_fire: float = 0


def _known_files(d: Path, pattern: str, limit: int = 5000) -> set[str]:
    out: set[str] = set()
    try:
        for f in d.iterdir():
            if f.is_file() and fnmatch.fnmatch(f.name, pattern):
                out.add(str(f))
                if len(out) >= limit:
                    break
    except (PermissionError, OSError):
        pass
    return out


class TriggerManager:
    def __init__(self, state_dir: Path, on_fire: Callable[[FolderTrigger, Path], Awaitable[None]],
                 poll: float = 3.0) -> None:
        self.path = Path(state_dir) / "triggers.json"
        self.on_fire = on_fire
        self.poll = poll
        self.items: list[FolderTrigger] = []
        self._task: asyncio.Task | None = None
        self._baseline: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                for d in json.loads(self.path.read_text(encoding="utf-8")):
                    self.items.append(FolderTrigger(**d))
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for s in self.items:
                s.processed = s.processed[-200:]
            self.path.write_text(json.dumps(
                [s.__dict__ for s in self.items], ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def add(self, path: str, pattern: str, instruction: str) -> FolderTrigger:
        t = FolderTrigger(id=uuid.uuid4().hex[:8], path=path, pattern=pattern,
                          instruction=instruction)
        self.items.append(t)
        d = Path(path)
        if d.is_dir():
            self._baseline[t.id] = _known_files(d, pattern)
        self._save()
        return t

    def remove(self, tid: str) -> bool:
        before = len(self.items)
        self.items = [t for t in self.items if t.id != tid]
        self._baseline.pop(tid, None)
        if len(self.items) != before:
            self._save()
            return True
        return False

    def list(self) -> list[FolderTrigger]:
        return self.items

    async def start(self) -> None:
        if self._task is None:
            for t in self.items:
                d = Path(t.path)
                if d.is_dir() and t.id not in self._baseline:
                    self._baseline[t.id] = _known_files(d, t.pattern)
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                for t in list(self.items):
                    if not t.enabled:
                        continue
                    d = Path(t.path)
                    if not d.is_dir():
                        continue
                    if t.id not in self._baseline:
                        self._baseline[t.id] = _known_files(d, t.pattern)
                    now_files = _known_files(d, t.pattern)
                    new = now_files - self._baseline[t.id]
                    self._baseline[t.id] = now_files
                    if new and (time.time() - t.last_fire) >= t.cooldown:
                        # берём самый свежий новый файл
                        fresh = max(new, key=lambda p: Path(p).stat().st_mtime)
                        t.last_fire = time.time()
                        t.processed.append(fresh)
                        self._save()
                        await self.on_fire(t, Path(fresh))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.poll)
