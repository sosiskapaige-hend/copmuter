"""Context Manager: в модель уходит только нужное (ТЗ §19).

Что попадает в контекст:
  1. компактный core-промпт;
  2. только релевантные инструменты (динамическая подгрузка по задаче);
  3. текущее состояние ПК (сжато, без дампов);
  4. релевантная история диалога (окно + резюме);
  5. задача и наблюдения текущего шага.

Чего в контексте нет: полного списка инструментов, полной истории, всех снимков
экрана, сырых дампов состояния.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import prompts

# Инструменты, которые нужны почти всегда (ядро «рук»).
CORE_TOOLS = [
    "launch_application",
    "focus_window",
    "open_url",
    "computer_state",
    "screenshot",
    "send_keys",
    "type_text",
    "wait_for",
    "run_command",
    "clipboard",
    "execute_powershell",
]

WORD_RE = re.compile(r"[a-zA-Zа-яА-Я0-9_]{3,}")


def estimate_tokens(text: str) -> int:
    """Грубая, но стабильная оценка: ~4 символа на токен для смеси RU/EN."""
    return max(1, len(text) // 4)


@dataclass
class ContextStats:
    messages: int = 0
    tools: int = 0
    history_messages: int = 0
    summarized: bool = False
    tokens: int = 0


@dataclass
class ContextManager:
    """Сборка messages для модели с жёстким бюджетом."""

    core_prompt: str = prompts.CORE
    max_tokens: int = 8192
    reserve_for_reply: int = 900
    history_limit: int = 24
    tool_limit: int = 24
    state_limit_chars: int = 900
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    stats: ContextStats = field(default_factory=ContextStats)

    # ------------------------------------------------------------------ инструменты
    def select_tools(self, tools: Iterable[dict], task: str, wanted: Iterable[str] = ()) -> list[dict]:
        """Динамическая подгрузка: ядро + упоминания в задаче + явно запрошенные."""
        task_words = {w.lower() for w in WORD_RE.findall(task or "")}
        scored: list[tuple[int, dict]] = []
        for tool in tools:
            name = str(tool.get("name") or tool.get("function", {}).get("name") or "")
            if not name:
                continue
            description = str(tool.get("description") or tool.get("function", {}).get("description") or "")
            haystack = f"{name} {description}".lower()
            score = 0
            if name in CORE_TOOLS:
                score += 3
            if name in wanted:
                score += 6
            for word in task_words:
                if word in haystack:
                    score += 2
                elif word[:5] and word[:5] in haystack:
                    score += 1
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = [tool for _, tool in scored[: self.tool_limit]]
        # Пол мини-набора: если по словам задачи совпало мало инструментов, добираем
        # остальные по порядку. Иначе модель физически не сможет выполнить задачу
        # («write_file» не совпал со словами «сделай отчёт» — и его нет в контексте).
        floor = min(self.tool_limit, 12)
        if len(chosen) < floor:
            taken = {str(t.get("name")) for t in chosen}
            for tool in tools:
                name = str(tool.get("name") or "")
                if not name or name in taken:
                    continue
                chosen.append(tool)
                taken.add(name)
                if len(chosen) >= floor:
                    break
        self.stats.tools = len(chosen)
        return chosen

    # ------------------------------------------------------------------ сообщения
    def build(
        self,
        task: str,
        *,
        history: list[dict] | None = None,
        state: dict | None = None,
        observations: list[str] | None = None,
        step: int = 1,
        max_steps: int = 12,
        extra_system: str = "",
        apps: list[dict] | None = None,
    ) -> list[dict]:
        system_parts = [self.core_prompt]
        state_text = prompts.with_state(state)
        if state_text:
            system_parts.append("Текущее состояние компьютера:\n" + state_text[: self.state_limit_chars])
        apps_text = prompts.with_apps(apps)
        if apps_text:
            # Реестр приложений этой конкретной машины: точные имена, пути и протоколы.
            system_parts.append(apps_text)
        if self.summary:
            system_parts.append("Что было раньше:\n" + self.summary)
        if self.facts:
            system_parts.append("Важные факты:\n" + "\n".join(f"- {f}" for f in self.facts[:8]))
        if extra_system:
            system_parts.append(extra_system)
        system_parts.append(f"Шаг {step} из {max_steps}. Действуй по одному короткому шагу.")

        messages: list[dict] = [{"role": "system", "content": "\n\n".join(system_parts)}]

        history = history or []
        kept = self._trim_history(history)
        messages.extend(kept)

        user_text = task
        if observations:
            user_text += "\n\nНаблюдения (результат прошлых шагов):\n" + "\n".join(
                f"- {obs}" for obs in observations[-6:]
            )
        messages.append({"role": "user", "content": user_text})

        self._shrink(messages)
        self.stats.messages = len(messages)
        self.stats.history_messages = len(kept)
        self.stats.tokens = sum(estimate_tokens(m.get("content") or "") for m in messages)
        return messages

    def _trim_history(self, history: list[dict]) -> list[dict]:
        if not history:
            return []
        window = history[-self.history_limit :]
        # длинные сообщения режем: в историю попадает суть, не простыни
        out: list[dict] = []
        for message in window:
            content = message.get("content") or ""
            if isinstance(content, str) and len(content) > 1500:
                content = content[:1500] + "…"
            out.append({"role": message.get("role", "user"), "content": content})
        return out

    def _shrink(self, messages: list[dict]) -> None:
        """Укладываемся в бюджет: режем историю, при нужде — наблюдения."""
        budget = max(1024, self.max_tokens - self.reserve_for_reply)
        while self._tokens(messages) > budget and len(messages) > 2:
            # удаляем самое старое сообщение истории (после system)
            del messages[1]
            self.stats.summarized = True

    @staticmethod
    def _tokens(messages: list[dict]) -> int:
        return sum(estimate_tokens(m.get("content") if isinstance(m.get("content"), str) else "") for m in messages)

    # ------------------------------------------------------------------ резюме
    def maybe_summarize(
        self,
        history: list[dict],
        summarizer: Callable[[list[dict]], str] | None = None,
    ) -> bool:
        """Если история переросла окно — сжать её в резюме (вызов модели)."""
        if len(history) <= self.history_limit or not summarizer:
            return False
        older = history[: -self.history_limit]
        try:
            summary = summarizer(older).strip()
        except Exception:  # pragma: no cover - модель может упасть, это не критично
            return False
        if summary:
            self.summary = summary
            return True
        return False


def compress_prompt() -> str:
    return prompts.SUMMARY
