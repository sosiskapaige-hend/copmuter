"""OpenAI-совместимый клиент (chat/completions) на чистом stdlib (urllib).

Работает с: OpenAI, Groq, OpenRouter, DeepSeek, Mistral, Ollama
(/v1/chat/completions), LM Studio и любым другим совместимым сервером.
Поддержка: function calling (tools), vision (image_url base64), JSON-ответы.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from .base import LLM, Plan, PlanStep, ToolCall, Decision, Assessment


class LLMError(RuntimeError):
    pass


class OpenAICompatibleLLM:
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int = 4096, temperature: float = 0.2,
                 supports_tool_calling: bool = True, vision: bool = True,
                 timeout: float = 180.0, max_retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = self._safe_max_tokens(max_tokens)
        self.temperature = temperature
        self.weak_model = self._is_weak_model(model)
        self.supports_tool_calling = supports_tool_calling and not self.weak_model
        self.vision = vision
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def _is_weak_model(model: str) -> bool:
        """Совсем маленькие модели: JSON-протокол и tool-calling для них
        отключаем. 4B/8B (например Qwen3-VL-8B-Instruct) нормально работают
        с инструментами через LM Studio, поэтому сюда не входят."""
        name = (model or "").lower()
        return any(token in name for token in ("tiny", "mini", "0.5b", "1b", "1.5b", "2b"))

    @staticmethod
    def _safe_max_tokens(max_tokens: int) -> int:
        try:
            v = int(max_tokens)
        except (TypeError, ValueError):
            return 2048
        if v <= 0:
            return 2048
        return min(v, 16384)

    # ---------------- HTTP ----------------
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key or 'sk-none'}",
        }
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")[:800]
                last_err = LLMError(f"HTTP {e.code}: {raw}")
                if e.code in (408, 429, 500, 502, 503, 504) and attempt < self.max_retries:
                    import time
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = LLMError(f"Сеть: {e}")
                if attempt < self.max_retries:
                    import time
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err
        raise last_err or LLMError("LLM недоступна")

    @staticmethod
    def _trim_text(value: str, limit: int = 1200) -> str:
        if not isinstance(value, str):
            return value
        if len(value) <= limit:
            return value
        return value[:limit].rstrip() + "... [обрезано]"

    def _compact_messages(self, messages: list[dict]) -> list[dict]:
        compact: list[dict] = []
        for msg in messages[:20]:
            compact.append(self._wire_message(msg))
        return compact

    def _wire_message(self, msg: dict) -> dict:
        """Приводит сообщение к валидной для chat/completions форме.

        OpenAI-совместимые серверы (OpenAI, LM Studio, Ollama, Groq, ...)
        требуют, чтобы у сообщений ролей user/system/tool было поле `content`.
        Внутренняя история хранит наблюдения инструментов как
        {role: tool, tool, ok, text} — здесь они превращаются в стандартное
        {role: tool, content: "..."}, а служебные поля отбрасываются, чтобы
        строгие серверы не отвергли payload.
        """
        role = msg.get("role") or "user"
        out: dict[str, Any] = {"role": role}

        if role == "tool":
            text = msg.get("content", msg.get("text", ""))
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            out["content"] = self._trim_text(text, 1200)
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id:
                out["tool_call_id"] = tc_id
        else:
            content = msg.get("content")
            if isinstance(content, str):
                out["content"] = self._trim_text(content, 1200)
            elif content is None:
                out["content"] = ""
            else:
                out["content"] = content   # мультимодальный content (список частей)

        if role == "assistant":
            tc = msg.get("tool_calls")
            if isinstance(tc, list) and tc:
                out["tool_calls"] = tc[:2]

        return out

    def _compact_tools(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools or not self.supports_tool_calling:
            return tools
        compact: list[dict] = []
        for tool in tools[:8]:
            schema = dict(tool)
            fn = dict(schema.get("function") or {})
            desc = fn.get("description")
            if isinstance(desc, str):
                fn["description"] = self._trim_text(desc, 180)
            schema["function"] = fn
            compact.append(schema)
        return compact

    def _chat(self, messages: list[dict], tools: list[dict] | None = None,
              temperature: float | None = None, json_mode: bool = False) -> dict:
        compact_messages = self._compact_messages(messages)
        compact_tools = self._compact_tools(tools)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": compact_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.weak_model:
            compact_tools = None
            json_mode = False
        if compact_tools and self.supports_tool_calling:
            payload["tools"] = compact_tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Неожиданный ответ LLM: {str(data)[:400]}") from e
        return choice

    @staticmethod
    def _extract_json(text: str) -> Any:
        """Достаёт JSON из ответа (с possible ```-обёртками)."""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start = text.find("{")
        start_arr = text.find("[")
        if start_arr != -1 and (start == -1 or start_arr < start):
            start = start_arr
        if start == -1:
            raise LLMError(f"JSON не найден в ответе: {text[:200]}")
        # ищем последний закрытый } или ]
        for end_char in ("}", "]"):
            end = text.rfind(end_char)
            if end != -1:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
        raise LLMError(f"Некорректный JSON: {text[:200]}")

    # ---------------- LLM interface ----------------
    async def plan(self, goal: str, context: str, tools_hint: str) -> Plan:
        msg = (
            "Ты — планировщик AI-агента, который управляет компьютером пользователя.\n"
            f"Контекст системы:\n{context}\n\n"
            f"Доступные инструменты:\n{tools_hint}\n\n"
            f"Задача пользователя: «{goal}»\n\n"
            "Составь краткий план (3-8 шагов). Возвращай СТРОГО JSON без пояснений:\n"
            '{"summary": "...", "steps": [{"title": "...", "detail": "..."}], '
            '"needs_user": ""}  (needs_user — вопрос, если задача неопределённая, иначе "")'
        )
        try:
            choice = self._chat([{"role": "user", "content": msg}], json_mode=True)
            obj = self._extract_json(choice.get("content") or "{}")
        except LLMError:
            try:
                choice = self._chat([{"role": "user", "content": msg}])
                obj = self._extract_json(choice.get("content") or "{}")
            except LLMError:
                text = (choice.get("content") if 'choice' in locals() else "") or ""
                return Plan(steps=[PlanStep(title="Наблюдение", detail=text[:200] or "Продолжить")],
                            summary="План сформирован в упрощённом режиме.",
                            needs_user="")
        steps = [PlanStep(title=str(s.get("title", "")), detail=str(s.get("detail", "")))
                 for s in (obj.get("steps") or []) if s.get("title")]
        if not steps:
            return Plan(steps=[PlanStep(title="Выполнить задачу", detail="Скорректировать по фактическому состоянию")],
                        summary=str(obj.get("summary", "")) or "План не был структурирован моделью.",
                        needs_user=str(obj.get("needs_user", "")))
        return Plan(steps=steps, summary=str(obj.get("summary", "")),
                    needs_user=str(obj.get("needs_user", "")))

    async def next_action(self, goal: str, context: str,
                          history: list[dict], tools: list[dict]) -> Decision:
        messages = [
            {"role": "system", "content": (
                "Ты — AI-оператор компьютера пользователя. Ты выполняешь задачу, вызывая инструменты.\n"
                "Правила:\n"
                "1. Один инструмент за ход. Выбирай НАДЁЖНЕЙШИЙ способ: прямые системные инструменты "
                "(fs, terminal, processes) предпочтительнее GUI, если задача выполняется напрямую.\n"
                "2. После каждого действия обязательно оцени результат по наблюдению. Не выдумывай успех.\n"
                "3. Если наблюдение показывает ошибку или действие не сработало — проанализируй и выбери ДРУГОЙ путь.\n"
                "4. Завершай (без вызова инструмента, обычным текстом) только когда цель ФАКТИЧЕСКИ достигнута "
                "и проверена. Тогда дай краткий итог для пользователя.\n"
                "5. Если нужен ответ пользователя (данные, выбор, код из 2FA) — напиши: ASK_TO_USER: <вопрос>\n"
                "Контекст: " + context
            )},
            {"role": "user", "content": f"Цель задачи: {goal}"},
        ]
        messages.extend(history)
        choice = self._chat(messages, tools=tools or None)
        content = (choice.get("content") or "").strip()
        tool_calls = choice.get("tool_calls") or []

        if tool_calls:
            tc = tool_calls[0]
            fn = (tc.get("function") or {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            thought = content or ""
            # если модель не умеет tool calling, она может писать JSON в content
            if not name:
                try:
                    obj = self._extract_json(content)
                    if isinstance(obj, dict) and obj.get("tool"):
                        return Decision(thought=obj.get("thought", ""),
                                        tool_call=ToolCall(obj["tool"], obj.get("args") or {}))
                except LLMError:
                    pass
            if not name:
                return Decision(thought=thought, final=thought or "Я не могу продолжить.")
            return Decision(thought=thought, tool_call=ToolCall(name, args))

        if "ASK_TO_USER:" in content:
            q = content.split("ASK_TO_USER:", 1)[1].strip()
            return Decision(thought=content, ask=q)
        if content:
            return Decision(final=content)
        # пустой ответ — просим явно завершиться или продолжить
        return Decision(thought="(пустой ответ LLM)", ask="(внутренняя ошибка: пустой ответ модели)")

    async def assess(self, goal: str, done_summary: str, last_error: str | None) -> Assessment:
        msg = (
            "Ты проверяешь прогресс AI-агента.\n"
            f"Цель: «{goal}»\n\nВыполнено до сих пор:\n{done_summary[-4000:]}\n"
            + (f"\nПоследняя ошибка: {last_error[-1000:]}\n" if last_error else "")
            + "\nОтветь СТРОГО JSON: "
              '{"progress": 0-100, "achieved": true|false, "remaining": ["..."], '
              '"message": "оценка одним-двумя предложениями", "adjust_plan": false, '
              '"error_analysis": ""}'
        )
        try:
            choice = self._chat([{"role": "user", "content": msg}], json_mode=True)
            obj = self._extract_json(choice.get("content") or "{}")
        except LLMError:
            try:
                choice = self._chat([{"role": "user", "content": msg}])
                obj = self._extract_json(choice.get("content") or "{}")
            except LLMError:
                text = (choice.get("content") if 'choice' in locals() else "") or ""
                return Assessment(
                    progress=50,
                    achieved=False,
                    remaining=["Проверить результат вручную"],
                    message=text[:200] or "Оценка не получена, продолжить проверку.",
                    adjust_plan=False,
                    error_analysis="",
                )
        return Assessment(
            progress=int(obj.get("progress", 0) or 0),
            achieved=bool(obj.get("achieved", False)),
            remaining=[str(x) for x in (obj.get("remaining") or [])],
            message=str(obj.get("message", "")),
            adjust_plan=bool(obj.get("adjust_plan", False)),
            error_analysis=str(obj.get("error_analysis", "")),
        )

    async def answer(self, messages: list[dict]) -> str:
        choice = self._chat(messages)
        return (choice.get("content") or "").strip()

    # ---------------- чат: стриминг и диагностика ----------------
    def chat_stream(self, messages: list[dict], temperature: float | None = None):
        """Потоковая генерация. Отдаёт (kind, piece), где kind — "content" или
        "think" (содержимое <think>…</think> и reasoning_content)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": True,
        }
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key or 'sk-none'}",
        }
        filt = _ThinkFilter()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            for raw in resp:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                choices = chunk.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    yield "think", reasoning
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    c, t = filt.feed(piece)
                    if t:
                        yield "think", t
                    if c:
                        yield "content", c
            c, t = filt.flush()
            if t:
                yield "think", t
            if c:
                yield "content", c

    def list_models(self) -> list[str]:
        """Список моделей сервера (GET /models) — для выпадающего списка в UI."""
        url = f"{self.base_url}/models"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key or 'sk-none'}"})
        with urllib.request.urlopen(req, timeout=6.0) as r:
            data = json.loads(r.read().decode("utf-8"))
        return sorted(str(m.get("id")) for m in (data.get("data") or []) if m.get("id"))

    async def describe_image(self, image_b64: str, prompt: str) -> str:
        if not self.vision:
            return ("Модель LLM не поддерживает vision — изображение проанализировать "
                    "нельзя. Используйте OCR (ocr_image).")
        b64 = image_b64
        if isinstance(b64, bytes):
            b64 = base64.b64encode(b64).decode()
        if not b64.startswith("data:"):
            b64 = "data:image/png;base64," + b64
        msg = ([{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": b64}},
        ]}])
        choice = self._chat(msg, temperature=0.1)
        return (choice.get("content") or "").strip()


class _ThinkFilter:
    """Выделяет <think>…</think> из потока токенов, не ломая стриминг.

    Держит «хвост» из 7 символов в буфере — на случай, что тег разрезан
    границей чанка."""
    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.in_think = False
        self.buf = ""

    def _drain(self, c_out: list, t_out: list) -> None:
        while self.buf:
            tag, out = (self.CLOSE, t_out) if self.in_think else (self.OPEN, c_out)
            idx = self.buf.find(tag)
            if idx == -1:
                safe = len(self.buf) - len(tag)
                if safe > 0:
                    out.append(self.buf[:safe])
                    self.buf = self.buf[safe:]
                break
            out.append(self.buf[:idx])
            self.buf = self.buf[idx + len(tag):]
            self.in_think = not self.in_think

    def feed(self, piece: str) -> tuple[str, str]:
        self.buf += piece
        c_out: list[str] = []
        t_out: list[str] = []
        self._drain(c_out, t_out)
        return "".join(c_out), "".join(t_out)

    def flush(self) -> tuple[str, str]:
        rest, self.buf = self.buf, ""
        if self.in_think:
            return "", rest
        return rest, ""
