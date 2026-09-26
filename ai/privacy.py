"""Центр конфиденциальности: инвентаризация, экспорт и безопасное удаление данных (ТЗ §149, §150, §151).

Позволяет пользователю:
  1. Видеть, какие данные хранятся на локальном компьютере;
  2. Экспортировать историю диалогов, задачи, память и настройки в JSON;
  3. Выборочно или полностью стирать историю и память с выполнением VACUUM;
  4. Проверять целостность базы данных SQLite.
"""

from __future__ import annotations

from contextlib import closing

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .security.secrets import redact_secrets


@dataclass
class PrivacyInventory:
    chats_count: int = 0
    messages_count: int = 0
    tasks_count: int = 0
    tool_calls_count: int = 0
    facts_count: int = 0
    db_size_bytes: int = 0
    undo_records_count: int = 0
    trash_size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chats": self.chats_count,
            "messages": self.messages_count,
            "tasks": self.tasks_count,
            "tool_calls": self.tool_calls_count,
            "facts": self.facts_count,
            "db_size_kb": round(self.db_size_bytes / 1024, 1),
            "undo_records": self.undo_records_count,
            "trash_size_kb": round(self.trash_size_bytes / 1024, 1),
        }


class PrivacyManager:
    """Управление локальными данными и конфиденциальностью."""

    def __init__(self, db_path: str | Path, state_dir: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.state_dir = Path(state_dir) if state_dir else self.db_path.parent
        self.undo_dir = self.state_dir / "undo"

    def get_inventory(self) -> PrivacyInventory:
        """Собирает отчёт о хранящихся данных пользователя."""
        inv = PrivacyInventory()
        if self.db_path.exists():
            inv.db_size_bytes = self.db_path.stat().st_size
            try:
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM chats")
                    inv.chats_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM messages")
                    inv.messages_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM tasks")
                    inv.tasks_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM tool_calls")
                    inv.tool_calls_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM facts")
                    inv.facts_count = cur.fetchone()[0]
            except Exception:
                pass

        if self.undo_dir.exists():
            trash_dir = self.undo_dir / "trash"
            if trash_dir.exists():
                for f in trash_dir.rglob("*"):
                    if f.is_file():
                        inv.trash_size_bytes += f.stat().st_size
            journal_db = self.undo_dir / "journal.db"
            if journal_db.exists():
                try:
                    with closing(sqlite3.connect(journal_db)) as conn, conn:
                        cur = conn.cursor()
                        cur.execute("SELECT COUNT(*) FROM journal")
                        inv.undo_records_count = cur.fetchone()[0]
                except Exception:
                    pass

        return inv

    def export_data(self, output_file: str | Path, include_system_facts: bool = True) -> str:
        """Экспортирует все данные пользователя в JSON-файл с маскированием секретов."""
        out_path = Path(output_file).resolve()
        data: dict[str, Any] = {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chats": [],
            "tasks": [],
            "facts": [],
        }

        if self.db_path.exists():
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # Чаты и сообщения
                chats = cur.execute("SELECT * FROM chats ORDER BY id").fetchall()
                for c in chats:
                    chat_dict = dict(c)
                    msgs = cur.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (c["id"],)).fetchall()
                    chat_dict["messages"] = [
                        {
                            "role": m["role"],
                            "content": redact_secrets(m["content"])[0],
                            "tool_name": m["tool_name"],
                            "created_ms": m["created_ms"],
                        }
                        for m in msgs
                    ]
                    data["chats"].append(chat_dict)

                # Задачи
                tasks = cur.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 100").fetchall()
                for t in tasks:
                    t_dict = dict(t)
                    t_dict["goal"] = redact_secrets(t_dict["goal"])[0]
                    data["tasks"].append(t_dict)

                # Факты
                if include_system_facts:
                    facts = cur.execute("SELECT * FROM facts ORDER BY id").fetchall()
                    data["facts"] = [
                        {"text": redact_secrets(f["text"])[0], "weight": f["weight"], "created_ms": f["created_ms"]}
                        for f in facts
                    ]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(out_path)

    def purge_data(self, categories: list[str]) -> dict[str, bool]:
        """Удаляет выбранные категории данных."""
        res: dict[str, bool] = {}
        cat_set = set(categories)
        purge_all = "all" in cat_set

        if self.db_path.exists():
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                cur = conn.cursor()
                if purge_all or "chats" in cat_set or "messages" in cat_set:
                    cur.execute("DELETE FROM messages")
                    cur.execute("DELETE FROM chats")
                    res["chats"] = True

                if purge_all or "tasks" in cat_set:
                    cur.execute("DELETE FROM observations")
                    cur.execute("DELETE FROM tool_calls")
                    cur.execute("DELETE FROM tasks")
                    res["tasks"] = True

                if purge_all or "facts" in cat_set or "memory" in cat_set:
                    cur.execute("DELETE FROM facts")
                    res["facts"] = True

                conn.commit()
                if purge_all:
                    conn.execute("VACUUM")

        if purge_all or "undo" in cat_set:
            if self.undo_dir.exists():
                try:
                    shutil.rmtree(str(self.undo_dir))
                    self.undo_dir.mkdir(parents=True, exist_ok=True)
                    res["undo"] = True
                except Exception:
                    res["undo"] = False

        return res

    def verify_integrity(self) -> tuple[bool, str]:
        """Проверяет целостность базы данных SQLite."""
        if not self.db_path.exists():
            return True, "База данных ещё не создана"
        try:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check")
                row = cur.fetchone()
                status = row[0] if row else "unknown"
                ok = status == "ok"
                # Заодно выполняем WAL checkpoint
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                return ok, status
        except Exception as exc:
            return False, f"Ошибка проверки целостности: {exc}"
