"""Конфигурация агента: JSON-файл + переопределение через переменные окружения.

Приоритет: переменные окружения (AGENT_*) > config.json (рядом с main.py или
в AGENT_HOME) > значения по умолчанию.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env(key: str, default: Any = None) -> Any:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


@dataclass
class LLMConfig:
    provider: str = "openai"                      # openai | mock
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4o"
    max_tokens: int = 1024
    temperature: float = 0.2
    supports_tool_calling: bool = True
    vision: bool = True


@dataclass
class AgentConfig:
    max_iterations: int = 80
    max_retry_per_action: int = 3
    assess_every_n_steps: int = 3
    context_window_messages: int = 40
    ask_user_timeout: float = 300.0
    trash_before_delete: bool = True
    log_level: str = "INFO"
    gui_lock_timeout: float = 120.0


@dataclass
class SafetyConfig:
    mode: str = "confirm"          # auto | confirm | step | observe | plan_only
    confirm_from: str = "medium"   # low | medium | high | critical
    bulk_delete_threshold: int = 10
    auto_approve_timeout: float = 0.0


@dataclass
class ChatConfig:
    history_messages: int = 16        # сколько сообщений уходит в контекст LLM
    max_message_chars: int = 8000     # максимальная длина одного сообщения в контексте
    attach_inline_chars: int = 6000   # сколько символов текстового файла вставлять в контекст


@dataclass
class Config:
    agent_home: Path = field(default_factory=lambda: Path.home() / ".ai-computer-agent")
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    voice: dict = field(default_factory=lambda: {"stt_model": "small", "language": "ru"})
    apps: dict = field(default_factory=dict)
    web_ui: dict = field(default_factory=lambda: {"host": "0.0.0.0", "port": 8710})

    # ---------- каталоги состояния ----------
    @property
    def state_dir(self) -> Path:
        return self.agent_home / "state"

    @property
    def journal_file(self) -> Path:
        return self.state_dir / "journal.json"

    @property
    def memory_dir(self) -> Path:
        return self.agent_home / "memory"

    @property
    def trash_dir(self) -> Path:
        return self.agent_home / "trash"

    @property
    def downloads_dir(self) -> Path:
        return self.agent_home / "downloads"

    @property
    def logs_dir(self) -> Path:
        return self.agent_home / "logs"

    @property
    def chats_dir(self) -> Path:
        return self.agent_home / "chats"

    @property
    def uploads_dir(self) -> Path:
        return self.agent_home / "uploads"

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.memory_dir, self.trash_dir,
                  self.downloads_dir, self.logs_dir, self.chats_dir,
                  self.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- загрузка ----------
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        data: dict = {}
        candidates = []
        if path:
            candidates.append(Path(path))
        candidates += [
            Path(os.environ.get("AGENT_CONFIG", "")) if os.environ.get("AGENT_CONFIG") else None,
            Path.cwd() / "config.json",
            Path(__file__).resolve().parent.parent / "config.json",
            cfg.agent_home / "config.json",
        ]
        for p in candidates:
            if p and Path(p).is_file():
                try:
                    data = json.loads(Path(p).read_text(encoding="utf-8"))
                    break
                except (json.JSONDecodeError, OSError):
                    continue

        def _sec(name: str) -> dict:
            return data.get(name) or {}

        llm = _sec("llm")
        cfg.llm.provider = llm.get("provider", cfg.llm.provider)
        cfg.llm.base_url = llm.get("base_url", cfg.llm.base_url)
        cfg.llm.api_key = llm.get("api_key", cfg.llm.api_key)
        cfg.llm.api_key_env = llm.get("api_key_env", cfg.llm.api_key_env)
        cfg.llm.model = llm.get("model", cfg.llm.model)
        cfg.llm.max_tokens = int(llm.get("max_tokens", cfg.llm.max_tokens))
        cfg.llm.temperature = float(llm.get("temperature", cfg.llm.temperature))
        cfg.llm.supports_tool_calling = bool(llm.get("supports_tool_calling", cfg.llm.supports_tool_calling))
        cfg.llm.vision = bool(llm.get("vision", cfg.llm.vision))

        agent = _sec("agent")
        for k in ("max_iterations", "max_retry_per_action", "assess_every_n_steps",
                  "context_window_messages"):
            if k in agent:
                setattr(cfg.agent, k, int(agent[k]))
        if "ask_user_timeout" in agent:
            cfg.agent.ask_user_timeout = float(agent["ask_user_timeout"])
        if "trash_before_delete" in agent:
            cfg.agent.trash_before_delete = bool(agent["trash_before_delete"])
        if "log_level" in agent:
            cfg.agent.log_level = str(agent["log_level"])

        safety = _sec("safety")
        if "mode" in safety:
            cfg.safety.mode = str(safety["mode"])
        if "confirm_from" in safety:
            cfg.safety.confirm_from = str(safety["confirm_from"])
        if "bulk_delete_threshold" in safety:
            cfg.safety.bulk_delete_threshold = int(safety["bulk_delete_threshold"])

        cfg.voice = {**cfg.voice, **_sec("voice")}
        if _sec("apps"):
            cfg.apps = _sec("apps")
        cfg.web_ui = {**cfg.web_ui, **_sec("web_ui")}

        chat = _sec("chat")
        for k in ("history_messages", "max_message_chars", "attach_inline_chars"):
            if k in chat:
                setattr(cfg.chat, k, int(chat[k]))

        if "agent_home" in data:
            cfg.agent_home = Path(os.path.expanduser(str(data["agent_home"])))

        # --- переменные окружения ---
        if _env("AGENT_HOME"):
            cfg.agent_home = Path(os.path.expanduser(str(_env("AGENT_HOME"))))
        if _env("AGENT_LLM_BASE_URL"):
            cfg.llm.base_url = str(_env("AGENT_LLM_BASE_URL"))
        if _env("AGENT_LLM_MODEL"):
            cfg.llm.model = str(_env("AGENT_LLM_MODEL"))
        if _env("AGENT_LLM_API_KEY"):
            cfg.llm.api_key = str(_env("AGENT_LLM_API_KEY"))
        if _env("AGENT_LLM_PROVIDER"):
            cfg.llm.provider = str(_env("AGENT_LLM_PROVIDER"))
        if _env("AGENT_LLM_API_KEY"):
            cfg.llm.api_key = str(_env("AGENT_LLM_API_KEY"))
        key_from_env = os.environ.get(cfg.llm.api_key_env)
        if key_from_env:
            cfg.llm.api_key = key_from_env

        # mock только если провайдер не был выбран явно; LM Studio ключа не требует
        if (cfg.llm.provider == "openai" and not cfg.llm.api_key
            and "provider" not in llm and not _env("AGENT_LLM_PROVIDER")):
            cfg.llm.provider = "mock"

        cfg.ensure_dirs()
        return cfg

    def to_dict(self) -> dict:
        return {
            "agent_home": str(self.agent_home),
            "llm": {
                "provider": self.llm.provider,
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "api_key": "***" if self.llm.api_key else "",
            },
            "safety_mode": self.safety.mode,
        }
