"""Тесты мозга без LM Studio: подменяем клиент модели и проверяем решения воркера.

    python3 -m unittest discover -s ai/tests -p 'test_*.py'
"""

from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from ai import protocol
from ai.config import WorkerConfig
from ai.context import ContextManager, estimate_tokens
from ai.llm import LLMError, LLMReply
from ai.memory import Memory
from ai.vision import FrameGeometry, click_call, normalize_bbox, parse_elements, pick_best
from ai.worker import AiWorker, extract_tool_calls, strip_code_fences, to_openai_tools


class FakeClient:
    """Подмена LM Studio: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, replies: list[LLMReply] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.vision_calls: list[str] = []
        self._stats = {"calls": 0, "errors": 0, "last_ms": 1.0}

    def _next(self) -> LLMReply:
        return self.replies.pop(0) if self.replies else LLMReply(content="готово")

    def chat(self, messages, **kwargs) -> LLMReply:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._next()

    def vision(self, prompt, image_bytes=None, **kwargs) -> LLMReply:
        self.vision_calls.append(prompt)
        return self._next()

    def preflight(self, **kwargs) -> dict:
        return {"ready": True, "model_present": True, "tools_ok": True, "vision_ok": True}

    @property
    def stats(self) -> dict:
        self._stats["calls"] = len(self.calls)
        return self._stats

    def close(self) -> None:
        pass


def make_worker(replies=None, **kwargs) -> tuple[AiWorker, FakeClient, str]:
    tmp = tempfile.mkdtemp(prefix="ai_worker_test_")
    cfg = WorkerConfig(state_dir=tmp, history_limit=6)
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    client = FakeClient(replies)
    worker = AiWorker(cfg, client=client, memory=Memory(cfg.db_path))
    return worker, client, tmp


def tool_call(name: str, args: dict, note: str = "") -> LLMReply:
    return LLMReply(content="", tool_calls=[{"function": {"name": name,
                                                          "arguments": json.dumps(args)}}],
                    finish_reason="tool_calls")


class TestToolConversion(unittest.TestCase):
    def test_schemas_to_openai(self) -> None:
        schemas = to_openai_tools([
            {"name": "launch_application", "description": "запуск",
             "parameters": "{\"type\":\"object\",\"properties\":{}}"},
            {"name": "broken"},
        ])
        self.assertEqual(schemas[0]["function"]["name"], "launch_application")
        self.assertEqual(schemas[0]["function"]["parameters"]["type"], "object")
        self.assertEqual(schemas[1]["function"]["parameters"], {"type": "object", "properties": {}})

    def test_native_calls(self) -> None:
        reply = tool_call("launch_application", {"name": "telegram"})
        calls = extract_tool_calls(reply, [{"name": "launch_application"}])
        self.assertEqual(calls[0]["tool"], "launch_application")
        self.assertEqual(calls[0]["args"]["name"], "telegram")

    def test_text_calls(self) -> None:
        reply = LLMReply(content="думаю…\n<tool_call>{\"tool\":\"open_url\","
                                 "\"args\":{\"url\":\"https://ya.ru\"}}</tool_call>")
        calls = extract_tool_calls(reply, [{"name": "open_url"}])
        self.assertEqual(calls[0]["args"]["url"], "https://ya.ru")

    def test_unknown_tool_is_dropped(self) -> None:
        reply = tool_call("teleport_user", {})
        calls = extract_tool_calls(reply, [{"name": "launch_application"}])
        self.assertEqual(calls, [])

    def test_code_fences(self) -> None:
        self.assertEqual(strip_code_fences("```python\nprint('hi')\n```", "python"), "print('hi')")


class TestPlan(unittest.TestCase):
    def test_plan_returns_calls_and_uses_state(self) -> None:
        worker, client, _ = make_worker([tool_call("launch_application", {"name": "telegram"})])
        reply = worker.handle({"id": 1, "type": "plan", "task": "открой телегу", "step": 1,
                               "tools": [{"name": "launch_application", "description": "запуск"}],
                               "state": {"active_window": "Проводник"}})
        worker.close()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["calls"][0]["args"]["name"], "telegram")
        self.assertFalse(reply["finished"])
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("Проводник", system)
        self.assertEqual(client.calls[0]["kwargs"]["tools"][0]["function"]["name"],
                         "launch_application")

    def test_plan_passes_app_registry_to_context(self) -> None:
        worker, client, _ = make_worker([tool_call("launch_application", {"name": "discord"})])
        reply = worker.handle({"id": 3, "type": "plan", "task": "открой дискорд и отправь сообщение",
                               "step": 1,
                               "tools": [{"name": "launch_application", "description": "запуск"}],
                               "apps": [{"key": "discord", "name": "Discord",
                                         "path": "C:/Users/me/AppData/Discord.exe"}]})
        worker.close()
        self.assertTrue(reply["ok"], reply)
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("C:/Users/me/AppData/Discord.exe", system)

    def test_finished_without_calls(self) -> None:
        worker, _, _ = make_worker([LLMReply(content="Готово, задача выполнена")])
        reply = worker.handle({"id": 2, "type": "plan", "task": "установи и настрой окружение"})
        worker.close()
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["finished"])
        self.assertEqual(reply["calls"], [])

    def test_empty_answer_is_honest_error(self) -> None:
        worker, _, _ = make_worker([LLMReply(content="")])
        reply = worker.handle({"id": 3, "type": "plan", "task": "сделай непонятное"})
        worker.close()
        self.assertFalse(reply["ok"])
        self.assertIn("не предложила действий", reply["error"])

    def test_repeat_guard_only_after_first_step(self) -> None:
        same = tool_call("open_url", {"url": "https://ya.ru"})
        worker, _, _ = make_worker([same, same, same])
        first = worker.handle({"id": 1, "type": "plan", "task": "открой яндекс", "step": 1})
        second = worker.handle({"id": 2, "type": "replan", "task": "открой яндекс", "step": 2})
        worker.close()
        self.assertEqual(len(first["calls"]), 1)
        self.assertFalse(second["ok"])               # повтор отклонён, и это честно сказано
        self.assertIn("не предложила действий", second["error"])

    def test_model_failure_reported(self) -> None:
        class Broken(FakeClient):
            def chat(self, messages, **kwargs):
                raise LLMError("нет связи с LM Studio")

        tmp = tempfile.mkdtemp(prefix="ai_worker_test_")
        cfg = WorkerConfig(state_dir=tmp)
        worker = AiWorker(cfg, client=Broken(), memory=Memory(cfg.db_path))
        reply = worker.handle({"id": 1, "type": "plan", "task": "открой телегу"})
        worker.close()
        self.assertFalse(reply["ok"])
        self.assertIn("модель недоступна", reply["error"])

    def test_no_tools_means_no_function_calling(self) -> None:
        worker, client, _ = make_worker([tool_call("launch_application", {"name": "telegram"})],
                                        native_tools=False)
        worker.handle({"id": 1, "type": "plan", "task": "открой телегу", "step": 1,
                       "tools": [{"name": "launch_application"}]})
        worker.close()
        self.assertNotIn("tools", client.calls[0]["kwargs"])


class TestOtherHandlers(unittest.TestCase):
    def test_chat_saves_history(self) -> None:
        worker, _, _ = make_worker([LLMReply(content="Привет! Чем помочь?")])
        reply = worker.handle({"id": 1, "type": "chat", "task": "привет", "chat_id": 1})
        history = worker.memory.recent_messages(1)
        worker.close()
        self.assertEqual(reply["kind"], "chat")
        self.assertIn("Привет", reply["say"])
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])

    def test_code_returns_clean_text(self) -> None:
        worker, _, _ = make_worker([LLMReply(content="```python\nprint(1)\n```")])
        reply = worker.handle({"id": 1, "type": "code", "task": "калькулятор", "language": "python"})
        worker.close()
        self.assertEqual(reply["kind"], "code")
        self.assertEqual(reply["say"].strip(), "print(1)")

    def test_vision_find_returns_screen_click(self) -> None:
        frame = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()
        answer = LLMReply(content=json.dumps({"found": True, "label": "кнопка ОК",
                                              "bbox": [400, 400, 500, 500], "confidence": 0.9}))
        worker, _, _ = make_worker([answer])
        reply = worker.handle({"id": 1, "type": "vision", "mode": "find", "target": "кнопка ОК",
                               "frame_b64": frame,
                               "frame": {"width": 800, "height": 600, "origin_x": 200,
                                         "origin_y": 100, "scale": 1.0}})
        worker.close()
        self.assertTrue(reply["ok"], reply)
        call = reply["calls"][0]
        self.assertEqual(call["tool"], "click")
        self.assertEqual(call["args"]["x"], 200 + int(round(0.45 * 800)))
        self.assertEqual(call["args"]["y"], 100 + int(round(0.45 * 600)))

    def test_vision_without_frame(self) -> None:
        worker, _, _ = make_worker()
        reply = worker.handle({"id": 1, "type": "vision", "mode": "find", "target": "кнопка"})
        worker.close()
        self.assertFalse(reply["ok"])
        self.assertIn("нет кадра", reply["error"])

    def test_unknown_request_kind(self) -> None:
        worker, _, _ = make_worker()
        reply = worker.handle({"id": 1, "type": "телепорт"})
        worker.close()
        self.assertFalse(reply["ok"])
        self.assertIn("неизвестный тип", reply["error"])

    def test_preflight_and_health(self) -> None:
        worker, _, _ = make_worker()
        preflight = worker.handle({"id": 1, "type": "preflight"})
        health = worker.handle({"id": 2, "type": "health"})
        worker.close()
        self.assertTrue(preflight["data"]["ready"])
        self.assertEqual(health["data"]["model"], worker.config.model)

    def test_compress_updates_summary(self) -> None:
        worker, _, _ = make_worker([LLMReply(content="Пользователь просил открыть телегу.")])
        reply = worker.handle({"id": 1, "type": "compress", "budget": 50,
                               "messages": [{"role": "user", "content": "открой телегу"}]})
        summary = worker.context.summary
        worker.close()
        self.assertEqual(reply["kind"], "summary")
        self.assertIn("телегу", summary)


class TestContextManager(unittest.TestCase):
    def test_tool_selection_prefers_task_words(self) -> None:
        manager = ContextManager(max_tokens=4096)
        tools = [
            {"name": "delete_path", "description": "удалить файл или папку"},
            {"name": "launch_application", "description": "запустить приложение"},
            {"name": "set_wallpaper", "description": "сменить обои"},
        ]
        chosen = [t["name"] for t in manager.select_tools(tools, "удали папку 123 с рабочего стола")]
        self.assertIn("delete_path", chosen)
        self.assertIn("launch_application", chosen)      # ядро инструментов всегда рядом

    def test_apps_from_registry_reach_the_model(self) -> None:
        """Реестр приложений этой машины: точные имена и пути вместо догадок модели."""
        manager = ContextManager(max_tokens=4096)
        messages = manager.build(
            "открой дискорд",
            apps=[{"key": "discord", "name": "Discord", "protocol": "discord://"},
                  {"key": "vscode", "name": "Visual Studio Code", "path": "C:/VSCode/Code.exe"}],
        )
        system = messages[0]["content"]
        self.assertIn("Discord", system)
        self.assertIn("discord://", system)
        self.assertIn("C:/VSCode/Code.exe", system)
        self.assertIn("не выдумывай", system)

    def test_apps_are_optional(self) -> None:
        manager = ContextManager(max_tokens=4096)
        messages = manager.build("просто задача", apps=[])
        self.assertNotIn("Приложения на этой машине", messages[0]["content"])

    def test_apps_list_is_capped(self) -> None:
        from ai import prompts
        many = [{"key": f"app{i}", "name": f"App {i}"} for i in range(40)]
        text = prompts.with_apps(many)
        self.assertEqual(text.count("\n- "), 12)          # в контекст идёт ровно 12 приложений
        self.assertIn("- App 11", text)
        self.assertNotIn("- App 12", text)

    def test_budget_is_respected(self) -> None:
        manager = ContextManager(max_tokens=4000, reserve_for_reply=500, history_limit=20)
        history = [{"role": "user", "content": "длинное сообщение " * 80} for _ in range(20)]
        messages = manager.build("задача", history=history, state={"active_window": "VS Code"})
        total = sum(estimate_tokens(m["content"]) for m in messages)
        self.assertLessEqual(total, 3500)
        self.assertLess(len(messages), len(history) + 1)      # история ужата
        self.assertIn("VS Code", messages[0]["content"])


class TestVisionHelpers(unittest.TestCase):
    def test_parse_and_pick(self) -> None:
        text = ('{"elements":[{"label":"Отправить","bbox":[900,930,980,980],"confidence":0.8},'
                '{"label":"Отмена","bbox":[10,10,80,60],"confidence":0.4}]}')
        elements = parse_elements(text)
        self.assertEqual(len(elements), 2)
        best = pick_best(elements, "отправить")
        self.assertIsNotNone(best)
        self.assertEqual(best.label, "Отправить")

    def test_bbox_conventions(self) -> None:
        # уже нормализованные 0..1000
        self.assertEqual(normalize_bbox([100, 200, 300, 400]), [100, 200, 300, 400])
        # доли экрана
        self.assertEqual(normalize_bbox([0.1, 0.2, 0.3, 0.4]), [100, 200, 300, 400])
        # пиксели кадра 2000x1000 (значения вышли за 1000) → нормализуем
        self.assertEqual(normalize_bbox([400, 300, 1500, 900], 2000, 1000), [200, 300, 750, 900])
        # словарь вместо списка
        self.assertEqual(normalize_bbox({"x1": 100, "y1": 100, "x2": 200, "y2": 200}),
                         [100, 100, 200, 200])
        self.assertEqual(normalize_bbox([1, 2, 3]), [0, 0, 0, 0])

    def test_geometry(self) -> None:
        geometry = FrameGeometry(width=1000, height=500, origin_x=100, origin_y=50, scale=2.0)
        self.assertEqual(geometry.normalized_to_screen(500, 500), (1100, 550))
        self.assertTrue(geometry.is_reasonable(10, 10))
        self.assertFalse(geometry.is_reasonable(5000, 10))


class TestVisionCoordinates(unittest.TestCase):
    def test_pixel_bbox_becomes_screen_click(self) -> None:
        """Модель ответила пикселями кадра — клик всё равно должен попасть в цель."""
        from ai.vision import Element

        geometry = FrameGeometry(width=2000, height=1000, origin_x=0, origin_y=0, scale=1.0)
        element = Element(label="Отправить", bbox=[1000, 500, 1500, 900], confidence=0.9)
        call = click_call(element, geometry)
        # пиксели 2000x1000 → 0..1000: [500,500,750,900] → центр (625,700) → экран (1250,700)
        self.assertEqual(call["args"]["x"], 1250)
        self.assertEqual(call["args"]["y"], 700)

    def test_empty_bbox_is_rejected(self) -> None:
        from ai.vision import Element

        geometry = FrameGeometry(width=800, height=600)
        with self.assertRaises(ValueError):
            click_call(Element(label="?", bbox=[0, 0, 0, 0]), geometry)


class TestProtocol(unittest.TestCase):
    def test_frame_roundtrip_over_socketpair(self) -> None:
        left, right = socket.socketpair()
        try:
            protocol.write_frame(left, {"id": 7, "type": "chat", "text": "привет"})
            request = protocol.decode(protocol.read_frame(right))
            self.assertEqual(request["id"], 7)
            protocol.write_frame(right, protocol.text_reply(7, "и тебе привет"))
            reply = protocol.decode(protocol.read_frame(left))
            self.assertEqual(reply["say"], "и тебе привет")
        finally:
            left.close()
            right.close()

    def test_oversized_frame_rejected(self) -> None:
        left, right = socket.socketpair()
        try:
            left.sendall(b"\xff\xff\xff\xff")
            with self.assertRaises(protocol.ProtocolError):
                protocol.read_frame(right)
        finally:
            left.close()
            right.close()


class TestServer(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "POSIX transport; Windows uses Named Pipes")
    def test_serve_handles_two_requests_on_one_channel(self) -> None:
        worker, _, tmp = make_worker([tool_call("open_url", {"url": "https://ya.ru"}),
                                      LLMReply(content="готово")])
        path = os.path.join(tmp, "test.sock")
        thread = threading.Thread(target=worker.serve, args=(path,), daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.02)
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(path)
            protocol.write_frame(client, {"id": 1, "type": "plan", "task": "открой яндекс",
                                          "step": 1,
                                          "tools": [{"name": "open_url"}]})
            first = protocol.decode(protocol.read_frame(client))
            protocol.write_frame(client, {"id": 2, "type": "health"})
            second = protocol.decode(protocol.read_frame(client))
            client.close()
        finally:
            worker.close()
        self.assertEqual(first["calls"][0]["tool"], "open_url")
        self.assertTrue(second["ok"])

    def test_serve_once(self) -> None:
        worker, _, _ = make_worker([LLMReply(content="ответ")])
        reply = json.loads(worker.serve_once(json.dumps({"id": 5, "type": "chat", "task": "привет"})))
        broken = json.loads(worker.serve_once("{не json"))
        worker.close()
        self.assertEqual(reply["say"], "ответ")
        self.assertFalse(broken["ok"])


if __name__ == "__main__":
    unittest.main()
