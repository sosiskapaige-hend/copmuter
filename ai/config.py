"""Настройки AI-воркера.

Все значения можно переопределить через переменные окружения — так UI/установщик
и тесты настраивают воркер, не переписывая файлы.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_ENDPOINT = "http://127.0.0.1:1234/v1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class WorkerConfig:
    """Конфигурация воркера: модель, лимиты, адрес канала, хранилище."""

    endpoint: str = field(default_factory=lambda: os.environ.get("AGENT_LLM_URL", DEFAULT_ENDPOINT))
    model: str = field(default_factory=lambda: os.environ.get("AGENT_LLM_MODEL", DEFAULT_MODEL))
    api_key: str = field(default_factory=lambda: os.environ.get("AGENT_LLM_KEY", "lm-studio"))
    socket: str = field(
        default_factory=lambda: os.environ.get("AGENT_AI_SOCKET", "")
    )
    state_dir: str = field(
        default_factory=lambda: os.environ.get(
            "AGENT_STATE_DIR", str(Path.home() / ".local" / "share" / "copmuter")
        )
    )
    temperature: float = field(default_factory=lambda: _env_float("AGENT_LLM_TEMPERATURE", 0.2))
    context_tokens: int = field(default_factory=lambda: _env_int("AGENT_CONTEXT_TOKENS", 8192))
    max_tokens: int = field(default_factory=lambda: _env_int("AGENT_MAX_TOKENS", 1024))
    request_timeout: float = field(default_factory=lambda: _env_float("AGENT_LLM_TIMEOUT", 120.0))
    vision_timeout: float = field(default_factory=lambda: _env_float("AGENT_VISION_TIMEOUT", 180.0))
    max_steps: int = field(default_factory=lambda: _env_int("AGENT_MAX_STEPS", 12))
    browser_timeout_ms: int = field(default_factory=lambda: _env_int("AGENT_BROWSER_TIMEOUT", 15000))
    history_limit: int = field(default_factory=lambda: _env_int("AGENT_HISTORY_LIMIT", 24))
    native_tools: bool = field(default_factory=lambda: os.environ.get("AGENT_LLM_TOOLS", "1") != "0")
    log_level: str = field(default_factory=lambda: os.environ.get("AGENT_LOG_LEVEL", "INFO"))
    # Размер кадра, который мы готовы принять от рантайма (снимок экрана идёт
    # отдельным файлом, но на всякий случай ограничиваем base64-полезную нагрузку).
    max_frame_bytes: int = field(default_factory=lambda: _env_int("AGENT_MAX_FRAME", 8 * 1024 * 1024))

    @property
    def socket_path(self) -> str:
        if self.socket:
            return self.socket
        base = Path(self.state_dir)
        return str(base / "agent_ai.sock")

    @property
    def db_path(self) -> Path:
        return Path(self.state_dir) / "agent.db"

    def ensure_dirs(self) -> None:
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_json(cls, payload: str | dict | None) -> "WorkerConfig":
        if not payload:
            return cls()
        data = json.loads(payload) if isinstance(payload, str) else dict(payload)
        cfg = cls()
        for key, value in data.items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        return cfg

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, default=str)
