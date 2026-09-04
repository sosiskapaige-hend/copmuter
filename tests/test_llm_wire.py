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
        self.assertEqual(wire["role"], "tool")
        self.assertIn("content", wire)
        self.assertIsInstance(wire["content"], str)
        self.assertEqual(wire["content"], "a.txt")
        # служебные поля не должны уходить на сервер
        self.assertNotIn("text", wire)
        self.assertNotIn("ok", wire)
        self.assertNotIn("tool", wire)

    def test_tool_message_with_content_is_kept(self):
        llm = make_llm()
        msg = {"role": "tool", "tool": "fs_read", "ok": True, "content": "данные"}
        wire = llm._compact_messages([msg])[0]
        self.assertEqual(wire["content"], "данные")

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
