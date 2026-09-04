"""Тесты чат-подсистемы: хранилище, API, SSE-события генерации."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config
from agent.runtime import AgentRuntime
from agent.ui.web import serve


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def post_json(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 4xx с JSON-телом — валидный ответ сервера
        return json.loads(e.read().decode())


class TestChat(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agent_chat_"))
        cls._orig_cwd = os.getcwd()
        os.chdir(cls.tmp)
        cfg = Config()
        cfg.agent_home = cls.tmp / "home"
        cfg.llm.provider = "mock"
        cfg.safety.mode = "auto"
        cfg.chat.history_messages = 8
        cfg.ensure_dirs()
        cls.rt = AgentRuntime(cfg, workdir=str(cls.tmp))
        cls.rt.start()
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        cls.ui = serve(cls.rt, host="127.0.0.1", port=cls.port, open_browser=False)
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.events = []
        cls._unsub = cls.rt.bus.subscribe(lambda ev: cls.events.append(ev))

    @classmethod
    def tearDownClass(cls):
        cls.ui.stop()
        cls.rt.stop()
        os.chdir(cls._orig_cwd)

    def _wait(self, cond, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def _wait_event(self, type_, **match):
        def found():
            for ev in self.events:
                if ev.type != type_:
                    continue
                if all(ev.data.get(k) == v for k, v in match.items()):
                    return True
            return False
        ok = self._wait(found)
        self.assertTrue(ok, f"событие {type_} {match} не пришло; есть: "
                            f"{[e.type for e in self.events][-20:]}")
        return True

    def test_01_store_crud(self):
        st = self.rt.chats.store
        chat = st.create()
        self.assertTrue(st.has(chat["id"]))
        st.append(chat["id"], "user", "привет")
        st.append(chat["id"], "assistant", "здравствуй")
        got = st.get(chat["id"])
        self.assertEqual(len(got["messages"]), 2)
        self.assertEqual(got["messages"][0]["role"], "user")
        st.rename(chat["id"], "тест")
        self.assertEqual(st.get(chat["id"])["title"], "тест")
        lst = st.list()
        self.assertTrue(any(c["id"] == chat["id"] for c in lst))
        self.assertTrue(st.delete(chat["id"]))
        self.assertFalse(st.has(chat["id"]))

    def test_02_chat_send_stream(self):
        r = post_json(self.base + "/api/chat/send",
                      {"text": "привет, кто ты?"})
        self.assertTrue(r.get("ok"), r)
        cid = r["chat_id"]
        self.assertTrue(cid)
        # заголовок чата взялся из сообщения
        self.assertIn("привет", r.get("title", ""))
        self._wait_event("chat_done", chat_id=cid, ok=True)
        # сообщение сохранено в хранилище
        chat = get_json(self.base + f"/api/chats/{cid}")["chat"]
        roles = [m["role"] for m in chat["messages"]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertIn("mock", chat["messages"][1]["content"])

    def test_03_chat_list_and_rename(self):
        r = post_json(self.base + "/api/chat/send", {"text": "второй чат"})
        cid = r["chat_id"]
        self._wait_event("chat_done", chat_id=cid)
        lst = get_json(self.base + "/api/chats")["chats"]
        self.assertTrue(any(c["id"] == cid for c in lst))
        rr = post_json(self.base + f"/api/chats/{cid}/rename", {"title": "переименован"})
        self.assertTrue(rr["ok"])
        lst = get_json(self.base + "/api/chats")["chats"]
        self.assertEqual(next(c for c in lst if c["id"] == cid)["title"],
                         "переименован")

    def test_04_chat_delete(self):
        r = post_json(self.base + "/api/chat/send", {"text": "на удаление"})
        cid = r["chat_id"]
        self._wait_event("chat_done", chat_id=cid)
        d = post_json(self.base + f"/api/chats/{cid}/delete", {})
        self.assertTrue(d.get("ok"))
        self.assertIsNone(self.rt.chats.store.get(cid))

    def test_05_agent_mode_in_chat(self):
        r = post_json(self.base + "/api/chat/send",
                      {"text": "Создай папку ChatAgentTest и проверь её",
                       "agent": True})
        self.assertTrue(r.get("ok"), r)
        cid = r["chat_id"]
        # агент должен отработать и дать финальное сообщение
        self._wait_event("chat_done", chat_id=cid)
        chat = self.rt.chats.store.get(cid)
        self.assertIsNotNone(chat)
        self.assertEqual(chat["messages"][-1]["role"], "assistant")
        self.assertTrue((self.tmp / "ChatAgentTest").is_dir())
        # активности записаны в сообщение
        last = chat["messages"][-1]
        self.assertTrue(last.get("activities"), "нет activities в ответе агента")

    def test_06_stop_nonbusy(self):
        r = post_json(self.base + "/api/chat/stop", {"chat_id": "nope"})
        self.assertFalse(r.get("ok"))

    def test_07_upload_and_file_serving(self):
        data = b"hello world \xf0\x9f\x96\x96"
        req = urllib.request.Request(
            self.base + "/api/upload", data=data, method="POST",
            headers={"Content-Type": "text/plain",
                     "X-File-Name": urllib.parse.quote("привет.txt")})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                j = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            j = json.loads(e.read().decode())
        self.assertTrue(j["ok"], j)
        att = j["attachment"]
        self.assertEqual(att["name"], "привет.txt")
        self.assertEqual(att["size"], len(data))
        fname = Path(att["path"]).name
        with urllib.request.urlopen(
                self.base + "/api/files/" + urllib.parse.quote(fname),
                timeout=10) as resp:
            self.assertEqual(resp.read(), data)

    def test_08_empty_send_rejected(self):
        r = post_json(self.base + "/api/chat/send", {"text": "   "})
        self.assertFalse(r.get("ok"))

    def test_09_llm_models_endpoint(self):
        j = get_json(self.base + "/api/llm/models")
        self.assertIn("ok", j)

    def test_10_index_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            html = r.read().decode()
        self.assertIn("AI Computer Agent", html)
        self.assertIn("Создай папку", html)


if __name__ == "__main__":
    unittest.main()
