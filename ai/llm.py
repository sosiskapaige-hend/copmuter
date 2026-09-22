"""Клиент LM Studio (OpenAI-совместимый API) — постоянное соединение.

Ключевые требования ТЗ:
  * модель локальная (LM Studio + Qwen3-VL), облако не обязательно;
  * соединение тёплое: HTTP keep-alive, переподключение только по факту обрыва;
  * preflight при старте: сервер доступен, нужная модель есть, vision и
    tool calling работают.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("ai.llm")


class LLMError(RuntimeError):
    """Ошибка обращения к модели (сеть, формат, отказ сервера)."""


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[dict] = None  # type: ignore[assignment]
    usage: dict | None = None
    ms: float = 0.0
    finish_reason: str = ""

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


class LMStudioClient:
    """Минимальный клиент: /chat/completions, /models, vision через image_url."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        api_key: str = "lm-studio",
        timeout: float = 120.0,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> None:
        parts = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.secure = parts.scheme == "https"
        self.base_path = (parts.path or "/v1").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._conn: http.client.HTTPConnection | None = None
        self._calls = 0
        self._errors = 0
        self._last_ms = 0.0

    # ------------------------------------------------------------------ служебное
    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
            self._conn = cls(self.host, self.port, timeout=self.timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:  # pragma: no cover - закрытие уже закрытого
                pass
            self._conn = None

    def reconnect(self) -> None:
        """Явный сброс соединения: вызывается после ошибки ввода-вывода."""
        self.close()

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        attempts = 2
        last_error: Exception | None = None
        for attempt in range(attempts):
            conn = self._connection()
            if timeout is not None:
                conn.timeout = timeout
            try:
                conn.request(method, f"{self.base_path}{path}", body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                if response.status >= 400:
                    text = raw.decode("utf-8", "replace")[:400]
                    raise LLMError(f"модель вернула HTTP {response.status}: {text}")
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                self.reconnect()
                if attempt + 1 == attempts:
                    raise LLMError(f"нет связи с LM Studio ({self.host}:{self.port}): {exc}") from exc
                time.sleep(0.2)
            except json.JSONDecodeError as exc:
                raise LLMError(f"ответ модели не JSON: {exc}") from exc
        raise LLMError(str(last_error) if last_error else "неизвестная ошибка модели")

    # ------------------------------------------------------------------ запросы
    def models(self) -> list[str]:
        data = self._request("GET", "/models")
        return [str(item.get("id", "")) for item in data.get("data", []) if item.get("id")]

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        started = time.perf_counter()
        data = self._request("POST", "/chat/completions", payload, timeout=timeout)
        self._calls += 1
        self._last_ms = (time.perf_counter() - started) * 1000.0
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("модель не вернула ни одного варианта ответа")
        message = choices[0].get("message") or {}
        reply = LLMReply(
            content=(message.get("content") or "").strip(),
            tool_calls=list(message.get("tool_calls") or []),
            usage=data.get("usage"),
            ms=self._last_ms,
            finish_reason=str(choices[0].get("finish_reason") or ""),
        )
        return reply

    def vision(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        image_path: str | None = None,
        mime: str = "image/png",
        system: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMReply:
        """Кадр → модель зрения. Картинка идёт как data-URI (без файлов на диске)."""
        if image_bytes is None and image_path:
            with open(image_path, "rb") as handle:
                image_bytes = handle.read()
        if image_bytes is None:
            raise LLMError("не переданы данные кадра")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }
        )
        return self.chat(messages, max_tokens=max_tokens, timeout=timeout, temperature=0.1)

    # ------------------------------------------------------------------ preflight
    def preflight(self, *, need_vision: bool = True, need_tools: bool = True) -> dict:
        """Проверка готовности модели (ТЗ §46): что именно не так — видно сразу."""
        report: dict[str, Any] = {
            "endpoint": f"{self.host}:{self.port}",
            "model": self.model,
            "reachable": False,
            "model_present": False,
            "tools_ok": False,
            "vision_ok": False,
            "ms": 0.0,
            "problems": [],
        }
        started = time.perf_counter()
        try:
            names = self.models()
            report["reachable"] = True
            report["models"] = names
        except LLMError as exc:
            report["problems"].append(str(exc))
            report["ms"] = (time.perf_counter() - started) * 1000.0
            return report
        model = self.model.lower()
        report["model_present"] = any(
            model == name.lower() or model in name.lower() or name.lower() in model for name in names
        )
        if not report["model_present"]:
            report["problems"].append(
                f"модель «{self.model}» не найдена в LM Studio (есть: {', '.join(names) or '—'})"
            )
            report["ms"] = (time.perf_counter() - started) * 1000.0
            return report
        if need_tools:
            try:
                probe_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": "ping",
                            "description": "Проверка вызова инструмента",
                            "parameters": {
                                "type": "object",
                                "properties": {"echo": {"type": "string"}},
                                "required": ["echo"],
                            },
                        },
                    }
                ]
                reply = self.chat(
                    [
                        {"role": "system", "content": "Всегда отвечай вызовом инструмента."},
                        {
                            "role": "user",
                            "content": "Вызови ping с echo=\"ok\", ничего не объясняй.",
                        },
                    ],
                    tools=probe_tools,
                    max_tokens=64,
                    timeout=min(self.timeout, 60.0),
                )
                report["tools_ok"] = bool(reply.tool_calls)
                if not report["tools_ok"]:
                    report["problems"].append("модель не поддерживает вызов инструментов (tools)")
            except LLMError as exc:
                report["problems"].append(f"проверка инструментов не удалась: {exc}")
        if need_vision:
            # Однопиксельный PNG: проверяем, что модель принимает изображения.
            pixel = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            )
            try:
                reply = self.vision(
                    "Ответь одним словом: какой цвет на картинке?",
                    pixel,
                    max_tokens=16,
                    timeout=min(self.timeout, 90.0),
                )
                report["vision_ok"] = bool(reply.content)
                if not report["vision_ok"]:
                    report["problems"].append("модель не обработала тестовое изображение")
            except LLMError as exc:
                report["problems"].append(f"проверка зрения не удалась: {exc}")
        report["ms"] = (time.perf_counter() - started) * 1000.0
        report["ready"] = bool(
            report["reachable"] and report["model_present"]
            and (report["tools_ok"] or not need_tools)
            and (report["vision_ok"] or not need_vision)
        )
        return report

    @property
    def stats(self) -> dict:
        return {"calls": self._calls, "errors": self._errors, "last_ms": round(self._last_ms, 1)}
