"""Интерфейс LLM-провайдера.

Агент общается с LLM только через эти 6 методов — так любые провайдеры
(OpenAI, Groq, OpenRouter, Ollama, LM Studio, DeepSeek, ...) подключаются
единообразно.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PlanStep:
    title: str
    detail: str = ""


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    summary: str = ""
    needs_user: str = ""     # вопрос пользователю, если задача неопределена


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class Decision:
    """Ответ LLM на «что делать дальше».

    Rовно один из: tool_call (вызвать инструмент), final (задача завершена),
    ask (нужен ответ пользователя).
    """
    thought: str = ""
    tool_call: ToolCall | None = None
    final: str | None = None
    ask: str | None = None
    step_done: str | None = None   # id завершённого шага плана (если есть)


@dataclass
class Assessment:
    progress: int = 0            # 0..100
    achieved: bool = False
    remaining: list[str] = field(default_factory=list)
    message: str = ""
    adjust_plan: bool = False    # LLM рекомендует пересобрать план
    error_analysis: str = ""     # анализ последней ошибки (если была)


class LLM(Protocol):
    name: str

    async def plan(self, goal: str, context: str, tools_hint: str) -> Plan: ...

    async def next_action(self, goal: str, context: str,
                          history: list[dict], tools: list[dict]) -> Decision: ...

    async def assess(self, goal: str, done_summary: str, last_error: str | None) -> Assessment: ...

    async def answer(self, messages: list[dict]) -> str: ...

    async def describe_image(self, image_b64: str, prompt: str) -> str: ...

    # Опционально (чат-режим): потоковая генерация и список моделей сервера.
    def chat_stream(self, messages: list[dict], temperature: float | None = None): ...
    def list_models(self) -> list[str]: ...
