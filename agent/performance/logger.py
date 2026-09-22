"""Структурный лог агента (ТЗ §43): что запросил пользователь, какой маршрут
выбран, какой инструмент и метод выполнились, сколько это заняло, чем
закончилось и был ли fallback.

Формат — JSONL (`AGENT_HOME/logs/agent.jsonl`) с ротацией по размеру:
его удобно грепать и анализировать, а одна строка выглядит так:

    {"ts": 1758530000.1, "time": "12:42:10", "kind": "tool", "task": "Открой Discord",
     "route": "fast_direct", "tool": "launch_app", "method": "cached_executable",
     "ms": 182.4, "ok": true, "fallback": false}

Плюс человекочитаемый вид для консоли/UI:

    12:42:10 User: Открой Discord | Router: launch_application | Tool: cached_executable | 182ms | SUCCESS
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
MAX_BYTES = 2 * 1024 * 1024
KEEP_FILES = 3


def _short(v: Any, n: int = 300) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "…"
    if isinstance(v, dict):
        return {k: _short(x, max(80, n // max(1, len(v)))) for k, x in list(v.items())[:12]}
    if isinstance(v, (list, tuple)):
        return [_short(x, max(60, n // max(1, len(v)))) for x in list(v)[:8]]
    return v


class RunLog:
    """Журнал действий агента (JSONL + человекочитаемые строки + ринг-буфер)."""

    def __init__(self, path: Path | str | None = None, level: str = "INFO",
                 enabled: bool = True, ring: int = 500) -> None:
        self.path = Path(path) if path else None
        self.level = (level or "INFO").upper()
        self.enabled = enabled and self.level != "OFF"
        self._min = LEVELS.get(self.level, 20)
        self._lock = threading.RLock()
        self._ring: deque[dict] = deque(maxlen=ring)
        self._logger = logging.getLogger("agent.trace")
        self.written = 0

    # ---------------- запись ----------------
    def event(self, kind: str, level: str = "INFO", **fields: Any) -> dict:
        now = time.time()
        rec = {"ts": round(now, 3),
               "time": time.strftime("%H:%M:%S", time.localtime(now)),
               "kind": kind, "level": level.upper()}
        for k, v in fields.items():
            if v is not None and v != "":
                rec[k] = _short(v)
        with self._lock:
            self._ring.append(rec)
        if not self.enabled:
            return rec
        if LEVELS.get(rec["level"], 20) >= self._min:
            self._write(rec)
        return rec

    def _write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        self._logger.debug(line)
        if not self.path:
            return
        try:
            with self._lock:
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self.written += 1
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        assert self.path is not None
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                for i in range(KEEP_FILES - 1, 0, -1):
                    src = self.path.with_suffix(f".{i}.jsonl")
                    dst = self.path.with_suffix(f".{i + 1}.jsonl")
                    if src.exists():
                        src.replace(dst)
                self.path.replace(self.path.with_suffix(".1.jsonl"))
        except OSError:
            pass

    # ---------------- удобные обёртки (контракт ТЗ §43) ----------------
    def task_start(self, goal: str, route: str, mode: str = "") -> None:
        self.event("task_start", task=goal, route=route, mode=mode)

    def routing(self, goal: str, route: str, reason: str = "", ms: float = 0.0,
                intents: Any = None, llm_calls_expected: int | None = None,
                confidence: float | None = None) -> dict:
        return self.event("route", task=goal, route=route, reason=reason,
                          ms=round(ms, 2),
                          intents=intents if isinstance(intents, (str, list)) else None,
                          llm_calls_expected=llm_calls_expected,
                          confidence=confidence)

    def tool(self, tool: str, args: Any = None, ms: float = 0.0, ok: bool = True,
             task: str = "", route: str = "", method: str = "", fallback: bool = False,
             error: str = "", status: str = "", cached: bool = False) -> dict:
        return self.event("tool", task=task, route=route, tool=tool, method=method,
                          args=args, ms=round(ms, 1), ok=ok, status=status or ("success" if ok else "failed"),
                          fallback=fallback or None, error=error or None,
                          cached=cached or None)

    def fallback(self, kind: str, frm: str, to: str, reason: str = "", ms: float = 0.0,
                 task: str = "") -> dict:
        return self.event("fallback", level="WARN", task=task, tool=kind, method=frm,
                          to=to, reason=reason, ms=round(ms, 1), fallback=True)

    def task_end(self, ok: bool, ttc_ms: float, **fields: Any) -> dict:
        return self.event("task_end", level="INFO" if ok else "WARN", ok=ok,
                          ms=round(ttc_ms, 1), **fields)

    def info(self, message: str, **fields: Any) -> dict:
        return self.event("info", message=message, **fields)

    def warn(self, message: str, **fields: Any) -> dict:
        return self.event("warn", level="WARN", message=message, **fields)

    def error(self, message: str, **fields: Any) -> dict:
        return self.event("error", level="ERROR", message=message, **fields)

    # ---------------- чтение (UI) ----------------
    def tail(self, n: int = 100, kind: str = "") -> list[dict]:
        with self._lock:
            items = list(self._ring)
        if kind:
            items = [r for r in items if r.get("kind") == kind]
        return items[-n:]

    def human_line(self, rec: dict) -> str:
        """Одна строка «как в ТЗ»: время, запрос, маршрут, инструмент, время, результат."""
        parts = [rec.get("time", "")]
        if rec.get("task"):
            parts.append(f"User: {rec['task']}")
        if rec.get("route"):
            parts.append(f"Router: {rec['route']}")
        if rec.get("intents"):
            parts.append(f"Intent: {rec['intents']}")
        if rec.get("tool"):
            parts.append(f"Tool: {rec['tool']}")
        if rec.get("method"):
            parts.append(f"Method: {rec['method']}")
        if rec.get("ms"):
            parts.append(f"{rec['ms']:.0f}ms")
        if "ok" in rec:
            parts.append("SUCCESS" if rec["ok"] else "FAILED")
        if rec.get("fallback"):
            parts.append("FALLBACK")
        if rec.get("error"):
            parts.append(f"error={rec['error']}")
        return " | ".join(str(p) for p in parts if p != "")

    def human_tail(self, n: int = 50) -> list[str]:
        return [self.human_line(r) for r in self.tail(n)]

    def stats(self) -> dict:
        with self._lock:
            items = list(self._ring)
        by_kind: dict[str, int] = {}
        for r in items:
            by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + 1
        return {"path": str(self.path) if self.path else None, "level": self.level,
                "written": self.written, "buffer": len(items), "by_kind": by_kind}

    def clear_file(self) -> bool:
        if not self.path:
            return False
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
