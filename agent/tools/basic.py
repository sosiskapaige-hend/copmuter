"""Инструменты быстрого слоя с «человеческими» именами (ТЗ §12).

ТЗ перечисляет инструменты в терминах задач: `create_folder`, `delete_file`,
`type_text`, `press_key`, `open_default_browser`… Часть из них уже есть под
техническими именами (`fs_mkdir`, `fs_delete`, `keyboard_type`, `open_url`).
Здесь собраны тонкие алиасы: они ничего не дублируют логически, но дают модели
и быстрому маршрутизатору понятные названия. Алиасы помечены `is_alias=True`,
поэтому при нехватке бюджета схем (`tools_budget`) модель получает
предпочтительно основные инструменты, а не их синонимы.
"""
from __future__ import annotations

from .base import Risk, Tool, ToolContext, ToolResult


def _prop(t: str, desc: str) -> dict:
    return {"type": t, "description": desc}


class _Alias(Tool):
    """Базовый класс алиаса: делегирует вызов основному инструменту."""

    target_tool: str = ""
    is_alias = True
    is_gui = False
    category = "alias"

    def args_to_target(self, args: dict) -> dict:
        return dict(args or {})

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:  # pragma: no cover
        return await self._delegate(ctx, kwargs)

    async def _delegate(self, ctx: ToolContext, kwargs: dict) -> ToolResult:
        reg = ctx.service("registry") or ctx.services.get("tools")
        if reg is None:
            return ToolResult.fail("реестр инструментов недоступен")
        mapped = self.args_to_target({k: v for k, v in kwargs.items() if v is not None})
        res = await reg.call(self.target_tool, mapped, ctx)
        if res.ok and not res.data.get("alias"):
            res.data = {**res.data, "alias": self.name, "via": self.target_tool}
        return res


def register_basic_tools(reg) -> None:

    @reg.tool("launch_application",
              "Запустить приложение по названию/алиасу («телега», «вс код», «хром») "
              "или по пути к exe. Работает через реестр приложений и лестницу "
              "способов запуска — без модели.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Название или путь"),
                  "args": _prop("string", "Аргументы командной строки"),
                  "wait": _prop("boolean", "Дождаться появления окна"),
                  "timeout": _prop("number", "Сколько ждать, сек")},
                  "required": ["name"]})
    class LaunchApplication(Tool):
        async def execute(self, ctx: ToolContext, name: str, args: str = "", wait: bool = False,
                          timeout: float = 0) -> ToolResult:
            launcher = ctx.service("launcher")
            target = (name or "").strip()
            if launcher is not None:
                res = await launcher.launch(ctx, target, target=args, verify=bool(wait) or True,
                                            timeout=float(timeout or 12))
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(res.message, method=res.method, pid=res.pid,
                                                verified=res.verified,
                                                attempts=[a.to_dict() for a in res.attempts][-6:])
                if not res.ok and res.method == "not_found":
                    reg2 = ctx.service("registry")
                    if reg2 is not None:
                        r = await reg2.call("launch_app", {"name": target, "path": args}, ctx)
                        return r
                return ToolResult(ok=False, output=res.message, error=res.error or res.message,
                                  data={"attempts": [a.to_dict() for a in res.attempts][-6:]})
            reg2 = ctx.service("registry")
            if reg2 is None:
                return ToolResult.fail("нет доступа к реестру приложений")
            return await reg2.call("launch_app", {"name": target, "path": args}, ctx)

    @reg.tool("open_folder", "Открыть папку в проводнике (понимает «загрузки», "
                             "«рабочий стол», «документы»).",
              risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь или название системной папки")},
                  "required": ["path"]})
    class OpenFolder(_Alias):
        target_tool = "open_path"

        def args_to_target(self, args: dict) -> dict:
            path = str(args.get("path", ""))
            try:
                from ..system import paths as path_tools
                known = path_tools.place_dir(path)
                if known is not None and not path_tools.is_absolute_like(path):
                    path = str(known)
            except Exception:
                pass
            return {"path": path}

    @reg.tool("create_folder", "Создать папку (вместе с родительскими).",
              risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь новой папки"),
                  "place": _prop("string", "Системная папка («рабочий стол», «загрузки»)")},
                  "required": ["path"]})
    class CreateFolder(_Alias):
        target_tool = "fs_mkdir"

        def args_to_target(self, args: dict) -> dict:
            return {"path": _with_place(args.get("path", ""), args.get("place", ""))}

    @reg.tool("delete_file", "Удалить файл (по умолчанию — в корзину агента, "
                             "чтобы можно было отменить).",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу"),
                  "permanent": _prop("boolean", "Удалить окончательно")},
                  "required": ["path"]})
    class DeleteFile(_Alias):
        target_tool = "fs_delete"

        def args_to_target(self, args: dict) -> dict:
            return {"path": args.get("path", ""), "recursive": False,
                    "permanent": bool(args.get("permanent", False))}

    @reg.tool("delete_folder", "Удалить папку (со содержимым, если recursive).",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к папке"),
                  "recursive": _prop("boolean", "Удалить вместе с содержимым"),
                  "permanent": _prop("boolean", "Мимо корзины агента")},
                  "required": ["path"]})
    class DeleteFolder(_Alias):
        target_tool = "fs_delete"

        def args_to_target(self, args: dict) -> dict:
            return {"path": args.get("path", ""), "recursive": bool(args.get("recursive", True)),
                    "permanent": bool(args.get("permanent", False))}

    @reg.tool("copy_file", "Скопировать файл или папку.", risk=Risk.LOW, category="fs",
              parameters={"type": "object", "properties": {
                  "src": _prop("string", "Откуда"), "dst": _prop("string", "Куда")},
                  "required": ["src", "dst"]})
    class CopyFile(_Alias):
        target_tool = "fs_copy"

    @reg.tool("move_file", "Переместить (или переименовать) файл/папку.",
              risk=Risk.MEDIUM, category="fs",
              parameters={"type": "object", "properties": {
                  "src": _prop("string", "Откуда"), "dst": _prop("string", "Куда")},
                  "required": ["src", "dst"]})
    class MoveFile(_Alias):
        target_tool = "fs_move"

    @reg.tool("type_text",
              "Напечатать текст в активное окно. Длинный текст вставляется через "
              "буфер обмена (быстро и без потерь).",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "text": _prop("string", "Текст"),
                  "interval": _prop("number", "Пауза между символами (для коротких строк)")},
                  "required": ["text"]})
    class TypeText(Tool):
        async def execute(self, ctx: ToolContext, text: str, interval: float = 0.0) -> ToolResult:
            inputs = ctx.service("inputs")
            if inputs is not None:
                res = await inputs.type_text(text)
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(getattr(res, "message", "текст введён"),
                                                method="input_controller",
                                                chars=len(text or ""))
                return ToolResult.fail(getattr(res, "message", "не удалось напечатать"))
            reg2 = ctx.service("registry")
            if reg2 is None:
                return ToolResult.fail("нет подсистемы ввода")
            return await reg2.call("keyboard_type", {"text": text, "interval": interval}, ctx)

    @reg.tool("press_key", "Нажать клавишу или сочетание (ctrl+s, alt+tab, enter).",
              risk=Risk.LOW, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "key": _prop("string", "Клавиша или сочетание"),
                  "presses": _prop("integer", "Сколько раз нажать")},
                  "required": ["key"]})
    class PressKey(Tool):
        async def execute(self, ctx: ToolContext, key: str, presses: int = 1) -> ToolResult:
            inputs = ctx.service("inputs")
            combo = (key or "").strip()
            n = max(1, int(presses or 1))
            if inputs is not None:
                res = (await inputs.hotkey(combo) if "+" in combo
                       else await inputs.press_key(combo, presses=n))
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(f"Нажал {combo}", method="input_controller")
                return ToolResult.fail(getattr(res, "message", "не удалось нажать клавишу"))
            reg2 = ctx.service("registry")
            if reg2 is None:
                return ToolResult.fail("нет подсистемы ввода")
            return await reg2.call("keyboard_hotkey", {"keys": combo}, ctx)

    @reg.tool("move_mouse", "Переместить курсор в точку экрана.",
              risk=Risk.NONE, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "x": _prop("integer", "X"), "y": _prop("integer", "Y"),
                  "duration": _prop("number", "Плавность, сек")},
                  "required": ["x", "y"]})
    class MoveMouse(Tool):
        async def execute(self, ctx: ToolContext, x: int, y: int, duration: float = 0.0) -> ToolResult:
            inputs = ctx.service("inputs")
            if inputs is not None:
                res = await inputs.move(int(x), int(y), duration=float(duration or 0))
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(f"Курсор в ({x}, {y})", method="input_controller")
                return ToolResult.fail(getattr(res, "message", "не удалось переместить курсор"))
            reg2 = ctx.service("registry")
            if reg2 is None:
                return ToolResult.fail("нет подсистемы ввода")
            return await reg2.call("mouse_click", {"x": int(x), "y": int(y), "clicks": 0}, ctx)

    @reg.tool("double_click", "Двойной щелчок по координатам.",
              risk=Risk.MEDIUM, category="input", is_gui=True,
              parameters={"type": "object", "properties": {
                  "x": _prop("integer", "X"), "y": _prop("integer", "Y")},
                  "required": ["x", "y"]})
    class DoubleClick(Tool):
        async def execute(self, ctx: ToolContext, x: int, y: int) -> ToolResult:
            inputs = ctx.service("inputs")
            if inputs is not None:
                res = await inputs.click(int(x), int(y), clicks=2)
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(f"Двойной клик в ({x}, {y})", method="input_controller")
                return ToolResult.fail(getattr(res, "message", "двойной клик не удался"))
            reg2 = ctx.service("registry")
            return await reg2.call("mouse_click", {"x": int(x), "y": int(y), "clicks": 2}, ctx) \
                if reg2 is not None else ToolResult.fail("нет подсистемы ввода")

    @reg.tool("search_web", "Найти в интернете (прямая ссылка поиска, без адресной строки).",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "query": _prop("string", "Запрос"),
                  "engine": _prop("string", "google | yandex | bing | duckduckgo")},
                  "required": ["query"]})
    class SearchWeb(_Alias):
        target_tool = "search_google"

    @reg.tool("open_default_browser", "Открыть браузер по умолчанию или ссылку в нём.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "Ссылка (необязательно)")},
                  "required": []})
    class OpenDefaultBrowser(Tool):
        async def execute(self, ctx: ToolContext, url: str = "") -> ToolResult:
            reg2 = ctx.service("registry")
            if reg2 is None:
                return ToolResult.fail("реестр инструментов недоступен")
            r = await reg2.call("open_url", {"url": url or ""}, ctx)
            if r.ok:
                return ToolResult.ok_result(r.output or "Браузер открыт", method="open_url")
            return r

    @reg.tool("clipboard_get", "Прочитать буфер обмена.", risk=Risk.NONE, category="clipboard",
              parameters={"type": "object", "properties": {}})
    class ClipboardGet(_Alias):
        target_tool = "clip_get"

    @reg.tool("clipboard_set", "Положить текст в буфер обмена.", risk=Risk.LOW, category="clipboard",
              parameters={"type": "object", "properties": {
                  "text": _prop("string", "Текст")}, "required": ["text"]})
    class ClipboardSet(_Alias):
        target_tool = "clip_set"

    @reg.tool("kill_process", "Завершить процесс по имени или PID.",
              risk=Risk.HIGH, category="system",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Имя процесса"),
                  "pid": _prop("integer", "PID"),
                  "force": _prop("boolean", "Принудительно")},
                  "required": []})
    class KillProcess(_Alias):
        target_tool = "process_kill"

        def args_to_target(self, args: dict) -> dict:
            return {"name": args.get("name", ""), "pid": int(args.get("pid") or 0),
                    "force": bool(args.get("force", False))}


def _with_place(path: str, place: str) -> str:
    """«123» + place=«рабочий стол» → «C:/Users/…/Desktop/123»."""
    p = str(path or "").strip()
    if not p:
        return p
    try:
        from ..system import paths as path_tools
        if place:
            base = path_tools.place_dir(place)
            if base is not None and not path_tools.is_absolute_like(p):
                return str(base / p)
        resolved, _ = path_tools.resolve_path(p, base=path_tools.place_dir(place) if place else None,
                                              fuzzy=False)
        if resolved is not None:
            return str(resolved)
    except Exception:
        pass
    return p


__all__ = ["register_basic_tools"]
