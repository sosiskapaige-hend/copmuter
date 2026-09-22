"""SQLite-хранилище воркера: чаты, задачи, история инструментов, факты, метрики.

Память разделена (ТЗ §20): чат (messages), состояние ПК (tasks/observations),
долгосрочные предпочтения (facts/settings). Здесь — долговременная часть;
быстрое состояние держит рантайм в памяти.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    goal TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    steps INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    ttc_ms REAL NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL,
    finished_ms INTEGER
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    ok INTEGER NOT NULL DEFAULT 0,
    method TEXT NOT NULL DEFAULT '',
    fallback INTEGER NOT NULL DEFAULT 0,
    ms REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    step INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS apps (
    key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    uses INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    avg_ms REAL NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE,
    weight REAL NOT NULL DEFAULT 1,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '',
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Memory:
    """Тонкая обёртка над SQLite: потокобезопасная, без ORM."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ чаты
    def ensure_chat(self, chat_id: int | None, title: str = "") -> int:
        with self._lock:
            if chat_id:
                row = self._db.execute("SELECT id FROM chats WHERE id=?", (chat_id,)).fetchone()
                if row:
                    return int(row["id"])
            cur = self._db.execute(
                "INSERT INTO chats(title, created_ms, updated_ms) VALUES(?,?,?)",
                (title[:80], now_ms(), now_ms()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def add_message(self, chat_id: int, role: str, content: str, tool_name: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages(chat_id, role, content, tool_name, created_ms) VALUES(?,?,?,?,?)",
                (chat_id, role, content[:20000], tool_name, now_ms()),
            )
            self._db.execute("UPDATE chats SET updated_ms=? WHERE id=?", (now_ms(), chat_id))
            self._db.commit()

    def recent_messages(self, chat_id: int, limit: int = 24) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content, tool_name FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [
            {"role": row["role"], "content": row["content"], "tool_name": row["tool_name"]}
            for row in reversed(rows)
        ]

    # ------------------------------------------------------------------ задачи
    def start_task(self, goal: str, route: str = "", chat_id: int | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO tasks(chat_id, goal, route, status, created_ms) VALUES(?,?,?,?,?)",
                (chat_id, goal[:2000], route, "running", now_ms()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def finish_task(
        self,
        task_id: int,
        status: str,
        *,
        steps: int = 0,
        llm_calls: int = 0,
        tool_calls: int = 0,
        ttc_ms: float = 0.0,
    ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status=?, steps=?, llm_calls=?, tool_calls=?, ttc_ms=?, finished_ms=? "
                "WHERE id=?",
                (status, steps, llm_calls, tool_calls, ttc_ms, now_ms(), task_id),
            )
            self._db.commit()

    def add_tool_call(
        self,
        task_id: int | None,
        tool: str,
        args: dict | None = None,
        *,
        ok: bool = False,
        method: str = "",
        fallback: bool = False,
        ms: float = 0.0,
        error: str = "",
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO tool_calls(task_id, tool, args_json, ok, method, fallback, ms, error, created_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    tool,
                    json.dumps(args or {}, ensure_ascii=False)[:4000],
                    1 if ok else 0,
                    method,
                    1 if fallback else 0,
                    ms,
                    error[:500],
                    now_ms(),
                ),
            )
            self._db.commit()

    def add_observation(self, task_id: int | None, step: int, text: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO observations(task_id, step, text, created_ms) VALUES(?,?,?,?)",
                (task_id, step, text[:4000], now_ms()),
            )
            self._db.commit()

    # ------------------------------------------------------------------ приложения
    def remember_app(
        self, key: str, *, display_name: str = "", path: str = "", method: str = "",
        ok: bool = True, ms: float = 0.0,
    ) -> None:
        with self._lock:
            row = self._db.execute("SELECT uses, ok_count, fail_count, avg_ms FROM apps WHERE key=?",
                                   (key,)).fetchone()
            if row:
                uses = int(row["uses"]) + 1
                ok_count = int(row["ok_count"]) + (1 if ok else 0)
                fail_count = int(row["fail_count"]) + (0 if ok else 1)
                avg = float(row["avg_ms"])
                avg = ms if uses <= 1 else (avg * (uses - 1) + ms) / uses
                self._db.execute(
                    "UPDATE apps SET path=COALESCE(NULLIF(?,''), path), method=?, uses=?, ok_count=?,"
                    " fail_count=?, avg_ms=?, updated_ms=? WHERE key=?",
                    (path, method, uses, ok_count, fail_count, avg, now_ms(), key),
                )
            else:
                self._db.execute(
                    "INSERT INTO apps(key, display_name, path, method, uses, ok_count, fail_count, avg_ms,"
                    " updated_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, display_name, path, method, 1, 1 if ok else 0, 0 if ok else 1, ms, now_ms()),
                )
            self._db.commit()

    def app_path(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM apps WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ факты/настройки
    def remember_fact(self, text: str, weight: float = 1.0) -> None:
        text = text.strip()
        if len(text) < 3:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO facts(text, weight, created_ms) VALUES(?,?,?) "
                "ON CONFLICT(text) DO UPDATE SET weight=weight+?",
                (text[:500], weight, now_ms(), weight),
            )
            self._db.commit()

    def facts(self, limit: int = 12) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT text FROM facts ORDER BY weight DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [str(row["text"]) for row in rows]

    def forget_fact(self, text: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM facts WHERE text=?", (text,))
            self._db.commit()

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=False)),
            )
            self._db.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:  # pragma: no cover - старые записи
            return default

    # ------------------------------------------------------------------ метрики
    def metric(self, kind: str, name: str = "", value: float = 0.0, meta: Any = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO metrics(kind, name, value, meta, created_ms) VALUES(?,?,?,?,?)",
                (kind, name, float(value), json.dumps(meta, ensure_ascii=False)[:2000] if meta else "",
                 now_ms()),
            )
            self._db.commit()

    def metrics_summary(self, limit: int = 500) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, name, value FROM metrics ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        summary: dict[str, dict] = {}
        for row in rows:
            bucket = summary.setdefault(str(row["kind"]), {"count": 0, "total": 0.0, "max": 0.0})
            bucket["count"] += 1
            bucket["total"] += float(row["value"])
            bucket["max"] = max(bucket["max"], float(row["value"]))
        for bucket in summary.values():
            bucket["avg"] = bucket["total"] / bucket["count"] if bucket["count"] else 0.0
        return summary

    def task_stats(self, limit: int = 200) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT route, status, ttc_ms, llm_calls, tool_calls FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        total = len(rows)
        ok = sum(1 for row in rows if row["status"] == "done")
        ttc = [float(row["ttc_ms"]) for row in rows if row["ttc_ms"]]
        fast = sum(1 for row in rows if int(row["llm_calls"]) == 0)
        return {
            "tasks": total,
            "success_rate": (ok / total) if total else 0.0,
            "ttc_avg_ms": (sum(ttc) / len(ttc)) if ttc else 0.0,
            "fast_share": (fast / total) if total else 0.0,
            "llm_calls": sum(int(row["llm_calls"]) for row in rows),
            "tool_calls": sum(int(row["tool_calls"]) for row in rows),
        }

    def tool_stats(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT tool, COUNT(*) AS calls, SUM(ok) AS ok_count, AVG(ms) AS avg_ms FROM tool_calls "
                "GROUP BY tool ORDER BY calls DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def vacuum(self) -> None:
        with self._lock:
            self._db.execute("VACUUM")
            self._db.commit()
