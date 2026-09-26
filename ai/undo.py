"""Журнал транзакций и откат операций (Undo) (ТЗ §37, §248).

Все обратимые операции с файлами и папками (создание, перезапись, удаление, перемещение)
фиксируются в журнале. При удалении файлы перемещаются в защищённый локальный каталог отката,
а при перезаписи сохраняется резервная копия оригинала.

Это позволяет отменять действия: «отмени последнее действие», «верни удалённый файл».
"""

from __future__ import annotations

from contextlib import closing

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any


class UndoActionType:
    CREATE_FILE = "create_file"
    CREATE_DIR = "create_dir"
    DELETE_PATH = "delete_path"
    MODIFY_FILE = "modify_file"
    MOVE_PATH = "move_path"


class TransactionJournal:
    """Журнал обратимых файловых операций."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            base_dir = Path.home() / ".local" / "share" / "copmuter" / "undo"
        self.base_dir = Path(base_dir)
        self.trash_dir = self.base_dir / "trash"
        self.backup_dir = self.base_dir / "backups"
        self.db_path = self.base_dir / "journal.db"

        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    backup_path TEXT DEFAULT '',
                    task_id INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    undone INTEGER DEFAULT 0,
                    created_ms INTEGER NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_undone ON journal(undone, id);")

    def record_create(self, path: str | Path, is_dir: bool = False, task_id: int = 0) -> int:
        """Регистрирует создание файла или директории."""
        p = str(Path(path).resolve())
        action = UndoActionType.CREATE_DIR if is_dir else UndoActionType.CREATE_FILE
        desc = f"Создан {'каталог' if is_dir else 'файл'}: {p}"
        now_ms = int(time.time() * 1000)

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO journal (action, target_path, backup_path, task_id, description, undone, created_ms)
                VALUES (?, ?, '', ?, ?, 0, ?)
                """,
                (action, p, task_id, desc, now_ms),
            )
            return cur.lastrowid or 0

    def prepare_delete(self, path: str | Path, task_id: int = 0) -> tuple[int, str]:
        """Безопасное удаление: перемещает в папку отката вместо полного уничтожения."""
        p = Path(path).resolve()
        if not p.exists():
            return 0, ""

        now_ms = int(time.time() * 1000)
        backup_name = f"{now_ms}_{p.name}"
        backup_dest = self.trash_dir / backup_name

        try:
            shutil.move(str(p), str(backup_dest))
        except Exception:
            # Fallback к копированию и удалению
            if p.is_dir():
                shutil.copytree(str(p), str(backup_dest))
                shutil.rmtree(str(p))
            else:
                shutil.copy2(str(p), str(backup_dest))
                p.unlink()

        desc = f"Удалён объект: {p} (сохранён в корзине отката)"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO journal (action, target_path, backup_path, task_id, description, undone, created_ms)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (UndoActionType.DELETE_PATH, str(p), str(backup_dest), task_id, desc, now_ms),
            )
            return cur.lastrowid or 0, str(backup_dest)

    def prepare_modify(self, path: str | Path, task_id: int = 0) -> int:
        """Сохраняет резервную копию файла перед модификацией/перезаписью."""
        p = Path(path).resolve()
        if not p.is_file():
            return 0

        now_ms = int(time.time() * 1000)
        backup_name = f"{now_ms}_{p.name}"
        backup_dest = self.backup_dir / backup_name

        try:
            shutil.copy2(str(p), str(backup_dest))
        except Exception:
            return 0

        desc = f"Изменён файл: {p}"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO journal (action, target_path, backup_path, task_id, description, undone, created_ms)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (UndoActionType.MODIFY_FILE, str(p), str(backup_dest), task_id, desc, now_ms),
            )
            return cur.lastrowid or 0

    def undo_last(self, task_id: int | None = None) -> tuple[bool, str]:
        """Откатывает последнюю выполненную файловую операцию."""
        query = "SELECT id, action, target_path, backup_path, description FROM journal WHERE undone = 0 "
        params: list[Any] = []
        if task_id:
            query += "AND task_id = ? "
            params.append(task_id)
        query += "ORDER BY id DESC LIMIT 1"

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cur = conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                return False, "Журнал отката пуст: нет действий для отмены"

            jid, action, target_path, backup_path, desc = row
            target = Path(target_path)
            backup = Path(backup_path) if backup_path else None

            try:
                if action == UndoActionType.CREATE_FILE:
                    if target.is_file():
                        target.unlink()
                    msg = f"Отменено создание файла: удалён {target}"

                elif action == UndoActionType.CREATE_DIR:
                    if target.is_dir():
                        shutil.rmtree(str(target))
                    msg = f"Отменено создание каталога: удалён {target}"

                elif action == UndoActionType.DELETE_PATH:
                    if backup and backup.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(backup), str(target))
                        msg = f"Восстановлен удалённый объект: {target}"
                    else:
                        msg = f"Не удалось восстановить {target}: резервная копия не найдена"
                        return False, msg

                elif action == UndoActionType.MODIFY_FILE:
                    if backup and backup.exists():
                        shutil.copy2(str(backup), str(target))
                        msg = f"Восстановлена предыдущая версия файла: {target}"
                    else:
                        msg = f"Не удалось восстановить {target}: резервная копия не найдена"
                        return False, msg
                else:
                    return False, f"Неизвестный тип действия: {action}"

                cur.execute("UPDATE journal SET undone = 1 WHERE id = ?", (jid,))
                conn.commit()
                return True, msg

            except Exception as exc:
                return False, f"Ошибка отката действия ({desc}): {exc}"

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Возвращает недавние записи журнала."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, action, target_path, task_id, description, undone, created_ms
                FROM journal
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
