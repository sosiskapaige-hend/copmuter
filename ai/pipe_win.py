"""Named Pipes на Windows без внешних зависимостей (ctypes).

Рантайм (C++) подключается к `\\\\.\\pipe\\agent_ai_v1`, воркер обслуживает его
постоянно. Протокол тот же, что и в dev-режиме: [uint32 BE длина][UTF-8 JSON].
"""

from __future__ import annotations

import ctypes
import logging
import struct
import time
from ctypes import wintypes

log = logging.getLogger("ai.pipe")

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_CONNECTED = 535
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

HEADER = struct.Struct(">I")


class PipeError(RuntimeError):
    pass


def _check(result, what: str) -> None:
    if not result:
        raise PipeError(f"{what}: ошибка {ctypes.get_last_error()}")


class PipeServer:
    """Сервер Named Pipe: одна задача за раз, один клиент (рантайм)."""

    def __init__(self, name: str) -> None:
        self.name = name if name.startswith("\\\\") else rf"\\.\pipe\{name}"
        self.handle = None

    def create(self) -> None:
        self.handle = kernel32.CreateNamedPipeW(
            ctypes.c_wchar_p(self.name),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            1 << 20,
            1 << 20,
            0,
            None,
        )
        if self.handle == INVALID_HANDLE_VALUE:
            raise PipeError(f"CreateNamedPipeW({self.name}): ошибка {ctypes.get_last_error()}")

    def accept(self) -> None:
        if self.handle is None:
            self.create()
        if kernel32.ConnectNamedPipe(self.handle, None):
            return
        err = ctypes.get_last_error()
        if err == ERROR_PIPE_CONNECTED:
            return
        raise PipeError(f"ConnectNamedPipe: ошибка {err}")

    def disconnect(self) -> None:
        if self.handle is not None:
            kernel32.DisconnectNamedPipe(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None

    # --------------------------------------------------------------- ввод-вывод
    def read_exact(self, size: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        buffer = ctypes.create_string_buffer(min(size, 1 << 20) or 1)
        while remaining > 0:
            read = wintypes.DWORD(0)
            chunk = min(remaining, len(buffer))
            ok = kernel32.ReadFile(self.handle, buffer, chunk, ctypes.byref(read), None)
            if not ok or read.value == 0:
                return None
            chunks.append(buffer.raw[: read.value])
            remaining -= read.value
        return b"".join(chunks)

    def read_frame(self) -> bytes | None:
        header = self.read_exact(HEADER.size)
        if header is None:
            return None
        (size,) = HEADER.unpack(header)
        if not size:
            return b""
        return self.read_exact(size)

    def write_frame(self, payload: bytes) -> None:
        data = HEADER.pack(len(payload)) + payload
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(self.handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            raise PipeError(f"WriteFile: ошибка {ctypes.get_last_error()}")

    def wait_for_pipe(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if kernel32.WaitNamedPipeW(ctypes.c_wchar_p(self.name), 200):
                return True
            time.sleep(0.05)
        return False


def run_pipe_worker(worker, name: str = r"\\.\pipe\agent_ai_v1") -> None:
    """Обслуживать рантайм по Named Pipe (Windows)."""
    from . import protocol

    server = PipeServer(name)
    log.info("воркер слушает %s", name)
    try:
        while True:
            server.create()
            try:
                server.accept()
                log.info("рантайм подключился")
                while True:
                    frame = server.read_frame()
                    if frame is None:
                        break
                    request = protocol.decode(frame)
                    reply = worker.handle(request if isinstance(request, dict) else {})
                    server.write_frame(protocol.encode(reply))
            except PipeError as exc:
                log.warning("канал: %s", exc)
            finally:
                server.disconnect()
    finally:
        server.disconnect()
