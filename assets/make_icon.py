"""Генерация иконки приложения: icon.png (RGBA 256) + icon.ico (PNG-in-ICO).

Стиль: скруглённый квадрат, градиент голубой → бирюзовый, «орб» — кольцо
и точка (AI-ядро). Чистый stdlib (zlib/struct).
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

W = H = 256
OUT = Path(__file__).parent


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def pixel(x: int, y: int):
    """Возвращает (r, g, b, a) для пикселя."""
    # скруглённые углы
    rad = 46
    cx = cy = None
    if x < rad and y < rad:
        cx, cy = rad, rad
    elif x >= W - rad and y < rad:
        cx, cy = W - rad - 1, rad
    elif x < rad and y >= H - rad:
        cx, cy = rad, H - rad - 1
    elif x >= W - rad and y >= H - rad:
        cx, cy = W - rad - 1, H - rad - 1
    if cx is not None:
        dx, dy = x - cx, y - cy
        if dx * dx + dy * dy > rad * rad:
            return (0, 0, 0, 0)
    # градиент: голубой (#0ea5e9) → бирюзовый (#2dd4bf), по диагонали
    t = (x + y) / (W + H - 2)
    r = lerp(0x0e, 0x2d, t)
    g = lerp(0xa5, 0xd4, t)
    b = lerp(0xe9, 0xbf, t)
    # лёгкое свечение в центре
    mx, my = W / 2, H / 2
    dist_c = ((x - mx) ** 2 + (y - my) ** 2) ** 0.5
    glow = max(0.0, 1.0 - dist_c / 130.0) * 0.22
    r = min(255, r + 255 * glow * 0.5)
    g = min(255, g + 255 * glow * 0.7)
    b = min(255, b + 255 * glow * 0.6)

    # «орб»: белое кольцо (R=62, толщина 12) + точка (R=24)
    d = ((x - mx) ** 2 + (y - my) ** 2) ** 0.5
    if abs(d - 62) <= 12 or d <= 24:
        # мягкий край
        edge = min(1.0, min(abs(d - 62) + 3, d + 3, 13 - abs(d - 62)) )
        a_white = min(255, 235 * max(0.25, edge))
        r = lerp(r, 255, a_white / 255)
        g = lerp(g, 255, a_white / 255)
        b = lerp(b, 255, a_white / 255)
    # 4 «сенсора» на кольсе
    for ang in (45, 135, 225, 315):
        import math
        sx = mx + 62 * math.cos(math.radians(ang))
        sy = my + 62 * math.sin(math.radians(ang))
        dd = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
        if dd <= 10:
            r = lerp(r, 255, 0.95)
            g = lerp(g, 255, 0.95)
            b = lerp(b, 255, 0.95)
    return (int(r), int(g), int(b), 255)


def write_png(path: Path) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            raw += bytes(pixel(x, y))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)
    return png


def write_ico(png_bytes: bytes, path: Path) -> None:
    """ICO с одним 256x256 кадром в PNG-сжатии (поддерживается с Vista)."""
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png_bytes), 22)
    path.write_bytes(header + entry + png_bytes)


if __name__ == "__main__":
    png = write_png(OUT / "icon.png")
    write_ico(png, OUT / "icon.ico")
    print("icon.png:", (OUT / "icon.png").stat().st_size, "Б")
    print("icon.ico:", (OUT / "icon.ico").stat().st_size, "Б")
