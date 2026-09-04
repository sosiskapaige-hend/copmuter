"""Реестр инструментов.

Регистрация декоратором @register_tool. GUI-инструменты (мышь, окна,
скриншоты) сериализуются через общий GUI-lock — параллельные задачи не
могут «сорвать» физический ввод, но несерийные инструменты (fs, terminal)
выполняются параллельно.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, RISK_NAMES


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._gui_lock = asyncio.Lock()
        self.execution_log: list[dict] = []

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool.name не задан")
        self._tools[tool.name] = tool

    def tool(self, name: str, description: str, risk: Risk = Risk.LOW,
             is_gui: bool = False, category: str = "misc",
             parameters: dict | None = None, estimate_risk=None):
        def deco(cls):
            cls.name = name
            cls.description = description
            cls.risk = risk
            cls.is_gui = is_gui
            cls.category = category
            if parameters is not None:
                cls.parameters = parameters
            if estimate_risk is not None:
                cls.estimate_risk = estimate_risk
            self.register(cls())
            return cls
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def schemas(self) -> list[dict]:
        return [t.to_schema() for t in self.all()]

    def brief_list(self) -> str:
        lines = []
        for t in self.all():
            lines.append(f"- {t.name} [{RISK_NAMES[t.risk]}]: {t.description}")
        return "\n".join(lines)

    async def _call(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult:
        args = {k: v for k, v in (args or {}).items()}
        try:
            cor = tool.execute(ctx, **args)
            if tool.is_gui:
                async with self._gui_lock:
                    result = await cor
            else:
                result = await cor
        except TypeError as e:
            result = ToolResult.fail(f"неверные аргументы: {e}")
        except Exception as e:  # noqa: BLE001 — инструмент не должен ронять агента
            result = ToolResult.fail(f"{type(e).__name__}: {e}")
        self.execution_log.append({
            "name": tool.name, "args": args, "ok": result.ok,
            "ts": time.time(), "error": result.error,
        })
        if len(self.execution_log) > 500:
            self.execution_log = self.execution_log[-500:]
        return result

    async def call(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail(f"инструмент '{name}' не существует. "
                                   f"Доступны: {', '.join(self.names())}")
        return await self._call(tool, args, ctx)
