"""Зрение агента: ScreenshotManager → VisionProcessor → CoordinateMapper (ТЗ §47)."""
from .coordinates import CoordinateMapper, Point, dpi_aware, dpi_scale_for_point
from .elements import Element, VisionProcessor, extract_json
from .screenshot import ScreenshotManager, Shot, crop, png_read, png_write, subsample

__all__ = ["CoordinateMapper", "Point", "dpi_aware", "dpi_scale_for_point",
           "Element", "VisionProcessor", "extract_json",
           "ScreenshotManager", "Shot", "crop", "png_read", "png_write", "subsample"]
