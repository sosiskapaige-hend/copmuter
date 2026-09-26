"""Воркер-мозг: принимает запросы нативного ядра, думает и возвращает план.

Роли разделены строго:
    * C++ ядро (``native/``) — «руки»: нативные инструменты, окна, ввод, файлы, проверка;
    * Python-воркер — «мозг»: план, рассуждение, код, зрение, память, контекст;
    * канал между ними — Named Pipes (Windows) / Unix-сокет (dev), один постоянный канал.

Воркер поднимается один раз вместе с приложением и живёт всю сессию: никаких запусков
Python на каждую команду и никакого «модель на каждый чих» — простые команды ядро
выполняет само, не доходя до этого модуля.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import socket
import time
from typing import Any

from . import prompts, protocol
from .config import WorkerConfig
from .context import ContextManager
from .diagnostics import collect_system_diagnostics
from .licensing import LicenseManager
from .llm import LLMError, LLMReply, LMStudioClient
from .loop_guard import LoopGuard
from .memory import Memory
from .browser import BrowserError, BrowserSession
from .privacy import PrivacyManager
from .security import CredentialVault, detect_prompt_injection, redact_secrets, wrap_untrusted_content
from .tools import global_tool_registry
from .undo import TransactionJournal
from .vision import FrameGeometry, VisionService, click_call, parse_elements, pick_best

log = logging.getLogger("ai.worker")

# Вызов инструмента текстом (если модель не умеет function calling) — два формата.
_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)

PLAN_INSTRUCTION = (
    "Верни следующий шаг: либо вызовы инструментов (можно несколько, если это одно "
    "логическое действие), либо finished=true, если задача уже выполнена. "
    "Не выдумывай результаты — их вернёт рантайм."
)


class AiWorker:
    """Мозг агента. Один экземпляр на процесс; состояние — в SQLite и контексте."""

    def __init__(
        self,
        config: WorkerConfig | None = None,
        *,
        client: LMStudioClient | None = None,
        memory: Memory | None = None,
        vision: VisionService | None = None,
    ) -> None:
        self.config = config or WorkerConfig()
        self.config.ensure_dirs()
        self.memory = memory or Memory(self.config.db_path)
        self.client = client or LMStudioClient(
            self.config.endpoint,
            self.config.model,
            api_key=self.config.api_key,
            timeout=self.config.request_timeout,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        self.context = ContextManager(
            core_prompt=prompts.CORE,
            max_tokens=self.config.context_tokens,
            history_limit=self.config.history_limit,
        )
        self.vision = vision or VisionService(self.client, timeout=self.config.vision_timeout,
                                              metrics=self.memory)
        # Браузер поднимается лениво: пока задачи решаются прямыми ссылками и
        # нативными инструментами, Chromium вообще не запускается.
        self.browser = BrowserSession(timeout_ms=self.config.browser_timeout_ms)
        self._seen: dict[tuple[str, str], int] = {}
        # Подсистемы коммерческого продукта (ТЗ §37, §72, §144, §149, §160)
        self.loop_guard = LoopGuard()
        self.undo_journal = TransactionJournal(os.path.join(self.config.state_dir, "undo"))
        self.privacy_manager = PrivacyManager(self.config.db_path, self.config.state_dir)
        self.license_manager = LicenseManager(os.path.join(self.config.state_dir, "license.json"))
        self.credential_vault = CredentialVault(os.path.join(self.config.state_dir, "vault"))
        self.tool_registry = global_tool_registry

    # ------------------------------------------------------------------ план
    def handle_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        task = str(request.get("task") or "").strip()
        if not task:
            return protocol.error_reply(request_id, "пустая задача")

        # Проверка на Prompt Injection и маскирование секретов (ТЗ §141, §143)
        inj = detect_prompt_injection(task)
        if inj.is_injected:
            log.warning("Prompt injection в задаче: %s", inj.matched_patterns)
            task = inj.sanitized_text
        task, _ = redact_secrets(task)

        step = int(request.get("step") or 1)
        max_steps = int(request.get("max_steps") or self.config.max_steps)
        if step <= 1:
            self._seen.clear()
            self.loop_guard.clear()

        tools = request.get("tools") or []
        selected = self.context.select_tools(tools, task)
        schemas = to_openai_tools(selected) if self.config.native_tools else None
        self.context.facts = self.memory.facts()

        # Очистка наблюдений от возможных секретов
        raw_obs = list(request.get("observations") or [])
        clean_obs = [redact_secrets(str(o))[0] for o in raw_obs]

        extra_sys = ""
        if step > 1:
            is_loop, loop_msg = self.loop_guard.check()
            if is_loop:
                extra_sys = f"ВНИМАНИЕ: {loop_msg}"

        messages = self.context.build(
            task,
            history=[],
            state=request.get("state") or {},
            observations=clean_obs,
            step=step,
            max_steps=max_steps,
            extra_system=extra_sys,
            apps=list(request.get("apps") or []),
        )
        messages.append({"role": "user", "content": f"Задача: {task}\n\n{PLAN_INSTRUCTION}"})

        started = time.perf_counter()
        try:
            # Без function calling модель отвечает текстом — разбор ниже это учитывает.
            reply = (self.client.chat(messages, tools=schemas) if schemas
                     else self.client.chat(messages))
        except LLMError as exc:
            return protocol.error_reply(request_id, f"модель недоступна: {exc}")
        ms = (time.perf_counter() - started) * 1000.0
        self.memory.metric("llm_plan", task[:120], ms, meta={"step": step})

        say = (reply.content or "").strip()
        # Инструмент, который ядро объявило, но не попало в контекст, всё равно исполним:
        # фильтр нужен только против выдуманных моделью имён.
        raw_calls = extract_tool_calls(reply, tools)
        calls = self._guard_repeats(raw_calls, step)
        for c in calls:
            self.loop_guard.record(c.get("tool", ""), c.get("args"), ok=True, step=step)
        if not calls:
            if _looks_finished(say) or step >= max_steps:
                return protocol.plan_reply(request_id, [], say=say or "Готово", finished=True)
            return protocol.error_reply(request_id, say or "модель не предложила действий")
        log.info("шаг %s: %s вызовов инструментов", step, len(calls))
        return protocol.plan_reply(request_id, calls, say=say, finished=False, usage=reply.usage)

    def _guard_repeats(self, calls: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
        """Защита от циклов: один и тот же вызов дважды подряд не повторяем без причины.

        На первом шаге повторы разрешены: пользователь вправе дважды подряд сказать
        «открой телегу».
        """
        if step <= 1:
            for call in calls:
                self._seen[_call_key(call)] = 1
            return calls
        out: list[dict[str, Any]] = []
        for call in calls:
            key = _call_key(call)
            if self._seen.get(key):
                log.warning("повтор вызова %s — прошу другой путь", call.get("tool"))
                continue
            self._seen[key] = 1
            out.append(call)
        return out

    # ------------------------------------------------------------------ разговор
    def handle_chat(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        text = str(request.get("task") or "").strip()
        if not text:
            return protocol.error_reply(request_id, "пустое сообщение")
        chat_id = self.memory.ensure_chat(request.get("chat_id"), title=text[:60])
        history = self.memory.recent_messages(chat_id, limit=self.config.history_limit)
        self.context.facts = self.memory.facts()
        messages = self.context.build(text, history=history, state=request.get("state") or {},
                                      step=1, max_steps=1,
                                      extra_system=prompts.CHAT)
        try:
            reply = self.client.chat(messages)
        except LLMError as exc:
            return protocol.error_reply(request_id, f"модель недоступна: {exc}")
        self.memory.add_message(chat_id, "user", text)
        self.memory.add_message(chat_id, "assistant", reply.content)
        return protocol.text_reply(request_id, reply.content, usage=reply.usage)

    def handle_code(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        task = str(request.get("task") or "").strip()
        language = str(request.get("language") or "python")
        if not task:
            return protocol.error_reply(request_id, "пустая задача")
        messages = [
            {"role": "system", "content": prompts.CODE},
            {"role": "user", "content": f"Язык: {language}\nЗадача: {task}"},
        ]
        started = time.perf_counter()
        try:
            reply = self.client.chat(messages, temperature=0.1)
        except LLMError as exc:
            return protocol.error_reply(request_id, f"модель недоступна: {exc}")
        self.memory.metric("llm_code", task[:120], (time.perf_counter() - started) * 1000.0)
        return protocol.text_reply(request_id, strip_code_fences(reply.content, language),
                                   kind="code", usage=reply.usage)

    # ------------------------------------------------------------------ зрение
    def handle_vision(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        frame_b64 = request.get("frame_b64") or ""
        if not frame_b64:
            return protocol.error_reply(request_id, "нет кадра для анализа")
        try:
            frame = base64.b64decode(frame_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return protocol.error_reply(request_id, f"кадр повреждён: {exc}")
        if len(frame) > self.config.max_frame_bytes:
            return protocol.error_reply(request_id, "кадр слишком большой")
        geometry = FrameGeometry(**(request.get("frame") or {"width": 0, "height": 0}))
        mode = str(request.get("mode") or "find")
        target = str(request.get("target") or "")
        started = time.perf_counter()
        try:
            if mode in ("read", "ocr"):
                text = self.vision.read(frame)
                return protocol.text_reply(request_id, text, kind="vision")
            if mode == "describe":
                text, elements = self.vision.describe(frame, target or "Что на экране?")
                return protocol.plan_reply(
                    request_id, [], say=text,
                    finished=True, data={"elements": [e.to_dict() for e in elements[:12]]})
            element = self.vision.find(frame, target)
        except LLMError as exc:
            return protocol.error_reply(request_id, f"зрение недоступно: {exc}")
        finally:
            self.memory.metric("llm_vision", target[:120],
                               (time.perf_counter() - started) * 1000.0, meta={"mode": mode})
        if element is None:
            return protocol.text_reply(request_id, "Не вижу на экране то, что нужно", kind="vision")
        try:
            call = click_call(element, geometry)
        except ValueError as exc:
            return protocol.error_reply(request_id, str(exc))
        return protocol.plan_reply(request_id, [call], say=f"Нашёл «{element.label}»", finished=False)

    # ------------------------------------------------------------------ прочее
    def handle_browser(self, request: dict[str, Any]) -> dict[str, Any]:
        """Сложная страница: Playwright. Прямые ссылки сюда не попадают — их делает ядро."""
        steps = request.get("steps")
        if isinstance(steps, list) and steps:
            outcome = self.browser.run_batch(steps)
        else:
            outcome = self.browser.handle(request)
        if not outcome.get("ok"):
            return protocol.error_reply(request.get("id"), str(outcome.get("error") or "браузер не справился"))
        data = outcome.get("data") if "data" in outcome else outcome
        return protocol.text_reply(request.get("id"),
                                   f"браузер: {outcome.get('action', 'шаг')} выполнен",
                                   kind="browser") | {"data": data}

    def handle_compress(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        messages = request.get("messages") or []
        text = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:600]}" for m in messages[-60:]
        )
        if not text.strip():
            return protocol.text_reply(request_id, self.context.summary, kind="summary")
        prompt = f"{prompts.SUMMARY}\n\n{text}"
        try:
            reply = self.client.chat(
                [{"role": "system", "content": prompts.SUMMARY}, {"role": "user", "content": text}],
                temperature=0.2,
            )
        except LLMError as exc:
            return protocol.error_reply(request_id, f"модель недоступна: {exc}")
        self.context.summary = reply.content.strip() or self.context.summary
        self.memory.set_setting("summary", self.context.summary)
        return protocol.text_reply(request_id, self.context.summary, kind="summary")

    def handle_preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        report = self.client.preflight(need_vision=True, need_tools=True)
        return {
            "id": request.get("id"),
            "ok": bool(report.get("ready")),
            "kind": "preflight",
            "say": "окружение готово" if report.get("ready") else "окружение не готово",
            "finished": True,
            "calls": [],
            "data": report,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "model": self.config.model,
            "endpoint": self.config.endpoint,
            "socket": self.config.socket_path,
            "pid": os.getpid(),
            "generation": 1,          # номер сессии воркера: UI видит, что мозг не перезапускался
            "steps_limit": self.config.max_steps,
            "client": self.client.stats,
            "tasks": self.memory.task_stats(),
            "browser": {"running": self.browser.running, "actions": self.browser.actions,
                        "errors": self.browser.errors},
            "metrics": self.memory.metrics_summary(),
        }

    def handle_diagnostics(self, request: dict[str, Any]) -> dict[str, Any]:
        """Сбор доказательной диагностики состояния системы (ТЗ §257, §258)."""
        report = collect_system_diagnostics()
        return {
            "id": request.get("id"),
            "ok": True,
            "kind": "diagnostics",
            "say": report.summary,
            "output": report.evidence_text,
            "data": report.to_dict(),
            "calls": [],
            "finished": True,
        }

    def handle_undo(self, request: dict[str, Any]) -> dict[str, Any]:
        """Откат последней обратимой файловой операции (ТЗ §37, §248)."""
        ok, msg = self.undo_journal.undo_last()
        return {
            "id": request.get("id"),
            "ok": ok,
            "kind": "undo",
            "say": msg,
            "output": msg,
            "error": "" if ok else msg,
            "calls": [],
            "finished": True,
        }

    def handle_custom_tool(self, request: dict[str, Any]) -> dict[str, Any]:
        """Обработка кастомных инструментов SDK (ТЗ §118)."""
        tool_name = str(request.get("tool") or "")
        if tool_name in ("system_diagnostics", "diagnostics"):
            return self.handle_diagnostics(request)
        if tool_name in ("undo_last_action", "undo"):
            return self.handle_undo(request)
        return protocol.error_reply(request.get("id"), f"неизвестный кастомный инструмент: {tool_name}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = str(request.get("type") or "")
        started = time.perf_counter()
        try:
            if kind in ("plan", "replan", "task"):
                reply = self.handle_plan(request)
            elif kind == "chat":
                reply = self.handle_chat(request)
            elif kind == "code":
                reply = self.handle_code(request)
            elif kind == "vision":
                reply = self.handle_vision(request)
            elif kind in ("browser", "playwright"):
                reply = self.handle_browser(request)
            elif kind in ("compress", "summary"):
                reply = self.handle_compress(request)
            elif kind == "preflight":
                reply = self.handle_preflight(request)
            elif kind in ("diagnostics", "system_diagnostics"):
                reply = self.handle_diagnostics(request)
            elif kind in ("undo", "undo_last_action"):
                reply = self.handle_undo(request)
            elif kind == "privacy_inventory":
                reply = {
                    "id": request.get("id"),
                    "ok": True,
                    "kind": "privacy_inventory",
                    "data": self.privacy_manager.get_inventory().to_dict(),
                    "calls": [],
                    "finished": True,
                }
            elif kind == "privacy_export":
                out_path = request.get("path") or os.path.join(self.config.state_dir, "export.json")
                exported = self.privacy_manager.export_data(out_path)
                reply = {
                    "id": request.get("id"),
                    "ok": True,
                    "kind": "privacy_export",
                    "path": exported,
                    "say": f"Данные экспортированы в {exported}",
                    "calls": [],
                    "finished": True,
                }
            elif kind == "privacy_purge":
                cats = request.get("categories") or ["all"]
                purged = self.privacy_manager.purge_data(cats)
                reply = {
                    "id": request.get("id"),
                    "ok": True,
                    "kind": "privacy_purge",
                    "data": purged,
                    "say": "Выбранные данные удалены",
                    "calls": [],
                    "finished": True,
                }
            elif kind == "license_status":
                reply = {
                    "id": request.get("id"),
                    "ok": True,
                    "kind": "license_status",
                    "data": self.license_manager.status.to_dict(),
                    "calls": [],
                    "finished": True,
                }
            elif kind == "license_activate":
                key = str(request.get("key") or "")
                ok, msg = self.license_manager.activate(key)
                reply = {
                    "id": request.get("id"),
                    "ok": ok,
                    "kind": "license_activate",
                    "say": msg,
                    "data": self.license_manager.status.to_dict(),
                    "calls": [],
                    "finished": True,
                }
            elif kind == "tool":
                reply = self.handle_custom_tool(request)
            elif kind == "health":
                reply = {"id": request.get("id"), "ok": True, "kind": "health",
                         "say": f"воркер жив, модель {self.config.model}",
                         "data": self.health(), "calls": [], "finished": True}
            else:
                reply = protocol.error_reply(request.get("id"), f"неизвестный тип запроса: {kind}")
        except Exception as exc:                      # noqa: BLE001 — воркер не должен падать
            log.exception("сбой обработки запроса %s", kind)
            reply = protocol.error_reply(request.get("id"), f"внутренняя ошибка: {exc}")
        self.memory.metric(f"request_{kind or 'unknown'}", "", (time.perf_counter() - started) * 1000.0,
                           meta={"ok": bool(reply.get("ok"))})
        return reply

    # ------------------------------------------------------------------ сервер
    def serve(self, socket_path: str | None = None) -> None:
        path = socket_path or self.config.socket_path
        if path.startswith("\\\\"):                   # Windows Named Pipe
            from .pipe_win import run_pipe_worker

            run_pipe_worker(self, path)
            return
        if os.path.exists(path):
            os.unlink(path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(8)
        log.info("канал открыт: %s", path)
        try:
            while True:
                conn, _ = server.accept()
                try:
                    while True:
                        frame = protocol.read_frame(conn)
                        if frame is None:
                            break
                        reply = self.handle(protocol.decode(frame))
                        protocol.write_frame(conn, reply)
                except (protocol.ProtocolError, ConnectionError, OSError) as exc:
                    log.warning("канал закрыт: %s", exc)
                finally:
                    conn.close()
        finally:
            server.close()
            if os.path.exists(path):
                os.unlink(path)

    def serve_once(self, payload: str) -> str:
        """Одна строка JSON → один ответ. Отладка и тесты, без канала."""
        try:
            request = json.loads(payload)
        except json.JSONDecodeError as exc:
            return json.dumps(protocol.error_reply(0, f"плохой JSON: {exc}"), ensure_ascii=False)
        return json.dumps(self.handle(request), ensure_ascii=False)

    def close(self) -> None:
        self.browser.close()
        self.client.close()
        self.memory.close()


# ---------------------------------------------------------------------------
#  Разбор ответа модели
# ---------------------------------------------------------------------------
def to_openai_tools(tools: Any) -> list[dict[str, Any]]:
    """Схемы ядра → формат OpenAI function calling."""
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        params = tool.get("parameters")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = None
        out.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": params if isinstance(params, dict) else {"type": "object",
                                                                      "properties": {}},
            },
        })
    return out


def extract_tool_calls(reply: LLMReply, tools: Any) -> list[dict[str, Any]]:
    """Вызовы инструментов: сначала настоящее function calling, затем JSON в тексте."""
    known = {str(t.get("name")) for t in (tools or []) if isinstance(t, dict)}
    calls: list[dict[str, Any]] = []
    for raw in reply.tool_calls or []:
        function = raw.get("function") or {}
        name = function.get("name") or raw.get("name")
        if not name:
            continue
        calls.append({"tool": str(name),
                      "args": _parse_args(function.get("arguments", raw.get("arguments"))),
                      "note": ""})
    if calls:
        # Модель могла выдумать инструмент: такого у ядра нет — вызов не пропускаем.
        return [c for c in calls if not known or c["tool"] in known]

    text = reply.content or ""
    for block in _TOOL_CALL_RE.findall(text):
        calls.extend(_calls_from_json(block, known))
    if not calls:
        calls.extend(_calls_from_json(text, known))
    return calls


def _calls_from_json(text: str, known: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for candidate in _JSON_OBJECT_RE.findall(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or "tool" not in item and "name" not in item:
                continue
            tool = item.get("tool") or item.get("name")
            if isinstance(tool, dict):
                tool = tool.get("name")
            if not isinstance(tool, str) or (known and tool not in known):
                continue
            out.append({"tool": tool,
                        "args": _parse_args(item.get("args") or item.get("arguments")
                                            or item.get("parameters") or {}),
                        "note": str(item.get("note") or "")})
    return out


def _parse_args(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _call_key(call: dict[str, Any]) -> tuple[str, str]:
    return (str(call.get("tool")), json.dumps(call.get("args") or {}, sort_keys=True,
                                              ensure_ascii=False))


def _looks_finished(text: str) -> bool:
    lowered = (text or "").lower()
    markers = ("готово", "выполнено", "сделано", "завершено", "done", "finished",
               "всё сделал", "все сделал", "задача решена")
    return any(marker in lowered for marker in markers)


def strip_code_fences(text: str, language: str) -> str:
    """«```python … ```» → чистый код: его сразу можно отправить в буфер обмена."""
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    for index, part in enumerate(parts):
        if index % 2 == 1:
            body = part
            first_line, _, rest = body.partition("\n")
            if first_line.strip().lower().startswith(language.lower()) or len(first_line.strip()) <= 12:
                body = rest if rest else body
            return body.strip("\n")
    return text.replace("```", "").strip()
