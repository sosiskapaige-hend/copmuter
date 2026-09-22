"""Состояние компьютера (ТЗ §18): минимум, который агент должен «чувствовать».

    active_window, active_process, screen_resolution, mouse_position,
    clipboard, opened_applications, available_monitors, current_directory,
    foreground monitor, DPI-масштаб

Ключевой принцип: состояние обновляется по требованию и с коротким TTL —
никаких постоянных скриншотов и опросов. Дорогие части (список процессов)
кэшируются на 2 секунды, дешёвые (курсор, активное окно) читаются напрямую
через нативный API (Win32 ctypes — микросекунды).

Всё, что блокирует, выполняется в executor'е, поэтому UI никогда не висит.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..apps.aliases import similarity

# --- Windows API (только на Windows) ---
_VK_NAMES = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20}


def _user32():
    try:
        import ctypes
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:
        return None


@dataclass
class WindowInfo:
    hwnd: int = 0
    title: str = ""
    pid: int = 0
    process: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    monitor: int = 0
    maximized: bool = False
    minimized: bool = False

    def to_dict(self) -> dict:
        return {"hwnd": self.hwnd, "title": self.title, "pid": self.pid,
                "process": self.process, "x": self.x, "y": self.y, "w": self.w,
                "h": self.h, "monitor": self.monitor, "maximized": self.maximized,
                "minimized": self.minimized}


class ComputerState:
    """Снимок состояния ПК + кэш с TTL."""

    def __init__(self, platform: Any, cfg: Any = None, cache: Any = None,
                 workdir: str = ".", apps: Any = None) -> None:
        self.platform = platform
        self.cfg = cfg
        self.cache = cache
        self.workdir = workdir
        self.apps = apps
        self._processes: tuple[float, list[dict]] = (0.0, [])
        self._windows: tuple[float, list[WindowInfo]] = (0.0, [])
        self._dpi_scale = self._detect_dpi()
        self._console = None

    # ------------------------------------------------------------- примитивы
    @staticmethod
    def _detect_dpi() -> float:
        if os.name != "nt":
            return 1.0
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            return round(dpi / 96.0, 3) if dpi else 1.0
        except Exception:
            return 1.0

    @property
    def dpi_scale(self) -> float:
        return self._dpi_scale

    def monitors(self) -> list[dict]:
        mons = list(getattr(self.platform, "monitors", []) or [])
        if mons:
            return mons
        return [{"index": 1, "name": "primary", "left": 0, "top": 0,
                 "width": 1920, "height": 1080, "primary": True}]

    def virtual_desktop(self) -> dict:
        """Габариты объединённого рабочего стола (все мониторы)."""
        mons = self.monitors()
        left = min(m.get("left", 0) for m in mons)
        top = min(m.get("top", 0) for m in mons)
        right = max(m.get("left", 0) + m.get("width", 0) for m in mons)
        bottom = max(m.get("top", 0) + m.get("height", 0) for m in mons)
        return {"left": left, "top": top, "width": right - left, "height": bottom - top,
                "right": right, "bottom": bottom}

    def monitor_at(self, x: int, y: int) -> int:
        for m in self.monitors():
            if m.get("left", 0) <= x < m.get("left", 0) + m.get("width", 0) and \
                    m.get("top", 0) <= y < m.get("top", 0) + m.get("height", 0):
                return int(m.get("index", 1))
        return int(self.monitors()[0].get("index", 1))

    # ------------------------------------------------------------- активное окно
    def _active_window_sync(self) -> WindowInfo:
        u = _user32()
        if u is None:
            return self._active_window_fallback()
        try:
            import ctypes
            hwnd = int(u.GetForegroundWindow())
            if not hwnd:
                return self._active_window_fallback()
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, buf, 512)
            pid_ref = ctypes.c_uint()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_ref))
            pid = int(pid_ref.value)
            rect = self._window_rect(hwnd)
            return WindowInfo(hwnd=hwnd, title=buf.value, pid=pid,
                              process=self._proc_name(pid), **rect,
                              maximized=bool(u.IsZoomed(hwnd)),
                              minimized=bool(u.IsIconic(hwnd)),
                              monitor=self.monitor_at(rect["x"] + rect["w"] // 2,
                                                      rect["y"] + rect["h"] // 2))
        except Exception:
            return self._active_window_fallback()

    def _active_window_fallback(self) -> WindowInfo:
        """Linux/X11: xdotool, затем wmctrl; headless — пустое окно."""
        if self.platform.has_x11:
            try:
                import subprocess
                wid = subprocess.run(["xdotool", "getactivewindow"], capture_output=True,
                                     text=True, timeout=3).stdout.strip()
                title = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True,
                                       text=True, timeout=3).stdout.strip() if wid else ""
                pid = 0
                if wid:
                    out = subprocess.run(["xdotool", "getwindowpid", wid], capture_output=True,
                                         text=True, timeout=3).stdout.strip()
                    pid = int(out or 0)
                geom = {}
                if wid:
                    g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                                       capture_output=True, text=True, timeout=3).stdout
                    for line in g.splitlines():
                        if "=" in line:
                            k, v = line.split("=", 1)
                            geom[k.strip().lower()] = int(v or 0)
                w = WindowInfo(title=title, pid=pid,
                               x=int(geom.get("x", 0)), y=int(geom.get("y", 0)),
                               w=int(geom.get("width", 0)), h=int(geom.get("height", 0)),
                               process=self._proc_name(pid))
                w.monitor = self.monitor_at(w.x + w.w // 2, w.y + w.h // 2)
                return w
            except Exception:
                pass
        return WindowInfo(title="", monitor=1)

    def _window_rect(self, hwnd: int) -> dict:
        try:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rc = RECT()
            u = _user32()
            u.GetWindowRect(hwnd, ctypes.byref(rc))
            return {"x": int(rc.left), "y": int(rc.top),
                    "w": int(rc.right - rc.left), "h": int(rc.bottom - rc.top)}
        except Exception:
            return {"x": 0, "y": 0, "w": 0, "h": 0}

    def _proc_name(self, pid: int) -> str:
        if not pid:
            return ""
        for p in self._process_list_uncached(limit=400):
            if p.get("pid") == pid:
                return str(p.get("name") or "")
        return ""

    # ------------------------------------------------------------- списки
    def _process_list_uncached(self, limit: int = 200) -> list[dict]:
        try:
            import psutil  # type: ignore
            out = []
            for p in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
                try:
                    i = p.info
                    mem = i.get("memory_info")
                    out.append({"pid": i.get("pid"), "name": i.get("name") or "",
                                "rss_mb": round((mem.rss / 1048576) if mem else 0.0, 1),
                                "started": i.get("create_time") or 0.0})
                except Exception:
                    continue
                if len(out) >= limit * 3:
                    break
            return out
        except Exception:
            return self._process_list_fallback(limit)

    @staticmethod
    def _process_list_fallback(limit: int) -> list[dict]:
        import subprocess
        out: list[dict] = []
        try:
            if os.name == "nt":
                r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                   text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in r.stdout.splitlines()[:limit]:
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 2:
                        try:
                            out.append({"pid": int(parts[1]), "name": parts[0], "rss_mb": 0.0,
                                        "started": 0.0})
                        except ValueError:
                            continue
            else:
                r = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True, text=True,
                                   timeout=10)
                for line in r.stdout.splitlines()[:limit]:
                    line = line.strip()
                    if not line:
                        continue
                    pid_s, _, name = line.partition(" ")
                    try:
                        out.append({"pid": int(pid_s), "name": os.path.basename(name.strip()),
                                    "rss_mb": 0.0, "started": 0.0})
                    except ValueError:
                        continue
        except Exception:
            pass
        return out

    def processes(self, ttl: float = 2.0, limit: int = 200) -> list[dict]:
        ts, data = self._processes
        if data and (time.time() - ts) < ttl:
            return data[:limit]
        data = self._process_list_uncached(limit)
        self._processes = (time.time(), data)
        return data[:limit]

    def find_process(self, name: str) -> dict | None:
        n = (name or "").lower().strip()
        if not n:
            return None
        for p in self.processes():
            pn = str(p.get("name") or "").lower()
            if pn == n or (len(n) > 3 and n in pn):
                return p
        return None

    def open_apps(self, ttl: float = 3.0) -> list[str]:
        """Только «пользовательские» приложения (без системных служб)."""
        interesting = {"explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe", "code.exe",
                       "telegram.exe", "discord.exe", "spotify.exe", "steam.exe",
                       "windowsterminal.exe", "wt.exe", "notepad.exe", "pycharm64.exe"}
        for rec in (self.apps.all() if self.apps is not None else []):
            interesting |= set(rec.proc_names())
        seen: list[str] = []
        for p in self.processes(ttl):
            nm = str(p.get("name") or "")
            if nm.lower() in interesting and nm not in seen:
                seen.append(nm)
            if len(seen) >= 25:
                break
        return seen

    # ------------------------------------------------------------- курсор/буфер
    def mouse_position(self) -> tuple[int, int]:
        u = _user32()
        if u is not None:
            try:
                import ctypes

                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                pt = POINT()
                u.GetCursorPos(ctypes.byref(pt))
                return int(pt.x), int(pt.y)
            except Exception:
                pass
        try:
            import pyautogui  # type: ignore
            x, y = pyautogui.position()
            return int(x), int(y)
        except Exception:
            return 0, 0

    def clipboard_text(self, max_chars: int = 400) -> str:
        try:
            import subprocess
            if os.name == "nt":
                from ..tools.clipboard import _win_get  # noqa: PLC0415
                return (_win_get() or "")[:max_chars]
            for cmd in (["xclip", "-selection", "clipboard", "-o"],
                        ["xsel", "--clipboard", "--output"]):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                except FileNotFoundError:
                    continue
                if r.returncode == 0:
                    return (r.stdout or "")[:max_chars]
            from ..tools.clipboard import _virtual_buf  # noqa: PLC0415
            return str(_virtual_buf or "")[:max_chars]
        except Exception:
            return ""

    # ------------------------------------------------------------- сборка
    async def snapshot(self, parts: tuple[str, ...] = ("window", "processes", "mouse",
                                                       "monitors", "env"),
                       include_clipboard: bool = False) -> dict:
        # parts=None/"" → все части; строка «window,monitors» тоже принимается
        if not parts:
            parts = ("window", "processes", "mouse", "monitors", "env")
        elif isinstance(parts, str):
            parts = tuple(x.strip() for x in parts.replace(";", ",").split(",") if x.strip())
        else:
            parts = tuple(parts)
        if any(p in ("clipboard", "буфер") for p in parts):
            include_clipboard = True
            parts = tuple(p for p in parts if p not in ("clipboard", "буфер"))

        loop = asyncio.get_running_loop()
        out: dict[str, Any] = {"ts": time.time()}

        async def run(fn, *a, **kw):
            return await loop.run_in_executor(None, lambda: fn(*a, **kw))

        if "window" in parts:
            w = await run(self._active_window_sync)
            out["active_window"] = w.to_dict()
            out["active_process"] = w.process
        if "processes" in parts:
            procs = await run(self.processes)
            out["process_count"] = len(procs)
            out["opened_applications"] = await run(self.open_apps)
        if "mouse" in parts:
            x, y = await run(self.mouse_position)
            out["mouse_position"] = {"x": x, "y": y}
        if "monitors" in parts:
            mons = self.monitors()
            out["monitors"] = mons
            primary = next((m for m in mons if m.get("primary")), mons[0] if mons else {})
            out["screen_resolution"] = (f"{primary.get('width')}x{primary.get('height')}"
                                        if primary else "unknown")
            out["virtual_desktop"] = self.virtual_desktop()
        if "env" in parts:
            out["current_directory"] = os.getcwd()
            out["workdir"] = self.workdir
            out["dpi_scale"] = self._dpi_scale
            out["platform"] = getattr(self.platform, "system", "unknown")
        if include_clipboard:
            out["clipboard"] = await run(self.clipboard_text)
        return out

    async def summary(self, include_clipboard: bool = False) -> str:
        """Компактный текст для контекста модели (ТЗ §23)."""
        snap = await self.snapshot(include_clipboard=include_clipboard)
        return self.format_summary(snap, include_clipboard=include_clipboard)

    def format_summary(self, snap: dict, include_clipboard: bool = False) -> str:
        """Тот же текст, но по уже готовому снимку (без повторных системных вызовов)."""
        lines = []
        if "active_window" in snap:
            w = snap["active_window"]
            title = (w.get("title") or "(без заголовка)")[:60]
            proc = w.get("process") or "?"
            lines.append(f"Активное окно: «{title}» ({proc}, PID {w.get('pid')})")
        if snap.get("opened_applications"):
            lines.append("Запущено: " + ", ".join(snap["opened_applications"][:8]))
        if snap.get("mouse_position"):
            mp = snap["mouse_position"]
            lines.append(f"Курсор: {mp['x']},{mp['y']}")
        if snap.get("screen_resolution"):
            lines.append(f"Экран: {snap['screen_resolution']} (масштаб {snap.get('dpi_scale', 1.0)})")
            if len(snap.get("monitors") or []) > 1:
                lines.append(f"Мониторов: {len(snap['monitors'])}")
        if snap.get("current_directory"):
            lines.append(f"Текущая папка: {snap['current_directory']}")
        if include_clipboard and snap.get("clipboard"):
            lines.append(f"Буфер обмена: {snap['clipboard'][:120]!r}")
        return "\n".join(lines)
