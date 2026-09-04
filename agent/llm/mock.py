"""Mock-LLM: детерминированный «агент» для тестов и демо без API-ключа.

Сценарии подбираются по регулярным выражениям из формулировки задачи.
Это НЕ заглушка «ничего не делает»: mock реально вызывает инструменты
через обычный агентный цикл, поэтому весь конвейер (план → действие →
наблюдение → проверка → результат) работает вживую.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .base import LLM, Plan, PlanStep, ToolCall, Decision, Assessment


@dataclass
class _Scenario:
    pattern: str
    summary: str
    plan: list[tuple[str, str]]                     # (title, detail)
    actions: list[dict]                             # [{"tool":..., "args":...}, ...]
    final: str


@dataclass
class _State:
    index: int = 0
    scenario: _Scenario | None = None
    errors: int = 0


def _pick_folder(goal: str) -> str:
    m = re.search(r"(?:папку|папке|folder|каталог|проекта|проекте)\s+[\"«]?\s*([A-Za-z0-9_.\-]+)", goal, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"([A-Za-z0-9_.\-]{3,24})\s+(?:папк|каталог|проек)", goal, re.I)
    if m:
        return m.group(1).strip()
    return "NewProject"


def _pick_file(goal: str, default: str = "report.txt") -> str:
    m = re.search(r"(?:файл|file|документ) [\"]?([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,5})[\”\".!?\s,;]?", goal, re.I)
    if m:
        return m.group(1)
    return default


def _scenario_for(goal: str) -> _Scenario:
    g = goal.lower()

    if re.search(r"(создай|создать|сделай)\s+(новую\s+)?(папку|folder|каталог)", g) or \
       re.search(r"(создай|сделай)\s+(новый\s+)?проект", g) or "структур" in g:
        folder = _pick_folder(goal)
        return _Scenario(
            pattern="create",
            summary=f"Создание структуры проекта «{folder}»",
            plan=[
                ("Создание папки", f"fs_mkdir: {folder}"),
                ("Создание стартовых файлов", "README.md, main.py, .gitignore"),
                ("Проверка структуры", "fs_list + terminal"),
            ],
            actions=[
                {"tool": "fs_mkdir", "args": {"path": folder}},
                {"tool": "fs_write", "args": {"path": f"{folder}/README.md",
                                              "content": f"# {folder}\n\nСоздано AI-агентом.\n"}},
                {"tool": "fs_write", "args": {"path": f"{folder}/main.py",
                                              "content": "def main():\n    print('Hello from {folder}')\n\n"
                                                        "if __name__ == '__main__':\n    main()\n".format(folder=folder)}},
                {"tool": "fs_write", "args": {"path": f"{folder}/.gitignore", "content": "__pycache__/\n*.pyc\n.venv/\n"}},
                {"tool": "fs_list", "args": {"path": folder}},
                {"tool": "terminal_run", "args": {"command": f"test -f '{folder}/main.py' && echo OK"}},
            ],
            final=f"Готово: создана папка «{folder}» со стартовыми файлами (README.md, main.py, .gitignore). "
                  "Структура проверена — все файлы на месте.",
        )

    if re.search(r"(напиши|создай)\s+файл|создать\s+файл", g) or re.search(r"файл\s+\S+\.(txt|md|py|json|csv)", g):
        fname = _pick_file(goal)
        content = "Содержимое созданное агентом по запросу.\n"
        m = re.search(r"с\s+содержимым\s+[\"«]?(.+?)[\"»]?\s*$", g)
        if m:
            content = m.group(1) + "\n"
        return _Scenario(
            pattern="write_file",
            summary=f"Создание файла {fname}",
            plan=[("Запись файла", fname), ("Проверка", "fs_read")],
            actions=[
                {"tool": "fs_write", "args": {"path": fname, "content": content}},
                {"tool": "fs_read", "args": {"path": fname}},
            ],
            final=f"Файл {fname} создан и проверен (содержимое совпадает).",
        )

    if "найди" in g and ("файл" in g or "pdf" in g or "документ" in g):
        pat = "pdf" if "pdf" in g else "*"
        return _Scenario(
            pattern="find",
            summary="Поиск файлов",
            plan=[("Поиск", "fs_search по рабочей папке"), ("Отчёт", "результаты поиска")],
            actions=[
                {"tool": "fs_search", "args": {"root": ".", "pattern": f"*.{pat}", "max_results": 50}},
            ],
            final="Поиск выполнен. Список найденных файлов — в наблюдении.",
        )

    if "скопируй" in g or "copy" in g:
        return _Scenario(
            pattern="copy",
            summary="Копирование файла",
            plan=[("Копирование", "fs_copy"), ("Проверка", "fs_read")],
            actions=[
                {"tool": "fs_copy", "args": {"src": "main.py", "dst": "main_backup.py"}},
                {"tool": "fs_read", "args": {"path": "main_backup.py"}},
            ],
            final="Файл скопирован и проверен.",
        )

    if "запусти" in g or "run" in g:
        return _Scenario(
            pattern="run",
            summary="Запуск и проверка",
            plan=[("Запуск", "terminal"), ("Анализ вывода", "stdout/exit code")],
            actions=[
                {"tool": "terminal_run", "args": {"command": "echo AGENT_RUN_OK && python3 -c \"print(2+2)\""}},
            ],
            final="Команда выполнена успешно, exit code 0, вывод получен.",
        )

    if "диагностик" in g or "почему" in g or "тормозит" in g:
        return _Scenario(
            pattern="diagnose",
            summary="Диагностика системы",
            plan=[("Сбор метрик", "system_info"), ("Процессы", "process_top"), ("Вывод", "рекомендации")],
            actions=[
                {"tool": "system_info", "args": {}},
                {"tool": "process_top", "args": {"limit": 5}},
                {"tool": "disk_usage", "args": {}},
            ],
            final="Диагностика завершена: метрики системы, топ процессов и использование дисков собраны. "
                  "Рекомендации — в наблюдении.",
        )

    if "скриншот" in g or "снимок" in g or "экран" in g:
        return _Scenario(
            pattern="screenshot",
            summary="Скриншот экрана",
            plan=[("Скриншот", "screen_capture"), ("Анализ", "screen_describe")],
            actions=[
                {"tool": "screen_capture", "args": {}},
                {"tool": "screen_describe", "args": {"question": "Что на экране? Какие окна открыты?"}},
            ],
            final="Скриншот сделан и проанализирован.",
        )

    if re.search(r"(подожди|подожди|жди |след за|наблюд|watch|следи)", g):
        return _Scenario(
            pattern="watch",
            summary="Наблюдение и ожидание",
            plan=[("Ожидание", "wait 1с"), ("Проверка системы", "process_top"),
                  ("Ожидание 2", "wait 1с"), ("Итог", "отчёт")],
            actions=[
                {"tool": "wait", "args": {"seconds": 1}},
                {"tool": "process_top", "args": {"limit": 3}},
                {"tool": "wait", "args": {"seconds": 1}},
            ],
            final="Наблюдение завершено: состояние системы проверено в два прохода.",
        )

    if "память" in g or "запомни" in g:
        return _Scenario(
            pattern="memory",
            summary="Сохранение предпочтения",
            plan=[("Сохранить", "memory_add")],
            actions=[
                {"tool": "memory_add", "args": {"kind": "preference", "text": goal}},
            ],
            final="Предпочтение сохранено в долговременную память.",
        )

    # универсальный сценарий: исследовать и отчитаться
    return _Scenario(
        pattern="generic",
        summary="Исследование и отчёт",
        plan=[("Состояние системы", "system_info"), ("Топ процессов", "process_top"),
              ("Итог", "ответ пользователю")],
        actions=[
            {"tool": "system_info", "args": {}},
            {"tool": "process_top", "args": {"limit": 5}},
            {"tool": "fs_list", "args": {"path": "."}},
        ],
        final="Задача выполнена в рамках доступных средств: собрано состояние системы, процессов и текущей папки.",
    )


class MockLLM:
    """Детерминированный провайдер для офлайн-режима и тестов."""

    name = "mock"

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}
        self.call_count = 0

    def _state(self, goal: str) -> _State:
        st = self._states.get(goal)
        if st is None:
            st = _State(scenario=_scenario_for(goal))
            self._states[goal] = st
        return st

    async def plan(self, goal: str, context: str, tools_hint: str) -> Plan:
        st = self._state(goal)
        sc = st.scenario
        return Plan(steps=[PlanStep(t, d) for t, d in sc.plan], summary=sc.summary)

    async def next_action(self, goal: str, context: str,
                          history: list[dict], tools: list[dict]) -> Decision:
        self.call_count += 1
        st = self._state(goal)
        sc = st.scenario
        assert sc is not None
        # если была ошибка — "меняем подход" (в mock — пропускаем шаг)
        last = history[-1] if history else None
        if last and last.get("role") == "tool" and last.get("ok") is False and st.index < len(sc.actions):
            st.errors += 1
        idx = st.index
        st.index += 1
        if idx >= len(sc.actions):
            return Decision(final=sc.final)
        act = sc.actions[idx]
        return Decision(thought=f"[mock] шаг {idx + 1}/{len(sc.actions)}: {act['tool']}",
                        tool_call=ToolCall(act["tool"], dict(act["args"])))

    async def assess(self, goal: str, done_summary: str, last_error: str | None) -> Assessment:
        st = self._state(goal)
        sc = st.scenario
        if sc is None:
            return Assessment(progress=0, achieved=False, message="нет сценария")
        n = len(sc.actions)
        done = min(st.index, n)
        progress = int(100 * done / max(n, 1))
        return Assessment(
            progress=progress,
            achieved=done >= n,
            remaining=[] if done >= n else [sc.plan[min(n - done, len(sc.plan) - 1)][0]],
            message=f"Выполнено {done} из {n} действий.",
        )

    async def answer(self, messages: list[dict]) -> str:
        last = messages[-1].get("content", "") if messages else ""
        return f"[mock-ответ] {str(last)[:120]}"

    def chat_stream(self, messages: list[dict], temperature: float | None = None):
        import time as _t
        last = ""
        for m in reversed(messages):
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                last = c
                break
        text = (f"[mock] Ассистент работает в офлайн-режиме (LLM не подключена). "
                f"Вы сказали: «{last[:200]}». Подключите модель в ⚙ Настройках "
                f"(например, LM Studio: http://localhost:1234/v1).")
        for word in text.split(" "):
            yield "content", word + " "
            _t.sleep(0.01)

    def list_models(self) -> list[str]:
        return ["mock"]

    async def describe_image(self, image_b64: str, prompt: str) -> str:
        return "[mock-vision] изображение получено (headless-демо): на экране ничего критичного."
