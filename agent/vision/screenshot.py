"""Скриншоты: захват, кроп, масштабирование, метаданные для координат.

Зачем отдельный менеджер (ТЗ §17, §47, §48):

  * модели НЕ отправляется скриншот 4K «как есть» — изображение уменьшается
    до разумного размера и при необходимости обрезается до нужной области;
  * каждое изображение знает своё происхождение: монитор, смещение в
    виртуальном рабочем столе, коэффициент масштабирования и DPI — без этого
    клик по координатам с картинки промахивается (классическая ошибка);
  * кэшируется последний снимок, чтобы серия операций не делала новый захват.

Бэкенды: mss (быстро, все мониторы) → нативный Windows GDI (ctypes) →
синтетический PNG в headless-режиме (конвейер «снимок → VLM → действие»
работает и без дисплея).
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Shot:
    path: str
    width: int
    height: int
    origin_x: int = 0          # левый верхний угол области в координатах экрана
    origin_y: int = 0
    monitor: int = 1
    scale: float = 1.0         # пиксели изображения / пиксели экрана
    dpi_scale: float = 1.0
    headless: bool = False
    created: float = field(default_factory=time.time)
    rgb: bytes | None = None   # только если запрошен raw-режим (для сравнений)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"path": self.path, "width": self.width, "height": self.height,
             "origin_x": self.origin_x, "origin_y": self.origin_y, "monitor": self.monitor,
             "scale": round(self.scale, 4), "dpi_scale": self.dpi_scale,
             "headless": self.headless, "created": self.created}
        d.update(self.meta)
        return d

    def describe(self) -> str:
        return (f"{self.width}x{self.height} px, монитор {self.monitor}, "
                f"смещение ({self.origin_x},{self.origin_y}), масштаб {self.scale:.2f}"
                + (" [headless]" if self.headless else ""))


# --------------------------------------------------------------------------
#  Минимальный PNG-кодек (stdlib): нужен для кропа/сравнения без Pillow
# --------------------------------------------------------------------------
def png_write(path: Path, width: int, height: int, rgb: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 3))
            + chunk(b"IEND", b""))
    path.write_bytes(data)


def png_read(path: str | Path) -> tuple[int, int, bytes]:
    """Читает PNG 8-бит (RGB/RGBA/GRAY) → (w, h, rgb-байты). Без внешних зависимостей."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("не PNG")
    pos = 8
    width = height = 0
    color_type = 2
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", body[:10])
            if depth != 8:
                raise ValueError("поддерживается только 8-битный PNG")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 3)
    stride = width * channels
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        row_out = y * width * 3
        if channels == 3:
            out[row_out:row_out + width * 3] = line
        elif channels == 4:
            for x in range(width):
                out[row_out + x * 3: row_out + x * 3 + 3] = line[x * 4:x * 4 + 3]
        elif channels == 1:
            for x in range(width):
                v = line[x]
                out[row_out + x * 3: row_out + x * 3 + 3] = bytes((v, v, v))
        else:
            for x in range(width):
                v = line[x * 2]
                out[row_out + x * 3: row_out + x * 3 + 3] = bytes((v, v, v))
        prev = line
    return width, height, bytes(out)


def subsample(w: int, h: int, rgb: bytes, max_pixels: int) -> tuple[int, int, bytes]:
    """Быстрое уменьшение (срезовые шаги — исполняются в C, не в Python)."""
    if max_pixels <= 0 or w * h <= max_pixels:
        return w, h, rgb
    import math
    step = max(2, int(math.ceil(math.sqrt(w * h / max_pixels))))
    nw = max(1, w // step)
    nh = max(1, h // step)
    stride = w * 3
    row_step = step * 3
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        src_off = y * step * stride
        row = rgb[src_off: src_off + stride][::row_step]
        # выравниваем длину строки
        if len(row) < nw * 3:
            row = row + bytes(nw * 3 - len(row))
        out[y * nw * 3:(y + 1) * nw * 3] = row[:nw * 3]
    return nw, nh, bytes(out)


def crop(w: int, h: int, rgb: bytes, x: int, y: int, cw: int, ch: int) -> tuple[int, int, bytes]:
    x, y = max(0, int(x)), max(0, int(y))
    cw, ch = max(1, min(int(cw), w - x)), max(1, min(int(ch), h - y))
    stride = w * 3
    out = bytearray(cw * ch * 3)
    for row in range(ch):
        src = (y + row) * stride + x * 3
        out[row * cw * 3:(row + 1) * cw * 3] = rgb[src:src + cw * 3]
    return cw, ch, bytes(out)


class ScreenshotManager:
    """Единая точка снятия экрана."""

    def __init__(self, cfg: Any = None, state: Any = None, cache: Any = None,
                 log: Any = None, metrics: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.cache = cache
        self.log = log
        self.metrics = metrics
        self.last: Shot | None = None
        self._counter = 0

    # -------------------------------------------------------------- настройки
    @property
    def max_pixels(self) -> int:
        # Приоритет: секция vision.max_pixels (если её задали) → fast.screenshot_max_pixels.
        v = getattr(getattr(self.cfg, "vision", None), "max_pixels", None)
        if not v:
            v = getattr(getattr(self.cfg, "fast", None), "screenshot_max_pixels", None)
        return int(v or 1474560)

    @property
    def screens_dir(self) -> Path:
        d = Path(getattr(self.cfg, "state_dir", Path.cwd() / "state")) / "screens"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _out_path(self, tag: str = "shot") -> Path:
        self._counter += 1
        return self.screens_dir / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}_{self._counter:03d}.png"

    # -------------------------------------------------------------- захват
    async def capture(self, monitor: int | None = None, region: tuple[int, int, int, int] | None = None,
                      max_pixels: int | None = None, raw: bool = False,
                      path: Path | None = None, tag: str = "shot") -> Shot:
        """Снять экран (всё/монитор/область), при необходимости уменьшить и сохранить."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.capture_sync(monitor, region, max_pixels, raw, path, tag))

    def capture_sync(self, monitor: int | None = None,
                     region: tuple[int, int, int, int] | None = None,
                     max_pixels: int | None = None, raw: bool = False,
                     path: Path | None = None, tag: str = "shot") -> Shot:
        t0 = time.perf_counter()
        out = Path(path) if path else self._out_path(tag)
        limit = self.max_pixels if max_pixels is None else max_pixels
        origin_x, origin_y = 0, 0
        dpi = float(getattr(self.state, "dpi_scale", 1.0) or 1.0)
        w = h = 0
        rgb: bytes | None = None
        headless = False

        got = self._grab_mss(monitor, region)
        if got is not None:
            w, h, rgb, origin_x, origin_y = got
        else:
            got = self._grab_win_gdi(region)
            if got is not None:
                w, h, rgb, origin_x, origin_y = got
        if rgb is None:
            # headless / нет бэкенда: синтетический кадр, конвейер остаётся рабочим
            headless = True
            w, h = 640, 360
            seed = int(time.time() * 1000) % 256
            buf = bytearray(w * h * 3)
            for y in range(h):
                for x in range(0, w, 4):
                    base = 30 + (x * 40 // w) + (y * 20 // h)
                    buf[(y * w + x) * 3] = (base + 10) & 0xFF
                    buf[(y * w + x) * 3 + 1] = (base + 30 + seed // 8) & 0xFF
                    buf[(y * w + x) * 3 + 2] = (base + 70) & 0xFF
            rgb = bytes(buf)

        scale = 1.0
        if limit and w * h > limit:
            nw, nh, rgb2 = subsample(w, h, rgb, limit)
            scale = nw / w
            w, h, rgb = nw, nh, rgb2
        if region and got is None:
            origin_x, origin_y = int(region[0]), int(region[1])
        png_write(out, w, h, rgb)
        shot = Shot(path=str(out), width=w, height=h, origin_x=origin_x, origin_y=origin_y,
                    monitor=monitor or 1, scale=round(scale, 6), dpi_scale=dpi,
                    headless=headless, rgb=rgb if raw else None,
                    meta={"ms": round((time.perf_counter() - t0) * 1000, 1),
                          "bytes": len(rgb), "region": list(region) if region else None,
                          "capped": bool(limit and w * h >= limit)})
        self.last = shot
        if self.metrics is not None:
            self.metrics.vision_call(shot.meta["ms"], ok=not headless)
        if self.log is not None:
            self.log.event("screenshot", ms=shot.meta["ms"], width=w, height=h,
                           monitor=shot.monitor, headless=headless, path=str(out))
        return shot

    def _grab_mss(self, monitor: int | None, region: tuple[int, int, int, int] | None):
        try:
            import mss  # type: ignore
            import mss.tools  # type: ignore
        except Exception:
            return None
        try:
            with mss.mss() as sct:
                if region:
                    x, y, w, h = region
                    box = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
                elif monitor:
                    mons = sct.monitors[1:]
                    box = mons[monitor - 1] if 0 < monitor <= len(mons) else sct.monitors[0]
                else:
                    box = sct.monitors[0]
                img = sct.grab(box)
                rgb = bytes(img.rgb)
                return img.size[0], img.size[1], rgb, int(box.get("left", 0)), int(box.get("top", 0))
        except Exception as e:  # noqa: BLE001
            if self.log:
                self.log.warn(f"mss-захват не удался: {e}")
            return None

    def _grab_win_gdi(self, region: tuple[int, int, int, int] | None):
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32  # type: ignore[attr-defined]
            g = ctypes.windll.gdi32  # type: ignore[attr-defined]
            if region:
                x, y, w, h = (int(v) for v in region)
            else:
                x = u.GetSystemMetrics(76)
                y = u.GetSystemMetrics(77)
                w = u.GetSystemMetrics(78)
                h = u.GetSystemMetrics(79)
            hdc = u.GetDC(0)
            mem = g.CreateCompatibleDC(hdc)
            bmp = g.CreateCompatibleBitmap(hdc, w, h)
            g.SelectObject(mem, bmp)
            g.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)  # SRCCOPY

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            bi = BITMAPINFOHEADER()
            bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.biWidth = w
            bi.biHeight = -h            # top-down
            bi.biPlanes = 1
            bi.biBitCount = 24
            bi.biCompression = 0
            stride = ((w * 3 + 3) // 4) * 4
            buf = ctypes.create_string_buffer(stride * h)
            g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
            u.ReleaseDC(0, hdc)
            raw = buf.raw
            # BGR → RGB и срез padding'а
            out = bytearray(w * h * 3)
            for row in range(h):
                src = row * stride
                seg = raw[src:src + w * 3]
                out[row * w * 3:(row + 1) * w * 3] = bytes(
                    [seg[i + 2] if i % 3 == 0 else (seg[i + 1] if i % 3 == 1 else seg[i - 2])
                     for i in range(len(seg))])
            return w, h, bytes(out), int(x), int(y)
        except Exception as e:  # noqa: BLE001
            if self.log:
                self.log.warn(f"GDI-захват не удался: {e}")
            return None

    # -------------------------------------------------------------- утилиты
    def crop_last(self, x: int, y: int, w: int, h: int) -> Shot | None:
        """Обрезать последний снимок (координаты — в пикселях изображения)."""
        shot = self.last
        if shot is None:
            return None
        try:
            iw, ih, rgb = png_read(shot.path)
            cw, ch, cropped = crop(iw, ih, rgb, x, y, w, h)
            out = self._out_path("crop")
            png_write(out, cw, ch, cropped)
            new = Shot(path=str(out), width=cw, height=ch,
                       origin_x=shot.origin_x + int(x / max(shot.scale, 1e-6)),
                       origin_y=shot.origin_y + int(y / max(shot.scale, 1e-6)),
                       monitor=shot.monitor, scale=shot.scale, dpi_scale=shot.dpi_scale,
                       headless=shot.headless, meta={**shot.meta, "cropped_from": shot.path})
            self.last = new
            return new
        except Exception:
            return None

    def read_rgb(self, shot: Shot | None = None) -> tuple[int, int, bytes] | None:
        shot = shot or self.last
        if shot is None:
            return None
        if shot.rgb is not None:
            return shot.width, shot.height, shot.rgb
        try:
            return png_read(shot.path)
        except Exception:
            return None

    def difference(self, a: Shot | None, b: Shot | None) -> float:
        """Доля изменившихся пикселей между снимками (0..1) — для верификации."""
        ra, rb = self.read_rgb(a), self.read_rgb(b)
        if not ra or not rb:
            return 0.0
        w = min(ra[0], rb[0])
        h = min(ra[1], rb[1])
        if w <= 0 or h <= 0:
            return 0.0
        da, db = ra[2], rb[2]
        step = 4        # сравнение по каждой 4-й точке — быстрее и достаточно
        changed = total = 0
        for y in range(0, h, 2):
            row_a = y * ra[0] * 3
            row_b = y * rb[0] * 3
            for x in range(0, w, step):
                ia = row_a + x * 3
                ib = row_b + x * 3
                if abs(da[ia] - db[ib]) + abs(da[ia + 1] - db[ib + 1]) + abs(da[ia + 2] - db[ib + 2]) > 36:
                    changed += 1
                total += 1
        return round(changed / total, 4) if total else 0.0
