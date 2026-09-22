"""Состояние системы: снимок ПК (state) и ожидания (wait)."""
from .state import ComputerState, WindowInfo
from .wait import TIMEOUTS, WaitManager, WaitResult

__all__ = ["ComputerState", "WindowInfo", "TIMEOUTS", "WaitManager", "WaitResult"]
