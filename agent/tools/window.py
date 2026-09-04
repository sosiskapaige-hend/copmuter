"""Управление окнами: список, поиск, фокус, закрытие, сворачивание,
разворачивание, перемещение, размер, перенос между мониторами.

Бэкенды:
- Windows: ctypes + user32 (без сторонних зависимостей).
- Linux X11: xdotool / wmctrl (если установлены).
- Headless: ясное сообщение — агент переключается на прямые инструменты.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry

_WINDOWS = sys.platform == "win32"

# win32 константы
SW_HIDE, SW_SHOW, SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 0, 5, 6, 3, 9
WM_CLOSE = 0x0010
GWL_STYLE = -16
WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000


class Win32Backend:
    def __init__(self) -> None:
        import ctypes
        self.u = ctypes.windll.user32  # type: ignore[attr-defined]
        self.k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self.u.GetWindowTextW.restype = ctypes.c_int
        self.u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        self.u.GetWindowThreadProcessId.restype = ctypes.c_uint

    def list_windows(self) -> list[dict]:
        out: list[dict] = []
        results: list[tuple[int, str, int]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            if not self.u.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(512)
            self.u.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            if not title:
                return True
            pid_ref = ctypes.c_uint()
            pid = self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_ref))
            results.append((int(hwnd), title, int(pid or 0)))
            return True

        self.u.EnumWindows(cb, 0)
        for hwnd, title, pid in results:
            rect = self._rect(hwnd)
            out.append({"id": hwnd, "title": title, "pid": pid, **rect})
        return out

    def _rect(self, hwnd: int) -> dict:
        rect = self._get_rect(hwnd)
        return {"x": rect[0], "y": rect[1], "w": rect[2] - rect[0], "h": rect[3] - rect[1]}

    def _get_rect(self, hwnd: int):
        import ctypes
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rc = RECT()
        self.u.GetWindowRect(hwnd, ctypes.byref(rc))
        return (rc.left, rc.top, rc.right, rc.bottom)

    def focus(self, hwnd: int) -> bool:
        import ctypes
        if self.u.IsIconic(hwnd):
            self.u.ShowWindow(hwnd, SW_RESTORE)
        self.u.SetForegroundWindow(hwnd)
        return True

    def close(self, hwnd: int, force: bool = False) -> bool:
        if force:
            self.u.PostMessageW(hwnd, 0x0011, 0, 0)  # WM_DESTROY
        else:
            self.u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    def minimize(self, hwnd: int) -> bool:
        return bool(self.u.ShowWindow(hwnd, SW_MINIMIZE))

    def restore(self, hwnd: int) -> bool:
        return bool(self.u.ShowWindow(hwnd, SW_RESTORE))

    def maximize(self, hwnd: int) -> bool:
        return bool(self.u.ShowWindow(hwnd, SW_MAXIMIZE))

    def move(self, hwnd: int, x: int, y: int) -> bool:
        return bool(self.u.MoveWindow(hwnd, x, y, 1, 1, True))

    def resize(self, hwnd: int, w: int, h: int) -> bool:
        rc = self._get_rect(hwnd)
        return bool(self.u.MoveWindow(hwnd, rc[0], rc[1], w, h, True))


class X11Backend:
    def __init__(self) -> None:
        self._xdotool = bool(subprocess.run(["which", "xdotool"], capture_output=True).returncode == 0)
        self._wmctrl = bool(subprocess.run(["which", "wmctrl"], capture_output=True).returncode == 0)

    def _run(self, cmd: list[str]) -> str:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr or "").strip()

    def list_windows(self) -> list[dict]:
        out = []
        if self._wmctrl:
            for line in self._run(["wmctrl", "-l"]).splitlines():
                parts = line.split(None, 2)
                if len(parts) == 3:
                    out.append({"id": parts[0], "title": parts[2], "pid": 0,
                                "x": 0, "y": 0, "w": 0, "h": 0})
        elif self._xdotool:
            for line in self._run(["xdotool", "search", "--name", "", "getwindowname", "%1"]).splitlines():
                out.append({"id": "", "title": line, "pid": 0, "x": 0, "y": 0, "w": 0, "h": 0})
        return out

    def focus(self, title: str) -> bool:
        if self._wmctrl:
            return self._run(["wmctrl", "-a", title]) != "" or True
        if self._xdotool:
            r = subprocess.run(["xdotool", "search", "--name", title, "windowfocus"],
                               capture_output=True)
            return r.returncode == 0
        return False

    def close(self, title: str, force: bool = False) -> bool:
        if self._wmctrl:
            subprocess.run(["wmctrl", "-c", title], capture_output=True)
            return True
        if self._xdotool:
            r = subprocess.run(["xdotool", "search", "--name", title,
                                "windowclose"] + (["--sync"] if force else []),
                               capture_output=True)
            return r.returncode == 0
        return False

    def _win(self, title: str) -> list[str]:
        return ["xdotool", "search", "--name", title]

    def minimize(self, title: str) -> bool:
        r = subprocess.run(self._win(title) + ["windowminimize"], capture_output=True)
        return r.returncode == 0

    def restore(self, title: str) -> bool:
        r = subprocess.run(self._win(title) + ["windowactivate"], capture_output=True)
        return r.returncode == 0

    def maximize(self, title: str) -> bool:
        r = subprocess.run(self._win(title) + ["windowstate", "--add", "maximized_v"],
                           capture_output=True)
        return r.returncode == 0

    def move(self, title: str, x: int, y: int) -> bool:
        r = subprocess.run(self._win(title) + ["windowmove", str(x), str(y)], capture_output=True)
        return r.returncode == 0

    def resize(self, title: str, w: int, h: int) -> bool:
        r = subprocess.run(self._win(title) + ["windowsize", str(w), str(h)], capture_output=True)
        return r.returncode == 0


class HeadlessBackend:
    def list_windows(self) -> list[dict]:
        return []

    def __getattr__(self, name):
        def _no(*a, **k):
            return False
        return _no


def _get_backend(ctx: ToolContext):
    if ctx.platform.system == "windows":
        try:
            return Win32Backend()
        except Exception:
            return HeadlessBackend()
    if ctx.platform.has_x11:
        return X11Backend()
    return HeadlessBackend()


def register_window_tools(reg: ToolRegistry) -> None:

    @reg.tool("window_list",
              "Список открытых окон: id, заголовок, pid, координаты. "
              "Сначала используйте его, чтобы понять, что открыто.",
              risk=Risk.NONE, category="window", is_gui=True,
              parameters={"type": "object", "properties": {}})
    class WindowList(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            be = _get_backend(ctx)
            wins = be.list_windows()
            if not wins and not ctx.platform.has_display:
                return ToolResult.ok_result(
                    "(headless-режим: окна недоступны. На машине с дисплеем здесь будет список всех окон.)",
                    windows=[], headless=True)
            lines = [f"[{w['id']}] {w['title'][:60]}  (pid {w.get('pid') or '?'}) "
                     f"({w.get('x')},{w.get('y')}) {w.get('w')}x{w.get('h')}" for w in wins[:80]]
            return ToolResult.ok_result("\n".join(lines) or "(нет видимых окон)", windows=wins)

    @reg.tool("window_focus", "Перевести фокус на окно по (части) заголовка.",
              risk=Risk.NONE, category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок или его часть")}, "required": ["title"]})
    class WindowFocus(Tool):
        async def execute(self, ctx: ToolContext, title: str) -> ToolResult:
            be = _get_backend(ctx)
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"окно с заголовком «{title}» не найдено")
                ok = be.focus(match["id"])
                return ToolResult.ok_result(f"Фокус: {match['title']}") if ok \
                    else ToolResult.fail("не удалось перевести фокус")
            ok = be.focus(title)
            return ToolResult.ok_result(f"Фокус: {title}") if ok else ToolResult.fail(f"окно не найдено: {title}")

    @reg.tool("window_close", "Закрыть окно (мягко, WM_CLOSE).", risk=Risk.MEDIUM,
              category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок или часть"), "force": _prop("boolean", "Принудительно")},
                  "required": ["title"]})
    class WindowClose(Tool):
        async def execute(self, ctx: ToolContext, title: str, force: bool = False) -> ToolResult:
            be = _get_backend(ctx)
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"окно не найдено: {title}")
                be.close(match["id"], force)
                return ToolResult.ok_result(f"Закрыто окно: {match['title']}",
                                            undo={"tool": "launch_app",
                                                  "args": {"name": "terminal"},
                                                  "note": "окно закрыто (возможный перезапуск через launch_app)"})
            ok = be.close(title, force)
            return ToolResult.ok_result(f"Закрыто окно: {title}") if ok else ToolResult.fail(f"не найдено: {title}")

    @reg.tool("window_minimize", "Свернуть окно.", risk=Risk.LOW, category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок")}, "required": ["title"]})
    class WindowMinimize(Tool):
        async def execute(self, ctx: ToolContext, title: str) -> ToolResult:
            be = _get_backend(ctx)
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"не найдено: {title}")
                be.minimize(match["id"])
                return ToolResult.ok_result(f"Свёрнуто: {match['title']}")
            ok = be.minimize(title)
            return ToolResult.ok_result(f"Свёрнуто: {title}") if ok else ToolResult.fail("не сработало")

    @reg.tool("window_restore", "Развернуть/вернуть окно из свёрнутого состояния.",
              risk=Risk.LOW, category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок")}, "required": ["title"]})
    class WindowRestore(Tool):
        async def execute(self, ctx: ToolContext, title: str) -> ToolResult:
            be = _get_backend(ctx)
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"не найдено: {title}")
                be.restore(match["id"])
                return ToolResult.ok_result(f"Возвращено: {match['title']}")
            ok = be.restore(title)
            return ToolResult.ok_result(f"Возвращено: {title}") if ok else ToolResult.fail("не сработало")

    @reg.tool("window_maximize", "Развернуть окно на весь экран.", risk=Risk.LOW,
              category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок")}, "required": ["title"]})
    class WindowMaximize(Tool):
        async def execute(self, ctx: ToolContext, title: str) -> ToolResult:
            be = _get_backend(ctx)
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"не найдено: {title}")
                be.maximize(match["id"])
                return ToolResult.ok_result(f"Развернуто: {match['title']}")
            ok = be.maximize(title)
            return ToolResult.ok_result(f"Развернуто: {title}") if ok else ToolResult.fail("не сработало")

    @reg.tool("window_move", "Переместить окно в координаты (или на монитор по индексу).",
              risk=Risk.LOW, category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок"),
                  "x": _prop("integer", "X"), "y": _prop("integer", "Y"),
                  "monitor": _prop("integer", "Или перенести на монитор N (x/y по его центру)")},
                  "required": ["title"]})
    class WindowMove(Tool):
        async def execute(self, ctx: ToolContext, title: str, x: int | None = None,
                          y: int | None = None, monitor: int | None = None) -> ToolResult:
            be = _get_backend(ctx)
            if monitor:
                ms = ctx.platform.monitors
                m = next((m for m in ms if m.get("index") == monitor), None)
                if not m:
                    return ToolResult.fail(f"монитор {monitor} не найден (list_monitors)")
                x = m["left"] + 50
                y = m["top"] + 50
            if x is None or y is None:
                return ToolResult.fail("нужны x,y или monitor")
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"не найдено: {title}")
                be.move(match["id"], x, y)
                return ToolResult.ok_result(f"Окно «{match['title']}» → ({x},{y})")
            ok = be.move(title, x, y)
            return ToolResult.ok_result(f"Окно → ({x},{y})") if ok else ToolResult.fail("не сработало")

    @reg.tool("window_resize", "Изменить размер окна.", risk=Risk.LOW, category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "title": _prop("string", "Заголовок"),
                  "width": _prop("integer", "Ширина"), "height": _prop("integer", "Высота")},
                  "required": ["title", "width", "height"]})
    class WindowResize(Tool):
        async def execute(self, ctx: ToolContext, title: str, width: int, height: int) -> ToolResult:
            be = _get_backend(ctx)
            if ctx.platform.system == "windows":
                wins = be.list_windows()
                match = next((w for w in wins if title.lower() in w["title"].lower()), None)
                if not match:
                    return ToolResult.fail(f"не найдено: {title}")
                be.resize(match["id"], width, height)
                return ToolResult.ok_result(f"Размер {width}x{height}: {match['title']}")
            ok = be.resize(title, width, height)
            return ToolResult.ok_result(f"Размер {width}x{height}") if ok else ToolResult.fail("не сработало")

    @reg.tool("close_all_except",
              "Закрыть ВСЕ окна, кроме перечисленных (части заголовков). "
              "Для «закрой всё лишнее». Высокий риск — обычно требует подтверждения.",
              risk=Risk.HIGH, category="window", is_gui=True,
              parameters={"type": "object", "properties": {
                  "keep": _prop("array", "Части заголовков, которые сохранить (напр. ['code', 'agent'])")},
                  "required": ["keep"]})
    class CloseAllExcept(Tool):
        async def execute(self, ctx: ToolContext, keep: list) -> ToolResult:
            be = _get_backend(ctx)
            keep_l = [k.lower() for k in keep]
            wins = be.list_windows()
            closed, kept = [], []
            for w in wins:
                t = w["title"].lower()
                if any(k in t for k in keep_l):
                    kept.append(w["title"])
                    continue
                be.close(w["id"])
                closed.append(w["title"])
            return ToolResult.ok_result(
                f"Закрыто: {len(closed)} ({', '.join(closed[:15])}); сохранено: {len(kept)}")
