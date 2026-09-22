"""Зрение: кадр → элементы/координаты (ТЗ §14).

Важно: координаты модели (0..1000) — это НЕ координаты мыши. Здесь они
пересчитываются в экранные с учётом области кадра, масштаба и DPI, и
проверяются на здравый смысл до того, как рантайм двинет курсор.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import prompts
from .llm import LLMError, LMStudioClient

log = logging.getLogger("ai.vision")

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Действия, для которых зрение запрещено: их делают API/горячие клавиши (ТЗ §15).
NEVER_VISION = {
    "ctrl+l", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+s", "alt+tab", "win+d", "win+r",
    "ctrl+shift+esc", "alt+f4", "ctrl+t", "ctrl+w",
}


def vision_allowed(action: str) -> bool:
    """Нужно ли вообще смотреть на экран для этого действия."""
    return action.strip().lower() not in NEVER_VISION


@dataclass
class Element:
    label: str
    bbox: list[int] = field(default_factory=lambda: [0, 0, 0, 0])   # нормализованные 0..1000
    kind: str = "element"
    confidence: float = 0.0
    how: str = ""

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "bbox": list(self.bbox),
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
        }


def extract_json(text: str) -> dict | None:
    """Достать JSON из ответа модели (снимает ```-обёртки и лишний текст)."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = JSON_BLOCK.search(cleaned)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def normalize_bbox(raw, width: int = 0, height: int = 0) -> list[int]:
    """bbox от модели → нормализованные 0..1000.

    Модель отвечает по-разному: долями (0..1), пикселями кадра или уже 0..1000.
    Все три случая приводим к одному — иначе клик уедет не туда.

    Промпт просит координаты 0..1000, поэтому неоднозначные маленькие значения
    трактуются как нормализованные. Пиксели распознаём по выходу за 1000.
    """
    if isinstance(raw, dict):   # {"x1":…} тоже встречается
        raw = [raw.get("x1", raw.get("left", 0)), raw.get("y1", raw.get("top", 0)),
               raw.get("x2", raw.get("right", 0)), raw.get("y2", raw.get("bottom", 0))]
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return [0, 0, 0, 0]
    try:
        values = [float(item) for item in raw[:4]]
    except (TypeError, ValueError):
        return [0, 0, 0, 0]
    largest = max(values)
    if 0 < largest <= 1.0:
        values = [value * 1000.0 for value in values]
    elif largest > 1000 and width > 0 and height > 0:
        values = [values[0] * 1000.0 / width, values[1] * 1000.0 / height,
                  values[2] * 1000.0 / width, values[3] * 1000.0 / height]
    return [max(0, min(1000, int(round(value)))) for value in values]


def _clamp_bbox(raw) -> list[int]:
    """Совместимость: то же приведение без знания размеров кадра."""
    return normalize_bbox(raw)


def parse_elements(text: str) -> list[Element]:
    """Ответ модели → список элементов. Мусор отбрасывается, а не «исправляется»."""
    data = extract_json(text)
    if not data:
        return []
    raw_items = data.get("elements") if isinstance(data, dict) else None
    if raw_items is None and isinstance(data, dict) and data.get("label"):
        raw_items = [data]
    if not isinstance(raw_items, list):
        return []
    out: list[Element] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        bbox = _clamp_bbox(item.get("bbox") or item.get("box"))
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            Element(
                label=str(item.get("label") or item.get("text") or "")[:120],
                bbox=bbox,
                kind=str(item.get("kind") or "element")[:32],
                confidence=max(0.0, min(1.0, confidence)),
                how=str(item.get("how") or "")[:200],
            )
        )
    return out


@dataclass
class FrameGeometry:
    """Геометрия кадра: где он находится на экране и как масштабирован."""

    width: int
    height: int
    origin_x: int = 0
    origin_y: int = 0
    scale: float = 1.0          # экранные пиксели / пиксели кадра
    dpi_scale: float = 1.0

    def normalized_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """0..1000 (как отдаёт модель) → координаты экрана."""
        px = self.origin_x + int(round((x / 1000.0) * self.width * self.scale))
        py = self.origin_y + int(round((y / 1000.0) * self.height * self.scale))
        return px, py

    def is_reasonable(self, x: int, y: int) -> bool:
        """Отсечение «галлюцинированных» координат до движения мыши."""
        sx, sy = self.normalized_to_screen(x, y)
        if 0 <= x <= 1000 and 0 <= y <= 1000:
            return True
        log.debug("координаты вне диапазона: %s,%s → %s,%s", x, y, sx, sy)
        return False

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "scale": self.scale,
            "dpi_scale": self.dpi_scale,
        }


def similarity(a: str, b: str) -> float:
    """Грубое сравнение подписи элемента с целью (без внешних зависимостей)."""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def pick_best(elements: list[Element], target: str, min_score: float = 0.33) -> Element | None:
    """Лучший элемент под описание цели (учитываем и подпись, и уверенность модели)."""
    if not elements:
        return None
    scored = [(similarity(target, e.label) * 0.7 + e.confidence * 0.3, e) for e in elements]
    scored.sort(key=lambda item: item[0], reverse=True)
    score, best = scored[0]
    if score < min_score:
        log.info("зрение: ничего похожего на «%s» (лучший %.2f: %s)", target, score, best.label)
        return None
    return best


class VisionService:
    """Обёртка над моделью зрения: описать экран, найти элемент, прочитать текст."""

    def __init__(self, client: LMStudioClient, *, timeout: float = 180.0, metrics=None) -> None:
        self.client = client
        self.timeout = timeout
        self.metrics = metrics
        self.calls = 0

    def _complete(self, prompt: str, frame: bytes, *, system: str = prompts.VISION,
                  max_tokens: int = 512) -> str:
        self.calls += 1
        try:
            reply = self.client.vision(
                prompt,
                frame,
                system=system,
                max_tokens=max_tokens,
                timeout=self.timeout,
            )
        except LLMError as exc:
            log.warning("зрение недоступно: %s", exc)
            raise
        if self.metrics is not None:
            try:
                self.metrics.vision_call(reply.ms)
            except Exception:  # pragma: no cover - метрики не критичны
                pass
        return reply.content

    def describe(self, frame: bytes, question: str = "Что на экране?") -> tuple[str, list[Element]]:
        text = self._complete(question, frame)
        return text, parse_elements(text)

    def find(self, frame: bytes, target: str) -> Element | None:
        text = self._complete(prompts.VISION_FIND + f"\nОписание элемента: {target}", frame,
                              max_tokens=256)
        data = extract_json(text)
        if data and data.get("found") is False:
            return None
        elements = parse_elements(text)
        return pick_best(elements, target) if elements else None

    def read(self, frame: bytes) -> str:
        return self._complete(prompts.VISION_READ, frame, system=prompts.VISION_READ, max_tokens=1024)


def click_call(element: Element, geometry: FrameGeometry, *, button: str = "left",
               clicks: int = 1) -> dict:
    """Элемент + геометрия → вызов инструмента рантайма (координаты уже экранные)."""
    bbox = normalize_bbox(element.bbox, geometry.width, geometry.height)
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError(f"модель вернула пустую область: {element.bbox}")
    nx = (bbox[0] + bbox[2]) // 2
    ny = (bbox[1] + bbox[3]) // 2
    if not geometry.is_reasonable(nx, ny):
        raise ValueError(f"координаты модели вне диапазона: {element.bbox}")
    sx, sy = geometry.normalized_to_screen(nx, ny)
    return {
        "tool": "click",
        "args": {"x": sx, "y": sy, "button": button, "clicks": clicks},
        "note": f"Кликаю «{element.label}»",
        "data": {"bbox": element.bbox, "screen": [sx, sy], "label": element.label},
    }
