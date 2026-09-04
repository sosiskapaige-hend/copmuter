"""Запуск/закрытие приложений.

Регистр известных приложений (config.apps) + поиск в PATH + startfile на
Windows / xdg-open на Linux. Это НЕ закрытый список: при отсутствии в
регистре агент может воспользоваться terminal_run или GUI-поиском.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry

_DEFAULTS = {
    "vscode": {"win32": "code", "linux": "code", "darwin": "code"},
    "code": {"win32": "code", "linux": "code", "darwin": "code"},
    "chrome": {"win32": "chrome", "linux": "google-chrome", "darwin": "open -a 'Google Chrome'"},
    "firefox": {"win32": "firefox", "linux": "firefox", "darwin": "open -a Firefox"},
    "explorer": {"win32": "explorer", "linux": "xdg-open", "darwin": "open"},
    "terminal": {"win32": "wt", "linux": "x-terminal-emulator", "darwin": "open -a Terminal"},
    "notepad": {"win32": "notepad", "linux": "gedit", "darwin": "open -a TextEdit"},
    "calc": {"win32": "calc", "linux": "gnome-calculator", "darwin": "open -a Calculator"},
    "settings": {"win32": "ms-settings:", "linux": "gnome-control-center", "darwin": "open 'x-apple.systempreferences:'"},
    "taskmgr": {"win32": "taskmgr", "linux": "gnome-system-monitor", "darwin": "open -a 'Activity Monitor'"},
}


def _resolve(ctx: ToolContext, name: str) -> str | None:
    plat = {"windows": "win32", "linux": "linux", "macos": "darwin"}[ctx.platform.system]
    apps = {**_DEFAULTS, **(ctx.cfg.apps or {})}
    key = name.lower().strip()
    entry = apps.get(key)
    if isinstance(entry, dict):
        cmd = entry.get(plat) or entry.get("linux")
    else:
        cmd = str(entry)
    if cmd:
        return cmd
    in_path = shutil.which(key)
    if in_path:
        return in_path
    return None


def register_app_tools(reg: ToolRegistry) -> None:

    @reg.tool("launch_app",
              "Запустить приложение по имени (vscode, chrome, terminal, explorer, ...) или "
              "командой. Открывает файл/папку, если передан path.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Имя приложения или команда"),
                  "path": _prop("string", "Файл/папка, которую открыть")},
                  "required": ["name"]})
    class LaunchApp(Tool):
        async def execute(self, ctx: ToolContext, name: str, path: str = "") -> ToolResult:
            cmd = _resolve(ctx, name) or name
            args = []
            if path:
                p = os.path.expanduser(path)
                if ctx.platform.system == "windows" and name.lower() in ("explorer",):
                    return await _open_windows(p)
                args = [p]
            if ctx.platform.system == "windows":
                if cmd.startswith("ms-settings:") or cmd.endswith(":"):
                    r = subprocess.run(["cmd", "/c", "start", "", cmd], capture_output=True, text=True)
                    return ToolResult.ok_result(f"Запущено: {name}") if r.returncode == 0 \
                        else ToolResult.fail(r.stderr or f"не удалось запустить {name}")
                exe = shutil.which(cmd) or cmd
                subprocess.Popen([exe, *args], shell=False,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                return ToolResult.ok_result(f"Запущено: {name} ({' '.join(args) if args else ''})")
            else:
                exe = shutil.which(cmd.split()[0])
                if not exe:
                    return ToolResult.fail(f"не найдена команда: {name}. "
                                           f"Попробуйте terminal_run или укажите полный путь.")
                subprocess.Popen(cmd.split() + args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return ToolResult.ok_result(f"Запущено: {name}")

    async def _open_windows(p: str) -> ToolResult:
        try:
            os.startfile(p)  # type: ignore[attr-defined]
            return ToolResult.ok_result(f"Открыто: {p}")
        except Exception as e:
            return ToolResult.fail(f"не удалось открыть: {e}")

    @reg.tool("open_path", "Открыть файл/папку стандартным приложением ОС.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь")}, "required": ["path"]})
    class OpenPath(Tool):
        async def execute(self, ctx: ToolContext, path: str) -> ToolResult:
            p = os.path.expanduser(path)
            if not os.path.exists(p):
                return ToolResult.fail(f"не существует: {p}")
            if ctx.platform.system == "windows":
                return await _open_windows(p)
            cmd = ["open"] if ctx.platform.system == "macos" else ["xdg-open"]
            r = subprocess.run(cmd + [p], capture_output=True, text=True)
            return ToolResult.ok_result(f"Открыто: {p}") if r.returncode == 0 \
                else ToolResult.fail(f"нет xdg-open/open: {r.stderr.strip()}")

    @reg.tool("list_apps",
              "Список приложений, которые агент умеет запускать по имени (регистр + PATH).",
              risk=Risk.NONE, category="apps",
              parameters={"type": "object", "properties": {}})
    class ListApps(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            apps = {**_DEFAULTS, **(ctx.cfg.apps or {})}
            lines = [f"- {k}" for k in sorted(apps)]
            extra = []
            for d in (os.environ.get("PATH", "").split(os.pathsep) if ctx.platform.system != "windows"
                      else [r"C:\Program Files", r"C:\Program Files (x86)"]):
                try:
                    for e in os.listdir(d):
                        if e.lower().endswith((".exe", ".desktop")):
                            extra.append(e)
                except OSError:
                    continue
                if len(extra) > 200:
                    break
            if extra:
                lines.append("В PATH найдено:")
                lines += [f"- {e}" for e in sorted(set(extra))[:100]]
            return ToolResult.ok_result("\n".join(lines))

    @reg.tool("kill_app", "Закрыть приложение по имени (мягко, затем принудительно).",
              risk=Risk.HIGH, category="apps",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Имя процесса"), "force": _prop("boolean", "Сразу принудительно")},
                  "required": ["name"]})
    class KillApp(Tool):
        async def execute(self, ctx: ToolContext, name: str, force: bool = False) -> ToolResult:
            from .system import _psutil
            ps = _psutil()
            if ps:
                killed = []
                for p in ps.process_iter(["pid", "name"]):
                    try:
                        nm = (p.info.get("name") or "").lower()
                        if name.lower() in nm or nm in name.lower():
                            if force:
                                p.kill()
                            else:
                                p.terminate()
                            killed.append(f"{p.info.get('name')} ({p.info.get('pid')})")
                    except Exception:
                        continue
                if killed:
                    return ToolResult.ok_result(f"Закрыто: {', '.join(killed[:10])}")
                return ToolResult.fail(f"процесс '{name}' не найден")
            # fallback: taskkill/pkill
            cmd = ["taskkill", "/IM", name] + (["/F"] if force else []) if ctx.platform.system == "windows" \
                else ["pkill", "-f" if force else "", name]
            r = subprocess.run([c for c in cmd if c], capture_output=True, text=True)
            return ToolResult.ok_result(r.stdout or "закрыто") if r.returncode == 0 \
                else ToolResult.fail(r.stderr or "не удалось закрыть")
