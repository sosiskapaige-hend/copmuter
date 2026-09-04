import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config
from agent.runtime import AgentRuntime
from agent.ui.web import serve


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


class TestWebApi(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agent_web_"))
        cls._orig_cwd = os.getcwd()
        os.chdir(cls.tmp)
        cfg = Config()
        cfg.agent_home = cls.tmp / "agent_home"
        cfg.llm.provider = "mock"
        cfg.safety.mode = "auto"
        cfg.web_ui = {"host": "127.0.0.1", "port": 0}
        cfg.ensure_dirs()
        cls.rt = AgentRuntime(cfg, workdir=str(cls.tmp))
        cls.rt.start()
        # выбираем свободный порт
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        cls.ui = serve(cls.rt, host="127.0.0.1", port=cls.port, open_browser=False)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.ui.stop()
        cls.rt.stop()
        os.chdir(cls._orig_cwd)

    def _wait(self, cond, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return True
            time.sleep(0.3)
        return False

    def test_state(self):
        d = get_json(self.base + "/api/state")
        self.assertIn("llm", d)
        self.assertIn("tools", d)
        self.assertGreater(len(d["tools"]), 20)

    def test_system(self):
        d = get_json(self.base + "/api/system")
        self.assertIsInstance(d, dict)

    def test_task_lifecycle(self):
        r = post_json(self.base + "/api/task",
                      {"text": "Создай папку WebTest через веб-интерфейс"})
        self.assertTrue(r.get("ok"), r)
        tid = r["task_id"]

        def done():
            try:
                st = self.rt.sessions.active.get(tid)
                return st is not None and st.status in ("done", "failed", "cancelled")
            except Exception:
                return False
        self.assertTrue(self._wait(done), "задача не завершилась")
        st = self.rt.sessions.active[tid]
        self.assertEqual(st.status, "done", st.summary)
        self.assertTrue((self.tmp / "WebTest").is_dir())

    def test_mode_switch(self):
        r = post_json(self.base + "/api/mode", {"mode": "plan_only"})
        self.assertEqual(r.get("mode"), "plan_only")
        post_json(self.base + "/api/mode", {"mode": "auto"})

    def test_memory_api(self):
        r = post_json(self.base + "/api/memory", {"action": "add", "text": "тест-предпочтение"})
        self.assertTrue(r.get("ok"))
        items = get_json(self.base + "/api/state")["memory"]
        self.assertTrue(any("тест-предпочтение" in i["text"] for i in items))

    def test_schedule_and_trigger_api(self):
        r = post_json(self.base + "/api/schedule",
                      {"action": "add", "expr": "every 30m", "text": "проверь почту"})
        self.assertTrue(r.get("ok"), r)
        sid = r.get("id")
        self.assertTrue(post_json(self.base + "/api/schedule", {"action": "remove", "id": sid})["ok"])
        r = post_json(self.base + "/api/trigger",
                      {"action": "add", "path": str(self.tmp / "DL"), "pattern": "*.pdf",
                       "text": "обработай {file}"})
        self.assertTrue(r.get("ok"), r)

    def test_journal_endpoint(self):
        d = get_json(self.base + "/api/journal")
        self.assertIsInstance(d, list)

    def test_index_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            html = r.read().decode()
        self.assertIn("AI Computer Agent", html)
        self.assertTrue("Создай папку" in html or "Задача" in html)

    def test_settings_api(self):
        # GET: текущие настройки
        d = get_json(self.base + "/api/settings")
        self.assertTrue(d.get("ok"))
        self.assertIn("base_url", d["llm"])
        # POST: смена base_url/model (без ключа — как Ollama)
        r = post_json(self.base + "/api/settings",
                      {"llm": {"base_url": "http://localhost:11434/v1",
                               "model": "llama3.1",
                               "supports_tool_calling": False}})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("model"), "llama3.1")
        # LLM реально пересоздан (имя openai-compatible вместо mock)
        self.assertEqual(self.rt.llm.name, "openai-compatible")
        d2 = get_json(self.base + "/api/settings")
        self.assertEqual(d2["llm"]["base_url"], "http://localhost:11434/v1")
        # вернулось mock-состояние
        post_json(self.base + "/api/settings",
                  {"llm": {"base_url": "https://api.openai.com/v1",
                           "model": "gpt-4o", "api_key": "sk-test",
                           "supports_tool_calling": True}})
        self.assertEqual(self.rt.cfg.llm.model, "gpt-4o")
        self.assertTrue(self.rt.cfg.llm.api_key)
        self.assertEqual(self.rt.llm.name, "openai-compatible")
        # возвращаем mock, чтобы остальные тесты не ходили в интернет
        self.rt.cfg.llm.provider = "mock"
        self.rt.reload_llm()
        self.assertEqual(self.rt.llm.name, "mock")

    def test_stt_graceful(self):
        # в песочнице faster-whisper нет → аккуратный 503 с сообщением
        req = urllib.request.Request(self.base + "/api/stt",
                                     data=b'{"seconds": 2}',
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            d = json.loads(e.read().decode())
            self.assertEqual(e.code, 503)
        self.assertFalse(d.get("ok", False) is True and "text" in d)

    def test_sse_stream(self):
        # короткий SSE-запрос: читаем первые данные
        req = urllib.request.Request(self.base + "/api/events")
        with urllib.request.urlopen(req, timeout=8) as r:
            line = r.readline()
            self.assertIn(b"event-stream", r.headers.get("Content-Type").encode())
            # история придёт сразу
            data = b""
            for _ in range(5):
                chunk = r.readline()
                data += chunk
                if b"task" in data or b"log" in data:
                    break
        self.assertTrue(b"data:" in data)


if __name__ == "__main__":
    unittest.main()
