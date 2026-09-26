#!/usr/bin/env python3
"""Сборка набора иконок приложения из исходной картинки `assets/icon-art.png`.

Что делает:
  1. убирает тёмный фон вокруг плитки (flood-fill от углов + смягчение альфы);
  2. кадрирует плитку в квадрат с полями;
  3. пишет `icon.png` (256×256, RGBA), `favicon.png` (64×64);
  4. пишет многосекционный `icon.ico` (16/32/48/64 BMP + 256 PNG) — максимум
     совместимости с Windows Explorer/панелью задач.

Только stdlib (zlib/struct). Запуск:  python3 assets/make_icon.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent
SRC = OUT / "icon-art.png"


# ---------------------------------------------------------------- PNG codec
def read_png(path: Path):
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "не PNG"
    pos, idat, meta = 8, [], {}
    while pos < len(raw):
        (ln,) = struct.unpack(">I", raw[pos:pos + 4])
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", data)
            meta = dict(w=w, h=h, depth=depth, ctype=ctype, inter=inter)
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"IEND":
            break
    assert meta and meta["depth"] == 8 and meta["inter"] == 0, f"не поддерживается: {meta}"
    ctype = meta["ctype"]
    nch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    assert nch, f"color type {ctype}"
    w, h = meta["w"], meta["h"]
    stream = zlib.decompress(b"".join(idat))
    stride = w * nch
    out = bytearray(w * h * nch)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        f = stream[pos]
        pos += 1
        line = bytearray(stream[pos:pos + stride])
        pos += stride
        if f == 1:      # Sub
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif f == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:    # Average
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:    # Paeth
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    # → RGBA
    px = bytearray(w * h * 4)
    if nch == 4:
        px[:] = out
    elif nch == 3:
        for i in range(w * h):
            px[i * 4:i * 4 + 3] = out[i * 3:i * 3 + 3]
            px[i * 4 + 3] = 255
    elif nch == 2:
        for i in range(w * h):
            g, a = out[i * 2], out[i * 2 + 1]
            px[i * 4:i * 4 + 4] = bytes((g, g, g, a))
    else:
        for i in range(w * h):
            g = out[i]
            px[i * 4:i * 4 + 4] = bytes((g, g, g, 255))
    return w, h, px


def write_png(path: Path, w: int, h: int, rgba: bytes) -> None:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- обработка
def cut_background(w: int, h: int, px: bytearray, tol: int = 46) -> None:
    """Ставит alpha=0 пикселям фона (связанным с границей), сглаживает край."""
    def is_bg(i: int) -> bool:
        r, g, b = px[i * 4], px[i * 4 + 1], px[i * 4 + 2]
        # фон — очень тёмный, близкий к угловым
        return r < 60 and g < 70 and b < 70 and (max(r, g, b) - min(r, g, b)) < tol

    seen = bytearray(w * h)
    stack: list[int] = []
    for x in range(w):
        stack.append(x)
        stack.append((h - 1) * w + x)
    for y in range(h):
        stack.append(y * w)
        stack.append(y * w + w - 1)
    for i in stack:
        if not seen[i] and is_bg(i):
            seen[i] = 1
            px[i * 4 + 3] = 0
    # BFS
    head = 0
    while head < len(stack):
        i = stack[head]
        head += 1
        x, y = i % w, i // w
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if not seen[j] and is_bg(j):
                    seen[j] = 1
                    px[j * 4 + 3] = 0
                    stack.append(j)
    # смягчение края: два прохода box-blur по альфе
    a = bytearray(px[i * 4 + 3] for i in range(w * h))
    for _ in range(2):
        tmp = bytearray(w * h)
        for y in range(h):
            for x in range(w):
                s = n = 0
                for dx in (-1, 0, 1):
                    xx = x + dx
                    if 0 <= xx < w:
                        s += a[y * w + xx]
                        n += 1
                tmp[y * w + x] = s // n
        for y in range(h):
            for x in range(w):
                s = n = 0
                for dy in (-1, 0, 1):
                    yy = y + dy
                    if 0 <= yy < h:
                        s += tmp[yy * w + x]
                        n += 1
                a[y * w + x] = s // n
    for i in range(w * h):
        px[i * 4 + 3] = a[i]


def crop_square(w: int, h: int, px: bytearray, pad: float = 0.045):
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if px[(y * w + x) * 4 + 3] > 16:
                xs.append(x)
                ys.append(y)
    if not xs:
        return w, h, px
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    side = max(x1 - x0, y1 - y0)
    side = int(side * (1 + pad * 2))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    nx0, ny0 = cx - side // 2, cy - side // 2
    out = bytearray(side * side * 4)
    for y in range(side):
        sy = ny0 + y
        if 0 <= sy < h:
            for x in range(side):
                sx = nx0 + x
                if 0 <= sx < w:
                    out[(y * side + x) * 4:(y * side + x) * 4 + 4] = \
                        px[(sy * w + sx) * 4:(sy * w + sx) * 4 + 4]
    return side, side, out


def resize(w: int, h: int, px: bytes, nw: int, nh: int) -> bytes:
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        y0, y1 = y * h // nh, max(y * h // nh + 1, (y + 1) * h // nh)
        for x in range(nw):
            x0, x1 = x * w // nw, max(x * w // nw + 1, (x + 1) * w // nw)
            r = g = b = a = n = 0
            for sy in range(y0, y1):
                for sx in range(x0, x1):
                    i = (sy * w + sx) * 4
                    r += px[i]
                    g += px[i + 1]
                    b += px[i + 2]
                    a += px[i + 3]
                    n += 1
            o = (y * nw + x) * 4
            out[o:o + 4] = bytes((r // n, g // n, b // n, a // n))
    return bytes(out)


def ico_bmp_entry(w: int, h: int, rgba: bytes) -> bytes:
    """Секция ICO в формате BMP (32bpp BGRA, bottom-up) + AND-маска."""
    head = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                       w * h * 4, 0, 0, 0, 0)
    xor_part = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            i = (y * w + x) * 4
            r, g, b, a = rgba[i:i + 4]
            xor_part += bytes((b, g, r, a))
    mask_stride = ((w + 31) // 32) * 4
    and_part = b"\x00" * (mask_stride * h)
    return head + bytes(xor_part) + and_part


def write_ico(path: Path, sizes: dict[int, bytes], png_sizes: dict[int, bytes]) -> None:
    entries = []
    blobs = []
    off = 6 + 16 * (len(sizes) + len(png_sizes))
    for s in sorted(sizes):
        blob = ico_bmp_entry(s, s, sizes[s])
        entries.append(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                                   len(blob), off))
        blobs.append(blob)
        off += len(blob)
    for s in sorted(png_sizes):
        blob = png_sizes[s]
        entries.append(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                                   len(blob), off))
        blobs.append(blob)
        off += len(blob)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(entries))
                     + b"".join(entries) + b"".join(blobs))


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"нет исходной картинки: {SRC}")
    w, h, px = read_png(SRC)
    print(f"исходник: {w}x{h}")
    cut_background(w, h, px)
    w, h, px = crop_square(w, h, px)
    print(f"после кадрирования: {w}x{h}")

    base256 = resize(w, h, px, 256, 256)
    write_png(OUT / "icon.png", 256, 256, base256)
    write_png(OUT / "favicon.png", 64, 64, resize(w, h, px, 64, 64))

    sizes = {s: resize(w, h, px, s, s) for s in (16, 32, 48, 64)}
    png_sizes = {256: (OUT / "icon.png").read_bytes()}
    write_ico(OUT / "icon.ico", sizes, png_sizes)
    print("готово: icon.png, icon.ico, favicon.png")


if __name__ == "__main__":
    main()
