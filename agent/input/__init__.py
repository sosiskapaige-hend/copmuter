"""Ввод: контроллер мыши/клавиатуры с блокировкой и «буфер-первым» вводом текста."""
from .controller import (AGENT_INPUT_LOCK, InputController, InputResult,
                         VirtualInputBackend, pick_backend)

__all__ = ["AGENT_INPUT_LOCK", "InputController", "InputResult",
           "VirtualInputBackend", "pick_backend"]
