"""Мета-инструменты: вопросы пользователю, ожидание, undo, уведомления,
финиш задачи. Они замыкают агентный цикл."""
from __future__ import annotations

import asyncio

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


def register_meta_tools(reg: ToolRegistry) -> None:

    @reg.tool("ask_user",
              "Запросить у пользователя информацию или решение (данные, выбор, код 2FA). "
              "Агент ПАУЗИРУЕТСЯ до ответа. Используйте только когда без ответа дальше нельзя.",
              risk=Risk.NONE, category="meta",
              parameters={"type": "object", "properties": {
                  "question": _prop("string", "Конкретный вопрос"),
                  "options": _prop("array", "Варианты ответа (опционально)"),
                  "timeout": _prop("number", "Таймаут секунд (по умолчанию 600)")},
                  "required": ["question"]})
    class AskUser(Tool):
        async def execute(self, ctx: ToolContext, question: str, options: list | None = None,
                          timeout: float = 600) -> ToolResult:
            answer = await ctx.gateway.ask(question, options=options, timeout=timeout)
            return ToolResult.ok_result(f"Ответ пользователя: {answer}")

    @reg.tool("wait", "Подождать N секунд (для фоновых процессов, загрузок, анимаций).",
              risk=Risk.NONE, category="meta",
              parameters={"type": "object", "properties": {
                  "seconds": _prop("number", "Сколько секунд (макс 120 за раз)")}, "required": ["seconds"]})
    class Wait(Tool):
        async def execute(self, ctx: ToolContext, seconds: float) -> ToolResult:
            s = max(0.1, min(float(seconds), 120))
            await asyncio.sleep(s)
            return ToolResult.ok_result(f"Подождено {s:.1f}с")

    @reg.tool("undo_last",
              "Отменить последнее изменяющее действие (undo из журнала). "
              "Напр. «верни как было», «отмени последнее действие».",
              risk=Risk.MEDIUM, category="meta",
              parameters={"type": "object", "properties": {
                  "steps": _prop("integer", "Сколько последних действий отменить (по умолчанию 1)")},
                  "required": []})
    class UndoLast(Tool):
        async def execute(self, ctx: ToolContext, steps: int = 1) -> ToolResult:
            if ctx.journal is None:
                return ToolResult.fail("журнал недоступен")
            results = []
            for _ in range(max(1, min(steps, 5))):
                entry = ctx.journal.pop_last_undoable()
                if entry is None:
                    if not results:
                        return ToolResult.fail("нет действий с undo в журнале")
                    break
                undo = entry.get("undo") or {}
                ut, args = undo.get("tool"), undo.get("args") or {}
                res = await reg.call(ut, args, ctx)
                results.append(f"{entry.get('tool')}: {'отменено' if res.ok else 'ОШИБКА: ' + res.error[:120]}")
            return ToolResult.ok_result("Undo: " + " | ".join(results))

    @reg.tool("notify_user",
              "Показать пользователю промежуточное/финальное сообщение (без завершения задачи).",
              risk=Risk.NONE, category="meta",
              parameters={"type": "object", "properties": {
                  "message": _prop("string", "Сообщение")}, "required": ["message"]})
    class NotifyUser(Tool):
        async def execute(self, ctx: ToolContext, message: str) -> ToolResult:
            ctx.bus.emit("log", level="user", message=message)
            return ToolResult.ok_result("Сообщение показано пользователю.")

    @reg.tool("finish_task",
              "ЗАВЕРШИТЬ задачу с итоговым ответом. Вызывается ТОЛЬКО когда цель достигнута "
              "и ФАКТИЧЕСКИ проверена (не «последний шаг выполнен», а «результат существует»).",
              risk=Risk.NONE, category="meta",
              parameters={"type": "object", "properties": {
                  "summary": _prop("string", "Итог для пользователя (кратко, что сделано и где результат)"),
                  "details": _prop("string", "Технические детали (опционально)")},
                  "required": ["summary"]})
    class FinishTask(Tool):
        async def execute(self, ctx: ToolContext, summary: str, details: str = "") -> ToolResult:
            return ToolResult.ok_result(summary, _finish=True, details=details)
