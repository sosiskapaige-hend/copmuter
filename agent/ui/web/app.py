"""Web UI: чат-интерфейс на чистом stdlib (http.server) + SSE live-поток.

Основные точки входа:
  GET  /                      — index.html (чат)
  GET  /api/chats             — список чатов
  POST /api/chats             — создать чат {title?}
  GET  /api/chats/<id>        — чат с сообщениями
  POST /api/chats/<id>/rename — {title}
  POST /api/chats/<id>/delete — удалить чат
  POST /api/chat/send         — {chat_id?, text, attachments?, agent?, mode?}
  POST /api/chat/stop         — {chat_id}
  POST /api/upload            — файл (raw body, имя в X-File-Name)
  GET  /api/files/<name>      — отдать загруженный файл (картинки в чате)
  GET  /api/events            — SSE: живой поток (дельты чата + агент)
  GET  /api/llm/models        — список моделей сервера (LM Studio / Ollama)
  GET  /api/state             — снимок состояния (настройки/агент)
  GET  /api/settings          — настройки модели
  POST /api/settings          — применить настройки модели
  POST /api/task              — задача агента без чата (совместимость)
  POST /api/mode              — {mode}
  POST /api/control           — {action: pause_all|resume_all|stop_all|cancel_next}
  POST /api/confirm           — {id, approve, comment}
  POST /api/answer            — {id, answer}
  POST /api/undo              — {steps}
  POST /api/memory            — {action: add|remove|list, ...}
  POST /api/schedule          — {action: add|remove, ...}
  POST /api/trigger           — {action: add|remove, ...}
  GET  /api/journal           — журнал действий (undo)
  GET  /api/system            — CPU/RAM/диск
  POST /api/stt               — серверный STT (Whisper, опционально)
  POST /api/stt/audio         — STT записанного аудио из браузера (raw body)
  GET  /api/selfcheck         — возможности агента (мышь/экран/окна/голос)
  POST /api/selfcheck/run     — самопроверка с реальными тестами
  POST /api/voice/settings    — обновить настройки голоса
  POST /api/voice/global      — restart|stop глобального голоса
  POST /api/voice/send        — голосовая команда (текст) в чат/агента
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ...runtime import AgentRuntime

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_LIMIT = 25 * 1024 * 1024  # 25 МБ

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class SSEClient:
    def __init__(self) -> None:
        self.q: "queue.Queue[str | None]" = queue.Queue(maxsize=2000)
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

            def log_message(self, *a) -> None:  # тише в логах
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
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-File-Name")

            def do_OPTIONS(self) -> None:
                self._cors()
                self._send(204, b"", "text/plain")

            # ---------- routes ----------
            def do_GET(self) -> None:
                p = urllib.parse.unquote(self.path.split("?")[0])
                if p in ("/", "/index.html"):
                    try:
                        data = (STATIC_DIR / "index.html").read_bytes()
                    except OSError:
                        self._json({"error": "index.html не найден"}, 500)
                        return
                    self._send(200, data, "text/html; charset=utf-8")
                    return
                if p == "/api/chats":
                    self._json({"chats": rt.chats.store.list()})
                    return
                m = re.fullmatch(r"/api/chats/([A-Za-z0-9_-]+)", p)
                if m:
                    chat = rt.chats.store.get(m.group(1))
                    if chat is None:
                        self._json({"error": "чат не найден"}, 404)
                        return
                    self._json({"chat": chat, "busy": rt.chats.busy(chat["id"])})
                    return
                m = re.fullmatch(r"/api/files/(.+)", p)
                if m:
                    fname = m.group(1)
                    # защита от traversal: только имя файла, без каталогов
                    if fname != Path(fname).name or fname in (".", ".."):
                        self._json({"error": "not found"}, 404)
                        return
                    self._send_file(rt.cfg.uploads_dir / fname)
                    return
                if p == "/api/llm/models":
                    self._json(rt.llm_models())
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
                if p == "/api/selfcheck":
                    self._json(rt.self_check(run_tests=False))
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
                p = urllib.parse.unquote(self.path.split("?")[0])
                if p == "/api/upload":
                    self._upload()   # читает raw-body сам
                    return
                if p == "/api/stt/audio":
                    self._stt_audio()   # читает raw-body сам
                    return
                b = self._body()
                if p == "/api/chat/send":
                    text = str(b.get("text") or "")
                    attachments = b.get("attachments") or []
                    if not isinstance(attachments, list):
                        attachments = []
                    r = rt.chat_send(b.get("chat_id") or None, text,
                                     attachments=attachments,
                                     agent=bool(b.get("agent")),
                                     mode=b.get("mode") or "")
                    self._json(r, 200 if r.get("ok") else 400)
                    return
                if p == "/api/chat/stop":
                    r = rt.chat_stop(str(b.get("chat_id") or ""))
                    self._json(r, 200 if r.get("ok") else 400)
                    return
                m = re.fullmatch(r"/api/chats/([A-Za-z0-9_-]+)/rename", p)
                if m:
                    chat = rt.chats.store.rename(m.group(1), str(b.get("title") or ""))
                    if chat is None:
                        self._json({"error": "чат не найден"}, 404)
                        return
                    rt.bus.emit("chats_changed")
                    self._json({"ok": True, "chat": {"id": chat["id"],
                                                     "title": chat["title"]}})
                    return
                m = re.fullmatch(r"/api/chats/([A-Za-z0-9_-]+)/delete", p)
                if m:
                    cid = m.group(1)
                    if rt.chats.busy(cid):
                        rt.chats.stop(cid)
                        time.sleep(0.15)
                    ok = rt.chats.store.delete(cid)
                    if ok:
                        rt.bus.emit("chats_changed")
                    self._json({"ok": ok}, 200 if ok else 404)
                    return
                if p == "/api/chats":
                    chat = rt.chats.store.create(str(b.get("title") or "Новый чат"))
                    rt.bus.emit("chats_changed")
                    self._json({"ok": True, "chat": {"id": chat["id"],
                                                     "title": chat["title"]}})
                    return
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
                        self._json({"ok": True,
                                    "item": rt.memory_add(b.get("text", ""),
                                                          b.get("kind", "preference"))})
                    elif action == "remove":
                        self._json({"ok": rt.memory_remove(b.get("id", ""))})
                    else:
                        self._json({"items": rt.memory_list()})
                    return
                if p == "/api/schedule":
                    action = b.get("action", "list")
                    if action == "add":
                        self._json(rt.schedule_add(b.get("expr", ""), b.get("text", ""),
                                                   b.get("mode", "")))
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
                        self._json(rt.trigger_add(b.get("path", ""), b.get("pattern", "*"),
                                                  b.get("text", "")))
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
                    seconds = max(2, min(int(b.get("seconds", 20)), 40))
                    from ...voice import Voice
                    v = Voice(rt.cfg)
                    if not v.stt_available():
                        self._json({"ok": False,
                                    "error": "локальный STT не установлен: "
                                             "pip install faster-whisper sounddevice numpy"}, 503)
                        return
                    try:
                        # VAD-запись: стартует со звуком, кончается по тишине
                        text = v.record_and_transcribe(seconds)
                        self._json({"ok": True, "text": text,
                                    "engine": "whisper"})
                    except Exception as e:  # noqa: BLE001
                        self._json({"ok": False, "error": str(e)}, 503)
                    return
                if p == "/api/voice/settings":
                    self._json(rt.voice_settings_apply(b))
                    return
                if p == "/api/voice/global":
                    action = str(b.get("action") or "")
                    if action == "restart":
                        ok = rt.start_global_voice()
                        self._json({"ok": ok, "status": rt.voice_support()})
                    elif action == "stop":
                        rt.stop_global_voice()
                        self._json({"ok": True, "status": rt.voice_support()})
                    else:
                        self._json({"ok": True, "status": rt.voice_support()})
                    return
                if p == "/api/selfcheck/run":
                    self._json(rt.self_check(run_tests=True))
                    return
                if p == "/api/voice/send":
                    # Голосовая команда: текст -> чат/агент как будто набрали руками
                    text = str(b.get("text") or "").strip()
                    if not text:
                        self._json({"ok": False, "error": "пустая команда"}, 400)
                        return
                    agent = bool(b.get("agent"))
                    mode = str(b.get("mode") or rt.cfg.safety.mode)
                    r = rt.chats.send(b.get("chat_id") or None, text,
                                      attachments=None, agent=agent, mode=mode)
                    self._json(r, 200 if r.get("ok") else 400)
                    return
                self._json({"error": "not found"}, 404)

            # ---------- обработчики ----------
            def _stt_audio(self) -> None:
                """Распознавание записанного в браузере аудио (MediaRecorder).

                Тело запроса — сырые байты (webm/opus, ogg, wav, mp3, m4a);
                язык — в заголовке X-Audio-Lang (опционально).
                """
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                if n <= 0:
                    self._json({"ok": False, "error": "пустая запись"}, 400)
                    return
                if n > UPLOAD_LIMIT:
                    self._json({"ok": False, "error": "запись больше 25 МБ"}, 413)
                    return
                raw = self.rfile.read(n)
                if len(raw) != n:
                    self._json({"ok": False, "error": "аудио прочитано не полностью"}, 400)
                    return
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                ext = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav",
                       "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mpeg": ".mp3",
                       "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                       "audio/aac": ".aac", "video/webm": ".webm"}.get(ctype, ".webm")
                from ...voice import Voice
                v = Voice(rt.cfg)
                if not v.stt_file_available():
                    self._json({"ok": False,
                                "error": "серверный STT не установлен: "
                                         "pip install faster-whisper numpy"}, 503)
                    return
                stt_dir = Path(rt.cfg.state_dir) / "stt"
                stt_dir.mkdir(parents=True, exist_ok=True)
                tmp = stt_dir / f"rec_{int(time.time() * 1000)}{ext}"
                try:
                    tmp.write_bytes(raw)
                except OSError as e:
                    self._json({"ok": False, "error": f"не удалось сохранить запись: {e}"}, 500)
                    return
                try:
                    lang = (self.headers.get("X-Audio-Lang") or "").strip() or None
                    text = v.transcribe_file(str(tmp), language=lang)
                    self._json({"ok": True, "text": text, "engine": "whisper",
                                "lang": lang or ""})
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": str(e)}, 503)
                finally:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass

            def _upload(self) -> None:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                if n <= 0:
                    self._json({"ok": False, "error": "пустой файл"}, 400)
                    return
                if n > UPLOAD_LIMIT:
                    self._json({"ok": False, "error": "файл больше 25 МБ"}, 413)
                    return
                raw_name = urllib.parse.unquote(self.headers.get("X-File-Name") or "file")
                name = Path(raw_name).name or "file"
                name = re.sub(r"[^\w.\-() а-яА-ЯёЁ]+", "_", name)[:120] or "file"
                data = self.rfile.read(n)
                if len(data) != n:
                    self._json({"ok": False, "error": "файл прочитан не полностью"}, 400)
                    return
                mime = self.headers.get("Content-Type") or ""
                if mime.startswith("text/") or "json" in mime:
                    mime = mimetypes.guess_type(name)[0] or "text/plain"
                dest_dir = rt.cfg.uploads_dir
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f"{int(time.time() * 1000)}_{name}"
                try:
                    dest.write_bytes(data)
                except OSError as e:
                    self._json({"ok": False, "error": f"не удалось сохранить: {e}"}, 500)
                    return
                is_image = mime.startswith("image/") or dest.suffix.lower() in IMAGE_EXTS
                self._json({"ok": True,
                            "attachment": {"name": name, "path": str(dest),
                                           "mime": mime or "application/octet-stream",
                                           "size": n, "is_image": is_image,
                                           "url": f"/api/files/{dest.name}"}})

            def _send_file(self, path: Path) -> None:
                if not path.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                try:
                    data = path.read_bytes()
                except OSError:
                    self._json({"error": "not found"}, 404)
                    return
                self._send(200, data, ctype,
                           {"Cache-Control": "public, max-age=86400",
                            "Access-Control-Allow-Origin": "*"})

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
                # короткая история — клиент сам разрулит дубли по pos/message_id
                try:
                    for ev in rt.bus.history(60):
                        self.wfile.write(f"data: {ev.to_json()}\n\n".encode("utf-8"))
                    self.wfile.flush()
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
    # Глобальный голос: хоткеи работают, даже когда окно приложения свёрнуто
    # или пользователь в другой программе (если библиотеки доступны).
    rt.start_global_voice()
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url.replace("0.0.0.0", "127.0.0.1"))
        except Exception:
            pass
    return ui
