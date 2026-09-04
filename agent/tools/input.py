"""Мышь и клавиатура: click, dblclick, right-click, drag&drop, scroll,
typing, hotkeys.

Реальный ввод — pyautogui. Без дисплея действия записываются в «виртуальный»
журнал (virtual input log) — UI показывает, что агент «сделал бы», а конвейер
остаётся рабочим. На реальной машине с pyautogui — исполняется физически.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


def _pa(ctx: ToolContext):
    if not ctx.platform.has_display:
        return None
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        return pyautogui
    except Exception:
        return None


class VirtualInputLog:
    """Журнал виртуального ввода (headless). UI может показывать его как
    «курсор агента»."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict] = []

    def record(self, action: str, **kw) -> None:
        ev = {"action": action, "ts": time.time(), **kw}
        self.events.append(ev)
        if len(self.events) > 500:
            self.events = self.events[-500:]
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _vlog(ctx: ToolContext) -> VirtualInputLog:
    p = Path(ctx.cfg.state_dir / "virtual_input.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    key = id(ctx)
    store = _vlog._store  # type: ignore[attr-defined]
    if key not in store:
        store[key] = VirtualInputLog(p)
    return store[key]


_vlog._store = {}  # type: ignore[attr-defined]


def register_input_tools(reg: ToolRegistry) -> None:

    @reg.tool("mouse_click",
              "Клик мышью в координатах. button: left/right/middle, clicks: 1 или 2.",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "x": _prop("integer", "X"), "y": _prop("integer", "Y"),
                  "button": _prop("string", "left | right | middle (по умолчанию left)"),
                  "clicks": _prop("integer", "1 = клик, 2 = двойной клик")},
                  "required": ["x", "y"]})
    class MouseClick(Tool):
        async def execute(self, ctx: ToolContext, x: int, y: int,
                          button: str = "left", clicks: int = 1) -> ToolResult:
            pa = _pa(ctx)
            if pa is None:
                vlog = _vlog(ctx)
                vlog.record("click", x=x, y=y, button=button, clicks=clicks)
                return ToolResult.ok_result(
                    f"[virtual] клик ({button}, x{clicks}) в ({x},{y}) записан в виртуальный журнал. "
                    f"Реальное нажатие доступно на машине с дисплеем.", virtual=True)
            pa.click(x, y, clicks=clicks, button=button)
            ctx.bus.emit("screen", virtual_click={"x": x, "y": y})
            return ToolResult.ok_result(f"Клик ({button}, x{clicks}) в ({x},{y})")

    @reg.tool("mouse_drag",
              "Перетащить мышью (drag & drop) из точки в точку. Работает между "
              "окнами, папками, полями загрузки.",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "x1": _prop("integer", "X от"), "y1": _prop("integer", "Y от"),
                  "x2": _prop("integer", "X до"), "y2": _prop("integer", "Y до"),
                  "duration": _prop("number", "Секунд на перетаскивание (по умолчанию 0.8)")},
                  "required": ["x1", "y1", "x2", "y2"]})
    class MouseDrag(Tool):
        async def execute(self, ctx: ToolContext, x1: int, y1: int, x2: int, y2: int,
                          duration: float = 0.8) -> ToolResult:
            pa = _pa(ctx)
            if pa is None:
                vlog = _vlog(ctx)
                vlog.record("drag", x1=x1, y1=y1, x2=x2, y2=y2, duration=duration)
                return ToolResult.ok_result(
                    f"[virtual] drag&drop ({x1},{y1})→({x2},{y2}) записан. "
                    f"Реальное перетаскивание — на машине с дисплеем.", virtual=True)
            pa.moveTo(x1, y1)
            pa.mouseDown(button="left")
            pa.moveTo(x2, y2, duration=duration)
            pa.mouseUp(button="left")
            return ToolResult.ok_result(f"Drag&drop ({x1},{y1})→({x2},{y2})")

    @reg.tool("mouse_scroll", "Прокрутка в точке (дельта: +вверх / -вниз).",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "x": _prop("integer", "X"), "y": _prop("integer", "Y"),
                  "delta": _prop("integer", "Сколько шагов (по умолчанию 5, отрицательное — вниз)")},
                  "required": ["x", "y"]})
    class MouseScroll(Tool):
        async def execute(self, ctx: ToolContext, x: int, y: int, delta: int = 5) -> ToolResult:
            pa = _pa(ctx)
            if pa is None:
                _vlog(ctx).record("scroll", x=x, y=y, delta=delta)
                return ToolResult.ok_result(f"[virtual] скролл {delta} в ({x},{y})", virtual=True)
            pa.moveTo(x, y)
            pa.scroll(delta)
            return ToolResult.ok_result(f"Скролл {delta} в ({x},{y})")

    @reg.tool("keyboard_type",
              "Ввести текст в активное окно/поле. Для кириллицы pyautogui требует "
              "установленный layout — иначе используйте clipboard + Ctrl+V (clip_set + hotkey ctrl+v).",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "text": _prop("string", "Текст"),
                  "interval": _prop("number", "Пауза между символами, сек (по умолчанию 0)")},
                  "required": ["text"]})
    class KeyboardType(Tool):
        async def execute(self, ctx: ToolContext, text: str, interval: float = 0.0) -> ToolResult:
            pa = _pa(ctx)
            if pa is None:
                _vlog(ctx).record("type", text=text)
                return ToolResult.ok_result(f"[virtual] введён текст ({len(text)} символов)", virtual=True)
            try:
                pa.typewrite(text, interval=interval)
                return ToolResult.ok_result(f"Введено: {len(text)} символов")
            except Exception:
                # fallback через clipboard
                return ToolResult.fail(
                    "прямой ввод не удался (layout). Сделайте: clip_set + hotkey 'ctrl+v'.")

    @reg.tool("keyboard_hotkey",
              "Горячая клавиша: 'ctrl+v', 'alt+f4', 'win+d', 'ctrl+shift+t', 'enter', 'tab' ...",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "keys": _prop("string", "Комбинация через + (напр. 'ctrl+c')")},
                  "required": ["keys"]})
    class KeyboardHotkey(Tool):
        def estimate_risk(self, ctx, args):
            keys = str(args.get("keys", "")).lower()
            if "alt+f4" in keys or "del" in keys.split("+"):
                return Risk.MEDIUM, keys
            return Risk.LOW, ""

        async def execute(self, ctx: ToolContext, keys: str) -> ToolResult:
            pa = _pa(ctx)
            if pa is None:
                _vlog(ctx).record("hotkey", keys=keys)
                return ToolResult.ok_result(f"[virtual] хоткей {keys} записан", virtual=True)
            try:
                pa.hotkey(*keys.lower().replace("-", "").split("+"))
                return ToolResult.ok_result(f"Нажата комбинация: {keys}")
            except Exception as e:
                return ToolResult.fail(f"не удалось нажать {keys}: {e}")

    @reg.tool("keyboard_select_all", "Выделить всё в активном поле (Ctrl+A).",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {}})
    class KeyboardSelectAll(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            pa = _pa(ctx)
            if pa is None:
                _vlog(ctx).record("select_all")
                return ToolResult.ok_result("[virtual] выделено всё", virtual=True)
            pa.hotkey("ctrl", "a")
            return ToolResult.ok_result("Выделено всё (Ctrl+A)")

    @reg.tool("input_log",
              "Показать журнал виртуального ввода (что агент сделал бы на дисплее).",
              risk=Risk.NONE, category="input",
              parameters={"type": "object", "properties": {
                  "last": _prop("integer", "Сколько последних (по умолчанию 30)")}, "required": []})
    class InputLog(Tool):
        async def execute(self, ctx: ToolContext, last: int = 30) -> ToolResult:
            vlog = _vlog(ctx)
            evs = vlog.events[-last:]
            if not evs:
                return ToolResult.ok_result("(журнал виртуального ввода пуст)")
            lines = [f"{e['ts']:.1f} {e['action']} { {k: v for k, v in e.items() if k not in ('action', 'ts')} }"
                     for e in evs]
            return ToolResult.ok_result("\n".join(lines))
