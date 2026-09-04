"""Определение платформы и доступности «железа»: дисплей, аудио, GUI-библиотеки.

Ключевой принцип: агент никогда не падает из-за отсутствия GUI-подсистемы.
Каждый GUI-инструмент проверяет доступность и, если её нет, возвращает
наблюдение «нет дисплея» — агент понимает это и выбирает прямой способ
(терминал, файловая система) или сообщает пользователю.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _cmd_available(name: str) -> bool:
    return shutil.which(name) is not None


@dataclass
class PlatformInfo:
    system: str = "unknown"          # windows | linux | macos
    has_display: bool = False
    has_terminal: bool = True
    has_mss: bool = False
    has_pyautogui: bool = False
    has_psutil: bool = False
    has_playwright: bool = False
    has_pypdf: bool = False
    has_pillow: bool = False
    has_tesseract: bool = False
    has_git: bool = False
    has_watchdog: bool = False
    has_x11: bool = False
    has_wmctrl: bool = False
    has_xdotool: bool = False
    monitors: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"Платформа: {self.system}",
            f"Дисплей: {'да' if self.has_display else 'НЕТ (headless) — GUI-инструменты недоступны, используйте прямые'}",
            f"Терминал: {'да' if self.has_terminal else 'нет'}",
            f"Git: {'да' if self.has_git else 'нет'}",
            f"psutil: {'да' if self.has_psutil else 'нет'}",
            f"Скриншоты (mss): {'да' if self.has_mss else 'нет (будут синтетические)'}",
            f"Мышь/клавиатура (pyautogui): {'да' if self.has_pyautogui else 'нет'}",
            f"Браузер (playwright): {'да' if self.has_playwright else 'нет'}",
            f"PDF (pypdf): {'да' if self.has_pypdf else 'нет'}",
            f"OCR (tesseract): {'да' if self.has_tesseract else 'нет'}",
        ]
        return "\n".join(lines)


def detect() -> PlatformInfo:
    info = PlatformInfo()
    if sys.platform == "win32":
        info.system = "windows"
        info.has_display = True
        info.has_wmctrl = False
    elif sys.platform == "darwin":
        info.system = "macos"
        info.has_display = True
    else:
        info.system = "linux"
        info.has_x11 = bool(os.environ.get("DISPLAY"))
        info.has_display = info.has_x11
        info.has_wmctrl = _cmd_available("wmctrl")
        info.has_xdotool = _cmd_available("xdotool")

    info.has_mss = _module_available("mss")
    info.has_pyautogui = _module_available("pyautogui")
    info.has_psutil = _module_available("psutil")
    info.has_pypdf = _module_available("pypdf")
    info.has_pillow = _module_available("PIL")
    info.has_watchdog = _module_available("watchdog")
    info.has_git = _cmd_available("git")
    info.has_terminal = _cmd_available("bash") or _cmd_available("sh") or sys.platform == "win32"

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        info.has_playwright = True
    except Exception:
        info.has_playwright = False

    if info.has_x11:
        # проверка tesseract
        info.has_tesseract = _cmd_available("tesseract")
    else:
        info.has_tesseract = False

    info.monitors = _detect_monitors(info)
    return info


def _detect_monitors(info: PlatformInfo) -> list[dict]:
    if info.has_mss:
        try:
            import mss
            with mss.mss() as sct:
                out = []
                for i, m in enumerate(sct.monitors[1:], start=1):
                    out.append({"index": i, "name": f"monitor-{i}",
                                "left": m["left"], "top": m["top"],
                                "width": m["width"], "height": m["height"],
                                "primary": i == 1})
                return out
        except Exception:
            pass
    if info.system == "windows":
        return [{"index": 1, "name": "primary", "left": 0, "top": 0,
                 "width": 1920, "height": 1080, "primary": True}]
    if info.has_x11:
        try:
            r = subprocess.run(["xdpyinfo"], capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if "dimensions:" in line:
                    dims = line.split("dimensions:")[1].strip().split("x")[0].strip()
                    h = line.split("x")[1].strip().split()[0]
                    return [{"index": 1, "name": "primary", "left": 0, "top": 0,
                             "width": int(dims), "height": int(h), "primary": True}]
        except Exception:
            pass
    return []


_cached: PlatformInfo | None = None


def get_platform(force: bool = False) -> PlatformInfo:
    global _cached
    if _cached is None or force:
        _cached = detect()
    return _cached
