"""Долговременная память: предпочтения пользователя и факты.

Хранится в JSON. Простой поиск по ключевым словам. UI позволяет просмотреть
и удалить сохранённые пункты (требование прозрачности).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path


class LongTermMemory:
    def __init__(self, mem_dir: Path) -> None:
        self.path = Path(mem_dir) / "longterm.json"
        self.items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.items = []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    def add(self, kind: str, text: str, tags: list[str] | None = None) -> dict:
        item = {"id": uuid.uuid4().hex[:8], "kind": kind or "fact",
                "text": text, "tags": tags or [], "ts": time.time()}
        # дедупликация
        for it in self.items:
            if it["text"].strip().lower() == text.strip().lower():
                it["ts"] = time.time()
                self._save()
                return it
        self.items.append(item)
        self._save()
        return item

    def remove(self, item_id: str) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != item_id]
        changed = len(self.items) != before
        if changed:
            self._save()
        return changed

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = query.lower()
        words = [w for w in re.split(r"\W+", q) if len(w) > 2]
        scored = []
        for it in self.items:
            text = it["text"].lower()
            score = sum(1 for w in words if w in text)
            if score:
                scored.append((score, it))
        scored.sort(key=lambda x: -x[0])
        return [it for _, it in scored[:limit]]

    def all(self) -> list[dict]:
        return list(self.items)

    def relevant_context(self, goal: str, limit: int = 5) -> str:
        hits = self.search(goal, limit=limit)
        if not hits:
            return ""
        lines = ["Память о пользователе (учитывай):"]
        for h in hits:
            lines.append(f"- [{h['kind']}] {h['text']}")
        return "\n".join(lines)
