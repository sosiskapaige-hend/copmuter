"""Планировщик: превращает цель в список шагов (через LLM.plan)."""
from __future__ import annotations

from ..llm.base import LLM, Plan


class Planner:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    async def make_plan(self, goal: str, context: str, tools_hint: str,
                        error_summary: str = "") -> Plan:
        if error_summary:
            goal = (f"{goal}\n\n(Перепланирование. Что пошло не так ранее: "
                    f"{error_summary[:1500]})")
        return await self.llm.plan(goal, context, tools_hint)
