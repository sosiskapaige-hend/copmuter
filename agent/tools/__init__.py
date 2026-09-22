"""Сборка реестра инструментов: все категории в одном месте.

Расширение: создайте класс Tool, зарегистрируйте его через reg.tool(...)
и добавьте register_*(reg) вызов в build_registry().
"""
from __future__ import annotations

from .base import Tool, ToolResult, ToolContext, Risk, RISK_NAMES
from .registry import ToolRegistry
from .fs import register_fs_tools
from .terminal import register_terminal_tools, assess_command
from .system import register_system_tools
from .git_tools import register_git_tools
from .apps import register_app_tools
from .window import register_window_tools
from .screen import register_screen_tools
from .input import register_input_tools
from .clipboard import register_clipboard_tools
from .vision import register_vision_tools
from .browser import register_browser_tools
from .os_settings import register_os_settings_tools
from .meta import register_meta_tools
from .win_system import register_win_system_tools
from .vscode import register_vscode_tools
from .browser_agent import register_browser_agent_tools
from .vision_agent import register_vision_agent_tools
from .basic import register_basic_tools


def build_registry(plugins: bool = True) -> ToolRegistry:
    """Собрать реестр инструментов.

    `plugins=False` — только встроенные инструменты (используется в тестах и
    когда нужен строго фиксированный набор).
    """
    reg = ToolRegistry()
    register_fs_tools(reg)
    register_terminal_tools(reg)
    register_system_tools(reg)
    register_git_tools(reg)
    register_app_tools(reg)
    register_window_tools(reg)
    register_screen_tools(reg)
    register_input_tools(reg)
    register_clipboard_tools(reg)
    register_vision_tools(reg)
    register_browser_tools(reg)
    register_os_settings_tools(reg)
    register_meta_tools(reg)
    # --- новые слои (ТЗ): нативные WinAPI-инструменты, VS Code, браузерный слой,
    #     зрение и «человеческие» алиасы быстрого пути ---
    register_win_system_tools(reg)
    register_vscode_tools(reg)
    register_browser_agent_tools(reg)
    register_vision_agent_tools(reg)
    register_basic_tools(reg)
    # --- Plugin/Tool SDK: инструменты из папок плагинов, без правок ядра (ТЗ §49) ---
    if plugins:
        try:
            from ..plugins import load_plugins
            load_plugins(reg)
        except Exception:                     # noqa: BLE001 — плагины не должны ломать старт
            pass
    return reg


__all__ = [
    "Tool", "ToolResult", "ToolContext", "Risk", "RISK_NAMES", "ToolRegistry",
    "build_registry", "assess_command",
]
