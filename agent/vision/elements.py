"""Зрение: найти элемент на экране и кликнуть по нему (ТЗ §15, §17, §47).

Конвейер ровно как в ТЗ:

    screenshot → (кроп нужной области) → Qwen3-VL → элементы с координатами
    → проверка координат (CoordinateMapper) → клик мышью → контрольный снимок
    → вердикт «изменилось / не изменилось».

Vision — самый медленный способ управления, поэтому он используется только
тогда, когда интерфейс неизвестен (нет API/CLI/горячей клавиши). Всё, что
можно сделать напрямую, делается напрямую.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..apps.aliases import similarity
from .coordinates import CoordinateMapper, Point
from .screenshot import ScreenshotManager, Shot

ELEMENT_PROMPT = (
    "Ты — зрительный модуль агента, который управляет компьютером. "
    "Перед тобой скриншот экрана пользователя.\n"
    "Найди элементы интерфейса и верни СТРОГО JSON без пояснений:\n"
    '{"screen_summary": "что происходит на экране (1-2 предложения)", '
    '"elements": [{"label": "название элемента", "type": "button|input|link|menu|checkbox|icon|text", '
    '"bbox": [x1, y1, x2, y2], "confidence": 0.0-1.0}]}\n'
    "Координаты bbox — НОРМАЛИЗОВАННЫЕ от 0 до 1000 относительно изображения "
    "(x1,y1 — левый верхний угол, x2,y2 — правый нижний).\n"
    "Если нужного элемента нет — верни пустой список elements."
)

READ_PROMPT = (
    "Опиши, что видно на экране: активное окно/приложение, видимый текст, кнопки, "
    "поля ввода, ошибки и диалоги. Кратко, по делу, на русском."
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass
class Element:
    label: str
    bbox: list[float] = field(default_factory=list)   # нормализованные 0..1000
    confidence: float = 0.0
    type: str = "unknown"
    score: float = 0.0                                 # совпадение с запросом
    center: tuple[int, int] | None = None              # координаты экрана
    monitor: int | None = None
    valid: bool = True
    reason: str = ""

    def to_dict(self) -> dict:
        return {"label": self.label, "type": self.type, "bbox": self.bbox,
                "confidence": self.confidence, "score": round(self.score, 3),
                "center": self.center, "monitor": self.monitor,
                "valid": self.valid, "reason": self.reason}


def extract_json(text: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели (с фенсом, лишним текстом и т.п.)."""
    if not text:
        return None
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        for attempt in (cand, cand.replace("'", '"')):
            try:
                data = json.loads(attempt)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    return None


class VisionProcessor:
    def __init__(self, llm: Any, screens: ScreenshotManager, state: Any = None,
                 log: Any = None, metrics: Any = None) -> None:
        self.llm = llm
        self.screens = screens
        self.state = state
        self.log = log
        self.metrics = metrics

    # ------------------------------------------------------------ низкий уровень
    def _b64(self, shot: Shot) -> str:
        with open(shot.path, "rb") as f:
            return base64.b64encode(f.read()).decode()

    async def analyze(self, shot: Shot, prompt: str, max_pixels: int | None = None) -> tuple[str, dict | None]:
        """Отправить снимок модели и вернуть (текст, распарсенный JSON если есть)."""
        if self.llm is None:
            # Модель не подключена — не выдумываем результат, честно сообщаем наверх.
            if self.log is not None:
                self.log.warn("Зрение недоступно: модель не подключена")
            return "", None
        if max_pixels and shot.width * shot.height > max_pixels:
            shot = await self.screens.capture(max_pixels=max_pixels, monitor=shot.monitor,
                                              tag="vision")
        import asyncio
        t0 = asyncio.get_running_loop().time()
        text = await self.llm.describe_image(self._b64(shot), prompt)
        ms = (asyncio.get_running_loop().time() - t0) * 1000
        if self.metrics is not None:
            self.metrics.vision_call(ms, ok=bool(text))
        if self.log is not None:
            self.log.event("vision", ms=round(ms, 1), prompt=prompt[:120],
                           chars=len(text or ""), shot=shot.path)
        return text or "", extract_json(text or "")

    async def describe(self, shot: Shot | None = None, prompt: str = "", max_pixels: int | None = None) -> str:
        shot = shot or self.screens.last or await self.screens.capture()
        text, _ = await self.analyze(shot, prompt or READ_PROMPT, max_pixels)
        return text

    async def read_screen(self, shot: Shot | None = None) -> str:
        return await self.describe(shot, READ_PROMPT)

    # ------------------------------------------------------------ поиск элементов
    async def find_elements(self, target: str = "", shot: Shot | None = None,
                            region: tuple[int, int, int, int] | None = None,
                            max_pixels: int | None = None,
                            limit: int = 12) -> list[Element]:
        """Все видимые элементы с координатами (при region — только в этой области)."""
        shot = shot or self.screens.last or await self.screens.capture()
        mapper = self._mapper(shot, region)
        shot_for_model = shot
        if region:
            x, y, w, h = mapper.region_in_image(*region)
            cropped = self.screens.crop_last(x, y, w, h)
            if cropped is not None:
                shot_for_model = cropped
                mapper = CoordinateMapper(cropped, self._monitors(), shot.dpi_scale)
        prompt = ELEMENT_PROMPT
        if target:
            prompt += (f"\nПользователь ищет: «{target}». "
                       f"Обрати внимание на этот элемент и элементы рядом.")
        text, data = await self.analyze(shot_for_model, prompt, max_pixels)
        items = []
        if isinstance(data, dict):
            items = data.get("elements") or []
            if not isinstance(items, list):
                items = []
        if not items and not data:
            # модель не вернула JSON — работаем по тексту (запасной путь)
            return []
        out: list[Element] = []
        for it in items[: max(1, limit)]:
            if not isinstance(it, dict):
                continue
            bbox = it.get("bbox") or it.get("box") or it.get("bbox_2d") or []
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            try:
                bbox = [float(v) for v in list(bbox)[:4]]
            except (TypeError, ValueError):
                continue
            bbox = self._normalize_bbox(bbox, shot_for_model)
            el = Element(label=str(it.get("label") or it.get("name") or "")[:120],
                         bbox=bbox, type=str(it.get("type") or "unknown"),
                         confidence=float(it.get("confidence") or it.get("score") or 0.5))
            el.score = similarity(target, el.label) if target else el.confidence
            center = mapper.bbox_center(bbox)
            ok, why = mapper.validate(center)
            el.center, el.valid, el.reason = (center.x, center.y), ok, why
            m = mapper.monitor_of(center)
            el.monitor = (m or {}).get("index")
            out.append(el)
        if target:
            out.sort(key=lambda e: -(e.score * 0.7 + e.confidence * 0.3))
        return out

    @staticmethod
    def _normalize_bbox(bbox: list[float], shot: Shot) -> list[float]:
        """Приводит bbox к нормализованному виду 0..1000 (модели любят пиксели)."""
        mx = max(bbox[0], bbox[2])
        my = max(bbox[1], bbox[3])
        if mx <= 1000 and my <= 1000 and (shot.width > 1000 or shot.height > 1000 or mx > 100):
            return bbox
        w = max(1, shot.width)
        h = max(1, shot.height)
        return [bbox[0] / w * 1000, bbox[1] / h * 1000, bbox[2] / w * 1000, bbox[3] / h * 1000]

    async def find_best(self, target: str, shot: Shot | None = None,
                        region: tuple[int, int, int, int] | None = None,
                        min_score: float = 0.33) -> Element | None:
        els = await self.find_elements(target, shot=shot, region=region)
        if not els:
            return None
        best = els[0]
        if best.score >= min_score and best.valid:
            return best
        # допускаем уверенный ответ модели даже при слабом совпадении названия
        if best.confidence >= 0.75 and best.valid:
            return best
        return None

    # ------------------------------------------------------------ клик + проверка
    async def click_element(self, target: str, inputs: Any, shot: Shot | None = None,
                            region: tuple[int, int, int, int] | None = None,
                            verify: bool = True, button: str = "left",
                            clicks: int = 1) -> dict:
        before = shot or self.screens.last or await self.screens.capture()
        el = await self.find_best(target, shot=before, region=region)
        if el is None or el.center is None:
            return {"ok": False, "error": f"не нашёл на экране элемент «{target}»",
                    "target": target}
        res = await inputs.click(el.center[0], el.center[1], button=button, clicks=clicks)
        out = {"ok": bool(res.ok), "element": el.to_dict(), "click": el.center,
               "message": res.message}
        if res.ok and verify:
            import asyncio
            await asyncio.sleep(0.25)
            after = await self.screens.capture(tag="verify", max_pixels=self.screens.max_pixels)
            diff = self.screens.difference(before, after)
            out["changed"] = diff
            out["verified"] = diff > 0.002
            if not out["verified"]:
                out["warning"] = ("экран не изменился после клика — возможно, промах "
                                  "или элемент не реагирует")
        return out

    # ------------------------------------------------------------ служебное
    def _monitors(self) -> list[dict]:
        if self.state is not None:
            try:
                return self.state.monitors()
            except Exception:
                return []
        return []

    def _mapper(self, shot: Shot, region: tuple[int, int, int, int] | None) -> CoordinateMapper:
        return CoordinateMapper(shot, self._monitors(), shot.dpi_scale)

    def capabilities(self) -> dict:
        return {"vision_model": getattr(self.llm, "name", "?"),
                "screens": bool(self.screens), "max_pixels": self.screens.max_pixels,
                "dpi_scale": getattr(self.state, "dpi_scale", 1.0)}
