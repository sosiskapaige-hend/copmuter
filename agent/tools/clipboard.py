"""Буфер обмена: чтение/запись текста, перенос между приложениями.

Windows — win32 API через ctypes; Linux — xclip/xsel; headless — виртуальный
буфер (промежуточный канал всё равно работает внутри системы агента).
"""
from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry

_virtual_buf: str = ""
_buf_lock = threading.Lock()


def _win32_clip() -> Any:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:
        return None


def _win_get() -> str | None:
    u = _win32_clip()
    if not u:
        return None
    import ctypes
    if not u.OpenClipboard(0):
        return None
    try:
        CF_UNICODETEXT = 13
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = ctypes.windll.kernel32.GlobalLock(h)  # type: ignore[attr-defined]
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            ctypes.windll.kernel32.GlobalUnlock(h)  # type: ignore[attr-defined]
    finally:
        u.CloseClipboard()


def _win_set(text: str) -> bool:
    u = _win32_clip()
    if not u:
        return False
    import ctypes
    if not u.OpenClipboard(0):
        return False
    try:
        u.EmptyClipboard()
        CF_UNICODETEXT = 13
        size = (len(text) + 1) * 2
        h = ctypes.windll.kernel32.GlobalAlloc(0x0042, size)  # type: ignore[attr-defined]  # GMEM_MOVEABLE|GMEM_ZEROINIT
        p = ctypes.windll.kernel32.GlobalLock(h)  # type: ignore[attr-defined]
        ctypes.memmove(p, text.encode("utf-16-le"), size)
        ctypes.windll.kernel32.GlobalUnlock(h)  # type: ignore[attr-defined]
        u.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        u.CloseClipboard()


def register_clipboard_tools(reg: ToolRegistry) -> None:

    @reg.tool("clip_get", "Прочитать текст из буфера обмена.", risk=Risk.NONE,
              category="clipboard", is_gui=True,
              parameters={"type": "object", "properties": {}})
    class ClipGet(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            if ctx.platform.system == "windows":
                text = _win_get()
                if text is None:
                    return ToolResult.fail("не удалось открыть буфер обмена")
                return ToolResult.ok_result(text or "(пусто)")
            if ctx.platform.has_x11:
                for cmd in (["xclip", "-selection", "clipboard", "-o"],
                            ["xsel", "--clipboard", "--output"]):
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode == 0:
                        return ToolResult.ok_result(r.stdout)
                return ToolResult.fail("нет xclip/xsel")
            with _buf_lock:
                return ToolResult.ok_result(_virtual_buf or "(виртуальный буфер пуст)")

    @reg.tool("clip_set",
              "Записать текст в буфер обмена (для переноса между приложениями: "
              "clip_set + hotkey ctrl+v).",
              risk=Risk.LOW, category="clipboard", is_gui=True,
              parameters={"type": "object", "properties": {
                  "text": _prop("string", "Текст")}, "required": ["text"]})
    class ClipSet(Tool):
        async def execute(self, ctx: ToolContext, text: str) -> ToolResult:
            if ctx.platform.system == "windows":
                ok = _win_set(text)
                return ToolResult.ok_result(f"Буфер: {len(text)} символов") if ok \
                    else ToolResult.fail("не удалось записать в буфер")
            if ctx.platform.has_x11:
                r = subprocess.run(["xclip", "-selection", "clipboard"],
                                   input=text.encode(), capture_output=True)
                return ToolResult.ok_result(f"Буфер: {len(text)} символов") if r.returncode == 0 \
                    else ToolResult.fail("нет xclip")
            global _virtual_buf
            with _buf_lock:
                _virtual_buf = text
            return ToolResult.ok_result(f"[virtual] буфер: {len(text)} символов", virtual=True)

    @reg.tool("copy_file_to_clip",
              "Положить файлы в буфер обмена как объект перетаскивания (Windows).",
              risk=Risk.LOW, category="clipboard", is_gui=True,
              parameters={"type": "object", "properties": {
                  "paths": _prop("array", "Пути файлов")}, "required": ["paths"]})
    class CopyFileToClip(Tool):
        async def execute(self, ctx: ToolContext, paths: list) -> ToolResult:
            if ctx.platform.system != "windows":
                return ToolResult.fail("доступно только на Windows; на Linux используйте fs_copy")
            import subprocess
            # PowerShell CFSTR_FILEDESCRIPTION + DropTarget: простой способ — pywin32, если есть
            try:
                import win32clipboard  # type: ignore
                from win32con import CF_HDROP  # type: ignore
                import ctypes
                from ctypes import wintypes
                import os
                files = [os.path.abspath(p) for p in paths]
                u = ctypes.windll.user32
                CF_UNICODETEXT = 13
                # упрощённо: кладём пути текстом (drag&drop объектов требует pywin32)
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText("\n".join(files))
                finally:
                    win32clipboard.CloseClipboard()
                return ToolResult.ok_result(f"Пути в буфере: {len(files)}")
            except ImportError:
                return ToolResult.fail("нужен pywin32 для файлов в буфере; "
                                       "альтернатива: fs_copy / mouse_drag")
