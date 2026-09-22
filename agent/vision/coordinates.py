"""Координаты: изображение → экран → физическая мышь (ТЗ §48, §49).

Главное правило: **нельзя считать, что пиксель картинки = координата мыши.**
Между ними стоят:

  * масштабирование снимка (мы уменьшаем картинку для модели);
  * область захвата (кроп/монитор, смещение в виртуальном рабочем столе);
  * DPI-масштаб Windows (125 %, 150 % — и на разных мониторах разный);
  * мультимониторная раскладка (монитор 2 может быть левее монитора 1).

CoordinateMapper делает преобразование явным и валидируемым: перед кликом
проверяется, что точка попадает в один из мониторов, иначе клик не выполняется
(лучше честная ошибка, чем клик «в никуда»).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from .screenshot import Shot


def dpi_aware() -> bool:
    """Делает процесс DPI-aware (Windows), чтобы координаты совпадали с пикселями."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def dpi_scale_for_point(x: int, y: int) -> float:
    """Масштаб монитора, на котором находится точка (Windows, если доступно)."""
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT(int(x), int(y))
        MONITOR_DEFAULTTONEAREST = 2
        hmon = ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)  # type: ignore[attr-defined]
        try:
            shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
            dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
            if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                return round(dpi_x.value / 96.0, 3)
        except Exception:
            pass
    except Exception:
        pass
    return 1.0


@dataclass
class Point:
    x: int
    y: int

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y}


class CoordinateMapper:
    """Преобразование координат конкретного снимка в координаты мыши."""

    def __init__(self, shot: Shot, monitors: Iterable[dict] | None = None,
                 dpi_scale: float | None = None) -> None:
        self.shot = shot
        self.monitors = list(monitors or [])
        self.dpi_scale = float(dpi_scale if dpi_scale is not None else shot.dpi_scale or 1.0)
        self.scale = float(shot.scale or 1.0)

    # -------------------------------------------------- изображение → экран
    def image_to_screen(self, x: float, y: float, use_dpi: bool = False) -> Point:
        """Пиксели изображения → координаты экрана (то, что принимает мышь)."""
        sx = self.shot.origin_x + float(x) / max(self.scale, 1e-6)
        sy = self.shot.origin_y + float(y) / max(self.scale, 1e-6)
        if use_dpi and self.dpi_scale and self.dpi_scale != 1.0:
            sx *= self.dpi_scale
            sy *= self.dpi_scale
        return Point(int(round(sx)), int(round(sy)))

    def normalized_to_screen(self, nx: float, ny: float, mode: str = "0-1000",
                             use_dpi: bool = False) -> Point:
        """Нормализованные координаты модели (Qwen-VL: 0..1000) → экран."""
        div = 1000.0 if mode == "0-1000" else 1.0
        x = float(nx) / div * self.shot.width
        y = float(ny) / div * self.shot.height
        return self.image_to_screen(x, y, use_dpi=use_dpi)

    def bbox_to_screen(self, bbox: list[float], mode: str = "0-1000") -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = (list(bbox) + [0, 0, 0, 0])[:4]
        p1 = self.normalized_to_screen(x1, y1, mode)
        p2 = self.normalized_to_screen(x2, y2, mode)
        left, right = sorted((p1.x, p2.x))
        top, bottom = sorted((p1.y, p2.y))
        return left, top, right, bottom

    def bbox_center(self, bbox: list[float], mode: str = "0-1000") -> Point:
        left, top, right, bottom = self.bbox_to_screen(bbox, mode)
        return Point(int((left + right) / 2), int((top + bottom) / 2))

    # -------------------------------------------------- экран → изображение
    def screen_to_image(self, x: float, y: float) -> Point:
        ix = (float(x) - self.shot.origin_x) * self.scale
        iy = (float(y) - self.shot.origin_y) * self.scale
        return Point(int(round(ix)), int(round(iy)))

    def region_in_image(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        """Область экрана → пиксели изображения (для кропа)."""
        p1 = self.screen_to_image(x, y)
        p2 = self.screen_to_image(x + w, y + h)
        return p1.x, p1.y, max(1, p2.x - p1.x), max(1, p2.y - p1.y)

    # -------------------------------------------------- валидация
    def virtual_desktop(self) -> dict:
        if self.monitors:
            left = min(int(m.get("left", 0)) for m in self.monitors)
            top = min(int(m.get("top", 0)) for m in self.monitors)
            right = max(int(m.get("left", 0)) + int(m.get("width", 0)) for m in self.monitors)
            bottom = max(int(m.get("top", 0)) + int(m.get("height", 0)) for m in self.monitors)
            return {"left": left, "top": top, "right": right, "bottom": bottom,
                    "width": right - left, "height": bottom - top}
        return {"left": 0, "top": 0, "right": self.shot.width, "bottom": self.shot.height,
                "width": self.shot.width, "height": self.shot.height}

    def monitor_of(self, point: Point) -> dict | None:
        for m in self.monitors:
            if (int(m.get("left", 0)) <= point.x < int(m.get("left", 0)) + int(m.get("width", 0))
                    and int(m.get("top", 0)) <= point.y < int(m.get("top", 0)) + int(m.get("height", 0))):
                return m
        return None

    def validate(self, point: Point) -> tuple[bool, str]:
        vd = self.virtual_desktop()
        if not (vd["left"] <= point.x < vd["right"] and vd["top"] <= point.y < vd["bottom"]):
            return False, (f"точка ({point.x},{point.y}) вне рабочего стола "
                           f"[{vd['left']},{vd['top']}..{vd['right']},{vd['bottom']}]")
        if self.monitors and self.monitor_of(point) is None:
            return False, f"точка ({point.x},{point.y}) не попадает ни в один монитор"
        return True, ""

    def clamp(self, point: Point) -> Point:
        vd = self.virtual_desktop()
        return Point(min(max(point.x, vd["left"]), vd["right"] - 1),
                     min(max(point.y, vd["top"]), vd["bottom"] - 1))

    def explain(self, point: Point) -> dict:
        m = self.monitor_of(point)
        ok, why = self.validate(point)
        return {"point": point.to_dict(), "monitor": (m or {}).get("index"),
                "dpi_scale": self.dpi_scale, "image_scale": self.scale,
                "origin": {"x": self.shot.origin_x, "y": self.shot.origin_y},
                "valid": ok, "reason": why}

    @staticmethod
    def sanity_check_image_coords(x: float, y: float, shot: Shot) -> tuple[bool, str]:
        """Ловит типичную ошибку модели: координаты «в пикселях 1920x1080», а не в 0..1000."""
        if 0 <= x <= shot.width and 0 <= y <= shot.height and (x > 1000 or y > 1000) \
                and shot.width <= 1000 and shot.height <= 1000:
            return False, "похоже, координаты заданы в пикселях, а не нормализованы (0..1000)"
        if x < 0 or y < 0:
            return False, "отрицательные координаты"
        return True, ""
