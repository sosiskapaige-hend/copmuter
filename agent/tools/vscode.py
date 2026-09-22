"""Инструменты VS Code (ТЗ §16).

Правило: сначала CLI (`code`), потом UI-автоматизация, и только в крайнем
случае — «слепые» клики. CLI умеет почти всё, что нужно агенту: открыть файл,
открыть папку, показать строку (`-g file:line`), даже поставить расширение.
Поэтому сценарий «создай файл и напиши код» не требует ни зрения, ни мыши.

Крупный текст всегда передаётся через буфер обмена (ТЗ §14): генерация →
clipboard → Ctrl+V. Посимвольный ввод оставлен только для коротких строк.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .base import Risk, Tool, ToolContext, ToolResult


def _prop(t: str, desc: str) -> dict:
    return {"type": t, "description": desc}


CLI_CANDIDATES = [
    "code", "code-insiders", "codium", "vscodium",
]
WIN_PATHS = [
    r"Microsoft VS Code\bin\code.cmd",
    r"Microsoft VS Code Insiders\bin\code-insiders.cmd",
    r"Programs\Microsoft VS Code\bin\code.cmd",
    r"Programs\VSCodium\bin\codium.cmd",
]


def find_cli(extra: str = "") -> str | None:
    """Путь к CLI VS Code (или None)."""
    if extra and Path(extra).is_file():
        return extra
    for name in CLI_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        for rel in WIN_PATHS:
            p = Path(local) / rel
            if p.is_file():
                return str(p)
        for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not pf:
                continue
            for rel in (r"Microsoft VS Code\bin\code.cmd", r"Microsoft VS Code\Code.exe"):
                p = Path(pf) / rel
                if p.is_file():
                    return str(p)
    else:
        for p in ("/usr/share/code/bin/code", "/snap/bin/code", "/usr/bin/codium",
                  "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"):
            if Path(p).is_file():
                return p
    return None


def register_vscode_tools(reg) -> None:
    @reg.tool("open_vscode",
              "Открыть VS Code (CLI `code`), при желании сразу папку или файл.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Файл или папка для открытия"),
                  "new_window": _prop("boolean", "Открыть в новом окне"),
                  "line": _prop("integer", "Строка для перехода")},
                  "required": []})
    class OpenVSCode(Tool):
        async def execute(self, ctx: ToolContext, path: str = "", new_window: bool = False,
                          line: int = 0) -> ToolResult:
            cli = find_cli(str(ctx.service("vscode_cli", "") or ""))
            target = _resolve(ctx, path) if path else ""
            if cli:
                argv = [cli]
                if new_window:
                    argv.append("-n")
                if target and line:
                    argv += ["-g", f"{target}:{int(line)}"]
                elif target:
                    argv.append(target)
                try:
                    creation = 0
                    if os.name == "nt":
                        creation = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
                    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, creationflags=creation,
                                     start_new_session=(os.name != "nt"))
                    return ToolResult.ok_result(
                        f"Открываю VS Code{': ' + Path(target).name if target else ''} (CLI).",
                        method="vscode_cli", cli=cli, path=target)
                except Exception as exc:                    # noqa: BLE001
                    cli_error = str(exc)
            else:
                cli_error = "CLI `code` не найден"
            # запасной путь: реестр приложений + UI
            launcher = ctx.service("launcher")
            if launcher is not None:
                res = await launcher.launch(ctx, "vscode", target=target)
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(f"Открываю VS Code ({res.method}).",
                                                method=res.method, path=target)
                return ToolResult.fail(f"не удалось открыть VS Code: {cli_error}; {res.message}")
            if cli_error:
                return ToolResult.fail(cli_error)
            return ToolResult.fail("VS Code не найден")

    @reg.tool("create_file",
              "Создать файл (при необходимости с текстом), при желании открыть в "
              "редакторе. Понимает путь или папку + имя.",
              risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "content": _prop("string", "Содержимое"),
                  "directory": _prop("string", "Папка, если имя дано отдельно"),
                  "open_in_editor": _prop("boolean", "Открыть файл в VS Code")},
                  "required": ["path"]})
    class CreateFile(Tool):
        async def execute(self, ctx: ToolContext, path: str, content: str = "",
                          directory: str = "", open_in_editor: bool = False) -> ToolResult:
            p = _resolve(ctx, path, directory)
            if not p:
                return ToolResult.fail("не понял путь")
            if p.exists() and p.is_dir():
                return ToolResult.fail(f"{p} — это папка, а нужен файл")
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content or "", encoding="utf-8")
            except OSError as exc:
                return ToolResult.fail(f"не удалось создать файл: {exc}")
            created = p.stat().st_size
            out = f"Файл создан: {p} ({created} байт)"
            if open_in_editor:
                res = await ctx.service("registry").call("open_file", {"path": str(p)}, ctx)
                if res.ok:
                    out += f"\n{res.output}"
            return ToolResult.ok_result(out, path=str(p), bytes=created, method="fs_write")

    @reg.tool("open_file", "Открыть файл в VS Code (или системным редактором).",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "line": _prop("integer", "Номер строки")},
                  "required": ["path"]})
    class OpenFile(Tool):
        async def execute(self, ctx: ToolContext, path: str, line: int = 0) -> ToolResult:
            p = _resolve(ctx, path)
            if not p or not p.is_file():
                return ToolResult.fail(f"файл не найден: {path}")
            reg_tools = ctx.service("registry")
            r = await reg_tools.call("open_vscode", {"path": str(p), "line": line}, ctx)
            if r.ok:
                return ToolResult.ok_result(f"Открыл файл: {p.name}", method=r.data.get("method", "vscode_cli"))
            r2 = await reg_tools.call("open_path", {"path": str(p)}, ctx)
            if r2.ok:
                return ToolResult.ok_result(f"Открыл файл системно: {p.name}", method="open_path")
            return ToolResult.fail(r.error or r2.error or "не удалось открыть файл")

    @reg.tool("write_code",
              "Записать код в файл. Режимы: file — записать сразу (надёжно), "
              "paste — положить в буфер и вставить в активный редактор (Ctrl+V). "
              "Для больших текстов всегда используется буфер обмена.",
              risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "code": _prop("string", "Код"),
                  "mode": _prop("string", "auto | file | paste"),
                  "open_in_editor": _prop("boolean", "Открыть файл после записи")},
                  "required": ["code"]})
    class WriteCode(Tool):
        async def execute(self, ctx: ToolContext, code: str, path: str = "", mode: str = "auto",
                          open_in_editor: bool = False) -> ToolResult:
            mode = (mode or "auto").lower()
            target = _resolve(ctx, path) if path else None
            if mode in ("auto", "paste") and target is None:
                target = Path(ctx.workdir or ".") / "code.txt"
            if mode == "paste":
                inputs = ctx.service("inputs")
                if inputs is None:
                    return ToolResult.fail("вставка недоступна (нет подсистемы ввода)")
                await inputs.set_clipboard(code)
                res = await inputs.hotkey("ctrl+v")
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(
                        f"Код ({len(code)} символов) вставлен через буфер обмена.",
                        method="clipboard_paste", chars=len(code))
                return ToolResult.fail(f"не удалось вставить из буфера: {getattr(res, 'message', '')}")
            if target is None:
                return ToolResult.fail("нужен путь к файлу")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                existed = target.is_file()
                target.write_text(code, encoding="utf-8")
            except OSError as exc:
                return ToolResult.fail(f"не удалось записать: {exc}")
            out = f"{'Обновил' if existed else 'Записал'}: {target} ({len(code)} символов)"
            if open_in_editor:
                r = await ctx.service("registry").call("open_file", {"path": str(target)}, ctx)
                if r.ok:
                    out += f"\n{r.output}"
            return ToolResult.ok_result(out, path=str(target), chars=len(code), method="fs_write")

    @reg.tool("save_file",
              "Сохранить файл. С путём — записывает содержимое; без пути — Ctrl+S "
              "в активном окне редактора.",
              risk=Risk.LOW, category="fs", is_gui=True,
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь (если нужно записать содержимое)"),
                  "content": _prop("string", "Содержимое (только с путём)")},
                  "required": []})
    class SaveFile(Tool):
        async def execute(self, ctx: ToolContext, path: str = "", content: str = "") -> ToolResult:
            if path:
                p = _resolve(ctx, path)
                if p is None:
                    return ToolResult.fail("не понял путь")
                try:
                    if content:
                        p.write_text(content, encoding="utf-8")
                    if p.is_file():
                        return ToolResult.ok_result(f"Файл сохранён: {p} ({p.stat().st_size} байт)",
                                                    path=str(p), method="fs_write")
                    return ToolResult.fail(f"файл не найден: {p}")
                except OSError as exc:
                    return ToolResult.fail(f"не удалось сохранить: {exc}")
            inputs = ctx.service("inputs")
            hotkey_tool = ctx.service("registry")
            if inputs is not None:
                res = await inputs.hotkey("ctrl+s")
                if getattr(res, "ok", False):
                    return ToolResult.ok_result("Отправил Ctrl+S в активное окно.", method="sendinput")
            r = await hotkey_tool.call("keyboard_hotkey", {"keys": "ctrl+s"}, ctx)
            return ToolResult.ok_result(r.output if r.ok else "Ctrl+S отправлен",
                                        method="keyboard_hotkey") if r.ok else ToolResult.fail(r.error)

    @reg.tool("open_terminal",
              "Открыть терминал: в VS Code (Ctrl+`) или системный (Windows Terminal/CMD).",
              risk=Risk.LOW, category="apps", is_gui=True,
              parameters={"type": "object", "properties": {
                  "where": _prop("string", "vscode | system"),
                  "cwd": _prop("string", "Рабочая папка")},
                  "required": []})
    class OpenTerminal(Tool):
        async def execute(self, ctx: ToolContext, where: str = "vscode", cwd: str = "") -> ToolResult:
            where = (where or "vscode").lower()
            if where == "vscode":
                inputs = ctx.service("inputs")
                if inputs is not None:
                    res = await inputs.hotkey("ctrl+`")
                    if getattr(res, "ok", False):
                        return ToolResult.ok_result("Открыл терминал в VS Code (Ctrl+`).",
                                                    method="sendinput")
                reg_tools = ctx.service("registry")
                r = await reg_tools.call("keyboard_hotkey", {"keys": "ctrl+`"}, ctx)
                if r.ok:
                    return ToolResult.ok_result("Открыл терминал в VS Code.", method="keyboard_hotkey")
                return ToolResult.fail(r.error or "не удалось открыть терминал VS Code")
            reg_tools = ctx.service("registry")
            for name, argv in (("wt", ["wt.exe"]), ("cmd", ["cmd.exe"])):
                if shutil.which(argv[0]) or os.name == "nt":
                    r = await reg_tools.call("terminal_run", {"command": "start", "cwd": cwd or ctx.workdir},
                                             ctx)
                    if r.ok:
                        return ToolResult.ok_result("Открыл системный терминал.", method="shell_start")
            if sys.platform.startswith("linux"):
                for term in ("x-terminal-emulator", "gnome-terminal", "konsole"):
                    if shutil.which(term):
                        try:
                            subprocess.Popen([term], cwd=cwd or ctx.workdir, start_new_session=True)
                            return ToolResult.ok_result(f"Открыл терминал {term}.", method="spawn")
                        except OSError:
                            continue
            return ToolResult.fail("не нашёл терминал")

    @reg.tool("run_terminal_command",
              "Выполнить команду и вернуть результат структурно (код, stdout, "
              "stderr, время). Для длинных задач задавайте timeout.",
              risk=Risk.MEDIUM, category="shell",
              parameters={"type": "object", "properties": {
                  "command": _prop("string", "Команда"),
                  "cwd": _prop("string", "Рабочая папка"),
                  "timeout": _prop("number", "Таймаут, сек"),
                  "in_vscode": _prop("boolean", "Выполнить в терминале VS Code (ввод с клавиатуры)")},
                  "required": ["command"]})
    class RunTerminalCommand(Tool):
        def estimate_risk(self, ctx, args):
            from .terminal import assess_command
            return assess_command(str(args.get("command", "")))

        async def execute(self, ctx: ToolContext, command: str, cwd: str = "",
                          timeout: float = 60.0, in_vscode: bool = False) -> ToolResult:
            reg_tools = ctx.service("registry")
            if in_vscode:
                inputs = ctx.service("inputs")
                if inputs is not None:
                    await reg_tools.call("open_terminal", {"where": "vscode"}, ctx)
                    await inputs.type_text(command + "\n")
                    return ToolResult.ok_result(
                        f"Команда отправлена в терминал VS Code: {command} "
                        f"(результат смотрите в окне редактора).",
                        method="ui_automation", verified=None)
            r = await reg_tools.call("execute_command", {"command": command, "cwd": cwd or ctx.workdir,
                                                         "timeout": timeout}, ctx)
            return r

    @reg.tool("run_file",
              "Запустить файл (по расширению: .py — Python, .js — node, .sh — bash, "
              ".exe — напрямую) и вернуть вывод. Используется для проверки результата.",
              risk=Risk.MEDIUM, category="shell",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "args": _prop("string", "Аргументы"),
                  "cwd": _prop("string", "Рабочая папка"),
                  "timeout": _prop("number", "Таймаут, сек")},
                  "required": ["path"]})
    class RunFile(Tool):
        async def execute(self, ctx: ToolContext, path: str, args: str = "", cwd: str = "",
                          timeout: float = 60.0) -> ToolResult:
            p = _resolve(ctx, path)
            if not p or not p.is_file():
                return ToolResult.fail(f"файл не найден: {path}")
            ext = p.suffix.lower()
            runners = {".py": [sys.executable], ".js": ["node"], ".sh": ["bash"], ".ps1": ["powershell", "-File"],
                       ".bat": ["cmd", "/c"], ".rb": ["ruby"], ".pl": ["perl"]}
            argv = runners.get(ext)
            if argv is None and ext in (".exe", ".com") and os.name == "nt":
                argv = []
            if argv is None:
                return ToolResult.fail(f"не знаю, чем запускать {ext or 'файл'} — используйте execute_command")
            cmd = " ".join([*(f'"{a}"' if " " in str(a) else str(a) for a in argv), f'"{p}"', args]).strip()
            reg_tools = ctx.service("registry")
            r = await reg_tools.call("execute_command", {"command": cmd, "cwd": cwd or str(p.parent),
                                                         "timeout": timeout}, ctx)
            return r


def _resolve(ctx: ToolContext, path: str, directory: str = "") -> Path | None:
    """Путь из фразы/частичного пути → абсолютный Path."""
    raw = (path or "").strip().strip('"«»')
    if not raw:
        return None
    try:
        from ..system import paths as path_tools
        if directory:
            base = path_tools.place_dir(directory) or Path(directory)
            return (base / raw).expanduser()
        resolved, _place = path_tools.resolve_path(raw, fuzzy=False)
        if resolved is not None:
            return Path(os.path.expanduser(os.path.expandvars(str(resolved))))
    except Exception:
        pass
    p = Path(os.path.expanduser(os.path.expandvars(raw)))
    if not p.is_absolute():
        p = Path(ctx.workdir or ".") / p
    return p


__all__ = ["register_vscode_tools", "find_cli"]
