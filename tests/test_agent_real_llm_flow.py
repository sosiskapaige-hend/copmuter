"""Регрессия «агент в режиме Агент отвечает, но ничего не делает».

Поднимаем фейковый OpenAI-совместимый сервер (как LM Studio), который ведёт
себя как реальная модель:
  * планировщик — отдаёт JSON-план;
  * первый ход — «болтает» текстом («Сейчас создам папку…») БЕЗ вызова
    инструмента (типичное поведение локальных моделей);
  * дальше — вызывает fs_mkdir только если ВИДИТ его в `tools` payload'а;
  * потом проверяет fs_list и вызывает finish_task.

До исправления: модели уходили только 8 инструментов (ask_user + browser_*),
system prompt резался до 1200 символов, а текстовый ответ считался финалом —
задача помечалась «done», папка не создавалась.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config
from agent.events import EventBus, InteractionGateway
from agent.llm.openai_client import OpenAICompatibleLLM
from agent.memory.longterm import LongTermMemory
from agent.memory.session import SessionStore
from agent.platform import get_platform
from agent.safety.journal import Journal
from agent.safety.policy import SafetyPolicy
from agent.agent.core import Agent
from agent.tools import build_registry


class FakeModel:
    """Сценарий «реальной» модели. Хранит все полученные payload'ы."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.chatter_done = False

    def reply(self, payload: dict) -> dict:
        self.requests.append(payload)
        msgs = payload.get("messages") or []
        tools = payload.get("tools") or []
        tool_names = {t["function"]["name"] for t in tools}
        text_all = "\n".join(str(m.get("content", "")) for m in msgs)
        last_user = next((str(m.get("content", "")) for m in reversed(msgs)
                          if m.get("role") == "user"), "")

        # --- планировщик / оценка (json_mode) ---
        if payload.get("response_format"):
            if "планировщик" in text_all:
                return self._msg(json.dumps({"summary": "создать папку", "steps": [
                    {"title": "Создать папку", "detail": "fs_mkdir"},
                    {"title": "Проверить", "detail": "fs_list"}]}))
            # assess: достигнуто, только если в списке действий есть успешный mkdir
            achieved = "✔ fs_mkdir" in text_all
            return self._msg(json.dumps({"progress": 100 if achieved else 10,
                                         "achieved": achieved, "remaining": [] if achieved else ["создать"],
                                         "message": "ok" if achieved else "ничего не сделано"}))

        # --- ход агента ---
        if "РЕЗУЛЬТАТ ИНСТРУМЕНТА fs_list" in last_user:
            return self._tool_call("finish_task", {"summary": "Папка RealFlow создана и проверена."})
        if "РЕЗУЛЬТАТ ИНСТРУМЕНТА fs_mkdir" in last_user:
            return self._tool_call("fs_list", {"path": "RealFlow"})
        if not self.chatter_done:
            # первый ход — как многие локальные модели: текст без действия
            self.chatter_done = True
            return self._msg("Хорошо! Сейчас создам папку RealFlow и проверю её.")
        if "fs_mkdir" in tool_names:
            return self._tool_call("fs_mkdir", {"path": "RealFlow"})
        # инструмента нет в списке — модель «не может» и снова болтает
        return self._msg("Готово, папка создана.")

    @staticmethod
    def _msg(content: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    @staticmethod
    def _tool_call(name: str, args: dict) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": "",
                                         "tool_calls": [{"id": "c1", "type": "function",
                                                         "function": {"name": name,
                                                                      "arguments": json.dumps(args)}}]}}]}


def serve_fake(model: FakeModel):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            body = json.dumps(model.reply(payload)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def build_agent(tmp: Path, llm):
    cfg = Config()
    cfg.agent_home = tmp / "home"
    cfg.safety.mode = "auto"
    cfg.agent.max_iterations = 12
    cfg.ensure_dirs()
    bus = EventBus()
    gw = InteractionGateway(bus)
    reg = build_registry()
    agent = Agent(cfg, llm, reg, bus, gw, Journal(cfg.journal_file),
                  SessionStore(cfg.state_dir), LongTermMemory(cfg.memory_dir),
                  SafetyPolicy("critical", 10), get_platform(), workdir=str(tmp))
    return agent, bus


class TestRealLLMFlow(unittest.IsolatedAsyncioTestCase):
    async def test_agent_actually_creates_folder(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent_realflow_"))
        orig = os.getcwd()
        os.chdir(tmp)
        model = FakeModel()
        srv, url = serve_fake(model)
        try:
            llm = OpenAICompatibleLLM(url, "", "qwen3-vl-8b-instruct", max_tokens=1024)
            agent, bus = build_agent(tmp, llm)
            events = []
            bus.subscribe(lambda e: events.append(e))
            st = await asyncio.wait_for(agent.run_task("Создай папку RealFlow и проверь", mode="auto"),
                                        timeout=60)
            self.assertEqual(st.status, "done", st.summary)
            self.assertTrue((tmp / "RealFlow").is_dir(), "папка НЕ создана — агент только поговорил")
            called = [e.data["name"] for e in events if e.type == "tool_call"]
            self.assertIn("fs_mkdir", called)
            self.assertIn("fs_list", called)
            # модель видела релевантный набор (бюджет 40), а не первые 8 по алфавиту
            action_payloads = [p for p in model.requests if p.get("tools")]
            self.assertTrue(action_payloads)
            names = {t["function"]["name"] for t in action_payloads[0]["tools"]}
            self.assertGreaterEqual(len(names), 30)
            for must in ("fs_mkdir", "fs_list", "terminal_run", "launch_app", "open_url",
                         "finish_task", "ask_user"):
                self.assertIn(must, names)
            # system prompt не обрезан до 1200 символов и содержит правила + план
            sysmsg = action_payloads[0]["messages"][0]["content"]
            self.assertGreater(len(sysmsg), 3000)
            self.assertIn("finish_task", sysmsg)
            self.assertIn("Создать папку", sysmsg)     # план виден модели
            # «болтовня» была зафиксирована предупреждением, а не финалом
            warns = [e for e in events if e.type == "log" and "без действия" in e.data.get("message", "")]
            self.assertTrue(warns, "текстовый ответ без действия должен логироваться как предупреждение")
        finally:
            srv.shutdown()
            os.chdir(orig)

    async def test_chatter_only_model_fails_honestly(self):
        """Модель, которая ТОЛЬКО говорит, — задача должна завершиться failed
        с внятным объяснением, а не done."""
        tmp = Path(tempfile.mkdtemp(prefix="agent_chatter_"))
        orig = os.getcwd()
        os.chdir(tmp)

        class ChatterOnly(FakeModel):
            def reply(self, payload):
                self.requests.append(payload)
                if payload.get("response_format"):
                    if "планировщик" in "\n".join(str(m.get("content", "")) for m in payload["messages"]):
                        return self._msg(json.dumps({"summary": "s", "steps": [{"title": "t", "detail": "d"}]}))
                    return self._msg(json.dumps({"progress": 0, "achieved": False}))
                return self._msg("Конечно! Папка Chatter создана.")

        model = ChatterOnly()
        srv, url = serve_fake(model)
        try:
            llm = OpenAICompatibleLLM(url, "", "qwen3-vl-8b-instruct")
            agent, bus = build_agent(tmp, llm)
            st = await asyncio.wait_for(agent.run_task("Создай папку Chatter", mode="auto"), timeout=60)
            self.assertEqual(st.status, "failed")
            self.assertIn("не вызывает инструменты", st.summary)
            self.assertFalse((tmp / "Chatter").exists())
        finally:
            srv.shutdown()
            os.chdir(orig)


if __name__ == "__main__":
    unittest.main()
