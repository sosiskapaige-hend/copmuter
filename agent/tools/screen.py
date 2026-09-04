"""Скриншоты: все мониторы, конкретный монитор, последний скриншот.

Реальный захват — через mss (если установлен и есть дисплей). В headless-
режиме генерируется синтетический PNG (градиент + «HEADLESS»-метка), чтобы
весь конвейер «снять экран → показать в UI → проанализировать» работал.
"""
from __future__ import annotations

import os
import struct
import time
import zlib
from pathlib import Path

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


def _write_png(path: Path, width: int, height: int, pixel_fn) -> None:
    """Минимальный PNG-генератор (stdlib). pixel_fn(x, y) -> (r, g, b)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            r, g, b = pixel_fn(x, y)
            raw += bytes((r, g, b))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def _headless_png(path: Path, label: str, seed: int = 0) -> None:
    w, h = 640, 360
    import hashlib
    hv = int(hashlib.md5(label.encode()).hexdigest()[:4], 16) % 256

    def px(x: int, y: int):
        base = 30 + (x * 40 // w) + (y * 20 // h)
        return (base + 10, base + 30 + hv // 8, base + 70)

    _write_png(path, w, h, px)


def _capture(monitor_index: int | None, out_path: Path) -> tuple[bool, str, dict]:
    """Возвращает (ok, error, meta)."""
    try:
        import mss
        import mss.tools
        with mss.mss() as sct:
            mon = None
            if monitor_index:
                mons = sct.monitors[1:]
                mon = mons[monitor_index - 1] if 0 < monitor_index <= len(mons) else mons[0]
            else:
                mon = sct.monitors[0]  # все мониторы вместе
            img = sct.grab(mon)
            mss.tools.to_png(img.rgb, img.size, output=str(out_path))
            return True, "", {"width": img.size[0], "height": img.size[1],
                              "monitor": monitor_index or 1}
    except ImportError:
        return False, "mss не установлен (pip install mss)", {}
    except Exception as e:  # noqa: BLE001
        return False, f"захват не удался: {e}", {}


def register_screen_tools(reg: ToolRegistry) -> None:

    @reg.tool("screen_capture",
              "Сделать скриншот экрана (или конкретного монитора). Сохраняет PNG, "
              "путь виден в UI. Следующий screen_describe/ocr_image работают на нём.",
              risk=Risk.NONE, category="screen", is_gui=True,
              parameters={"type": "object", "properties": {
                  "monitor": _prop("integer", "Индекс монитора (1..N). По умолчанию — весь виртуальный экран")},
                  "required": []})
    class ScreenCapture(Tool):
        async def execute(self, ctx: ToolContext, monitor: int | None = None) -> ToolResult:
            screen_dir = Path(ctx.cfg.state_dir / "screens")
            screen_dir.mkdir(parents=True, exist_ok=True)
            out = screen_dir / f"shot_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time()*1000) % 1000}.png"
            import asyncio
            loop = asyncio.get_running_loop()
            ok, err, meta = await loop.run_in_executor(None, _capture, monitor, out)
            if ok:
                ctx.set_screenshot_cache(str(out))
                ctx.bus.emit("screen", path=str(out), monitor=monitor or 1)
                return ToolResult.ok_result(
                    f"Скриншот сохранён: {out} ({meta.get('width')}x{meta.get('height')}, монитор {meta.get('monitor')})",
                    path=str(out), **meta)
            # headless fallback
            if not ctx.platform.has_display:
                _headless_png(out, label=f"headless-shot-{time.time()}")
                ctx.set_screenshot_cache(str(out))
                ctx.bus.emit("screen", path=str(out), monitor=monitor or 1)
                return ToolResult.ok_result(
                    f"HEADLESS-режим: захват дисплея невозможен ({err}). "
                    f"Сгенерирован синтетический снимок: {out}. "
                    f"Для реальных GUI-операций запустите агента на машине с дисплеем.",
                    path=str(out), headless=True, width=640, height=360)
            return ToolResult.fail(err)

    @reg.tool("last_screenshot",
              "Путь к последнему сделанному скриншоту (без нового захвата).",
              risk=Risk.NONE, category="screen",
              parameters={"type": "object", "properties": {}})
    class LastScreenshot(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            p = ctx.screenshot_cache()
            if not p or not os.path.exists(p):
                return ToolResult.fail("скриншот ещё не сделан — вызовите screen_capture")
            return ToolResult.ok_result(p, path=p)

    @reg.tool("screen_monitor",
              "Скриншот конкретного монитора по индексу (см. list_monitors).",
              risk=Risk.NONE, category="screen", is_gui=True,
              parameters={"type": "object", "properties": {
                  "index": _prop("integer", "Индекс монитора")}, "required": ["index"]})
    class ScreenMonitor(Tool):
        async def execute(self, ctx: ToolContext, index: int) -> ToolResult:
        # делегируем
            reg_tool = reg.get("screen_capture")
            assert reg_tool is not None
            return await reg_tool.execute(ctx, monitor=index)
