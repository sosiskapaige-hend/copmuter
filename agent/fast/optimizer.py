"""Execution Optimizer — выбор самого дешёвого и надёжного способа (ТЗ §13, §50).

ТЗ задаёт лестницу приоритетов:

    Native WinAPI → Shell → известный exe → PowerShell → UI automation → Vision

Но лестница статична, а компьютер — нет: на одной машине Telegram стоит в Store
(тогда прямой exe не сработает), на другой его нет в PATH, а на третьей
пользователь поставил портативную версию. Поэтому оптимизатор не только хранит
лестницу, но и учится: помнит, какой способ реально сработал, сколько занял
времени и как часто падал. Следующий раз самый надёжный способ пробуется первым.

Метрики берутся из performance/metrics (метод `method_call`), поэтому данные
живут между запусками и попадают в бенчмарк (`benchmark()`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# Статический порядок способов (чем меньше число — тем «дешевле»/надёжнее).
# Соответствует лестнице из ТЗ: WinAPI → Shell → exe → PowerShell → UI → Vision.
METHOD_LADDER: dict[str, list[str]] = {
    "launch_app": ["cached_executable", "registered_app", "shortcut", "protocol",
                   "start_menu", "windows_search", "shell_start", "powershell_start",
                   "apps_folder", "ui_automation"],
    "open_url": ["native_shell", "registered_browser", "browser_cli", "browser_cdp",
                 "ui_automation"],
    "open_folder": ["native_shell", "explorer_exe", "powershell_start", "ui_automation"],
    "type_text": ["clipboard", "sendinput", "ui_automation"],
    "press_key": ["sendinput", "ui_automation"],
    "click": ["sendinput", "ui_automation"],
    "screenshot": ["mss", "winapi_gdi", "qt", "ui_automation"],
    "find_element": ["ui_automation", "window_api", "accessibility", "vision"],
    "read_screen": ["ui_automation", "accessibility", "ocr", "vision"],
    "file_op": ["native_python", "shell", "powershell"],
    "browser_search": ["direct_url", "address_bar", "ui_automation"],
    "process_kill": ["winapi", "taskkill", "psutil", "powershell"],
    "wallpaper": ["winapi_systemparametersinfo", "registry", "gnome", "mate", "kde"],
    "windows_settings": ["ms_settings_uri", "control_exe", "ui_automation"],
}

# Что принципиально не должно отдаваться зрению/модели (ТЗ §15).
NEVER_VISION: set[str] = {
    "ctrl+l", "ctrl+c", "ctrl+v", "alt+tab", "win+d", "ctrl+s", "ctrl+w",
    "clipboard", "clipboard_set", "clipboard_get", "window_focus", "alt+f4",
    "win+r", "ctrl+t", "ctrl+shift+t",
}

# Синонимы имён способов: подсистемы называют один и тот же способ по-разному
# (лаунчер — «shell», лестница — «shell_start»). Приводим к одному виду только
# для статистики; наружу имя всегда возвращается таким, каким его передали.
METHOD_ALIASES: dict[str, str] = {
    "shell": "shell_start", "powershell": "powershell_start",
    "cached_exe": "cached_executable", "registered": "registered_app",
    "protocol_handler": "protocol", "native": "native_shell",
    "direct": "direct_url", "browser": "registered_browser",
    "ms_settings": "ms_settings_uri", "winapi_set": "winapi_systemparametersinfo",
    "python": "native_python", "gdi": "winapi_gdi", "task_kill": "taskkill",
}


def _val(obj: Any, name: str, default: Any = None) -> Any:
    """Значение метрики: поддерживает свойства, методы и словари."""
    if obj is None:
        return default
    v = obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    return v() if callable(v) else v


def normalize_method(capability: str, method: str) -> str:
    """Каноническое имя способа для статистики (без учёта регистра)."""
    m = (method or "").strip().lower()
    return METHOD_ALIASES.get(m, m)


GROUPS: dict[str, str] = {
    "launch_app": "Приложения", "open_url": "Браузер", "browser_search": "Браузер",
    "open_folder": "Проводник", "type_text": "Ввод", "press_key": "Ввод",
    "click": "Ввод", "screenshot": "Экран", "find_element": "Экран",
    "read_screen": "Экран", "file_op": "Файлы", "process_kill": "Процессы",
    "wallpaper": "Обои", "windows_settings": "Настройки",
}


@dataclass
class MethodPlan:
    capability: str
    method: str
    score: float
    static_rank: int
    samples: int = 0
    success_rate: float = 0.0
    avg_ms: float = 0.0
    why: str = ""

    def to_dict(self) -> dict:
        return {"capability": self.capability, "method": self.method,
                "score": round(self.score, 3), "rank": self.static_rank,
                "samples": self.samples, "success_rate": round(self.success_rate, 3),
                "avg_ms": round(self.avg_ms, 1), "why": self.why}


@dataclass
class Attempt:
    capability: str
    method: str
    ok: bool
    ms: float
    ts: float = field(default_factory=time.time)
    detail: str = ""


class ExecutionOptimizer:
    """Считает, каким способом выполнять действие, и учится на результатах."""

    ADAPT_MIN_SAMPLES = 2        # после двух успехов способ поднимается наверх
    STALE_SECONDS = 7 * 24 * 3600

    def __init__(self, metrics: Any = None, log: Any = None, cfg: Any = None) -> None:
        self.metrics = metrics
        self.log = log
        self.cfg = cfg
        self._local: dict[tuple[str, str], dict] = {}     # (capability, method) → stats
        self._history: list[Attempt] = []

    # ---------------------------------------------------------------- ранжирование
    def static_rank(self, capability: str, method: str) -> int:
        ladder = METHOD_LADDER.get(capability, [])
        m = normalize_method(capability, method)
        return ladder.index(m) if m in ladder else len(ladder) + 3

    def rank(self, capability: str,
             available: Iterable[str] | None = None,
             minutes: float | None = None) -> list[MethodPlan]:
        methods = list(available) if available is not None else list(METHOD_LADDER.get(capability, []))
        plans: list[MethodPlan] = []
        for m in methods:
            plans.append(self._score(capability, m, minutes))
        plans.sort(key=lambda p: -p.score)
        return plans

    def _score(self, capability: str, method: str, minutes: float | None) -> MethodPlan:
        rank = self.static_rank(capability, method)
        norm = normalize_method(capability, method)
        # базовая оценка: чем выше в лестнице — тем лучше
        base = max(0.0, 1.0 - rank * 0.09)
        stat = None
        if self.metrics is not None:
            try:
                stat = self.metrics.method_stat(capability, norm)
            except Exception:
                stat = None
        samples = int(_val(stat, "calls", 0) or 0) if stat else 0
        sr = float(_val(stat, "success_rate", 0.0) or 0.0) if stat else 0.0
        avg = float(_val(stat, "avg_ms", 0.0) or 0.0) if stat else 0.0
        local = self._local.get((capability, norm))
        if local:
            samples = max(samples, int(local["n"]))
            sr = max(sr, local["ok"] / max(1, local["n"]))
            avg = avg or local["ms"] / max(1, local["n"])
        score = base
        why = f"лестница #{rank}"
        if samples >= self.ADAPT_MIN_SAMPLES:
            adapt = sr * 1.25 - min(0.25, avg / 60000.0)
            score = score * 0.45 + adapt * 0.55
            why = f"опыт: {sr * 100:.0f}% успеха, {avg:.0f} мс"
            if sr <= 0.3:
                score *= 0.6
                why += " (проблемный способ)"
        return MethodPlan(capability, method, score, rank, samples, sr, avg, why)

    def best(self, capability: str, available: Iterable[str] | None = None) -> str | None:
        plans = self.rank(capability, available)
        return plans[0].method if plans else None

    def order(self, capability: str, methods: Iterable[str]) -> list[str]:
        """Упорядочить конкретный список способов по опыту (для fallback-цепочки)."""
        ms = list(methods)
        return [p.method for p in self.rank(capability, ms)]

    def rank_methods(self, capability: str, methods: Iterable[str]) -> list[str]:
        """Алиас `order` — так его вызывает AppLauncher."""
        return self.order(capability, methods)

    # ---------------------------------------------------------------- обучение
    def note(self, capability: str, method: str, ok: bool, ms: float, detail: str = "") -> None:
        key = (capability, normalize_method(capability, method))
        item = self._local.setdefault(key, {"n": 0, "ok": 0, "ms": 0.0, "last_ok": 0.0})
        item["n"] += 1
        item["ok"] += 1 if ok else 0
        item["ms"] += ms
        if ok:
            item["last_ok"] = time.time()
        self._history.append(Attempt(capability, method, ok, ms, detail=detail))
        if len(self._history) > 400:
            del self._history[:200]
        if self.metrics is not None:
            try:
                self.metrics.method_call(capability, method, ms, ok)
                if not ok:
                    self.metrics.fallback(capability, method)
            except Exception:
                pass
        if self.log is not None:
            try:
                self.log.event("method", capability=capability, method=method,
                               ok=ok, ms=round(ms, 1), detail=detail[:120])
            except Exception:
                pass

    # ---------------------------------------------------------------- отчёты
    def notes(self) -> dict:
        out = {}
        for (cap, method), st in self._local.items():
            n = max(1, st["n"])
            out[f"{cap}:{method}"] = {"calls": st["n"], "success_rate": round(st["ok"] / n, 3),
                                      "avg_ms": round(st["ms"] / n, 1)}
        return out

    def benchmark(self, capability: str | None = None) -> list[dict]:
        """Таблица «способ → среднее время / успешность» для UI и отладки."""
        rows: list[dict] = []
        caps = [capability] if capability else list(METHOD_LADDER)
        for cap in caps:
            for plan in self.rank(cap):
                if plan.samples == 0 or (capability and plan.capability == cap):
                    rows.append(plan.to_dict())
        rows = [r for r in rows if r["samples"] > 0]
        rows.sort(key=lambda r: (r["capability"], -r["score"]))
        return rows

    def explain(self, capability: str) -> str:
        lines = [f"Как выполняется «{capability}»:"]
        for p in self.rank(capability):
            mark = "★" if p.method == self.best(capability) else " "
            lines.append(f" {mark} {p.method:24} score={p.score:.2f}  {p.why}")
        return "\n".join(lines)

    @staticmethod
    def forbidden_for_vision(action: str) -> bool:
        """Действия, которые НИКОГДА не выполняются через зрение (ТЗ §15)."""
        return (action or "").strip().lower() in NEVER_VISION

    def capabilities(self) -> dict:
        return {"ladders": len(METHOD_LADDER), "learned_methods": len(self._local),
                "history": len(self._history),
                "never_vision": sorted(NEVER_VISION)[:8]}


__all__ = ["ExecutionOptimizer", "MethodPlan", "Attempt", "METHOD_LADDER",
           "NEVER_VISION", "GROUPS"]
