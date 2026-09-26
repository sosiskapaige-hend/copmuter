"""Обнаружение зацикливания и бесконечных повторов агента (ТЗ §160).

Предотвращает циклы вида:
  * A → A → A (повторный вызов одного инструмента с теми же аргументами без результата)
  * A → B → A → B (чередование двух действий по кругу)
  * Серия неудач без смены стратегии
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    step: int
    args_hash: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            self.args_hash = json.dumps(self.args, sort_keys=True, default=str)
        except Exception:
            self.args_hash = str(self.args)


class LoopGuard:
    """Контроллер зацикливания агентного цикла."""

    def __init__(self, max_consecutive_same: int = 2, max_consecutive_failures: int = 3) -> None:
        self.max_consecutive_same = max_consecutive_same
        self.max_consecutive_failures = max_consecutive_failures
        self.history: list[CallRecord] = []

    def record(self, tool: str, args: dict[str, Any] | None, ok: bool, step: int = 0) -> None:
        rec = CallRecord(tool=tool, args=args or {}, ok=ok, step=step)
        self.history.append(rec)

    def check(self) -> tuple[bool, str]:
        """Проверяет историю на наличие циклов.

        Возвращает (обнаружен_цикл, описание_проблемы).
        """
        n = len(self.history)
        if n < 2:
            return False, ""

        # 1. Проверка на N одинаковых вызовов подряд с идентичными аргументами
        if n >= self.max_consecutive_same + 1:
            last = self.history[-1]
            same_count = 1
            for prev in reversed(self.history[:-1]):
                if prev.tool == last.tool and prev.args_hash == last.args_hash:
                    same_count += 1
                else:
                    break
            if same_count > self.max_consecutive_same:
                return (
                    True,
                    f"Обнаружен цикл: инструмент «{last.tool}» вызван {same_count} раз(а) подряд с одинаковыми параметрами без прогресса. Требуется сменить подход.",
                )

        # 2. Проверка чередующегося цикла A -> B -> A -> B
        if n >= 4:
            c1, c2, c3, c4 = self.history[-4:]
            if (
                c1.tool == c3.tool
                and c1.args_hash == c3.args_hash
                and c2.tool == c4.tool
                and c2.args_hash == c4.args_hash
            ):
                return (
                    True,
                    f"Обнаружен 2-шаговый цикл «{c1.tool}» ↔ «{c2.tool}». Прекратите повторение и измените стратегию.",
                )

        # 3. Проверка на повторяющиеся ошибки (stagnation)
        if n >= self.max_consecutive_failures:
            failures = sum(1 for rec in self.history[-self.max_consecutive_failures:] if not rec.ok)
            if failures >= self.max_consecutive_failures:
                failed_tools = [rec.tool for rec in self.history[-self.max_consecutive_failures:]]
                return (
                    True,
                    f"Застой: {self.max_consecutive_failures} неудачных действий подряд ({', '.join(failed_tools)}). Смените инструмент или завершите с объяснением.",
                )

        return False, ""

    def clear(self) -> None:
        self.history.clear()
