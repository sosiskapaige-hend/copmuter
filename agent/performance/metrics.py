"""Метрики производительности (ТЗ §28, §44, §11 приложения к ТЗ).

Главная метрика — Time To Completion (сколько прошло от команды пользователя
до результата). Кроме неё считаем:

  * LLM latency и число вызовов модели (главный ресурс скорости);
  * latency и успешность каждого инструмента (чтобы выбирать быстрый путь);
  * статистику по методам выполнения (cached_executable / shell / vision …) —
    на ней работает адаптивная маршрутизация (ExecutionOptimizer);
  * fallback-и, vision-вызовы, разбивку по маршрутам (fast_direct / agent …).

Метрики живут в памяти и периодически сбрасываются в
`AGENT_HOME/state/metrics.json`, поэтому «агент со временем становится
быстрее» — не лозунг, а факт: следующая задача стартует с накопленной
статистикой.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

MAX_TASK_HISTORY = 200
MAX_METHOD_KEYS = 400


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


@dataclass
class Stat:
    """Счётчик с временами выполнения (мс)."""
    calls: int = 0
    ok: int = 0
    failed: int = 0
    timeouts: int = 0
    fallbacks: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0
    samples: list[float] = field(default_factory=list)

    def record(self, ms: float, ok: bool, timeout: bool = False) -> None:
        self.calls += 1
        self.total_ms += ms
        self.last_ms = ms
        self.max_ms = max(self.max_ms, ms)
        if ok:
            self.ok += 1
        elif timeout:
            self.timeouts += 1
            self.failed += 1
        else:
            self.failed += 1
        self.samples.append(round(ms, 1))
        if len(self.samples) > 100:
            del self.samples[:-100]

    @property
    def avg_ms(self) -> float:
        return round(self.total_ms / self.calls, 1) if self.calls else 0.0

    @property
    def p90_ms(self) -> float:
        return round(_pct(self.samples, 0.9), 1)

    @property
    def success_rate(self) -> float:
        return round(self.ok / self.calls, 4) if self.calls else 0.0

    def to_dict(self) -> dict:
        return {"calls": self.calls, "ok": self.ok, "failed": self.failed,
                "timeouts": self.timeouts, "fallbacks": self.fallbacks,
                "avg_ms": self.avg_ms, "p90_ms": self.p90_ms, "max_ms": round(self.max_ms, 1),
                "success_rate": self.success_rate}

    @classmethod
    def from_dict(cls, d: dict) -> "Stat":
        s = cls()
        s.calls = int(d.get("calls", 0))
        s.ok = int(d.get("ok", 0))
        s.failed = int(d.get("failed", 0))
        s.timeouts = int(d.get("timeouts", 0))
        s.fallbacks = int(d.get("fallbacks", 0))
        s.max_ms = float(d.get("max_ms", 0.0))
        avg = float(d.get("avg_ms", 0.0) or 0.0)
        s.total_ms = avg * s.calls
        s.last_ms = avg
        return s


@dataclass
class TaskMetric:
    """Одна выполненная задача — для Time To Completion и разбора полётов."""
    ts: float
    route: str
    goal: str
    ttc_ms: float
    route_ms: float = 0.0
    llm_calls: int = 0
    llm_ms: float = 0.0
    tool_calls: int = 0
    vision_calls: int = 0
    fallbacks: int = 0
    ok: bool = True
    steps: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["goal"] = (self.goal or "")[:160]
        return d


class Metrics:
    """Потокобезопасный сборщик метрик."""

    def __init__(self, path: Path | str | None = None, autosave_delay: float = 2.0) -> None:
        self.path = Path(path) if path else None
        self.autosave_delay = autosave_delay
        self._lock = threading.RLock()
        self.started = time.time()
        self.counters: dict[str, int] = {}
        self.tools: dict[str, Stat] = {}
        self.methods: dict[str, Stat] = {}          # "launch_app:cached_executable"
        self.routes: dict[str, Stat] = {}           # route kind
        self.llm: Stat = Stat()
        self.vision: Stat = Stat()
        self.tasks: list[TaskMetric] = []
        self._last_save = 0.0
        self._dirty = False
        # контекст текущей задачи (для ttc)
        self._cur: dict[str, Any] = {}
        if self.path and self.path.exists():
            self.load()

    # ---------------- счётчики ----------------
    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n
            self._dirty = True

    def count(self, name: str) -> int:
        with self._lock:
            return self.counters.get(name, 0)

    # ---------------- задачи ----------------
    def task_start(self, route: str, goal: str, t0: float | None = None) -> float:
        """Возвращает t0 (монотонное время) — для Time To Completion.

        `t0` можно передать снаружи, если отсчёт начался раньше (например, до
        попытки быстрого пути).
        """
        t0 = float(t0) if t0 else time.perf_counter()
        start = time.time()
        with self._lock:
            self._cur = {"route": route, "goal": goal, "t0": t0, "start": start,
                         "llm_calls": 0, "llm_ms": 0.0, "tool_calls": 0, "vision": 0,
                         "fallbacks": 0, "steps": 0}
            self.counters["tasks_started"] = self.counters.get("tasks_started", 0) + 1
        return t0

    def task_note(self, **kw: Any) -> None:
        with self._lock:
            if self._cur:
                for k, v in kw.items():
                    if isinstance(v, (int, float)) and isinstance(self._cur.get(k), (int, float)):
                        self._cur[k] = self._cur[k] + v
                    else:
                        self._cur[k] = v

    def task_end(self, ok: bool = True, note: str = "") -> dict:
        with self._lock:
            cur = dict(self._cur)
            self._cur = {}
            if not cur:
                return {}
            ttc = (time.perf_counter() - float(cur.get("t0") or time.perf_counter())) * 1000
            m = TaskMetric(ts=time.time(), route=str(cur.get("route") or "?"),
                           goal=str(cur.get("goal") or ""), ttc_ms=round(ttc, 1),
                           llm_calls=int(cur.get("llm_calls") or 0),
                           llm_ms=round(float(cur.get("llm_ms") or 0.0), 1),
                           tool_calls=int(cur.get("tool_calls") or 0),
                           vision_calls=int(cur.get("vision") or 0),
                           fallbacks=int(cur.get("fallbacks") or 0),
                           ok=ok, steps=int(cur.get("steps") or 0), note=note[:200])
            self.tasks.append(m)
            if len(self.tasks) > MAX_TASK_HISTORY:
                del self.tasks[:-MAX_TASK_HISTORY]
            self.counters["tasks_done" if ok else "tasks_failed"] = \
                self.counters.get("tasks_done" if ok else "tasks_failed", 0) + 1
            self._dirty = True
        self.maybe_save()
        return m.to_dict()

    # ---------------- инструменты / методы ----------------
    def tool_call(self, name: str, ms: float, ok: bool, timeout: bool = False) -> None:
        with self._lock:
            st = self.tools.setdefault(name, Stat())
            st.record(ms, ok, timeout)
            self.counters["tool_calls"] = self.counters.get("tool_calls", 0) + 1
            if self._cur:
                self._cur["tool_calls"] = int(self._cur.get("tool_calls") or 0) + 1
                self._cur["steps"] = int(self._cur.get("steps") or 0) + 1
            self._dirty = True

    def method_call(self, kind: str, method: str, ms: float, ok: bool) -> None:
        key = f"{kind}:{method}"
        with self._lock:
            if len(self.methods) >= MAX_METHOD_KEYS and key not in self.methods:
                return
            st = self.methods.setdefault(key, Stat())
            st.record(ms, ok)
            self._dirty = True

    def method_stat(self, kind: str, method: str) -> Stat | None:
        with self._lock:
            return self.methods.get(f"{kind}:{method}")

    def fallback(self, kind: str, method: str = "") -> None:
        with self._lock:
            self.counters["fallbacks"] = self.counters.get("fallbacks", 0) + 1
            if self._cur:
                self._cur["fallbacks"] = int(self._cur.get("fallbacks") or 0) + 1
            if method:
                st = self.methods.setdefault(f"{kind}:{method}", Stat())
                st.fallbacks += 1
            self._dirty = True

    def route_stat(self, route: str, ms: float, ok: bool) -> None:
        with self._lock:
            self.routes.setdefault(route, Stat()).record(ms, ok)
            self._dirty = True

    # ---------------- LLM / vision ----------------
    def llm_call(self, ms: float, ok: bool, kind: str = "chat") -> None:
        with self._lock:
            self.llm.record(ms, ok)
            self.counters["llm_calls"] = self.counters.get("llm_calls", 0) + 1
            self.counters[f"llm_calls_{kind}"] = self.counters.get(f"llm_calls_{kind}", 0) + 1
            if self._cur:
                self._cur["llm_calls"] = int(self._cur.get("llm_calls") or 0) + 1
                self._cur["llm_ms"] = float(self._cur.get("llm_ms") or 0.0) + ms
            self._dirty = True

    def vision_call(self, ms: float, ok: bool) -> None:
        with self._lock:
            self.vision.record(ms, ok)
            self.counters["vision_calls"] = self.counters.get("vision_calls", 0) + 1
            if self._cur:
                self._cur["vision"] = int(self._cur.get("vision") or 0) + 1
            self._dirty = True

    # ---------------- сохранение ----------------
    def maybe_save(self, force: bool = False) -> None:
        if not self.path:
            return
        now = time.time()
        with self._lock:
            if not self._dirty and not force:
                return
            if not force and (now - self._last_save) < self.autosave_delay:
                return
            self._last_save = now
            self._dirty = False
            payload = {
                "saved": now,
                "started": self.started,
                "counters": dict(self.counters),
                "tools": {k: v.to_dict() for k, v in self.tools.items()},
                "methods": {k: v.to_dict() for k, v in self.methods.items()},
                "routes": {k: v.to_dict() for k, v in self.routes.items()},
                "llm": self.llm.to_dict(),
                "vision": self.vision.to_dict(),
                "tasks": [t.to_dict() for t in self.tasks[-60:]],
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def load(self) -> None:
        assert self.path is not None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self.counters = {k: int(v) for k, v in (data.get("counters") or {}).items()}
            for k, v in (data.get("tools") or {}).items():
                self.tools[k] = Stat.from_dict(v)
            for k, v in (data.get("methods") or {}).items():
                self.methods[k] = Stat.from_dict(v)
            for k, v in (data.get("routes") or {}).items():
                self.routes[k] = Stat.from_dict(v)
            self.llm = Stat.from_dict(data.get("llm") or {})
            self.vision = Stat.from_dict(data.get("vision") or {})
            self.tasks = [TaskMetric(**{**t, "ts": float(t.get("ts", 0))})
                          for t in (data.get("tasks") or []) if isinstance(t, dict)]
            self.started = float(data.get("started") or self.started)

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.tools.clear()
            self.methods.clear()
            self.routes.clear()
            self.tasks.clear()
            self.llm = Stat()
            self.vision = Stat()
            self._dirty = True
        self.maybe_save(force=True)

    # ---------------- сводка ----------------
    def summary(self, top: int = 12) -> dict:
        with self._lock:
            tools = sorted(self.tools.items(), key=lambda kv: -kv[1].calls)
            methods = sorted(self.methods.items(), key=lambda kv: -kv[1].calls)
            routes = {k: v.to_dict() for k, v in self.routes.items()}
            counters = dict(self.counters)
            tasks = [t.to_dict() for t in self.tasks[-20:]]
            llm, vision = self.llm.to_dict(), self.vision.to_dict()
        done = [t for t in self.tasks if t.ok]
        ttc = [t.ttc_ms for t in self.tasks]
        fast = [t.ttc_ms for t in self.tasks if t.route == "fast_direct"]
        return {
            "counters": counters,
            "llm": llm,
            "vision": vision,
            "routes": routes,
            "tools": {k: v.to_dict() for k, v in tools[:top]},
            "methods": {k: v.to_dict() for k, v in methods[:top]},
            "tasks": tasks,
            "ttc": {
                "tasks": len(self.tasks),
                "success_rate": round(len(done) / len(self.tasks), 3) if self.tasks else 0.0,
                "avg_ms": round(sum(ttc) / len(ttc), 1) if ttc else 0.0,
                "p90_ms": round(_pct(ttc, 0.9), 1),
                "fast_path_avg_ms": round(sum(fast) / len(fast), 1) if fast else 0.0,
                "llm_calls_total": counters.get("llm_calls", 0),
                "tool_calls_total": counters.get("tool_calls", 0),
                "fallbacks_total": counters.get("fallbacks", 0),
            },
        }
