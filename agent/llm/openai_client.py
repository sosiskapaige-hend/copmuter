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


def is_context_overflow_error(e: Exception) -> bool:
    """Похоже ли сообщение об ошибке на переполнение контекста модели.

    Разные серверы пишут по-разному: «context length exceeded» (LM Studio /
    llama.cpp), «maximum context length is N tokens» (OpenAI), «prompt is too
    long», «ctx_len», «too many tokens», «reduce the length» (Ollama) и т.п.
    """
    msg = str(e)
    low = msg.lower()
    if not any(f"http {code}" in low for code in (400, 413, 429, 500, 503)):
        return False
    if "context" in low or "ctx" in low:
        return True
    return any(k in low for k in (
        "token", "too long", "too many", "prompt is too", "maximum length",
        "max length", "max_model_len", "kv cache", "window is too small",
        "reduce the length", "exceeds", "exceeded", "larger than",
        "longer than", "cannot fit", "doesn't fit", "не помещается",
    ))


class OpenAICompatibleLLM:
    name = "openai-compatible"

    # Лимиты «сжатия» контекста. Они защищают локальные модели с маленьким
    # окном, но НЕ должны лишать агента возможности действовать: раньше
    # модели уходило только 8 инструментов из 83 (по алфавиту: ask_user +
    # 7×browser_*) и system prompt, обрезанный до 1200 символов — в итоге
    # агент «отвечал, но ничего не делал» (не было fs_mkdir/terminal_run/
    # launch_app/finish_task, а правила и план были отрезаны).
    MAX_TOOLS = 128                  # все инструменты (83) помещаются
    MAX_HISTORY_MESSAGES = 40        # последние N сообщений истории
    SYSTEM_LIMIT = 16000             # system prompt (контекст + правила)
    USER_LIMIT = 6000                # пользовательские/служебные сообщения
    TOOL_LIMIT = 3000                # наблюдения инструментов
    TOOL_DESC_LIMIT = 400            # описание инструмента в schema

    # Уровни сжатия при переполнении контекста. На каждом уровне «давления»
    # контекст ужимается по чуть-чуть: сначала уходят старые сообщения истории,
    # потом укорачиваются поля, потом сокращается число схем инструментов.
    # Уровень 0 = обычные лимиты (см. выше), дальше — всё скромнее, поэтому
    # модель ВСЕГДА сможет ответить, а не падает с «контекст переполнен».
    PRESSURE_LEVELS = 9
    P_HISTORY = (40, 32, 26, 20, 14, 10, 7, 5, 3)
    P_SYSTEM = (16000, 14000, 12000, 10000, 8000, 6500, 5000, 4000, 3200)
    P_USER = (6000, 5000, 4000, 3200, 2600, 2000, 1600, 1300, 1000)
    P_TOOL = (3000, 2600, 2200, 1800, 1400, 1100, 900, 700, 550)
    P_TOOLS_NUM = (128, 128, 112, 96, 84, 72, 60, 50, 40)
    P_DESC = (400, 360, 320, 280, 240, 200, 170, 150, 130)

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
        """Совсем маленькие модели (≤2B параметров): tool-calling для них
        отключаем — они не держат схему из 80+ функций.

        Проверяем ТОЛЬКО размер модели как отдельный токен имени
        («…-1b-…», «0.5b», «2b»), а не подстроки: раньше по вхождению
        «mini»/«2b» под раздачу попадали gpt-4o-mini, gemini-*, gemma-3-12b,
        qwen2.5-vl-72b — и у них молча выключался function calling.
        """
        name = (model or "").lower()
        import re as _re
        m = _re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![a-z0-9])", name)
        if m:
            try:
                return float(m.group(1)) <= 2.0
            except ValueError:
                return False
        return "tiny" in name.split("-") or "tiny" in name.split("_")

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
                reason = str(getattr(e, "reason", e)).lower()
                # сервер не запущен — повторы бессмысленны, отвечаем сразу
                if "refused" in reason or "10061" in reason or "errno 111" in reason:
                    raise last_err
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

    @staticmethod
    def _pressure(table: tuple, retry: int):
        i = max(0, min(int(retry), len(table) - 1))
        return table[i]

    def _compact_messages(self, messages: list[dict], retry: int = 0) -> list[dict]:
        """Готовит messages для отправки.

        Раньше брались ПЕРВЫЕ 20 сообщений (`messages[:20]`) — при длинной
        истории модель не видела последние наблюдения инструментов и
        «зацикливалась». Теперь: system-сообщения сохраняются всегда,
        а из остальных берётся хвост (последние N). При переполнении
        контекста (`retry` > 0) история и лимиты постепенно ужимаются.
        """
        system = [m for m in messages if (m.get("role") or "user") == "system"]
        rest = [m for m in messages if (m.get("role") or "user") != "system"]
        keep = self._pressure(self.P_HISTORY, retry)
        if len(rest) > keep:
            rest = rest[-keep:]
        return [self._wire_message(m, retry) for m in system + rest]

    def _wire_message(self, msg: dict, retry: int = 0) -> dict:
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
            # Наблюдение инструмента для модели: без tool_call_id строгие
            # серверы (OpenAI) отвергают role=tool, а LM Studio/Ollama —
            # принимают, но связь с вызовом теряется. Внутренняя история не
            # хранит id вызовов, поэтому наблюдение отдаём как user-сообщение
            # с явной пометкой — это понимает любая модель.
            tool_name = msg.get("tool") or msg.get("name") or "инструмент"
            status = "OK" if msg.get("ok", True) else "ОШИБКА"
            out["role"] = "user"
            out["content"] = (f"[РЕЗУЛЬТАТ ИНСТРУМЕНТА {tool_name} — {status}]\n"
                              + self._trim_text(text, self._pressure(self.P_TOOL, retry)))
        else:
            content = msg.get("content")
            table = self.P_SYSTEM if role == "system" else self.P_USER
            limit = self._pressure(table, retry)
            if isinstance(content, str):
                out["content"] = self._trim_text(content, limit)
            elif content is None:
                out["content"] = ""
            else:
                # мультимодальный content (список частей) — режем текстовые куски
                out["content"] = self._trim_parts(content, limit)

        if role == "assistant":
            tc = msg.get("tool_calls")
            if isinstance(tc, list) and tc:
                out["tool_calls"] = tc[:2]

        return out

    @classmethod
    def _trim_parts(cls, parts, limit: int):
        """Ужимает текстовые части мультимодального content."""
        if not isinstance(parts, list):
            return parts
        out = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                p = dict(part)
                p["text"] = cls._trim_text(str(p.get("text", "")), limit)
                out.append(p)
            else:
                out.append(part)
        return out

    def _compact_tools(self, tools: list[dict] | None, retry: int = 0) -> list[dict] | None:
        """Схемы инструментов для function calling.

        Раньше отправлялись только ПЕРВЫЕ 8 инструментов из отсортированного
        по алфавиту списка (ask_user + browser_*): модель физически не могла
        вызвать fs_mkdir, terminal_run, launch_app или finish_task — и просто
        писала текст «готово». Теперь уходят все инструменты, а сокращаются
        только слишком длинные описания. Схемы отсортированы по релевантности
        (ядро в начале), поэтому при сильном сжатии отбрасываются наименее
        нужные для текущей задачи, а не алфавитные первые.
        """
        if not tools or not self.supports_tool_calling:
            return tools
        num = self._pressure(self.P_TOOLS_NUM, retry)
        desc_limit = self._pressure(self.P_DESC, retry)
        compact: list[dict] = []
        for tool in tools[:num]:
            schema = dict(tool)
            fn = dict(schema.get("function") or {})
            desc = fn.get("description")
            if isinstance(desc, str):
                fn["description"] = self._trim_text(desc, desc_limit)
            schema["function"] = fn
            compact.append(schema)
        return compact

    def _chat(self, messages: list[dict], tools: list[dict] | None = None,
              temperature: float | None = None, json_mode: bool = False,
              _retry: int = 0) -> dict:
        """Один вызов chat/completions с адаптивным сжатием контекста.

        Если сервер ответил «контекст переполнен» — запрос повторяется с
        чуть более ужатым контекстом (старее сообщения истории, короче поля,
        меньше схем инструментов) до тех пор, пока не влезет. Поэтому агент
        больше НЕ падает с ошибкой про контекст, а сам освобождает место.
        """
        compact_messages = self._compact_messages(messages, _retry)
        compact_tools = self._compact_tools(tools, _retry)
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
        try:
            data = self._post(payload)
        except LLMError as e:
            # КОНТЕКСТ ПЕРЕПОЛНЕН → ужимаем и пробуем ещё (по чуть-чуть)
            if is_context_overflow_error(e) and _retry < len(self.P_HISTORY) - 1:
                return self._chat(messages, tools, temperature=temperature,
                                  json_mode=json_mode, _retry=_retry + 1)
            # Некоторые серверы (старые Ollama, llama.cpp, некоторые прокси)
            # отвечают 400 на response_format/tools. Повторяем без них, чтобы
            # агент не останавливался — парсер JSON/текстовых вызовов справится.
            msg = str(e)
            retry = False
            if json_mode and "HTTP 4" in msg:
                payload.pop("response_format", None)
                retry = True
            if "tools" in payload and "HTTP 4" in msg and \
                    any(k in msg.lower() for k in ("tool", "function")):
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                retry = True
            if not retry:
                raise
            try:
                data = self._post(payload)
            except LLMError as e2:
                # и сжатый контекст всё ещё не влезает? ещё одно ужатие
                if is_context_overflow_error(e2) and _retry < len(self.P_HISTORY) - 1:
                    return self._chat(messages, tools, temperature=temperature,
                                      json_mode=False, _retry=_retry + 1)
                raise
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Неожиданный ответ LLM: {str(data)[:400]}") from e
        if not isinstance(choice, dict):
            raise LLMError(f"Неожиданный ответ LLM: {str(data)[:400]}")
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
        # Контекст уже содержит список инструментов (build_context) — не
        # дублируем его; задача идёт ПОСЛЕДНЕЙ и в system, и в user, чтобы
        # не потеряться при усечении длинного контекста.
        system = (
            "Ты — планировщик AI-агента, который управляет компьютером пользователя.\n"
            f"{context}\n"
        )
        if tools_hint and tools_hint not in context:
            system += f"\nДоступные инструменты:\n{tools_hint}\n"
        msg = (
            f"Задача пользователя: «{goal}»\n\n"
            "Составь краткий план (3-8 шагов) с указанием конкретных инструментов. "
            "Возвращай СТРОГО JSON без пояснений:\n"
            '{"summary": "...", "steps": [{"title": "...", "detail": "..."}], '
            '"needs_user": ""}  (needs_user — вопрос, если задача неопределённая, иначе "")'
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": msg}]
        choice: dict = {}
        try:
            choice = self._chat(messages, json_mode=True)
            obj = self._extract_json(self._strip_think(choice.get("content") or "{}")[0])
        except LLMError:
            try:
                choice = self._chat(messages)
                obj = self._extract_json(self._strip_think(choice.get("content") or "{}")[0])
            except LLMError:
                text = (choice.get("content") or "") if isinstance(choice, dict) else ""
                return Plan(steps=[PlanStep(title="Выполнить задачу напрямую",
                                            detail=text[:200] or "Действовать по ситуации")],
                            summary="План сформирован в упрощённом режиме.",
                            needs_user="")
        if not isinstance(obj, dict):
            obj = {}
        steps = [PlanStep(title=str(s.get("title", "")), detail=str(s.get("detail", "")))
                 for s in (obj.get("steps") or []) if s.get("title")]
        if not steps:
            return Plan(steps=[PlanStep(title="Выполнить задачу", detail="Скорректировать по фактическому состоянию")],
                        summary=str(obj.get("summary", "")) or "План не был структурирован моделью.",
                        needs_user=str(obj.get("needs_user", "")))
        return Plan(steps=steps, summary=str(obj.get("summary", "")),
                    needs_user=str(obj.get("needs_user", "")))

    # Маркер, которым модель ЯВНО завершает задачу текстом. Раньше любой
    # текстовый ответ без tool_call считался финалом — модель писала «Сейчас
    # создам папку…» и агент на этом заканчивал, ничего не сделав.
    FINAL_MARKER = "FINAL_ANSWER:"

    def _action_rules(self, native_tools: bool) -> str:
        rules = (
            "Ты — AI-оператор компьютера пользователя. Ты НЕ чат-бот: ты ВЫПОЛНЯЕШЬ задачу, "
            "вызывая инструменты, и отчитываешься только о ФАКТИЧЕСКИ сделанном.\n"
            "Правила:\n"
            "1. Каждый ход — ровно ОДИН вызов инструмента. Никогда не описывай словами, что "
            "«сейчас сделаешь» — вместо этого вызови инструмент. Текст без вызова инструмента "
            "НЕ выполняет никаких действий.\n"
            "2. Выбирай надёжнейший способ: прямые инструменты (fs_*, terminal_run, launch_app, "
            "open_url, process_*) предпочтительнее GUI-имитации.\n"
            "3. После каждого действия оцени результат по наблюдению. Не выдумывай успех.\n"
            "4. Ошибка или действие не сработало — проанализируй причину и выбери ДРУГОЙ путь.\n"
            "5. Когда цель ФАКТИЧЕСКИ достигнута и проверена — вызови finish_task(summary=...).\n"
            "6. Нужен ответ пользователя (данные, выбор, код 2FA) — вызови ask_user(question=...).\n"
        )
        if not native_tools:
            rules += (
                "\nФОРМАТ ОТВЕТА (function calling недоступен — используй JSON-протокол). "
                "Отвечай СТРОГО одним JSON-объектом без пояснений вокруг:\n"
                '{"thought": "кратко зачем", "tool": "имя_инструмента", "args": {...}}\n'
                "Завершение: {\"tool\": \"finish_task\", \"args\": {\"summary\": \"что сделано\"}}\n"
                "Вопрос пользователю: {\"tool\": \"ask_user\", \"args\": {\"question\": \"...\"}}\n"
            )
        else:
            rules += (
                f"\nЕсли инструмент вызвать невозможно, отвечай текстом, начинающимся с "
                f"«{self.FINAL_MARKER}» — только это считается завершением; любой другой текст "
                f"будет проигнорирован и тебя попросят вызвать инструмент.\n"
            )
        return rules

    @staticmethod
    def _tools_compact_hint(tools: list[dict]) -> str:
        """Список инструментов с параметрами для JSON-протокола."""
        lines = []
        for t in tools:
            fn = t.get("function") or {}
            props = ((fn.get("parameters") or {}).get("properties") or {})
            req = set((fn.get("parameters") or {}).get("required") or [])
            params = ", ".join(f"{k}{'' if k in req else '?'}" for k in props)
            desc = str(fn.get("description") or "")[:160]
            lines.append(f"- {fn.get('name')}({params}): {desc}")
        return "\n".join(lines)

    @staticmethod
    def _strip_think(text: str) -> tuple[str, str]:
        """Убирает <think>…</think> (thinking-модели: Qwen3, DeepSeek-R1)."""
        import re as _re
        thinks = _re.findall(r"<think>(.*?)</think>", text, flags=_re.S)
        clean = _re.sub(r"<think>.*?</think>", "", text, flags=_re.S)
        # незакрытый think — всё содержимое считается размышлением
        if "<think>" in clean and "</think>" not in clean:
            head, _, tail = clean.partition("<think>")
            thinks.append(tail)
            clean = head
        return clean.strip(), "\n".join(t.strip() for t in thinks)

    def _parse_text_tool_call(self, content: str, known: set[str]) -> ToolCall | None:
        """Локальные модели часто пишут вызов инструмента в текст:
        <tool_call>{"name": "fs_mkdir", "arguments": {...}}</tool_call>,
        ```json {"tool": "...", "args": {...}} ``` или просто JSON-объект.
        Достаём такие вызовы, чтобы агент реально действовал."""
        import re as _re
        text = content
        m = _re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, flags=_re.S)
        candidates: list[str] = []
        if m:
            candidates.append(m.group(1))
        # все JSON-подобные блоки в код-обёртках
        candidates += _re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.S)
        candidates.append(text)
        for cand in candidates:
            try:
                obj = self._extract_json(cand)
            except LLMError:
                continue
            if not isinstance(obj, dict):
                continue
            # {"function_call": {"name":..., "arguments":...}} → разворачиваем
            fc = obj.get("function_call")
            if isinstance(fc, dict):
                obj = {**obj, **fc}
            name = obj.get("tool") or obj.get("name") or obj.get("function")
            if isinstance(name, dict):          # {"function": {"name": ...}}
                name = name.get("name")
            args = obj.get("args")
            if args is None:
                args = obj.get("arguments")
            if args is None:
                args = obj.get("parameters")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if isinstance(name, str) and name in known:
                return ToolCall(name, args if isinstance(args, dict) else {})
        return None

    async def next_action(self, goal: str, context: str,
                          history: list[dict], tools: list[dict]) -> Decision:
        tools = tools or []
        native = bool(tools) and self.supports_tool_calling and not self.weak_model
        known = {(t.get("function") or {}).get("name") for t in tools}
        system = self._action_rules(native) + "\n" + context
        if tools and not native:
            system += "\n\n## Инструменты (JSON-протокол)\n" + self._tools_compact_hint(tools)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Цель задачи: {goal}"},
        ]
        messages.extend(history)
        choice = self._chat(messages, tools=tools if native else None)
        raw = (choice.get("content") or "")
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        content, think = self._strip_think(raw)
        reasoning = choice.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning and not think:
            think = reasoning
        tool_calls = choice.get("tool_calls") or []

        # 1) нативный function calling
        if tool_calls:
            tc = tool_calls[0] or {}
            fn = (tc.get("function") or {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
            if name:
                return Decision(thought=(content or think)[:600],
                                tool_call=ToolCall(name, args if isinstance(args, dict) else {}))

        # 2) вызов инструмента текстом (JSON-протокол / <tool_call> / ```json```)
        text_call = self._parse_text_tool_call(content, known) if content else None
        if text_call is not None:
            if text_call.name == "finish_task":
                return Decision(thought=think[:600],
                                final=str(text_call.args.get("summary") or content)[:2000])
            if text_call.name == "ask_user":
                q = str(text_call.args.get("question") or "").strip()
                if q:
                    return Decision(thought=think[:600], ask=q)
            return Decision(thought=think[:600], tool_call=text_call)

        # 3) явные текстовые маркеры
        if "ASK_TO_USER:" in content:
            q = content.split("ASK_TO_USER:", 1)[1].strip()
            return Decision(thought=content, ask=q)
        if self.FINAL_MARKER in content:
            final = content.split(self.FINAL_MARKER, 1)[1].strip()
            return Decision(thought=think[:600], final=final or content)

        # 4) просто текст без действия — это НЕ завершение. Возвращаем «мысль»,
        # цикл попросит модель вызвать инструмент (или finish_task).
        return Decision(thought=(content or think or "(пустой ответ модели)")[:1200])

    async def assess(self, goal: str, done_summary: str, last_error: str | None) -> Assessment:
        if not done_summary.strip():
            # Ни одного выполненного действия — цель не может быть достигнута.
            # Не тратим запрос к модели и не даём ей «засчитать» пустую работу.
            return Assessment(progress=0, achieved=False,
                              remaining=["ещё не выполнено ни одного действия"],
                              message="Действий не было — задача не начата.",
                              adjust_plan=False, error_analysis="")
        msg = (
            "Ты проверяешь прогресс AI-агента. Оценивай ТОЛЬКО по фактически выполненным "
            "действиям ниже (✔ — успех, ✘ — ошибка). Намерения и обещания не считаются.\n"
            f"Цель: «{goal}»\n\nВыполнено до сих пор:\n{done_summary[-4000:]}\n"
            + (f"\nПоследняя ошибка: {last_error[-1000:]}\n" if last_error else "")
            + "\nОтветь СТРОГО JSON: "
              '{"progress": 0-100, "achieved": true|false, "remaining": ["..."], '
              '"message": "оценка одним-двумя предложениями", "adjust_plan": false, '
              '"error_analysis": ""}'
        )
        choice: dict = {}
        try:
            choice = self._chat([{"role": "user", "content": msg}], json_mode=True)
            obj = self._extract_json(self._strip_think(choice.get("content") or "{}")[0])
        except LLMError:
            try:
                choice = self._chat([{"role": "user", "content": msg}])
                obj = self._extract_json(self._strip_think(choice.get("content") or "{}")[0])
            except LLMError:
                text = (choice.get("content") or "") if isinstance(choice, dict) else ""
                return Assessment(
                    progress=50,
                    achieved=False,
                    remaining=["Проверить результат вручную"],
                    message=text[:200] or "Оценка не получена, продолжить проверку.",
                    adjust_plan=False,
                    error_analysis="",
                )
        if not isinstance(obj, dict):
            obj = {}
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
        content = choice.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return self._strip_think(content)[0].strip()

    # ---------------- чат: стриминг и диагностика ----------------
    def chat_stream(self, messages: list[dict], temperature: float | None = None):
        """Потоковая генерация. Отдаёт (kind, piece), где kind — "content" или
        "think" (содержимое <think>…</think> и reasoning_content).

        При ошибке «контекст переполнен» стрим автоматически повторяется с
        ужатым контекстом (по чуть-чуть), пока запрос не влезет.
        """
        retry = 0
        sent_any = False
        while True:
            try:
                for kind, piece in self._stream_attempt(messages, temperature, retry):
                    sent_any = True
                    yield kind, piece
                return
            except LLMError as e:
                # повторяем только если сервер ещё ничего не успел отдать
                if (is_context_overflow_error(e) and not sent_any
                        and retry < len(self.P_HISTORY) - 1):
                    retry += 1
                    continue
                raise

    def _stream_attempt(self, messages: list[dict], temperature: float | None,
                        retry: int = 0):
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._compact_messages(messages, retry),
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
