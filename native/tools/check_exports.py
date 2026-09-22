#!/usr/bin/env python3
"""Сверка C ABI: что объявлено в runtime.h, то и должно быть в экспортах DLL.

C# вызывает функции через P/Invoke: если объявление есть, а символа в DLL нет,
приложение падает с EntryPointNotFoundException уже у пользователя. Проверка ловит
это на сборке.

    python3 tools/check_exports.py build/AgentRuntime.dll include/agent/runtime.h
"""

from __future__ import annotations

import re
import struct
import sys


def pe_exports(path: str) -> tuple[int, list[str]]:
    data = open(path, "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise SystemExit(f"{path}: это не PE-файл")
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    sections = []
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    size_opt = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    is_plus = struct.unpack_from("<H", data, opt)[0] == 0x20B
    exp_rva, _ = struct.unpack_from("<II", data, opt + (112 if is_plus else 96))
    sec_off = opt + size_opt
    for i in range(nsec):
        base = sec_off + i * 40
        vsize, vaddr = struct.unpack_from("<II", data, base + 8)
        raw_size, raw_ptr = struct.unpack_from("<II", data, base + 16)
        sections.append((vaddr, vsize, raw_ptr, raw_size))

    def to_offset(rva: int) -> int | None:
        for vaddr, vsize, raw_ptr, raw_size in sections:
            if vaddr <= rva < vaddr + max(vsize, raw_size):
                return raw_ptr + (rva - vaddr)
        return None

    if exp_rva == 0:
        return machine, []
    off = to_offset(exp_rva)
    assert off is not None
    count = struct.unpack_from("<I", data, off + 24)[0]
    names_rva = struct.unpack_from("<I", data, off + 32)[0]
    names_off = to_offset(names_rva)
    assert names_off is not None
    names = []
    for i in range(count):
        name_rva = struct.unpack_from("<I", data, names_off + i * 4)[0]
        noff = to_offset(name_rva)
        assert noff is not None
        end = data.index(b"\0", noff)
        names.append(data[noff:end].decode())
    return machine, sorted(names)


def declared_exports(header_path: str) -> list[str]:
    """Объявления C ABI: блок extern "C" { ... } в заголовке."""
    text = open(header_path, encoding="utf-8").read()
    start = text.find('extern "C" {')
    if start < 0:
        raise SystemExit("в заголовке нет блока extern \"C\"")
    end = text.find("}  // extern", start)
    if end < 0:
        end = text.find("\n}\n", start)
    body = text[start:end]
    names = re.findall(r"\b(agent_[a-z_0-9]+)\s*\(", body)
    return sorted(set(names))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    dll, header = argv[1], argv[2]
    machine, exported = pe_exports(dll)
    declared = declared_exports(header)
    arch = {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(machine, hex(machine))
    print(f"   архитектура: {arch}, экспортов в DLL: {len(exported)}, объявлено в ABI: {len(declared)}")

    missing = [name for name in declared if name not in exported]
    extra = [name for name in exported if name not in declared]
    if missing:
        print("   ОШИБКА: объявлены, но не экспортируются: " + ", ".join(missing))
    for name in extra:
        print("   ОШИБКА: экспортируется, но не объявлено в заголовке: " + name)
    if missing or extra:
        return 1
    print("   C ABI полностью совпадает: C# найдёт все точки входа")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
