"""Регрессионные тесты wire-формата сообщений для OpenAI-совместимого клиента.

Ошибка: сервер (LM Studio и др.) возвращал HTTP 400
{"error":"Your payload's 'messages' array in misformatted. Messages from roles
[user, system, tool] must contain a 'content' field. Got 'object'."} —
потому что наблюдения инструментов попадали в историю как
{"role": "tool", "text": ...} без поля content.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llm.openai_client import OpenAICompatibleLLM


def make_llm():
    return OpenAICompatibleLLM(base_url="http://localhost:1234/v1", api_key="",
                               model="qwen3-vl-8b-instruct",
                               supports_tool_calling=True, vision=True)


class TestWireFormat(unittest.TestCase):
    def test_tool_message_gets_string_content(self):
        llm = make_llm()
        # формат, который исторически формировал агентный цикл
        msg = {"role": "tool", "tool": "fs_list", "ok": True, "text": "a.txt"}
        wire = llm._compact_messages([msg])[0]
        # наблюдение уходит как user-сообщение (role=tool без tool_call_id
        # отвергают строгие серверы), с именем инструмента и статусом
        self.assertEqual(wire["role"], "user")
        self.assertIn("content", wire)
        self.assertIsInstance(wire["content"], str)
        self.assertIn("a.txt", wire["content"])
        self.assertIn("fs_list", wire["content"])
        self.assertIn("OK", wire["content"])
        # служебные поля не должны уходить на сервер
        self.assertNotIn("text", wire)
        self.assertNotIn("ok", wire)
        self.assertNotIn("tool", wire)

    def test_tool_message_with_content_is_kept(self):
        llm = make_llm()
        msg = {"role": "tool", "tool": "fs_read", "ok": False, "content": "данные"}
        wire = llm._compact_messages([msg])[0]
        self.assertIn("данные", wire["content"])
        self.assertIn("ОШИБКА", wire["content"])

    def test_history_keeps_tail_not_head(self):
        """Раньше отправлялись ПЕРВЫЕ 20 сообщений — модель не видела свежих
        наблюдений. Теперь system сохраняется, из остальных берётся хвост."""
        llm = make_llm()
        msgs = [{"role": "system", "content": "sys"}]
        msgs += [{"role": "user", "content": f"m{i}"} for i in range(100)]
        wire = llm._compact_messages(msgs)
        self.assertEqual(wire[0], {"role": "system", "content": "sys"})
        self.assertEqual(wire[-1]["content"], "m99")
        self.assertLessEqual(len(wire), llm.MAX_HISTORY_MESSAGES + 1)

    def test_system_prompt_not_truncated_to_1200(self):
        """System prompt с правилами/планом/инструментами не должен резаться
        до 1200 символов (иначе модель не видит ни правил, ни инструментов)."""
        llm = make_llm()
        big = "x" * 9000
        wire = llm._compact_messages([{"role": "system", "content": big}])[0]
        self.assertEqual(len(wire["content"]), 9000)

    def test_next_action_payload_all_content_fields_are_strings(self):
        llm = make_llm()
        captured = {}

        def fake_post(payload):
            captured["payload"] = payload
            return {"choices": [{"message": {"role": "assistant",
                                             "content": "готово"}}]}

        llm._post = fake_post
        history = [
            {"role": "tool", "tool": "fs_list", "ok": True, "text": "a.txt"},
            {"role": "tool", "tool": "terminal_run", "ok": False, "text": "ошибка"},
            {"role": "user", "content": "Ответ пользователя: да"},
        ]
        asyncio.run(llm.next_action("Цель", "контекст", history, tools=None))
        for m in captured["payload"]["messages"]:
            if m["role"] in ("user", "system", "tool"):
                self.assertIn("content", m, m)
                self.assertIsInstance(m["content"], str, m)

    def test_multimodal_content_preserved(self):
        llm = make_llm()
        parts = [{"type": "text", "text": "что на экране?"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
        wire = llm._compact_messages([{"role": "user", "content": parts}])[0]
        self.assertEqual(wire["role"], "user")
        self.assertEqual(wire["content"], parts)


if __name__ == "__main__":
    unittest.main()


class TestToolsExposure(unittest.TestCase):
    """Регрессия «агент отвечает, но ничего не делает»: модели уходило только
    8 инструментов из 83 (ask_user + browser_*), без fs_mkdir/terminal_run/
    launch_app/finish_task — и она физически не могла действовать."""

    def _capture_next_action(self, llm, history=None):
        from agent.tools import build_registry
        reg = build_registry()
        captured = {}

        def fake_post(payload):
            captured["payload"] = payload
            return {"choices": [{"message": {"role": "assistant", "content": "текст"}}]}

        llm._post = fake_post
        asyncio.run(llm.next_action("Создай папку Test на рабочем столе", "контекст",
                                    history or [], reg.schemas()))
        return captured["payload"], reg

    def test_all_tools_are_sent(self):
        llm = make_llm()
        payload, reg = self._capture_next_action(llm)
        names = {t["function"]["name"] for t in payload["tools"]}
        self.assertEqual(names, set(reg.names()))
        for must in ("fs_mkdir", "terminal_run", "launch_app", "open_url", "finish_task", "ask_user"):
            self.assertIn(must, names)
        self.assertEqual(payload.get("tool_choice"), "auto")

    def test_plain_text_is_not_final(self):
        """Текст «Сейчас создам папку…» без tool_call — НЕ завершение задачи."""
        llm = make_llm()
        from agent.tools import build_registry
        reg = build_registry()
        llm._post = lambda p: {"choices": [{"message": {"role": "assistant",
                                                        "content": "Сейчас создам папку Test."}}]}
        d = asyncio.run(llm.next_action("Создай папку Test", "ctx", [], reg.schemas()))
        self.assertIsNone(d.final)
        self.assertIsNone(d.tool_call)
        self.assertIn("Сейчас создам", d.thought)

    def test_final_marker_is_final(self):
        llm = make_llm()
        llm._post = lambda p: {"choices": [{"message": {"role": "assistant",
                                                        "content": "FINAL_ANSWER: папка создана и проверена"}}]}
        d = asyncio.run(llm.next_action("x", "ctx", [], []))
        self.assertEqual(d.final, "папка создана и проверена")

    def test_native_tool_call_parsed(self):
        llm = make_llm()
        llm._post = lambda p: {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "fs_mkdir", "arguments": "{\"path\": \"~/Desktop/Test\"}"}}]}}]}
        from agent.tools import build_registry
        d = asyncio.run(llm.next_action("x", "ctx", [], build_registry().schemas()))
        self.assertIsNotNone(d.tool_call)
        self.assertEqual(d.tool_call.name, "fs_mkdir")
        self.assertEqual(d.tool_call.args, {"path": "~/Desktop/Test"})

    def test_textual_tool_call_variants(self):
        """Локальные модели пишут вызовы текстом: <tool_call>, ```json```,
        {"tool": ...}, {"name": ..., "arguments": ...}, с <think>."""
        from agent.tools import build_registry
        schemas = build_registry().schemas()
        variants = [
            '<tool_call>{"name": "fs_mkdir", "arguments": {"path": "Test"}}</tool_call>',
            '```json\n{"tool": "fs_mkdir", "args": {"path": "Test"}}\n```',
            '{"thought": "создаю", "tool": "fs_mkdir", "args": {"path": "Test"}}',
            '<think>надо создать папку</think>\n{"name": "fs_mkdir", "arguments": "{\\"path\\": \\"Test\\"}"}',
            'Создаю папку: {"function_call": {"name": "fs_mkdir", "arguments": {"path": "Test"}}}',
        ]
        for v in variants:
            llm = make_llm()
            llm.supports_tool_calling = False   # JSON-протокол
            llm._post = lambda p, v=v: {"choices": [{"message": {"role": "assistant", "content": v}}]}
            d = asyncio.run(llm.next_action("x", "ctx", [], schemas))
            self.assertIsNotNone(d.tool_call, v)
            self.assertEqual(d.tool_call.name, "fs_mkdir", v)
            self.assertEqual(d.tool_call.args, {"path": "Test"}, v)

    def test_textual_finish_and_ask(self):
        from agent.tools import build_registry
        schemas = build_registry().schemas()
        llm = make_llm()
        llm.supports_tool_calling = False
        llm._post = lambda p: {"choices": [{"message": {
            "role": "assistant",
            "content": '{"tool": "finish_task", "args": {"summary": "готово, папка есть"}}'}}]}
        d = asyncio.run(llm.next_action("x", "ctx", [], schemas))
        self.assertEqual(d.final, "готово, папка есть")
        llm._post = lambda p: {"choices": [{"message": {
            "role": "assistant",
            "content": '{"tool": "ask_user", "args": {"question": "какое имя папки?"}}'}}]}
        d = asyncio.run(llm.next_action("x", "ctx", [], schemas))
        self.assertEqual(d.ask, "какое имя папки?")

    def test_json_protocol_without_native_tools(self):
        """Без function calling инструменты описываются в system prompt,
        а поле tools в payload отсутствует."""
        llm = make_llm()
        llm.supports_tool_calling = False
        payload, reg = self._capture_next_action(llm)
        self.assertNotIn("tools", payload)
        sysmsg = payload["messages"][0]["content"]
        self.assertIn("fs_mkdir(", sysmsg)
        self.assertIn("JSON", sysmsg)

    def test_weak_model_heuristic(self):
        weak = OpenAICompatibleLLM._is_weak_model
        # действительно маленькие
        self.assertTrue(weak("qwen2.5-0.5b-instruct"))
        self.assertTrue(weak("llama-3.2-1b-instruct"))
        self.assertTrue(weak("gemma-2-2b-it"))
        self.assertTrue(weak("tinyllama-1.1b"))
        # раньше ошибочно считались «слабыми» по подстроке mini/2b
        self.assertFalse(weak("gpt-4o-mini"))
        self.assertFalse(weak("gemini-2.5-flash"))
        self.assertFalse(weak("gemma-3-12b-it"))
        self.assertFalse(weak("qwen2.5-vl-72b-instruct"))
        self.assertFalse(weak("qwen3-vl-8b-instruct"))
        self.assertFalse(weak("mistral-7b-instruct-v0.2"))

    def test_assess_without_actions_is_not_achieved(self):
        llm = make_llm()
        llm._post = lambda p: {"choices": [{"message": {"role": "assistant",
                                                        "content": '{"progress": 100, "achieved": true}'}}]}
        a = asyncio.run(llm.assess("Создай папку", "", None))
        self.assertFalse(a.achieved)
        self.assertEqual(a.progress, 0)
