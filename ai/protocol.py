"""Общий протокол канала C++ ↔ Python.

Формат на проводе (одинаковый для Named Pipes в Windows и AF_UNIX в dev-режиме):

    [uint32 BE: длина N][N байт UTF-8 JSON]

Один запрос — один ответ. Соединение постоянное: воркер живёт всё время работы
приложения, никаких `python -c` на каждый вызов (ТЗ §16, §45).
"""

from __future__ import annotations

import json
import struct
from typing import Any

HEADER = struct.Struct(">I")
MAX_FRAME = 64 * 1024 * 1024

# Типы запросов (совпадают с тем, что шлёт AgentRuntime).
REQ_PLAN = "plan"
REQ_REPLAN = "replan"
REQ_CHAT = "chat"
REQ_CODE = "code"
REQ_VISION = "vision"
REQ_COMPRESS = "compress"
REQ_PREFLIGHT = "preflight"
REQ_HEALTH = "health"

ALL_REQUESTS = {
    REQ_PLAN,
    REQ_REPLAN,
    REQ_CHAT,
    REQ_CODE,
    REQ_VISION,
    REQ_COMPRESS,
    REQ_PREFLIGHT,
    REQ_HEALTH,
}


class ProtocolError(RuntimeError):
    """Нарушение формата кадра или неверный JSON."""


def encode(obj: Any) -> bytes:
    """Python-объект → кадр."""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"слишком большой кадр: {len(payload)} байт")
    return HEADER.pack(len(payload)) + payload


def decode(frame: bytes) -> Any:
    """Кадр без заголовка → Python-объект."""
    try:
        return json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - защита
        raise ProtocolError(f"не разобрать JSON: {exc}") from exc


def read_frame(sock) -> bytes | None:
    """Прочитать один кадр из сокета. None — соединение закрыто."""
    header = _read_exact(sock, HEADER.size)
    if header is None:
        return None
    (size,) = HEADER.unpack(header)
    if size > MAX_FRAME:
        raise ProtocolError(f"кадр слишком большой: {size} байт")
    if size == 0:
        return b""
    return _read_exact(sock, size)


def write_frame(sock, obj: Any) -> None:
    """Записать объект как один кадр."""
    sock.sendall(encode(obj))


def _read_exact(sock, size: int) -> bytes | None:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ProtocolError("обрыв кадра")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def error_reply(request_id: Any, message: str, *, kind: str = "error") -> dict:
    """Единый формат ошибки: UI всегда показывает человекочитаемую строку."""
    return {"id": request_id, "ok": False, "kind": kind, "error": message}


def plan_reply(
    request_id: Any,
    calls: list[dict],
    *,
    say: str = "",
    finished: bool = False,
    usage: dict | None = None,
    data: dict | None = None,
) -> dict:
    """Ответ-план: последовательность вызовов инструментов рантайма."""
    reply = {
        "id": request_id,
        "ok": True,
        "kind": "plan",
        "say": say,
        "calls": calls,
        "finished": bool(finished) and not calls,
    }
    if usage:
        reply["usage"] = usage
    if data:
        reply["data"] = data
    return reply


def text_reply(request_id: Any, text: str, *, kind: str = "chat", usage: dict | None = None) -> dict:
    reply = {"id": request_id, "ok": True, "kind": kind, "say": text, "calls": [], "finished": True}
    if usage:
        reply["usage"] = usage
    return reply
