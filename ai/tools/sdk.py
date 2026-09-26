"""Tool SDK, Dynamic Discovery, Versioning and Fallback Graphs (ТЗ §117-§122).

Предоставляет:
  * Стандарт описания инструментов (ToolSpec: имя, версия, пространство имён,
    JSON Schema, уровень риска, права доступа, кросс-платформенность, правила проверки,
    поддержка отката, цепочка запасных способов fallback);
  * Динамическую фильтрацию инструментов по контексту задачи (Discovery),
    чтобы не перегружать промпт модели сотнями ненужных определений;
  * Граф альтернатив (Fallback Graph) при сбоях инструментов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ToolSpec:
    """Самоописываемый инструмент SDK."""

    name: str
    version: str = "1.0.0"
    namespace: str = "general"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    risk: RiskLevel = RiskLevel.LOW
    platforms: list[str] = field(default_factory=lambda: ["windows", "linux", "darwin"])
    permissions: list[str] = field(default_factory=list)
    supports_undo: bool = False
    verification_hint: str = ""
    fallback_tools: list[str] = field(default_factory=list)

    def to_function_schema(self) -> dict[str, Any]:
        """Преобразует в формат OpenAI Function Calling."""
        return {
            "name": self.name,
            "description": f"[{self.namespace}:{self.version}] {self.description}",
            "parameters": self.parameters,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "namespace": self.namespace,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk.value,
            "platforms": self.platforms,
            "permissions": self.permissions,
            "supports_undo": self.supports_undo,
            "fallback_tools": self.fallback_tools,
        }


class ToolRegistrySDK:
    """Реестр инструментов с поддержкой версий, namespaces и динамического поиска."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._fallbacks: dict[str, list[str]] = {}

    def register(self, spec: ToolSpec) -> None:
        """Регистрирует инструмент в реестре."""
        self._tools[spec.name] = spec
        if spec.fallback_tools:
            self._fallbacks[spec.name] = list(spec.fallback_tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def get_fallback(self, name: str) -> list[str]:
        """Возвращает список запасных инструментов для указанного."""
        return self._fallbacks.get(name, [])

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def filter_by_namespace(self, namespace: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.namespace == namespace]

    def discover_tools_for_task(
        self,
        task: str,
        *,
        current_platform: str = "windows",
        core_tools: Iterable[str] = (),
        max_tools: int = 16,
    ) -> list[ToolSpec]:
        """Динамический подбор инструментов под контекст задачи (ТЗ §119)."""
        words = {w.lower() for w in re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", task or "")}
        scored: list[tuple[int, ToolSpec]] = []
        core_set = set(core_tools)

        for tool in self._tools.values():
            if current_platform not in tool.platforms:
                continue

            score = 0
            if tool.name in core_set:
                score += 5

            haystack = f"{tool.name} {tool.namespace} {tool.description}".lower()
            for w in words:
                if w in haystack:
                    score += 3
                elif w[:4] and w[:4] in haystack:
                    score += 1

            if score > 0:
                scored.append((score, tool))

        # Сортируем по релевантности
        scored.sort(key=lambda x: x[0], reverse=True)
        chosen = [t for _, t in scored[:max_tools]]

        # Если найдено мало, гарантируем наличие ядра
        taken_names = {t.name for t in chosen}
        for name in core_tools:
            if name in self._tools and name not in taken_names:
                chosen.append(self._tools[name])
                taken_names.add(name)
                if len(chosen) >= max_tools:
                    break

        return chosen


# Глобальный реестр SDK по умолчанию
global_tool_registry = ToolRegistrySDK()


def register_standard_tools(registry: ToolRegistrySDK) -> None:
    """Регистрирует стандартные инструменты системы."""
    # Filesystem
    registry.register(
        ToolSpec(
            name="create_folder",
            version="1.1.0",
            namespace="fs",
            description="Создаёт папку по указанному пути",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Путь к папке"}},
                "required": ["path"],
            },
            risk=RiskLevel.LOW,
            supports_undo=True,
            verification_hint="folder_exists",
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            version="1.2.0",
            namespace="fs",
            description="Создаёт или перезаписывает файл с указанным содержимым",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "content": {"type": "string", "description": "Текст файла"},
                },
                "required": ["path", "content"],
            },
            risk=RiskLevel.MEDIUM,
            supports_undo=True,
            verification_hint="file_exists_and_matches",
        )
    )
    registry.register(
        ToolSpec(
            name="delete_path",
            version="1.0.0",
            namespace="fs",
            description="Удаляет файл или папку в корзину отката",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Путь для удаления"}},
                "required": ["path"],
            },
            risk=RiskLevel.HIGH,
            supports_undo=True,
            verification_hint="path_does_not_exist",
        )
    )
    registry.register(
        ToolSpec(
            name="undo_last_action",
            version="1.0.0",
            namespace="fs",
            description="Откатывает последнее обратимое файловое действие (восстанавливает удалённый файл, удаляет созданный файл, возвращает предыдущую версию)",
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.MEDIUM,
            supports_undo=False,
            verification_hint="journal_undone",
        )
    )

    # Applications
    registry.register(
        ToolSpec(
            name="launch_application",
            version="1.3.0",
            namespace="apps",
            description="Запускает приложение по имени, ключу или пути",
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Имя или ключ приложения"},
                    "args": {"type": "string", "description": "Аргументы командной строки"},
                },
                "required": ["app"],
            },
            risk=RiskLevel.LOW,
            fallback_tools=["run_command", "execute_powershell"],
            verification_hint="process_or_window_running",
        )
    )

    # Browser
    registry.register(
        ToolSpec(
            name="browser_task",
            version="2.0.0",
            namespace="browser",
            description="Автоматизация браузера через Playwright: open, click, fill, text, eval, screenshot",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open", "click", "fill", "text", "links", "eval", "screenshot", "wait", "info"],
                    },
                    "url": {"type": "string"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["action"],
            },
            risk=RiskLevel.LOW,
            fallback_tools=["open_url"],
            verification_hint="browser_state_changed",
        )
    )

    # System Diagnostics
    registry.register(
        ToolSpec(
            name="system_diagnostics",
            version="1.0.0",
            namespace="system",
            description="Диагностика состояния ПК: загрузка CPU, память, диск, верхние процессы",
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.NONE,
            verification_hint="diagnostics_returned",
        )
    )


register_standard_tools(global_tool_registry)
