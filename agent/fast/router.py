"""Fast Router — развилка «простое / сложное» до обращения к модели (ТЗ §5, §6).

Архитектура из ТЗ:

    ПОЛЬЗОВАТЕЛЬ → FAST ROUTER → ┬ ПРОСТОЕ  → DIRECT TOOLS → FAST EXECUTION
                                  └ СЛОЖНОЕ  → QWEN3-VL → AGENT LOOP

Роутер отвечает на три вопроса:
  1. Понял ли движок намерений команду детерминированно?
  2. Есть ли для неё готовый обработчик инструментов?
  3. Не выглядит ли задача многошаговой/творческой (тогда нужна модель)?

Если все ответы «да/есть/нет» — модель не вызывается вообще.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .intent import IntentEngine, is_complex

# Виды маршрутов.
DIRECT = "direct"          # детерминированный вызов инструментов
VISION = "vision"          # нужен снимок экрана и модель-зрение
LLM_TEXT = "llm_text"      # нужен один вызов модели для текста/кода
CHAT = "chat"              # обычный вопрос — быстрый ответ без агентного цикла
AGENT = "agent"             # полноценный агентный цикл с планированием

FAST_KINDS = (DIRECT, VISION, LLM_TEXT, CHAT)


@dataclass
class Route:
    kind: str
    intent: Any = None
    reason: str = ""
    confidence: float = 0.0

    @property
    def is_fast(self) -> bool:
        return self.kind in FAST_KINDS

    @property
    def needs_llm(self) -> bool:
        return self.kind in (LLM_TEXT, CHAT, AGENT)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "reason": self.reason,
                "confidence": round(self.confidence, 3),
                "intent": getattr(self.intent, "name", ""),
                "slots": dict(getattr(self.intent, "slots", {}) or {}),
                "label": self.intent.label() if hasattr(self.intent, "label") else ""}


class FastRouter:
    def __init__(self, engine: IntentEngine | None = None, executor: Any = None,
                 cfg: Any = None, metrics: Any = None, log: Any = None,
                 platform: Any = None) -> None:
        self.engine = engine or IntentEngine()
        self.executor = executor
        self.cfg = cfg
        self.metrics = metrics
        self.log = log
        self.platform = platform
        self.stats = {"routed": 0, "direct": 0, "vision": 0, "llm_text": 0,
                      "chat": 0, "agent": 0, "complex": 0}

    # ------------------------------------------------------------------ маршрут
    def route(self, text: str, mode: str = "auto") -> Route:
        self.stats["routed"] += 1
        intent = self.engine.parse(text)
        route = self._decide(text or "", intent)
        self.stats[route.kind] = self.stats.get(route.kind, 0) + 1
        if self.metrics is not None:
            try:
                self.metrics.inc(f"route_{route.kind}")
            except Exception:
                pass
        if self.log is not None:
            try:
                self.log.event("route", kind=route.kind, intent=intent.name,
                               confidence=round(route.confidence, 3), reason=route.reason)
            except Exception:
                pass
        return route

    def _decide(self, text: str, intent: Any) -> Route:
        name = getattr(intent, "name", "")
        # 1) модель сама должна разбираться: явный провал распознавания
        if name == "agent_task":
            return Route(AGENT, intent, "движок намерений не распознал команду", 0.3)
        # 2) болтовня/вопрос — быстрый ответ без планирования
        if name == "chat":
            return Route(CHAT, intent, "вопрос/беседа", intent.confidence)
        # 3) задачи, требующие кода: один вызов генерации, дальше — инструменты
        if name == "code_task":
            return Route(LLM_TEXT, intent, "нужна генерация кода (один вызов модели)",
                         intent.confidence)
        # 4) интерфейс, которого мы не знаем: снимок экрана + зрение
        if name == "click_element":
            if self._vision_available():
                return Route(VISION, intent, "поиск элемента интерфейса через зрение",
                             intent.confidence)
            return Route(AGENT, intent, "зрение недоступно", intent.confidence)
        # 5) есть ли обработчик у быстрого исполнителя
        # «compound» обрабатывается самим исполнителем (у него нет метода _do_*)
        if self.executor is not None and name != "compound" \
                and not hasattr(self.executor, f"_do_{name}"):
            return Route(AGENT, intent, f"нет быстрого обработчика для «{name}»",
                         intent.confidence)
        # 6) многошаговость/творчество — отдаём LLM (ТЗ §5: «сложные задачи — модели»)
        if is_complex(text):
            self.stats["complex"] += 1
            return Route(AGENT, intent, "задача похожа на многошаговую/творческую",
                         intent.confidence)
        # 7) составная команда из простых частей — тоже быстрый путь
        if name == "compound":
            uncertain = [p for p in intent.parts if p.name in ("agent_task", "chat")]
            if uncertain:
                return Route(AGENT, intent, "в составной команде есть непонятая часть",
                             intent.confidence)
            return Route(DIRECT, intent, "составная команда из простых действий",
                         intent.confidence)
        return Route(DIRECT, intent, "детерминированный обработчик", intent.confidence)

    def _vision_available(self) -> bool:
        if self.executor is None:
            return False
        if getattr(self.executor, "vision", None) is None:
            return False
        if getattr(self.executor, "screens", None) is None:
            return False
        return bool(getattr(self.platform, "has_display", True))

    def summary(self) -> dict:
        total = max(1, self.stats.get("routed", 0))
        return {**self.stats,
                "fast_share": round((self.stats.get("direct", 0) + self.stats.get("vision", 0)
                                     + self.stats.get("llm_text", 0)) / total, 3)}


__all__ = ["FastRouter", "Route", "DIRECT", "VISION", "LLM_TEXT", "CHAT", "AGENT",
           "FAST_KINDS"]
