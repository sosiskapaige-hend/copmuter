"""Plugin/Tool SDK — новые инструменты без правок ядра (ТЗ §49).

Инструмент агента — это обычный Python-класс. Чтобы добавить свой, не нужно
трогать `agent/tools/*`: достаточно положить файл в папку плагинов.

Где искать (по порядку):

  1. `AGENT_HOME/plugins/`      — пользовательские плагины (по умолчанию
                                  `~/.ai-computer-agent/plugins`);
  2. `<рабочая папка>/plugins/` — плагины проекта;
  3. `AGENT_PLUGINS` (переменная окружения) — путь через `:`/`;`.

Как выглядит плагин — два допустимых способа:

    # my_tool.py
    from agent.tools.base import Risk, Tool, ToolResult, ToolContext

    class WeatherTool(Tool):
        name = "weather"
        description = "Погода в городе"
        risk = Risk.NONE
        category = "misc"
        parameters = {"type": "object",
                      "properties": {"city": {"type": "string"}},
                      "required": ["city"]}

        async def execute(self, ctx: ToolContext, city: str) -> ToolResult:
            return ToolResult.ok_result(f"В {city} солнечно")

    TOOLS = [WeatherTool()]          # или def register(reg): reg.register(WeatherTool())

Ошибки плагинов никогда не ломают агент: файл, который не импортировался,
попадает в сводку (`errors`), остальные инструменты продолжают работать.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from pathlib import Path
from typing import Any

SKIP_PREFIXES = ("_", ".")
SKIP_SUFFIXES = (".disabled", ".py.disabled", ".example", ".txt", ".md")


def plugin_dirs(cfg: Any = None, workdir: str | Path | None = None) -> list[Path]:
    """Папки, где искать плагины (существующие, в порядке приоритета)."""
    out: list[Path] = []
    home = None
    for attr in ("agent_home",):
        home = getattr(cfg, attr, None) or home
    if home:
        out.append(Path(home) / "plugins")
    env = os.environ.get("AGENT_PLUGINS", "")
    if env:
        for part in env.replace(";", os.pathsep).split(os.pathsep):
            if part.strip():
                out.append(Path(os.path.expanduser(part.strip())))
    out.append(Path(workdir or os.getcwd()) / "plugins")
    seen: list[Path] = []
    for d in out:
        if d not in seen:
            seen.append(d)
    return seen


def _load_module(path: Path):
    name = f"agent_plugin_{path.stem}_{abs(hash(str(path))) % 100000}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_plugins(registry: Any, cfg: Any = None, workdir: str | Path | None = None,
                 log: Any = None, dirs: list[Path] | None = None) -> dict:
    """Подключить плагины к реестру инструментов. Возвращает сводку."""
    summary: dict[str, Any] = {"dirs": [], "files": [], "tools": [], "skipped": [],
                               "errors": []}
    for d in (dirs if dirs is not None else plugin_dirs(cfg, workdir)):
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        summary["dirs"].append(str(d))
        for path in sorted(d.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".py":
                summary["skipped"].append(path.name)
                continue
            if path.name.startswith(SKIP_PREFIXES) or path.name.endswith(SKIP_SUFFIXES):
                summary["skipped"].append(path.name)
                continue
            before = set(registry.names())
            try:
                module = _load_module(path)
                registered: list[str] = []
                fn = getattr(module, "register", None)
                if callable(fn):
                    fn(registry)
                for tool in list(getattr(module, "TOOLS", []) or []):
                    try:
                        instance = tool() if isinstance(tool, type) else tool
                        registry.register(instance)
                    except Exception as exc:                 # noqa: BLE001
                        summary["errors"].append(f"{path.name}: инструмент не добавлен: {exc}")
                registered = sorted(set(registry.names()) - before)
                if not registered and not callable(fn) and not getattr(module, "TOOLS", None):
                    summary["errors"].append(
                        f"{path.name}: нет ни register(reg), ни TOOLS — пропущен")
                summary["files"].append(path.name)
                summary["tools"].extend(registered)
            except Exception as exc:                          # noqa: BLE001
                summary["errors"].append(f"{path.name}: {type(exc).__name__}: {exc}")
                if log is not None:
                    try:
                        log.error("Плагин не загрузился", file=path.name,
                                  error=str(exc)[:200],
                                  trace=traceback.format_exc()[-600:])
                    except Exception:
                        pass
    if log is not None and (summary["tools"] or summary["errors"]):
        try:
            log.event("plugins", ok=not summary["errors"], count=len(summary["tools"]),
                      tools=",".join(summary["tools"])[:300],
                      errors="; ".join(summary["errors"])[:300])
        except Exception:
            pass
    return summary


def plugin_help() -> str:
    """Короткая инструкция для пользователя/UI."""
    return ("Плагины: положите .py в AGENT_HOME/plugins (или в ./plugins). "
            "Внутри — классы, унаследованные от Tool, список TOOLS = [...] "
            "или функция register(reg). Ядро менять не нужно.")


__all__ = ["load_plugins", "plugin_dirs", "plugin_help"]
