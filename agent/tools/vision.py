"""Восприятие экрана: не просто OCR.

screen_describe — VLM-анализ (LLM с vision) последнего скриншота: понимает
текст, кнопки, поля, окна, контекст. ocr_image/ocr_file — точный текст
через tesseract (если установлен).
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry

_VLM_PROMPT = (
    "Ты — зрительный модуль AI-агента, который управляет компьютером. "
    "Это скриншот экрана пользователя. Опиши:\n"
    "1. Открытые окна и приложения (по заголовкам).\n"
    "2. Все видимые текстовые элементы и их примерные позиции (левый верхний / центр / ...).\n"
    "3. Кликабельные элементы: кнопки, поля ввода, ссылки, меню, чекбоксы — с подписями.\n"
    "4. Состояние: ошибки, диалоговые окна, прогресс-бары, что происходит.\n"
    "5. Если в вопросе есть конкретная задача — дай координатные подсказки в процентах экрана "
    "(x%:y% от левого верхнего угла) для ключевых элементов.\n"
    "Отвечай структурно, по-русски, кратко.\n\nВопрос: "
)


def _ocr_cmd(path: str, lang: str = "rus+eng") -> str | None:
    if not subprocess.run(["which", "tesseract"], capture_output=True).returncode == 0:
        return None
    r = subprocess.run(["tesseract", path, "-", "-l", lang],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None
    return r.stdout


def register_vision_tools(reg: ToolRegistry) -> None:

    @reg.tool("screen_describe",
              "УМНОЕ понимание последнего скриншота (VLM): что на экране, где кнопки и поля, "
              "какие окна открыты, есть ли ошибки. Используйте ДО GUI-действий, чтобы «увидеть».",
              risk=Risk.NONE, category="vision",
              parameters={"type": "object", "properties": {
                  "question": _prop("string", "Что именно понять/найти на экране (напр. 'где кнопка Login?')")},
                  "required": ["question"]})
    class ScreenDescribe(Tool):
        async def execute(self, ctx: ToolContext, question: str) -> ToolResult:
            path = ctx.screenshot_cache()
            if not path or not os.path.exists(path):
                return ToolResult.fail("нет скриншота — сначала screen_capture")
            if not ctx.platform.has_display:
                return ToolResult.ok_result(
                    "(headless: реальный экран недоступен, скриншот синтетический. "
                    f"VLM-ответ будет формальным.) Вопрос: {question}")
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            answer = await ctx.llm.describe_image(b64, _VLM_PROMPT + question)
            return ToolResult.ok_result(answer, image=path)

    @reg.tool("ocr_image", "Распознать текст на изображении (tesseract).",
              risk=Risk.NONE, category="vision",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к изображению (по умолчанию — последний скриншот)"),
                  "lang": _prop("string", "Язык tesseract (по умолчанию rus+eng)")},
                  "required": []})
    class OcrImage(Tool):
        async def execute(self, ctx: ToolContext, path: str = "", lang: str = "rus+eng") -> ToolResult:
            p = path or ctx.screenshot_cache()
            if not p or not os.path.exists(p):
                return ToolResult.fail("укажите path или сначала screen_capture")
            text = _ocr_cmd(p, lang)
            if text is None:
                return ToolResult.fail("tesseract не установлен. "
                                       "Альтернатива: screen_describe (VLM) или pip install pytesseract + apt install tesseract-ocr")
            return ToolResult.ok_result(text.strip() or "(текст не распознан)")

    @reg.tool("ocr_file", "Распознать текст в файле-изображении (для «посмотри фото/скан»).",
              risk=Risk.NONE, category="vision",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу")}, "required": ["path"]})
    class OcrFile(Tool):
        async def execute(self, ctx: ToolContext, path: str) -> ToolResult:
            p = Path(os.path.expanduser(path))
            if not p.exists():
                return ToolResult.fail(f"файл не найден: {p}")
            text = _ocr_cmd(str(p))
            if text is None:
                return ToolResult.fail("tesseract не установлен")
            return ToolResult.ok_result(text.strip() or "(текст не распознан)")

    @reg.tool("image_info", "Метаданные изображения: размер, формат, EXIF-дата (если есть).",
              risk=Risk.NONE, category="vision",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к файлу")}, "required": ["path"]})
    class ImageInfo(Tool):
        async def execute(self, ctx: ToolContext, path: str) -> ToolResult:
            p = Path(os.path.expanduser(path))
            if not p.exists():
                return ToolResult.fail(f"файл не найден: {p}")
            try:
                from PIL import Image  # type: ignore
                im = Image.open(p)
                exif_date = ""
                try:
                    ex = im.getexif()
                    if 36867 in ex:  # DateTimeOriginal
                        exif_date = f", EXIF: {ex[36867]}"
                except Exception:
                    pass
                return ToolResult.ok_result(f"{p.name}: {im.size[0]}x{im.size[1]}, {im.format}{exif_date}, "
                                            f"{p.stat().st_size} Б")
            except ImportError:
                st = p.stat()
                return ToolResult.ok_result(f"{p.name}: {st.st_size} Б (Pillow не установлен — детали недоступны)")
            except Exception as e:
                return ToolResult.fail(f"не удалось прочитать: {e}")

    @reg.tool("image_select_best",
              "Выбрать «лучшие» изображения из папки по эвристикам (разрешение, EXIF-дата, "
              "размер файла) — для «посмотри папку с фото и выбери лучшие».",
              risk=Risk.NONE, category="vision",
              parameters={"type": "object", "properties": {
                  "dir": _prop("string", "Папка с изображениями"),
                  "limit": _prop("integer", "Сколько выбрать (по умолчанию 5)")},
                  "required": ["dir"]})
    class ImageSelectBest(Tool):
        async def execute(self, ctx: ToolContext, dir: str, limit: int = 5) -> ToolResult:
            d = Path(os.path.expanduser(dir))
            if not d.is_dir():
                return ToolResult.fail(f"папка не найдена: {d}")
            exts = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}
            scored: list[tuple[float, str, str]] = []
            for f in d.iterdir():
                if f.suffix.lower() not in exts:
                    continue
                try:
                    st = f.stat()
                    score = min(st.st_size / 1048576, 20)  # размер как代理 качества (до 20МБ)
                    if ctx.platform.has_pillow:
                        from PIL import Image  # type: ignore
                        with Image.open(f) as im:
                            score += min(im.size[0] * im.size[1] / 4_000_000, 10)
                    scored.append((score, str(f), f"{st.st_size//1024} КБ"))
                except Exception:
                    continue
            scored.sort(reverse=True)
            if not scored:
                return ToolResult.ok_result(f"в {d} нет изображений")
            lines = [f"ТОП-{min(limit, len(scored))} по эвристике (разрешение+вес):"]
            for s, p, sz in scored[:limit]:
                lines.append(f"  {p}  ({sz}, score {s:.1f})")
            lines.append("Для художественного выбора (красивость) используйте screen_describe/VLM по каждому фото.")
            return ToolResult.ok_result("\n".join(lines), selected=[p for _, p, _ in scored[:limit]])
