"""Базовые типы системы инструментов.

Инструмент — именованная функция с JSON-schema параметров, уровнем риска
и необязательной оценкой динамического риска (например: «удаление 50 файлов»
рискующе выше, чем «удаление одного»).
"""
from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Risk(IntEnum):
    """Уровни риска действия (для системы безопасности)."""
    NONE = 0        # чтение, информация
    LOW = 1         # создание файлов/папок, открытие приложений
    MEDIUM = 2      # изменение содержимого, удаление 1 файла, установка пакетов
    HIGH = 3        # массовые операции, отправка сообщений, системные настройки
    CRITICAL = 4    # необратимые/финансовые/критично-системные


RISK_NAMES = {Risk.NONE: "none", Risk.LOW: "low", Risk.MEDIUM: "medium",
              Risk.HIGH: "high", Risk.CRITICAL: "critical"}
NAME_TO_RISK = {v: k for k, v in RISK_NAMES.items()}


@dataclass
class ToolResult:
    ok: bool
    output: str = ""                      # текст для LLM и UI
    data: dict = field(default_factory=dict)  # структурированные данные
    error: str = ""

    @classmethod
    def ok_result(cls, output: str, **data: Any) -> "ToolResult":
        return cls(ok=True, output=output, data=data)

    @classmethod
    def fail(cls, error: str, **data: Any) -> "ToolResult":
        return cls(ok=False, error=error, output=f"ОШИБКА: {error}", data=data)

    def for_llm(self, max_chars: int = 3000) -> str:
        s = self.output if self.ok else (f"ОШИБКА: {self.error}")
        if len(s) > max_chars:
            s = s[:max_chars] + f"\n... [обрезано, всего {len(self.output)} символов]"
        return s


@dataclass
class ToolContext:
    """Всё, что инструменты могут использовать во время выполнения."""
    cfg: Any
    bus: Any
    gateway: Any
    journal: Any
    memory: Any
    platform: Any
    llm: Any
    workdir: str = "."
    session_note: str = ""

    def screenshot_cache(self) -> str | None:
        return getattr(self, "_last_screenshot", None)

    def set_screenshot_cache(self, path: str) -> None:
        self._last_screenshot = path


class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    risk: Risk = Risk.LOW
    is_gui: bool = False        # требует дисплей/ввод → сериализуется GUI-замком
    category: str = "misc"

    @abc.abstractmethod
    async def execute(self, ctx: ToolContext, **args: Any) -> ToolResult: ...

    def estimate_risk(self, ctx: ToolContext, args: dict) -> tuple[Risk, str]:
        """Динамическая оценка риска по конкретным аргументам.

        По умолчанию — базовый риск инструмента. Инструменты с опасными
        массовыми операциями переопределяют (fs_delete, terminal_run, ...).
        """
        return self.risk, ""

    def to_schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe_brief(self) -> str:
        return f"{self.name}: {self.description}"

    def __repr__(self) -> str:
        return f"<Tool {self.name} risk={RISK_NAMES[self.risk]}>"


def _prop(t: str, desc: str, **extra: Any) -> dict:
    p = {"type": t, "description": desc}
    p.update(extra)
    return p
