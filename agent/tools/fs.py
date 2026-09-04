"""Файловая система: полный набор операций + поиск по имени и СОДЕРЖИМОМУ.

Особенности:
- Удаление по умолчанию идёт в корзину агента (возврат через undo).
- Каждая изменяющая операция возвращает data["undo"] — обратное действие.
- Поиск по содержимому: текст, .docx (zip+xml), .pdf (pypdf, если есть),
  .xlsx (zip+xml, базово).
- Извлечение архивов с защитой от path traversal.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


def _p(path: str) -> Path:
    return Path(os.path.expanduser(str(path)))


def _docx_text(p: Path) -> str:
    try:
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    except Exception:
        return ""


def _xlsx_text(p: Path) -> str:
    try:
        with zipfile.ZipFile(p) as z:
            parts = []
            if "xl/sharedStrings.xml" in z.namelist():
                xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                parts.extend(re.findall(r"<t[^>]*>([^<]*)</t>", xml))
            for name in z.namelist():
                if re.match(r"xl/worksheets/sheet\d+\.xml", name):
                    xml = z.read(name).decode("utf-8", "replace")
                    parts.extend(re.findall(r"<v>([^<]*)</v>", xml))
            return "\n".join(parts)
    except Exception:
        return ""


def _pdf_text(p: Path) -> str:
    try:
        from pypdf import PdfReader
        r = PdfReader(str(p))
        return "\n".join((pg.extract_text() or "") for pg in r.pages[:50])
    except ImportError:
        return ""
    except Exception:
        return ""


_TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
              ".ini", ".cfg", ".csv", ".log", ".xml", ".html", ".css", ".sh", ".bat",
              ".ps1", ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp"}
_DOC_EXTS = {".docx", ".xlsx", ".pdf"}
_MAX_SCAN_SIZE = 5 * 1024 * 1024  # 5 МБ на файл при content-search


def _file_text(p: Path) -> str | None:
    """Текст файла или None (бинарный/нечитаемый)."""
    if p.suffix.lower() in _TEXT_EXTS:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    if p.suffix.lower() == ".docx":
        return _docx_text(p) or None
    if p.suffix.lower() == ".xlsx":
        return _xlsx_text(p) or None
    if p.suffix.lower() == ".pdf":
        return _pdf_text(p) or None
    return None


def _trash_path(cfg, p: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    unique = p.parent.name + "__" + p.name + "__" + stamp
    return Path(cfg.trash_dir) / unique


def _count_matches(root: Path, pattern: str, recursive: bool,
                   only_dirs: bool = False, only_files: bool = False) -> int:
    n = 0
    try:
        if recursive:
            for dp, dns, fns in os.walk(root):
                for name in (fns if only_files else fns + dns):
                    if fnmatch.fnmatch(name, pattern):
                        if only_dirs and not (Path(dp) / name).is_dir():
                            continue
                        if only_files and (Path(dp) / name).is_dir():
                            continue
                        n += 1
        else:
            for entry in root.iterdir():
                if fnmatch.fnmatch(entry.name, pattern):
                    if only_dirs and not entry.is_dir():
                        continue
                    if only_files and entry.is_dir():
                        continue
                    n += 1
    except (PermissionError, OSError):
        pass
    return n


def _walk_files(root: Path, max_results: int = 5000):
    count = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in {".git", "node_modules", "__pycache__",
                                              ".venv", "venv", ".idea"}]
        for fn in fns:
            yield Path(dp) / fn
            count += 1
            if count >= max_results:
                return


class FsTools:
    """Миксин-контейнер: все fs_* инструменты."""


def register_fs_tools(reg: ToolRegistry) -> None:

    @reg.tool("fs_read",
              "Прочитать текст/документ по пути. Поддерживает .docx, .xlsx, .pdf "
              "(если установлен pypdf) и обычные текстовые файлы.",
              risk=Risk.NONE, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "max_chars": _prop("integer", "Максимум символов (по умолчанию 20000)")},
                  "required": ["path"]})
    class FsRead(Tool):
        async def execute(self, ctx: ToolContext, path: str, max_chars: int = 20000) -> ToolResult:
            p = _p(path)
            if not p.is_file():
                return ToolResult.fail(f"файл не найден: {p}")
            text = _file_text(p)
            if text is None:
                return ToolResult.fail(f"бинарный файл, чтение текстом недоступно: {p.name}. "
                                       f"Для изображений/скриншотов используйте vision/ocr инструменты.",
                                       size=p.stat().st_size)
            size = len(text)
            if size > max_chars:
                text = text[:max_chars] + f"\n... [обрезано, всего {size} символов]"
            return ToolResult.ok_result(text, path=str(p.resolve()), size=size)

    @reg.tool("fs_write",
              "Создать/переписать файл текстом (создаёт родительские папки).",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "content": _prop("string", "Содержимое файла")},
                  "required": ["path", "content"]})
    class FsWrite(Tool):
        async def execute(self, ctx: ToolContext, path: str, content: str) -> ToolResult:
            p = _p(path)
            undo = None
            if p.exists():
                prev = p.read_bytes()
                backup = Path(ctx.cfg.state_dir / "backups") / f"{int(time.time()*1000)}_{p.name}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(prev)
                undo = {"tool": "fs_restore_backup", "args": {"backup": str(backup), "path": str(p)},
                        "note": f"восстановить {p.name} (версия до записи)"}
            else:
                undo = {"tool": "fs_delete", "args": {"path": str(p), "permanent": True},
                        "note": f"удалить {p.name} (он был создан агентом)"}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult.ok_result(f"Файл записан: {p} ({len(content)} символов)",
                                        path=str(p.resolve()), undo=undo)

    @reg.tool("fs_append", "Добавить текст в конец файла.", risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь"), "content": _prop("string", "Текст")},
                  "required": ["path", "content"]})
    class FsAppend(Tool):
        async def execute(self, ctx: ToolContext, path: str, content: str) -> ToolResult:
            p = _p(path)
            undo = None
            if p.exists():
                backup = Path(ctx.cfg.state_dir / "backups") / f"{int(time.time()*1000)}_{p.name}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(p.read_bytes())
                undo = {"tool": "fs_restore_backup", "args": {"backup": str(backup), "path": str(p)},
                        "note": "вернуть файл до дополнения"}
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
            return ToolResult.ok_result(f"Дополнен: {p}", undo=undo)

    @reg.tool("fs_mkdir", "Создать папку (с родительскими, если нужно).", risk=Risk.LOW,
              category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь папки")}, "required": ["path"]})
    class FsMkdir(Tool):
        async def execute(self, ctx: ToolContext, path: str) -> ToolResult:
            p = _p(path)
            if p.is_dir():
                return ToolResult.ok_result(f"Папка уже существует: {p}")
            p.mkdir(parents=True, exist_ok=True)
            return ToolResult.ok_result(f"Создана папка: {p}",
                                        undo={"tool": "fs_rmdir", "args": {"path": str(p)},
                                              "note": f"удалить {p.name} (создана агентом)"})

    @reg.tool("fs_delete",
              "Удалить файл или папку. Поддерживает glob-шаблоны (например, '*.tmp'). "
              "По умолчанию файлы уходят в корзину агента (возможно undo).",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь или glob-шаблон"),
                  "recursive": _prop("boolean", "Рекурсивно для папки (по умолчанию true)"),
                  "permanent": _prop("boolean", "Безвозвратно, без корзины (по умолчанию false)")},
                  "required": ["path"]})
    class FsDelete(Tool):
        async def execute(self, ctx: ToolContext, path: str, recursive: bool = True,
                          permanent: bool = False) -> ToolResult:
            p = _p(path)
            use_globs = any(c in path for c in "*?[")
            removed: list[str] = []
            undo_items: list[dict] = []
            try:
                if use_globs:
                    base = _p(os.path.dirname(path) or ".")
                    pat = os.path.basename(path)
                    matches: list[Path] = []
                    if base.is_dir():
                        try:
                            matches += [e for e in base.iterdir() if fnmatch.fnmatch(e.name, pat)]
                        except PermissionError:
                            pass
                        for f in _walk_files(base, 20000):
                            if fnmatch.fnmatch(f.name, pat):
                                matches.append(f)
                    matches = sorted(set(matches), key=str)
                else:
                    matches = [p]
            except OSError:
                matches = [p]
            if not matches:
                return ToolResult.fail(f"ничего не найдено по шаблону: {path}")
            for m in matches:
                if not m.exists():
                    continue
                if permanent:
                    if m.is_dir() and not m.is_symlink():
                        shutil.rmtree(m, ignore_errors=True)
                    else:
                        m.unlink(missing_ok=True)
                else:
                    if m.is_dir() and not m.is_symlink():
                        tp = _trash_path(ctx.cfg, m)
                        tp.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(m), str(tp))
                        undo_items.append({"from": str(tp), "to": str(m)})
                    else:
                        tp = _trash_path(ctx.cfg, m)
                        tp.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(m), str(tp))
                        undo_items.append({"from": str(tp), "to": str(m)})
                removed.append(str(m))
            undo = None
            if undo_items:
                undo = {"tool": "fs_restore_list", "args": {"items": undo_items},
                        "note": f"вернуть {len(undo_items)} элемент(ов) из корзины"}
            return ToolResult.ok_result(f"Удалено: {len(removed)} элемент(ов)"
                                        + ("" if permanent else " (в корзину агента, можно отменить)"),
                                        removed=removed, undo=undo)

    @reg.tool("fs_move", "Переместить файл/папку (или переименовать).", risk=Risk.MEDIUM,
              category="fs",
              parameters={"type": "object", "properties": {
                  "src": _prop("string", "Откуда"), "dst": _prop("string", "Куда")},
                  "required": ["src", "dst"]})
    class FsMove(Tool):
        async def execute(self, ctx: ToolContext, src: str, dst: str) -> ToolResult:
            s, d = _p(src), _p(dst)
            if not s.exists():
                return ToolResult.fail(f"не найдено: {s}")
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.exists() and d.is_dir():
                d = d / s.name
            shutil.move(str(s), str(d))
            return ToolResult.ok_result(f"Перемещено: {s} → {d}",
                                        undo={"tool": "fs_move", "args": {"src": str(d), "dst": str(s)},
                                              "note": "вернуть на прежнее место"})

    @reg.tool("fs_copy", "Скопировать файл/папку.", risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "src": _prop("string", "Откуда"), "dst": _prop("string", "Куда"),
                  "dirs": _prop("boolean", "Копировать папку целиком (по умолчанию true)")},
                  "required": ["src", "dst"]})
    class FsCopy(Tool):
        async def execute(self, ctx: ToolContext, src: str, dst: str, dirs: bool = True) -> ToolResult:
            s, d = _p(src), _p(dst)
            if not s.exists():
                return ToolResult.fail(f"не найдено: {s}")
            d.parent.mkdir(parents=True, exist_ok=True)
            if s.is_dir():
                if not d.exists():
                    shutil.copytree(str(s), str(d), dirs_exist_ok=True)
                else:
                    shutil.copytree(str(s), str(d), dirs_exist_ok=True)
                dst_final = d
            else:
                if d.is_dir():
                    dst_final = d / s.name
                else:
                    dst_final = d
                shutil.copy2(str(s), str(dst_final))
            return ToolResult.ok_result(f"Скопировано: {s} → {dst_final}",
                                        undo={"tool": "fs_delete",
                                              "args": {"path": str(dst_final), "permanent": True},
                                              "note": f"удалить копию {dst_final.name}"})

    @reg.tool("fs_restore_backup", "Восстановить файл из бэкапа (внутренний, для undo).",
              risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "backup": _prop("string", "Путь к бэкапу"), "path": _prop("string", "Цель")},
                  "required": ["backup", "path"]})
    class FsRestoreBackup(Tool):
        async def execute(self, ctx: ToolContext, backup: str, path: str) -> ToolResult:
            b, p = _p(backup), _p(path)
            if not b.exists():
                return ToolResult.fail(f"бэкап не найден: {b}")
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(b), str(p))
            return ToolResult.ok_result(f"Восстановлено: {p}")

    @reg.tool("fs_restore_list", "Вернуть элементы из корзины (внутренний, для undo).",
              risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "items": _prop("array", "Список {from, to}")}, "required": ["items"]})
    class FsRestoreList(Tool):
        async def execute(self, ctx: ToolContext, items: list) -> ToolResult:
            restored = 0
            for it in items or []:
                f, t = _p(it.get("from", "")), _p(it.get("to", ""))
                if f.exists() and not t.exists():
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(t))
                    restored += 1
            return ToolResult.ok_result(f"Возвращено из корзины: {restored} элемент(ов)")

    @reg.tool("fs_rmdir", "Удалить пустую (или созданную агентом) папку. Для непустых — fs_delete.",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Папка"), "force": _prop("boolean", "Удалить даже если непустая")},
                  "required": ["path"]})
    class FsRmdir(Tool):
        async def execute(self, ctx: ToolContext, path: str, force: bool = False) -> ToolResult:
            p = _p(path)
            if not p.exists():
                return ToolResult.ok_result(f"Папки уже нет: {p}")
            if p.is_file():
                return ToolResult.fail(f"это файл, не папка: {p}")
            if force or not any(p.iterdir()):
                shutil.rmtree(str(p), ignore_errors=True)
                return ToolResult.ok_result(f"Удалена папка: {p}")
            return ToolResult.fail(f"папка не пуста: {p}. Используйте fs_delete с подтверждением.")

    @reg.tool("fs_list", "Список файлов/папок в каталоге (с именем, размером, датой).",
              risk=Risk.NONE, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Каталог (по умолчанию текущая)"),
                  "pattern": _prop("string", "glob-фильтр имени, напр. '*.py'")},
                  "required": []})
    class FsList(Tool):
        async def execute(self, ctx: ToolContext, path: str = ".", pattern: str = "*") -> ToolResult:
            p = _p(path)
            if not p.is_dir():
                return ToolResult.fail(f"каталог не найден: {p}")
            rows = []
            for e in sorted(p.iterdir()):
                if pattern != "*" and not fnmatch.fnmatch(e.name, pattern):
                    continue
                try:
                    st = e.stat()
                    rows.append(f"{'<d> ' if e.is_dir() else ''}{e.name:<40} "
                                f"{st.st_size:>12} {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")
                except OSError:
                    rows.append(f"{e.name} (нет доступа)")
                if len(rows) >= 300:
                    rows.append(f"... (показано 300 из многих)")
                    break
            return ToolResult.ok_result("\n".join(rows) if rows else "(пусто)", count=len(rows))

    @reg.tool("fs_search",
              "Поиск файлов по имени/расширению/размеру/возрасту. Возвращает список путей.",
              risk=Risk.NONE, category="fs",
              parameters={"type": "object", "properties": {
                  "root": _prop("string", "Корневая папка поиска (по умолчанию текущая)"),
                  "pattern": _prop("string", "glob по имени, напр. 'report*.pdf' (по умолчанию *)"),
                  "ext": _prop("string", "только расширение, напр. 'pdf' (без точки)"),
                  "min_size_kb": _prop("number", "минимальный размер, КБ"),
                  "max_age_days": _prop("number", "только моложе N дней (по mtime)"),
                  "max_results": _prop("integer", "лимит результатов (по умолчанию 200)")},
                  "required": []})
    class FsSearch(Tool):
        async def execute(self, ctx: ToolContext, root: str = ".", pattern: str = "*",
                          ext: str = "", min_size_kb: float = 0, max_age_days: float | None = None,
                          max_results: int = 200) -> ToolResult:
            r = _p(root)
            if not r.exists():
                return ToolResult.fail(f"корень не найден: {r}")
            found: list[str] = []
            total_size = 0
            cutoff = time.time() - max_age_days * 86400 if max_age_days else None
            for f in _walk_files(r, max(max_results * 5, 10000)):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if pattern != "*" and not fnmatch.fnmatch(f.name, pattern):
                    continue
                if ext and f.suffix.lower() != (ext if ext.startswith(".") else "." + ext.lower()):
                    continue
                if st.st_size < min_size_kb * 1024:
                    continue
                if cutoff and st.st_mtime < cutoff:
                    continue
                found.append(str(f))
                total_size += st.st_size
                if len(found) >= max_results:
                    break
            out = "\n".join(found) if found else "(ничего не найдено)"
            return ToolResult.ok_result(out, count=len(found),
                                        total_mb=round(total_size / 1048576, 2), paths=found)

    @reg.tool("fs_search_content",
              "ПОИСК ПО СОДЕРЖИМОМУ файлов: текст, .docx, .pdf, .xlsx. "
              "Пример: «найди документ, где упоминается договор с Ивановым».",
              risk=Risk.NONE, category="fs",
              parameters={"type": "object", "properties": {
                  "root": _prop("string", "Корневая папка"),
                  "text": _prop("string", "Подстрока (без учёта регистра)"),
                  "regex": _prop("string", "Или регулярное выражение (в приоритете)"),
                  "exts": _prop("string", "Фильтр расширений через запятую: 'pdf,docx,txt' (по умолчанию — читаемые)"),
                  "max_results": _prop("integer", "Лимит (по умолчанию 50)")},
                  "required": ["root"]})
    class FsSearchContent(Tool):
        async def execute(self, ctx: ToolContext, root: str, text: str = "",
                          regex: str = "", exts: str = "", max_results: int = 50) -> ToolResult:
            r = _p(root)
            if not r.exists():
                return ToolResult.fail(f"корень не найден: {r}")
            needle = (text or "").lower()
            rx = None
            if regex:
                try:
                    rx = re.compile(regex, re.IGNORECASE)
                except re.error as e:
                    return ToolResult.fail(f"неверный regex: {e}")
            if not needle and rx is None:
                return ToolResult.fail("нужен text или regex")
            if exts:
                allowed = {"." + e.strip().lstrip(".").lower() for e in exts.split(",") if e.strip()}
            else:
                allowed = _TEXT_EXTS | _DOC_EXTS
            hits: list[str] = []
            scanned = 0
            for f in _walk_files(r, 30000):
                if f.suffix.lower() not in allowed:
                    continue
                try:
                    if f.stat().st_size > _MAX_SCAN_SIZE:
                        continue
                except OSError:
                    continue
                scanned += 1
                t = _file_text(f)
                if not t:
                    continue
                if needle and needle in t.lower() or (rx and rx.search(t)):
                    line = ""
                    if needle:
                        low = t.lower()
                        i = low.find(needle)
                        if i >= 0:
                            line = " | " + re.sub(r"\s+", " ", t[max(0, i - 80):i + len(needle) + 80])
                    hits.append(f"{f}{line}")
                    if len(hits) >= max_results:
                        break
            out = "\n".join(hits) if hits else "(совпадений не найдено)"
            return ToolResult.ok_result(out, count=len(hits), scanned=scanned)

    @reg.tool("fs_info", "Информация о файлах: размер, даты, тип.", risk=Risk.NONE, category="fs",
              parameters={"type": "object", "properties": {
                  "paths": _prop("array", "Список путей")}, "required": ["paths"]})
    class FsInfo(Tool):
        async def execute(self, ctx: ToolContext, paths: list) -> ToolResult:
            rows = []
            for raw in paths[:50]:
                p = _p(raw)
                if not p.exists():
                    rows.append(f"{raw}: НЕ НАЙДЕНО")
                    continue
                st = p.stat()
                kind = "dir" if p.is_dir() else p.suffix or "file"
                rows.append(f"{p} | {kind} | {st.st_size} Б | "
                            f"изменён {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")
            return ToolResult.ok_result("\n".join(rows))

    @reg.tool("fs_archive",
              "Создать архив (zip) или распаковать его. extract: безопасное извлечение "
              "(защита от path traversal).",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "action": _prop("string", "create | extract"),
                  "path": _prop("string", "Файл/папка для create или архив для extract"),
                  "dest": _prop("string", "Путь архива (create) или папка извлечения (extract)")},
                  "required": ["action", "path"]})
    class FsArchive(Tool):
        async def execute(self, ctx: ToolContext, action: str, path: str, dest: str = "") -> ToolResult:
            p = _p(path)
            if action == "create":
                out = _p(dest) if dest else p.with_suffix(p.suffix + ".zip") if p.is_dir() else _p(str(p) + ".zip")
                out.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    if p.is_dir():
                        for f in _walk_files(p, 200000):
                            z.write(f, f.relative_to(p))
                    else:
                        z.write(p, p.name)
                return ToolResult.ok_result(f"Архив создан: {out} ({out.stat().st_size} Б)",
                                            undo={"tool": "fs_delete",
                                                  "args": {"path": str(out), "permanent": True},
                                                  "note": f"удалить {out.name}"})
            if action == "extract":
                out_dir = _p(dest) if dest else p.with_suffix("")
                out_dir.mkdir(parents=True, exist_ok=True)
                out_resolved = out_dir.resolve()
                try:
                    if p.name.lower().endswith((".tar", ".gz", ".tgz")):
                        with tarfile.open(p) as t:
                            for m in t.getmembers():
                                target = (out_dir / m.name).resolve()
                                if not str(target).startswith(str(out_resolved)):
                                    return ToolResult.fail(f"опасный элемент в архиве: {m.name}")
                            t.extractall(out_dir)
                    else:
                        with zipfile.ZipFile(p) as z:
                            for n in z.namelist():
                                target = (out_dir / n).resolve()
                                if not str(target).startswith(str(out_resolved)):
                                    return ToolResult.fail(f"опасный элемент в архиве: {n}")
                            z.extractall(out_dir)
                    return ToolResult.ok_result(f"Распаковано в: {out_dir}",
                                                undo={"tool": "fs_delete",
                                                      "args": {"path": str(out_dir), "permanent": True},
                                                      "note": f"удалить {out_dir.name} (распаковано агентом)"})
                except (zipfile.BadZipFile, tarfile.TarError) as e:
                    return ToolResult.fail(f"архив повреждён: {e}")
            return ToolResult.fail(f"неизвестное действие: {action}")

    @reg.tool("fs_organize",
              "Массовая сортировка файлов: разложить файлы по подпапкам по месяцу "
              "(YYYY-MM), по расширению (name) или по размеру. Возвращает undo.",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "dir": _prop("string", "Папка, где файлы"),
                  "exts": _prop("string", "Расширения через запятую, напр. 'jpg,jpeg,png' (по умолчанию — фото)"),
                  "by": _prop("string", "month | ext (по умолчанию month)")},
                  "required": ["dir"]})
    class FsOrganize(Tool):
        async def execute(self, ctx: ToolContext, dir: str, exts: str = "jpg,jpeg,png,webp,heic",
                          by: str = "month") -> ToolResult:
            d = _p(dir)
            if not d.is_dir():
                return ToolResult.fail(f"папка не найдена: {d}")
            allowed = {("." + e.strip().lstrip(".").lower()) for e in exts.split(",") if e.strip()}
            moved: list[tuple[str, str]] = []
            for f in sorted(d.iterdir()):
                if not f.is_file() or f.suffix.lower() not in allowed:
                    continue
                st = f.stat()
                if by == "month":
                    sub = time.strftime("%Y-%m", time.localtime(st.st_mtime))
                else:
                    sub = (f.suffix.lstrip(".").upper() or "OTHER")
                target = d / sub / f.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = d / sub / f"{f.stem}_{int(st.st_mtime)}{f.suffix}"
                shutil.move(str(f), str(target))
                moved.append((str(f), str(target)))
            undo = None
            if moved:
                undo = {"tool": "fs_restore_list",
                        "args": {"items": [{"from": dst, "to": src} for src, dst in moved]},
                        "note": f"вернуть {len(moved)} файлов в {d.name}"}
            return ToolResult.ok_result(f"Разложено: {len(moved)} файл(ов) по папкам ({by})",
                                        moved=len(moved), undo=undo)
