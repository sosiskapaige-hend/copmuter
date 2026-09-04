from __future__ import annotations

from ..config import Config
from .base import LLM, Plan, PlanStep, ToolCall, Decision, Assessment
from .openai_client import OpenAICompatibleLLM, LLMError
from .mock import MockLLM


def create_llm(cfg: Config) -> LLM:
    if cfg.llm.provider == "mock":
        return MockLLM()
    return OpenAICompatibleLLM(
        base_url=cfg.llm.base_url,
        api_key=cfg.llm.api_key,
        model=cfg.llm.model,
        max_tokens=cfg.llm.max_tokens,
        temperature=cfg.llm.temperature,
        supports_tool_calling=cfg.llm.supports_tool_calling,
        vision=cfg.llm.vision,
    )

__all__ = ["LLM", "Plan", "PlanStep", "ToolCall", "Decision", "Assessment",
           "OpenAICompatibleLLM", "MockLLM", "LLMError", "create_llm"]
