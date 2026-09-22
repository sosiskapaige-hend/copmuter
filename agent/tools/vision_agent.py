"""Инструменты зрения (ТЗ §15, §17, §47).

Это последний рубеж: если интерфейс незнаком и API/CLI/горячих клавиш нет —
снимаем экран, спрашиваем Qwen3-VL и кликаем по найденным координатам.
Каждый шаг конвейера виден в метриках (vision_call) и в логе.

Координаты изображения ≠ координаты мыши: преобразование делает
`vision/coordinates.CoordinateMapper` (масштаб, кроп, DPI, мультимонитор).
"""
from __future__ import annotations

from pathlib import Path

from .base import Risk, Tool, ToolContext, ToolResult


def _prop(t: str, desc: str) -> dict:
    return {"type": t, "description": desc}


def register_vision_agent_tools(reg) -> None:
    @reg.tool("take_screenshot",
              "Сделать снимок экрана (монитор, область). Возвращает путь к файлу и "
              "размеры; снимок попадает в кэш для последующего анализа.",
              risk=Risk.NONE, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "monitor": _prop("integer", "Номер монитора (0 — все экраны)"),
                  "region": _prop("array", "Область [x, y, w, h]"),
                  "max_pixels": _prop("integer", "Ограничение размера картинки")},
                  "required": []})
    class TakeScreenshot(Tool):
        async def execute(self, ctx: ToolContext, monitor: int = 0, region: list | None = None,
                          max_pixels: int = 0) -> ToolResult:
            screens = ctx.service("screens")
            if screens is not None:
                try:
                    shot = await screens.capture(monitor=int(monitor or 0),
                                                 region=tuple(region) if region else None,
                                                 max_pixels=int(max_pixels) or None,
                                                 tag="tool")
                    ctx.set_screenshot_cache(shot.path)
                    return ToolResult.ok_result(
                        f"Снимок экрана: {shot.path} ({shot.width}×{shot.height}"
                        f"{', DPI ' + str(shot.dpi_scale) if shot.dpi_scale != 1 else ''})",
                        path=shot.path, width=shot.width, height=shot.height,
                        origin={"x": shot.origin_x, "y": shot.origin_y},
                        scale=shot.scale, headless=shot.headless, method="screenshot_manager")
                except Exception as exc:                    # noqa: BLE001
                    fallback_error = str(exc)
            else:
                fallback_error = "нет сервиса скриншотов"
            reg_tools = ctx.service("registry")
            if reg_tools is not None:
                r = await reg_tools.call("screen_capture", {"monitor": int(monitor or 0)}, ctx)
                if r.ok:
                    path = str(r.data.get("path") or r.data.get("file") or "")
                    if path:
                        ctx.set_screenshot_cache(path)
                    return ToolResult.ok_result(r.output, path=path, method="screen_capture")
            return ToolResult.fail(f"не удалось сделать снимок: {fallback_error}")

    @reg.tool("analyze_screen",
              "Спросить Qwen3-VL, что происходит на экране (активное окно, текст, "
              "кнопки, ошибки). Можно сузить область.",
              risk=Risk.NONE, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "question": _prop("string", "Что именно узнать"),
                  "region": _prop("array", "Область [x, y, w, h]"),
                  "fresh": _prop("boolean", "Сделать новый снимок (по умолчанию да)")},
                  "required": []})
    class AnalyzeScreen(Tool):
        async def execute(self, ctx: ToolContext, question: str = "", region: list | None = None,
                          fresh: bool = True) -> ToolResult:
            vision = ctx.service("vision")
            screens = ctx.service("screens")
            if vision is None or screens is None:
                return ToolResult.fail("зрительный конвейер не собран")
            shot = None
            if fresh or not screens.last:
                shot = await screens.capture(region=tuple(region) if region else None, tag="analyze")
            else:
                shot = screens.last
            prompt = question or ("Опиши, что происходит на экране: активное окно, "
                                  "основные элементы, текст, ошибки.")
            text = await vision.describe(shot, prompt)
            return ToolResult.ok_result(text or "(модель не ответила)", path=getattr(shot, "path", ""),
                                        method="vision_model")

    @reg.tool("find_element",
              "Найти элемент интерфейса на экране и вернуть его координаты (для "
              "последующего клика). Понимает описания: «кнопка ОК», «поле поиска», "
              "«зелёная кнопка справа снизу».",
              risk=Risk.NONE, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "target": _prop("string", "Что найти"),
                  "region": _prop("array", "Область поиска [x, y, w, h]"),
                  "limit": _prop("integer", "Сколько кандидатов вернуть")},
                  "required": ["target"]})
    class FindElement(Tool):
        async def execute(self, ctx: ToolContext, target: str, region: list | None = None,
                          limit: int = 5) -> ToolResult:
            vision = ctx.service("vision")
            if vision is None:
                return ToolResult.fail("зрительный конвейер не собран")
            elements = await vision.find_elements(target, region=tuple(region) if region else None,
                                                 limit=int(limit or 5))
            if not elements:
                return ToolResult(ok=False, output=f"Не нашёл на экране: «{target}»",
                                  error="элемент не найден", data={"elements": []})
            best = elements[0]
            lines = [f"Нашёл: {best.label} — центр ({best.center[0]}, {best.center[1]}) "
                     f"[{best.type}, уверенность {best.confidence:.0%}]"]
            for e in elements[1:4]:
                lines.append(f"· альтернатива: {e.label} — ({e.center[0]}, {e.center[1]})")
            return ToolResult.ok_result("\n".join(lines), elements=[e.to_dict() for e in elements],
                                        point={"x": best.center[0], "y": best.center[1]},
                                        method="vision_model")

    @reg.tool("click_element",
              "Найти элемент на экране и кликнуть по нему (зрение → координаты → "
              "мышь → контрольный снимок).",
              risk=Risk.MEDIUM, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "target": _prop("string", "Что нажать"),
                  "region": _prop("array", "Область поиска [x, y, w, h]"),
                  "button": _prop("string", "left | right | middle"),
                  "clicks": _prop("integer", "1 или 2")},
                  "required": ["target"]})
    class ClickElement(Tool):
        async def execute(self, ctx: ToolContext, target: str, region: list | None = None,
                          button: str = "left", clicks: int = 1) -> ToolResult:
            vision = ctx.service("vision")
            inputs = ctx.service("inputs")
            if vision is None or inputs is None:
                return ToolResult.fail("нет зрительного конвейера или подсистемы ввода")
            res = await vision.click_element(target, inputs, region=tuple(region) if region else None,
                                             button=button, clicks=int(clicks or 1))
            if not res.get("ok"):
                return ToolResult.fail(res.get("error") or "не удалось кликнуть",
                                       **(res or {}))
            el = res.get("element") or {}
            out = (f"Нажал «{el.get('label', target)}» в точке {tuple(res.get('click', ()))}"
                   f"{' — экран изменился' if res.get('verified') else ' — экран не изменился'}")
            if res.get("warning"):
                out += f"\n{res['warning']}"
            return ToolResult(ok=bool(res.get("ok")), output=out,
                              error=res.get("warning", ""), data=res, method="vision_click")

    @reg.tool("read_screen",
              "Прочитать содержимое экрана: текст, кнопки, ошибки, поля. Дешевле "
              "полного анализа — отвечает на «что тут написано».",
              risk=Risk.NONE, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "region": _prop("array", "Область [x, y, w, h]"),
                  "ocr": _prop("boolean", "Только распознанный текст (без описания)")},
                  "required": []})
    class ReadScreen(Tool):
        async def execute(self, ctx: ToolContext, region: list | None = None,
                          ocr: bool = False) -> ToolResult:
            reg_tools = ctx.service("registry")
            vision = ctx.service("vision")
            if ocr and reg_tools is not None:
                shot_path = ctx.screenshot_cache()
                if not shot_path and ctx.service("screens") is not None:
                    shot = await ctx.service("screens").capture(tag="ocr")
                    shot_path = shot.path
                if shot_path:
                    r = await reg_tools.call("ocr_image", {"path": shot_path}, ctx)
                    if r.ok:
                        return ToolResult.ok_result(r.output, method="ocr")
            if vision is None:
                return ToolResult.fail("зрительный конвейер не собран")
            text = await vision.read_screen()
            return ToolResult.ok_result(text or "(пусто)", method="vision_model")

    @reg.tool("find_on_screen",
              "Быстрый поиск картинки/шаблона на экране (image → координаты). "
              "Для точных шаблонов; для описаний используйте find_element.",
              risk=Risk.NONE, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "template": _prop("string", "Путь к картинке-шаблону"),
                  "region": _prop("array", "Область поиска")},
                  "required": ["template"]})
    class FindOnScreen(Tool):
        async def execute(self, ctx: ToolContext, template: str, region: list | None = None) -> ToolResult:
            path = Path(template).expanduser()
            if not path.is_file():
                return ToolResult.fail(f"шаблон не найден: {template}")
            try:
                import pyautogui  # type: ignore
            except Exception:
                return ToolResult.fail("поиск по шаблону требует pyautogui")
            try:
                box = pyautogui.locateOnScreen(str(path), confidence=0.9)
            except Exception as exc:                        # noqa: BLE001
                return ToolResult.fail(f"ошибка поиска шаблона: {exc}")
            if not box:
                return ToolResult(ok=False, output="Шаблон не найден на экране.",
                                  error="не найдено")
            center = pyautogui.center(box)
            return ToolResult.ok_result(f"Нашёл шаблон в ({center.x}, {center.y})",
                                        point={"x": center.x, "y": center.y}, method="template_match")

    @reg.tool("click_on_screen",
              "Кликнуть в координаты экрана (в пикселях, не в координатах картинки). "
              "Проверяет, что точка попадает в монитор.",
              risk=Risk.MEDIUM, category="vision", is_gui=True,
              parameters={"type": "object", "properties": {
                  "x": _prop("integer", "X на экране"),
                  "y": _prop("integer", "Y на экране"),
                  "button": _prop("string", "left | right | middle"),
                  "clicks": _prop("integer", "1 или 2")},
                  "required": ["x", "y"]})
    class ClickOnScreen(Tool):
        async def execute(self, ctx: ToolContext, x: int, y: int, button: str = "left",
                          clicks: int = 1) -> ToolResult:
            inputs = ctx.service("inputs")
            if inputs is None:
                reg_tools = ctx.service("registry")
                if reg_tools is None:
                    return ToolResult.fail("нет подсистемы ввода")
                r = await reg_tools.call("mouse_click", {"x": int(x), "y": int(y),
                                                         "button": button, "clicks": int(clicks)}, ctx)
                return r
            res = await inputs.click(int(x), int(y), button=button, clicks=int(clicks))
            if getattr(res, "ok", False):
                return ToolResult.ok_result(f"Кликнул в ({x}, {y}).", point={"x": x, "y": y},
                                            method=getattr(res, "data", {}).get("backend", "input"))
            return ToolResult.fail(getattr(res, "message", "клик не удался"))


__all__ = ["register_vision_agent_tools"]
