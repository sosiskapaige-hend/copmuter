"""Журнал действий + undo.

Каждое действие фиксируется: инструмент, аргументы, результат, и undo-план
(если инструмент его вернул). «Отмени последние изменения» = прогон undo-планов
в обратном порядке.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.entries = []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.entries[-2000:], ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    def record(self, task_id: str, tool: str, args: dict, ok: bool,
               undo: dict | None = None, note: str = "") -> dict:
        entry = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.time(),
            "task_id": task_id,
            "tool": tool,
            "args": _small(args),
            "ok": ok,
            "undo": undo,
            "note": note,
            "undone": False,
        }
        self.entries.append(entry)
        if len(self.entries) > 4000:
            self.entries = self.entries[-2000:]
        self._save()
        return entry

    def pop_last_undoable(self) -> dict | None:
        for i in range(len(self.entries) - 1, -1, -1):
            e = self.entries[i]
            if e.get("undo") and not e.get("undone"):
                e["undone"] = True
                self._save()
                return e
        return None

    def recent(self, n: int = 20, task_id: str = "") -> list[dict]:
        out = [e for e in self.entries if not task_id or e.get("task_id") == task_id]
        return out[-n:]

    def stats(self) -> dict:
        return {
            "total": len(self.entries),
            "undoable": sum(1 for e in self.entries if e.get("undo") and not e.get("undone")),
            "last": self.entries[-1] if self.entries else None,
        }


def _small(v: Any) -> Any:
    """Обрезка больших аргументов для журнала."""
    if isinstance(v, str) and len(v) > 500:
        return v[:500] + "…"
    if isinstance(v, dict):
        return {k: _small(x) for k, x in list(v.items())[:20]}
    if isinstance(v, list):
        return [_small(x) for x in v[:20]]
    return v
