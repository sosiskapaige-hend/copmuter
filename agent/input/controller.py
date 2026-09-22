"""Контроллер ввода: один владелец мыши и клавиатуры (ТЗ §10, §11, §7 прил.).

Зачем отдельный слой:

  * **AGENT_INPUT_LOCK** — пока агент кликает/печатает, никакое другое действие
    не может «вклиниться» и сломать последовательность; очередь действий
    выполняется под одним захватом;
  * **конфликт с пользователем** — если во время работы агента пользователь
    двинул мышь или нажал клавишу (Windows: GetLastInputInfo), последовательность
    останавливается и об этом сообщается, а не «печатается в никуда»;
  * **clipboard-first** — длинный текст вставляется через буфер (clip_set +
    Ctrl+V), а не посимвольно: в десятки раз быстрее (ТЗ §11);
  * **нативный бэкенд** — Windows SendInput через ctypes (без pyautogui),
    что даёт скорость и поддержку Unicode/мультимониторности; pyautogui и
    xdotool остаются фолбэками; headless пишет в виртуальный журнал, поэтому
    конвейер агента работает и без дисплея.

Все методы асинхронные: блокирующие системные вызовы уходят в executor, event
loop (и UI) не замирают.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

AGENT_INPUT_LOCK = "AGENT_INPUT_LOCK"
USER_ACTIVE_WINDOW = 0.8      # если пользователь работал только что — не перехватываем ввод


class ClipboardAdapter:
    """Единый доступ к буферу обмена (Windows API / xclip / виртуальный)."""

    def __init__(self, cfg: Any = None) -> None:
        self.cfg = cfg
        self._virtual = ""

    def get(self) -> str:
        try:
            if os.name == "nt":
                from ..tools.clipboard import _win_get  # noqa: PLC0415
                return _win_get() or ""
            import subprocess
            for cmd in (["xclip", "-selection", "clipboard", "-o"],
                        ["xsel", "--clipboard", "--output"]):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                except FileNotFoundError:
                    continue
                if r.returncode == 0:
                    return r.stdout or ""
        except Exception:
            pass
        return self._virtual

    def set(self, text: str) -> bool:
        try:
            if os.name == "nt":
                from ..tools.clipboard import _win_set  # noqa: PLC0415
                return bool(_win_set(text))
            import subprocess
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    r = subprocess.run(cmd, input=text.encode(),
                                       capture_output=True, timeout=5)
                except FileNotFoundError:
                    continue
                if r.returncode == 0:
                    return True
        except Exception:
            pass
        self._virtual = text            # headless: виртуальный буфер
        return True


# --------------------------------------------------------------------------
#  Бэкенды
# --------------------------------------------------------------------------
class InputBackend:
    name = "base"

    def available(self) -> bool:
        return False

    # мышь
    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None: ...
    def scroll(self, x: int, y: int, delta: int) -> None: ...
    def position(self) -> tuple[int, int]: ...
    def screen_size(self) -> tuple[int, int]: ...
    def flags(self) -> list[str]:
        return []

    # клавиатура
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def press(self, key: str) -> None: ...
    def hotkey(self, keys: Iterable[str]) -> None: ...
    def type_unicode(self, text: str) -> None: ...


_VK_SPECIAL = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "windows": 0x5B, "super": 0x5B, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "menu": 0x5D, "capslock": 0x14, "numlock": 0x90,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_play": 0xB3, "media_stop": 0xB2,
}


class Win32InputBackend(InputBackend):
    """Нативный Windows-ввод: SendInput (мышь + Unicode-клавиатура)."""

    name = "win32_sendinput"
    MOUSEEVENTF = {"move": 0x0001, "leftdown": 0x0002, "leftup": 0x0004,
                   "rightdown": 0x0008, "rightup": 0x0010, "middledown": 0x0020,
                   "middleup": 0x0040, "wheel": 0x0800, "absolute": 0x8000,
                   "virtualdesk": 0x4000}
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.u = ctypes.windll.user32  # type: ignore[attr-defined]
        self.k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._sent_total = 0
        self._configure_dpi()

    def _configure_dpi(self) -> None:
        try:
            self.ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except Exception:
            try:
                self.u.SetProcessDPIAware()
            except Exception:
                pass

    def available(self) -> bool:
        return os.name == "nt"

    # ---------- мышь ----------
    def _send_mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        ctypes = self.ctypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                        ("wParamH", ctypes.c_ushort)]

        class _INPUTunion(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]

        inp = INPUT()
        inp.type = 0
        inp.union.mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
        self.u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        self._sent_total += 1

    def _send_key(self, vk: int = 0, scan: int = 0, flags: int = 0) -> None:
        ctypes = self.ctypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                        ("wParamH", ctypes.c_ushort)]

        class _INPUTunion(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]

        inp = INPUT()
        inp.type = 1
        inp.union.ki = KEYBDINPUT(vk, scan, flags, 0, None)
        self.u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        self._sent_total += 1

    def move(self, x: int, y: int) -> None:
        vd = self._virtual_desktop()
        if vd["width"] > 0 and vd["height"] > 0:
            nx = int((x - vd["left"]) * 65535 / max(1, vd["width"] - 1))
            ny = int((y - vd["top"]) * 65535 / max(1, vd["height"] - 1))
            self._send_mouse(self.MOUSEEVENTF["move"] | self.MOUSEEVENTF["absolute"]
                             | self.MOUSEEVENTF["virtualdesk"], nx, ny)
        else:
            self.u.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self.move(x, y)
        down = {"left": "leftdown", "right": "rightdown", "middle": "middledown"}.get(button, "leftdown")
        up = {"left": "leftup", "right": "rightup", "middle": "middleup"}.get(button, "leftup")
        for _ in range(max(1, clicks)):
            self._send_mouse(self.MOUSEEVENTF[down])
            self._send_mouse(self.MOUSEEVENTF[up])

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self.move(x1, y1)
        self._send_mouse(self.MOUSEEVENTF["leftdown"])
        steps = max(3, int(duration * 60))
        for i in range(1, steps + 1):
            xi = int(x1 + (x2 - x1) * i / steps)
            yi = int(y1 + (y2 - y1) * i / steps)
            self.move(xi, yi)
            time.sleep(max(0.001, duration / steps))
        self._send_mouse(self.MOUSEEVENTF["leftup"])

    def scroll(self, x: int, y: int, delta: int) -> None:
        self.move(x, y)
        self._send_mouse(self.MOUSEEVENTF["wheel"], data=int(delta) * 120)

    def position(self) -> tuple[int, int]:
        try:
            import ctypes
            from ctypes import wintypes

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = POINT()
            self.u.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        except Exception:
            return 0, 0

    def screen_size(self) -> tuple[int, int]:
        return int(self.u.GetSystemMetrics(0)), int(self.u.GetSystemMetrics(1))

    def _virtual_desktop(self) -> dict:
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
        return {"left": int(self.u.GetSystemMetrics(SM_XVIRTUALSCREEN)),
                "top": int(self.u.GetSystemMetrics(SM_YVIRTUALSCREEN)),
                "width": int(self.u.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                "height": int(self.u.GetSystemMetrics(SM_CYVIRTUALSCREEN))}

    # ---------- клавиатура ----------
    @staticmethod
    def vk(key: str) -> int:
        k = (key or "").strip().lower()
        if k in _VK_SPECIAL:
            return _VK_SPECIAL[k]
        if len(k) == 1:
            return ord(k.upper())
        if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
            return 0x70 + int(k[1:]) - 1
        return 0

    def key_down(self, key: str) -> None:
        vk = self.vk(key)
        if vk:
            self._send_key(vk=vk)

    def key_up(self, key: str) -> None:
        vk = self.vk(key)
        if vk:
            self._send_key(vk=vk, flags=self.KEYEVENTF_KEYUP)

    def press(self, key: str) -> None:
        self.key_down(key)
        time.sleep(0.01)
        self.key_up(key)

    def hotkey(self, keys: Iterable[str]) -> None:
        seq = [k for k in keys if k]
        for k in seq:
            self.key_down(k)
        for k in reversed(seq):
            self.key_up(k)

    def type_unicode(self, text: str) -> None:
        for ch in text:
            code = ord(ch)
            if code == 0x0A:      # \n → Enter
                self.press("enter")
                continue
            if code == 0x09:
                self.press("tab")
                continue
            self._send_key(scan=code, flags=self.KEYEVENTF_UNICODE)
            self._send_key(scan=code, flags=self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP)

    def user_last_input_age(self) -> float:
        """Сколько секунд назад пользователь что-то нажимал/двигал (Windows)."""
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            li = LASTINPUTINFO()
            li.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not self.u.GetLastInputInfo(ctypes.byref(li)):
                return -1.0
            ticks = self.k32.GetTickCount()
            return max(0.0, (ticks - li.dwTime) / 1000.0)
        except Exception:
            return -1.0

    def flags(self) -> list[str]:
        return ["native", "unicode", "multimonitor", "dpi"]


class PyAutoGuiBackend(InputBackend):
    name = "pyautogui"

    def __init__(self) -> None:
        import pyautogui  # type: ignore
        pyautogui.FAILSAFE = False
        self.pa = pyautogui

    def available(self) -> bool:
        return True

    def move(self, x: int, y: int) -> None:
        self.pa.moveTo(x, y, duration=0)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self.pa.click(x, y, clicks=clicks, button=button)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self.pa.moveTo(x1, y1)
        self.pa.mouseDown()
        self.pa.moveTo(x2, y2, duration=duration)
        self.pa.mouseUp()

    def scroll(self, x: int, y: int, delta: int) -> None:
        self.pa.scroll(int(delta), x=x, y=y)

    def position(self) -> tuple[int, int]:
        x, y = self.pa.position()
        return int(x), int(y)

    def screen_size(self) -> tuple[int, int]:
        return self.pa.size()

    def key_down(self, key: str) -> None:
        self.pa.keyDown(key)

    def key_up(self, key: str) -> None:
        self.pa.keyUp(key)

    def press(self, key: str) -> None:
        self.pa.press(key)

    def hotkey(self, keys: Iterable[str]) -> None:
        self.pa.hotkey(*[k for k in keys if k])

    def type_unicode(self, text: str) -> None:
        self.pa.typewrite(text, interval=0.01)

    def user_last_input_age(self) -> float:
        return -1.0

    def flags(self) -> list[str]:
        return ["pyautogui"]


class XdotoolBackend(InputBackend):
    name = "xdotool"

    def available(self) -> bool:
        import shutil
        return bool(shutil.which("xdotool"))

    def _run(self, args: list[str]) -> None:
        try:
            subprocess.run(["xdotool", *args], capture_output=True, timeout=10)
        except Exception:
            pass

    def move(self, x: int, y: int) -> None:
        self._run(["mousemove", str(int(x)), str(int(y))])

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        b = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        self.move(x, y)
        self._run(["click", "--repeat", str(max(1, clicks)), b])

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self.move(x1, y1)
        self._run(["mousedown", "1"])
        self.move(x2, y2)
        self._run(["mouseup", "1"])

    def scroll(self, x: int, y: int, delta: int) -> None:
        self.move(x, y)
        btn = "4" if delta > 0 else "5"
        for _ in range(abs(int(delta))):
            self._run(["click", btn])

    def position(self) -> tuple[int, int]:
        try:
            r = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True,
                               text=True, timeout=5)
            vals = {}
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            return int(vals.get("X", 0)), int(vals.get("Y", 0))
        except Exception:
            return 0, 0

    def screen_size(self) -> tuple[int, int]:
        try:
            r = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True,
                               text=True, timeout=5)
            w, h = (r.stdout or "0 0").split()[:2]
            return int(w), int(h)
        except Exception:
            return 0, 0

    def key_down(self, key: str) -> None:
        self._run(["keydown", key])

    def key_up(self, key: str) -> None:
        self._run(["keyup", key])

    def press(self, key: str) -> None:
        self._run(["key", key])

    def hotkey(self, keys: Iterable[str]) -> None:
        self._run(["key", "+".join([k for k in keys if k])])

    def type_unicode(self, text: str) -> None:
        self._run(["type", "--clearmodifiers", "--delay", "8", text])

    def user_last_input_age(self) -> float:
        return -1.0

    def flags(self) -> list[str]:
        return ["xdotool"]


class VirtualInputBackend(InputBackend):
    """Headless: записывает намерения в журнал (совместим с tools/input.py)."""

    name = "virtual"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._pos = (0, 0)
        self._size = (1920, 1080)

    def available(self) -> bool:
        return True

    def _rec(self, action: str, **kw: Any) -> None:
        ev = {"action": action, "ts": time.time(), **kw}
        with self._lock:
            self.events.append(ev)
            if len(self.events) > 1000:
                del self.events[:-1000]
        if self.path:
            try:
                import json
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def move(self, x: int, y: int) -> None:
        self._pos = (int(x), int(y))
        self._rec("move", x=x, y=y)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self.move(x, y)
        self._rec("click", x=x, y=y, button=button, clicks=clicks)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self._rec("drag", x1=x1, y1=y1, x2=x2, y2=y2, duration=duration)

    def scroll(self, x: int, y: int, delta: int) -> None:
        self._rec("scroll", x=x, y=y, delta=delta)

    def position(self) -> tuple[int, int]:
        return self._pos

    def screen_size(self) -> tuple[int, int]:
        return self._size

    def key_down(self, key: str) -> None:
        self._rec("key_down", key=key)

    def key_up(self, key: str) -> None:
        self._rec("key_up", key=key)

    def press(self, key: str) -> None:
        self._rec("press", key=key)

    def hotkey(self, keys: Iterable[str]) -> None:
        self._rec("hotkey", keys=list(keys))

    def type_unicode(self, text: str) -> None:
        self._rec("type", text=text[:200], chars=len(text))

    def user_last_input_age(self) -> float:
        return -1.0

    def flags(self) -> list[str]:
        return ["virtual", "headless"]


def pick_backend(platform: Any, virtual_path: Path | None = None) -> InputBackend:
    """Лучший доступный бэкенд: нативный → pyautogui → xdotool → виртуальный."""
    if platform is not None and not getattr(platform, "has_display", False):
        return VirtualInputBackend(virtual_path)
    if os.name == "nt":
        try:
            b = Win32InputBackend()
            if b.available():
                return b
        except Exception:
            pass
    try:
        b = PyAutoGuiBackend()
        if b.available():
            return b
    except Exception:
        pass
    try:
        b = XdotoolBackend()
        if b.available():
            return b
    except Exception:
        pass
    return VirtualInputBackend(virtual_path)


# --------------------------------------------------------------------------
#  Контроллер
# --------------------------------------------------------------------------
@dataclass
class InputResult:
    ok: bool
    message: str = ""
    virtual: bool = False
    conflict: bool = False
    ms: float = 0.0
    data: dict = field(default_factory=dict)


class InputController:
    """Единая точка ввода агента: блокировка, конфликты, clipboard-first."""

    def __init__(self, platform: Any = None, cfg: Any = None, bus: Any = None,
                 log: Any = None, metrics: Any = None, backend: InputBackend | None = None,
                 clipboard: Any = None, virtual_path: Path | None = None) -> None:
        self.platform = platform
        self.cfg = cfg
        self.bus = bus
        self.log = log
        self.metrics = metrics
        self.backend = backend or pick_backend(platform, virtual_path)
        self.clipboard = clipboard if clipboard is not None else ClipboardAdapter(cfg)
        self._async_lock = asyncio.Lock()
        self._thread_lock = threading.RLock()
        self.conflict_detected = False
        self.paused = False
        self.sequences = 0
        self.actions = 0
        self._own_input_ts = 0.0
        self._owner: int | None = None
        self._depth = 0

    # ------------------------------------------------------------- служебное
    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def virtual(self) -> bool:
        return isinstance(self.backend, VirtualInputBackend)

    @property
    def locked(self) -> bool:
        return self._async_lock.locked()

    def capabilities(self) -> dict:
        return {"backend": self.backend_name, "virtual": self.virtual,
                "flags": self.backend.flags(), "locked": self.locked,
                "conflict": self.conflict_detected, "actions": self.actions,
                "sequences": self.sequences}

    def user_active(self, within: float = 1.5) -> bool:
        """Была ли активность пользователя за последние N секунд."""
        try:
            age = self.backend.user_last_input_age()
        except Exception:
            age = -1.0
        return 0 <= age <= within

    def pointer(self) -> tuple[int, int]:
        try:
            return self.backend.position()
        except Exception:
            return 0, 0

    def _run(self, fn, *args: Any) -> None:
        with self._thread_lock:
            fn(*args)

    def _note(self, action: str, ok: bool = True, ms: float = 0.0, **data: Any) -> None:
        self.actions += 1
        if self.log is not None:
            self.log.event("input", tool=action, ms=round(ms, 1), ok=ok, **data)
        if self.bus is not None:
            try:
                self.bus.emit("input", action=action, ok=ok, backend=self.backend_name, **data)
            except Exception:
                pass

    # ------------------------------------------------------------- lock
    async def acquire(self, timeout: float = 30.0) -> bool:
        """Захват блокировки ввода. Повторный захват тем же действием разрешён.

        Реентерабельность нужна, чтобы последовательность (`run_sequence`) могла
        вызывать отдельные операции ввода, не блокируя саму себя.
        """
        me = id(asyncio.current_task())
        if self._owner == me:
            self._depth += 1
            return True
        try:
            await asyncio.wait_for(self._async_lock.acquire(), timeout)
            self._owner, self._depth = me, 1
            return True
        except asyncio.TimeoutError:
            if self.log:
                self.log.warn("Input lock занят дольше таймаута", lock=AGENT_INPUT_LOCK)
            return False

    def release(self) -> None:
        me = id(asyncio.current_task())
        if self._owner == me and self._depth > 1:
            self._depth -= 1
            return
        self._owner, self._depth = None, 0
        if self._async_lock.locked():
            self._async_lock.release()

    # ------------------------------------------------------------- мышь
    async def move(self, x: int, y: int) -> InputResult:
        return await self._act("mouse_move", self.backend.move, int(x), int(y))

    async def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> InputResult:
        return await self._act("mouse_click", self.backend.click, int(x), int(y), button, int(clicks),
                               data={"x": int(x), "y": int(y), "button": button})

    async def double_click(self, x: int, y: int) -> InputResult:
        return await self.click(x, y, clicks=2)

    async def right_click(self, x: int, y: int) -> InputResult:
        return await self.click(x, y, button="right")

    async def middle_click(self, x: int, y: int) -> InputResult:
        return await self.click(x, y, button="middle")

    async def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> InputResult:
        return await self._act("mouse_drag", self.backend.drag, int(x1), int(y1),
                               int(x2), int(y2), float(duration))

    async def scroll(self, x: int, y: int, delta: int) -> InputResult:
        return await self._act("mouse_scroll", self.backend.scroll, int(x), int(y), int(delta))

    # ------------------------------------------------------------- клавиатура
    async def press_key(self, key: str) -> InputResult:
        return await self._act("key_press", self.backend.press, key, data={"key": key})

    async def key_down(self, key: str) -> InputResult:
        return await self._act("key_down", self.backend.key_down, key)

    async def key_up(self, key: str) -> InputResult:
        return await self._act("key_up", self.backend.key_up, key)

    async def hotkey(self, keys: str | Iterable[str]) -> InputResult:
        seq = [k.strip() for k in (keys.split("+") if isinstance(keys, str) else keys) if k.strip()]
        return await self._act("hotkey", self.backend.hotkey, seq, data={"keys": seq})

    async def type_text(self, text: str, use_clipboard: bool | None = None,
                        restore_clipboard: bool = False) -> InputResult:
        """Ввод текста: длинный — через буфер обмена (ТЗ §11)."""
        if not text:
            return InputResult(True, "пустой текст", virtual=self.virtual)
        if use_clipboard is None:
            use_clipboard = len(text) > 40 or "\n" in text
        if use_clipboard and self.clipboard is not None:
            res = await self.paste(text, restore_clipboard=restore_clipboard)
            if res.ok:
                return res
            # буфер не сработал — печатаем посимвольно
        return await self._act("type_text", self.backend.type_unicode, text,
                               data={"chars": len(text), "text": text[:120]})

    async def paste(self, text: str = "", restore_clipboard: bool = False) -> InputResult:
        """clip_set(text) → Ctrl+V — быстрый способ вставить большой текст."""
        t0 = time.perf_counter()
        prev = None
        if not await self.acquire():
            return InputResult(False, "не удалось получить блокировку ввода")
        try:
            if self.clipboard is not None:
                if restore_clipboard:
                    prev = self.clipboard.get()
                self.clipboard.set(text)
            await self._in_executor(self.backend.hotkey, ["ctrl", "v"])
            self._own_input_ts = time.time()
            ms = (time.perf_counter() - t0) * 1000
            self._note("paste", True, ms, chars=len(text))
            if restore_clipboard and prev is not None and self.clipboard is not None:
                await asyncio.sleep(0.05)
                self.clipboard.set(prev)
            return InputResult(True, f"вставлено {len(text)} символов через буфер", ms=ms)
        except Exception as e:  # noqa: BLE001
            return InputResult(False, f"вставка не удалась: {e}")
        finally:
            self.release()

    # ------------------------------------------------------------- последовательности
    async def run_sequence(self, actions: list[dict], stop_on_conflict: bool = True,
                           check_user: bool = True, respect_user: bool = True) -> InputResult:
        """Несколько действий под одним захватом — самый быстрый и надёжный путь.

        actions: [{"action": "click", "x": 10, "y": 20}, {"action": "type", "text": "..."}]
        """
        if not actions:
            return InputResult(True, "нечего выполнять")
        t0 = time.perf_counter()
        self.sequences += 1
        self.conflict_detected = False
        if not await self.acquire():
            return InputResult(False, "не удалось получить блокировку ввода")
        try:
            # Политика конфликта №1: не начинаем ввод, если человек прямо сейчас
            # работает мышью/клавиатурой — иначе агент «дерётся» с пользователем.
            if respect_user and check_user and not self.virtual \
                    and self.user_active(within=USER_ACTIVE_WINDOW):
                msg = ("Пользователь работает за компьютером прямо сейчас — "
                       "ввод отложен, чтобы не мешать. Повторите команду, когда освободитесь.")
                self.conflict_detected = True
                if self.log:
                    self.log.warn(msg)
                if self.bus:
                    self.bus.emit("input_conflict", action="sequence", message=msg)
                return InputResult(False, msg, conflict=True,
                                   ms=(time.perf_counter() - t0) * 1000)
            for i, act in enumerate(self._norm_actions(actions)):
                name = str(act.get("action", "")).lower()
                # Политика конфликта №2: во время длинной последовательности следим,
                # не появился ли пользовательский ввод между нашими шагами.
                if check_user and not self.virtual and self._user_intervened():
                    msg = ("Похоже, пользователь вмешался в работу (мышь/клавиатура) — "
                           "последовательность ввода остановлена, чтобы не мешать.")
                    self.conflict_detected = True
                    if self.log:
                        self.log.warn(msg, step=i, action=name)
                    if self.bus:
                        self.bus.emit("input_conflict", action=name, step=i, message=msg)
                    return InputResult(False, msg, conflict=True,
                                       ms=(time.perf_counter() - t0) * 1000)
                if self.paused:
                    return InputResult(False, "ввод приостановлен пользователем",
                                       ms=(time.perf_counter() - t0) * 1000)
                res = await self._dispatch(act)
                if not res.ok and stop_on_conflict:
                    return InputResult(False, f"шаг {i + 1} ({name}) не выполнен: {res.message}",
                                       ms=(time.perf_counter() - t0) * 1000)
            ms = (time.perf_counter() - t0) * 1000
            return InputResult(True, f"выполнено действий: {len(actions)}", ms=ms,
                               data={"count": len(actions)})
        finally:
            self.release()

    @staticmethod
    def _norm_actions(actions) -> list[dict]:
        """Принимает и словари, и короткие формы: ("click", (x, y)), ("hotkey", "ctrl+s")."""
        out: list[dict] = []
        for a in actions or []:
            if isinstance(a, dict):
                out.append(a)
                continue
            if isinstance(a, (tuple, list)) and a:
                name = str(a[0]).lower()
                payload = a[1] if len(a) > 1 else None
                if name in ("click", "double_click", "dblclick", "right_click", "move") \
                        and isinstance(payload, (tuple, list)) and len(payload) >= 2:
                    out.append({"action": name, "x": payload[0], "y": payload[1]})
                elif name in ("type", "text", "type_text"):
                    out.append({"action": "type", "text": payload or ""})
                elif name in ("hotkey", "key", "press"):
                    out.append({"action": "hotkey", "keys": payload or ""})
                elif name == "paste":
                    out.append({"action": "paste", "text": payload or ""})
                else:
                    out.append({"action": name, "value": payload})
                continue
            if isinstance(a, str):
                out.append({"action": a})
        return out

    async def _dispatch(self, act: dict) -> InputResult:
        name = str(act.get("action", "")).lower()
        if name in ("click", "mouse_click"):
            return await self.click(act.get("x", 0), act.get("y", 0),
                                    act.get("button", "left"), int(act.get("clicks", 1)))
        if name in ("double_click", "dblclick"):
            return await self.double_click(act.get("x", 0), act.get("y", 0))
        if name == "right_click":
            return await self.right_click(act.get("x", 0), act.get("y", 0))
        if name == "move":
            return await self.move(act.get("x", 0), act.get("y", 0))
        if name == "drag":
            return await self.drag(act.get("x1", 0), act.get("y1", 0),
                                   act.get("x2", 0), act.get("y2", 0))
        if name == "scroll":
            return await self.scroll(act.get("x", 0), act.get("y", 0), act.get("delta", 1))
        if name in ("type", "type_text", "write"):
            return await self.type_text(str(act.get("text", "")))
        if name in ("key", "press", "press_key"):
            return await self.press_key(str(act.get("key") or act.get("text") or ""))
        if name in ("hotkey", "combo"):
            return await self.hotkey(act.get("keys") or act.get("key") or "")
        if name == "paste":
            return await self.paste(str(act.get("text", "")))
        if name == "sleep":
            await asyncio.sleep(min(5.0, float(act.get("seconds", 0.2))))
            return InputResult(True, "пауза")
        return InputResult(False, f"неизвестное действие ввода: {name}")

    def _user_intervened(self) -> bool:
        """Эвристика: был ли ввод, который сделал не агент.

        Сравниваем возраст последнего ввода ОС с моментом нашего последнего
        действия: если ввод «свежее» нашего действия более чем на 200 мс, значит
        его источник — человек (SendInput обновляет тот же счётчик, поэтому
        строго различить нельзя, и мы не спешим с выводами).
        """
        try:
            age = self.backend.user_last_input_age()
        except Exception:
            return False
        if age < 0:
            return False
        since_our_input = time.time() - self._own_input_ts
        return age + 0.2 < since_our_input

    # ------------------------------------------------------------- ядро
    async def _act(self, action: str, fn, *args: Any, data: dict | None = None) -> InputResult:
        t0 = time.perf_counter()
        if not await self.acquire():
            return InputResult(False, "не удалось получить блокировку ввода")
        try:
            await self._in_executor(fn, *args)
            self._own_input_ts = time.time()
            ms = (time.perf_counter() - t0) * 1000
            self._note(action, True, ms, **(data or {}))
            if self.metrics is not None:
                self.metrics.inc("input_actions")
            if self.bus is not None and action.startswith("mouse"):
                try:
                    self.bus.emit("screen", virtual_click={"x": args[0], "y": args[1]})
                except Exception:
                    pass
            msg = f"{action} выполнен" + (" (виртуально)" if self.virtual else "")
            return InputResult(True, msg, virtual=self.virtual, ms=ms)
        except Exception as e:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000
            self._note(action, False, ms, error=str(e))
            return InputResult(False, f"{action} не выполнен: {type(e).__name__}: {e}", ms=ms)
        finally:
            self.release()

    async def _in_executor(self, fn, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._run(fn, *args))
