"""Web UI: dashboard на чистом stdlib (http.server) + SSE live-поток.

Точки входа:
  GET  /               — index.html (одностраничный дашборд)
  GET  /api/state      — снимок состояния (режим, задачи, память, расписания...)
  GET  /api/system     — CPU/RAM/диск
  GET  /api/screen     — последний скриншот (PNG)
  GET  /api/events     — SSE: живой поток событий агента
  GET  /api/journal    — журнал действий (undo)
  GET  /api/voice      — доступность голосовых модулей
  POST /api/task       — {text, mode?, background?}
  POST /api/queue      — {texts: [...], mode?}
  POST /api/mode       — {mode}
  POST /api/control    — {action: pause_all|resume_all|stop_all|cancel_next}
  POST /api/confirm    — {id, approve, comment}
  POST /api/answer     — {id, answer}
  POST /api/undo       — {steps}
  POST /api/memory     — {action: add|remove|list, text?, id?}
  POST /api/schedule   — {action: add|remove, expr?, text?, id?}
  POST /api/trigger    — {action: add|remove, path?, pattern?, text?, id?}
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ...runtime import AgentRuntime

STATIC_DIR = Path(__file__).parent / "static"


class SSEClient:
    def __init__(self) -> None:
        self.q: "queue.Queue[str | None]" = queue.Queue(maxsize=1000)
        self.unsub = None

    def _on_event(self, ev) -> None:
        try:
            self.q.put_nowait(ev.to_json())
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(ev.to_json())
            except Exception:
                pass


class WebUI:
    def __init__(self, rt: AgentRuntime, host: str = "0.0.0.0", port: int = 8710) -> None:
        self.rt = rt
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        rt = self.rt

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a) -> None:  # тишина в логах
                pass

            # ---------- helpers ----------
            def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, obj: Any, code: int = 200) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(n) if n else b"{}"
                    return json.loads(raw.decode("utf-8") or "{}")
                except (json.JSONDecodeError, OSError):
                    return {}

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            # ---------- routes ----------
            def do_OPTIONS(self) -> None:
                self._cors()
                self._send(204, b"", "text/plain")

            def do_GET(self) -> None:
                p = self.path.split("?")[0]
                if p in ("/", "/index.html"):
                    f = STATIC_DIR / "index.html"
                    data = f.read_bytes()
                    self._send(200, data, "text/html; charset=utf-8")
                    return
                if p == "/api/state":
                    self._json(rt.state_snapshot())
                    return
                if p == "/api/system":
                    self._json(rt.system_stats())
                    return
                if p == "/api/journal":
                    self._json(rt.journal.recent(60))
                    return
                if p == "/api/voice":
                    self._json(rt.voice_support())
                    return
                if p == "/api/settings":
                    self._json(rt.settings_view())
                    return
                if p == "/api/screen":
                    self._last_shot()
                    return
                if p == "/api/events":
                    self._sse()
                    return
                if p == "/api/tools":
                    self._json(rt.state_snapshot()["tools"])
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                p = self.path.split("?")[0]
                b = self._body()
                if p == "/api/task":
                    text = (b.get("text") or "").strip()
                    if not text:
                        self._json({"error": "text пуст"}, 400)
                        return
                    tid = rt.submit_task(text, mode=b.get("mode") or "",
                                         background=bool(b.get("background")))
                    self._json({"ok": True, "task_id": tid})
                    return
                if p == "/api/queue":
                    texts = [t.strip() for t in (b.get("texts") or []) if t.strip()]
                    if not texts:
                        self._json({"error": "texts пуст"}, 400)
                        return
                    ids = [rt.submit_task(t, mode=b.get("mode") or "") for t in texts]
                    self._json({"ok": True, "task_ids": ids})
                    return
                if p == "/api/mode":
                    self._json({"ok": True, "mode": rt.set_mode(b.get("mode", "confirm"))})
                    return
                if p == "/api/control":
                    self._json(rt.control(b.get("action", "")))
                    return
                if p == "/api/confirm":
                    ok = rt.confirm(b.get("id", ""), bool(b.get("approve")),
                                    b.get("comment", ""))
                    self._json({"ok": ok})
                    return
                if p == "/api/answer":
                    ok = rt.answer(b.get("id", ""), b.get("answer", ""))
                    self._json({"ok": ok})
                    return
                if p == "/api/undo":
                    self._json(rt.undo_last(int(b.get("steps", 1))))
                    return
                if p == "/api/memory":
                    action = b.get("action", "list")
                    if action == "add":
                        self._json({"ok": True, "item": rt.memory_add(b.get("text", ""),
                                                                      b.get("kind", "preference"))})
                    elif action == "remove":
                        self._json({"ok": rt.memory_remove(b.get("id", ""))})
                    else:
                        self._json({"items": rt.memory_list()})
                    return
                if p == "/api/schedule":
                    action = b.get("action", "list")
                    if action == "add":
                        r = rt.schedule_add(b.get("expr", ""), b.get("text", ""),
                                            b.get("mode", ""))
                        self._json(r)
                    elif action == "remove":
                        self._json({"ok": rt.schedule_remove(b.get("id", ""))})
                    else:
                        self._json({"items": [{"id": s.id, "expr": s.expr,
                                               "instruction": s.instruction}
                                              for s in rt.schedules.list()]})
                    return
                if p == "/api/trigger":
                    action = b.get("action", "list")
                    if action == "add":
                        r = rt.trigger_add(b.get("path", ""), b.get("pattern", "*"),
                                           b.get("text", ""))
                        self._json(r)
                    elif action == "remove":
                        self._json({"ok": rt.trigger_remove(b.get("id", ""))})
                    else:
                        self._json({"items": [{"id": t.id, "path": t.path,
                                               "pattern": t.pattern,
                                               "instruction": t.instruction}
                                              for t in rt.triggers.list()]})
                    return
                if p == "/api/settings":
                    self._json(rt.settings_apply(b))
                    return
                if p == "/api/stt":
                    seconds = max(2, min(int(b.get("seconds", 8)), 30))
                    from ...voice import Voice
                    v = Voice(rt.cfg)
                    if not v.stt_available():
                        self._json({"ok": False, "error": "локальный STT не установлен: "
                                                          "pip install faster-whisper sounddevice"}, 503)
                        return
                    try:
                        text = v.record_and_transcribe(seconds)
                        self._json({"ok": True, "text": text})
                    except Exception as e:  # noqa: BLE001
                        self._json({"ok": False, "error": str(e)}, 503)
                    return
                self._json({"error": "not found"}, 404)

            def _last_shot(self) -> None:
                shots_dir = Path(rt.cfg.state_dir) / "screens"
                if not shots_dir.exists():
                    self._json({"error": "нет скриншотов"}, 404)
                    return
                shots = sorted(shots_dir.glob("*.png"), key=lambda x: x.stat().st_mtime,
                               reverse=True)
                if not shots:
                    self._json({"error": "нет скриншотов"}, 404)
                    return
                data = shots[0].read_bytes()
                self._send(200, data, "image/png",
                           {"X-Screen-Path": str(shots[0]), "Access-Control-Allow-Origin": "*"})

            def _sse(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self._cors()
                self.end_headers()
                client = SSEClient()
                client.unsub = rt.bus.subscribe(client._on_event)
                # история для первооткрывателя
                for ev in rt.bus.history(80):
                    self.wfile.write(f"data: {ev.to_json()}\n\n".encode("utf-8"))
                    self.wfile.flush()
                try:
                    while True:
                        try:
                            msg = client.q.get(timeout=15)
                            if msg is None:
                                break
                            self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    if client.unsub:
                        client.unsub()

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="web-ui")
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def serve(rt: AgentRuntime, host: str | None = None, port: int | None = None,
          open_browser: bool = False) -> WebUI:
    if rt.loop is None:
        rt.start()  # идемпотентно: если луп уже запущен — ничего не делает
    w = rt.cfg.web_ui
    ui = WebUI(rt, host or w.get("host", "0.0.0.0"), port or int(w.get("port", 8710)))
    ui.start()
    url = f"http://{ui.host}:{ui.port}"
    rt.bus.emit("log", level="info", message=f"Web UI: {url}")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url.replace("0.0.0.0", "127.0.0.1"))
        except Exception:
            pass
    return ui
