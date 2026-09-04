"""Политика безопасности: классификация риска и режимы работы.

Режимы (mode):
- auto       — без вопросов, кроме CRITICAL;
- confirm    — подтверждение от MEDIUM (по умолчанию);
- step       — подтверждение КАЖДОГО действия;
- observe    — как auto, но UI показывает всё вживую (визуальные эффекты);
- plan_only  — только план, ничего не выполняется.

Уровень риска = max(базовый риск инструмента, динамическая оценка по аргументам).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..tools.base import Risk, RISK_NAMES, ToolContext, Tool

_MODES = {"auto", "confirm", "step", "observe", "plan_only"}


@dataclass
class RiskDecision:
    risk: Risk
    needs_confirm: bool
    reason: str = ""


class SafetyPolicy:
    def __init__(self, confirm_from: str = "medium", bulk_delete_threshold: int = 10) -> None:
        self.confirm_from = _parse(confirm_from)
        self.bulk_delete_threshold = bulk_delete_threshold

    @property
    def confirm_threshold(self) -> Risk:
        return self.confirm_from

    def decide(self, mode: str, tool: Tool, args: dict, ctx: ToolContext | None = None) -> RiskDecision:
        if mode == "plan_only":
            return RiskDecision(tool.risk, needs_confirm=False, reason="plan_only: выполнение отключено")
        risk, reason = tool.risk, ""
        try:
            dyn = tool.estimate_risk(ctx, args) if ctx else (tool.risk, "")
            if isinstance(dyn, tuple) and len(dyn) == 2:
                risk, reason = max(risk, dyn[0]), dyn[1] or reason
        except Exception:
            pass
        # эскалация массовых операций
        if _is_bulk(args, self.bulk_delete_threshold):
            risk = max(risk, Risk.HIGH)
            reason = reason or f"массовая операция (>={self.bulk_delete_threshold} элементов)"
        needs = self._needs_confirm(mode, risk, reason)
        return RiskDecision(risk, needs, reason)

    def _needs_confirm(self, mode: str, risk: Risk, reason: str = "") -> bool:
        bulk = "массовая" in reason
        if mode == "step":
            return True
        if mode in ("auto", "observe"):
            # «сделай всё сам»: подтверждение — для критичного и массовых операций
            return risk >= Risk.CRITICAL or (bulk and risk >= Risk.HIGH)
        # confirm
        return risk >= self.confirm_threshold

    def describe(self) -> str:
        return (f"Режим: (см. config), подтверждение с уровня "
                f"{RISK_NAMES[self.confirm_from]}, порог массовых операций: {self.bulk_delete_threshold}")


def _parse(name: str) -> Risk:
    mapping = {"none": Risk.NONE, "low": Risk.LOW, "medium": Risk.MEDIUM,
               "high": Risk.HIGH, "critical": Risk.CRITICAL}
    return mapping.get(name, Risk.MEDIUM)


def _is_bulk(args: dict, threshold: int) -> bool:
    for k, v in (args or {}).items():
        if isinstance(v, (list, tuple)) and len(v) >= threshold:
            return True
        if isinstance(v, str) and any(c in v for c in "*?[") and k in ("path", "pattern", "paths"):
            return True
        if isinstance(v, str) and v.strip().lower().startswith(("rm -rf /", "format ", "mkfs", "del /s", "rmdir /s /q")):
            return True
    return False


NAMES = {Risk.NONE: "none", Risk.LOW: "low", Risk.MEDIUM: "medium",
         Risk.HIGH: "high", Risk.CRITICAL: "critical"}


def validate_mode(mode: str) -> str:
    m = (mode or "confirm").lower()
    return m if m in _MODES else "confirm"


MODES = _MODES
