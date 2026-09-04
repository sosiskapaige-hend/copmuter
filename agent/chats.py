"""Чаты: хранилище диалогов + сервис генерации ответов.

Два режима ответа:
  * chat   — прямой разговор с LLM (OpenAI-совместимый API, стриминг токенов);
  * agent  — полный агентный цикл (план → инструменты → проверка), ход работы
             транслируется в чат как «активности», итог — сообщение ассистента.

События в EventBus (для SSE/UI):
  chat_start    {chat_id, message_id, agent}
  chat_delta    {chat_id, message_id, kind: content|think, text, pos}
                pos — суммарная длина текста данного kind ПОСЛЕ этого куска
                (защита от дублей при пересоединении SSE)
  chat_done     {chat_id, message_id, ok, content, think, error}
  chat_activity {chat_id, kind, data}   — ход агентного выполнения
  chats_changed {}                      — список чатов изменился
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXTS = {".txt", ".md", ".json", ".csv", ".log", ".py", ".js", ".ts", ".html",
             ".css", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh",
             ".bat", ".ps1", ".sql", ".c", ".cpp", ".h", ".java", ".rs", ".go"}

SYSTEM_PROMPT = (
    "Ты — ассистент, работающий на компьютере пользователя через LM-совместимый "
    "локальный сервер. Отвечай на языке пользователя, по делу, структурировано. "
    "Используй markdown: заголовки, списки, блоки кода с указанием языка. "
    "Если пользователь просит что-то СДЕЛАТЬ в системе (файлы, процессы, браузер, "
    "настройки) — кратко объясни, что готово выполнить это в режиме «Агент» "
    "(переключатель над полем ввода), либо дай instructions, если просит совета."
)


def _uid() -> str:
    return uuid.uuid4().hex[:12]


class ChatStore:
    """JSON-файл на чат в AGENT_HOME/chats/."""

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, chat_id: str) -> Path:
        return self.dir / f"{chat_id}.json"

    # ---------- CRUD ----------
    def create(self, title: str = "Новый чат") -> dict:
        chat = {"id": _uid(), "title": title.strip() or "Новый чат",
                "created": time.time(), "updated": time.time(), "messages": []}
        self.save(chat)
        return chat

    def get(self, chat_id: str) -> dict | None:
        try:
            p = self._path(chat_id)
            if not p.is_file():
                return None
            chat = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(chat, dict) or "id" not in chat:
                return None
            chat.setdefault("messages", [])
            chat.setdefault("title", "Чат")
            return chat
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, chat: dict) -> None:
        with self._lock:
            chat["updated"] = time.time()
            tmp = self._path(chat["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(chat["id"]))

    def delete(self, chat_id: str) -> bool:
        try:
            p = self._path(chat_id)
            if p.exists():
                p.unlink()
                return True
        except OSError:
            pass
        return False

    def rename(self, chat_id: str, title: str) -> dict | None:
        chat = self.get(chat_id)
        if chat is None:
            return None
        chat["title"] = title.strip()[:120] or chat["title"]
        self.save(chat)
        return chat

    def list(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*.json"):
            chat = self.get(p.stem)
            if chat is None:
                continue
            out.append({"id": chat["id"], "title": chat["title"],
                        "created": chat.get("created", 0),
                        "updated": chat.get("updated", 0),
                        "count": len(chat.get("messages", []))})
        out.sort(key=lambda c: c["updated"], reverse=True)
        return out

    # ---------- сообщения ----------
    def append(self, chat_id: str, role: str, content: str, *,
               attachments: list | None = None, think: str = "",
               activities: list | None = None, mid: str | None = None,
               error: str = "") -> dict | None:
        chat = self.get(chat_id)
        if chat is None:
            return None
        msg: dict[str, Any] = {"id": mid or _uid(), "role": role,
                               "content": content, "ts": time.time()}
        if attachments:
            msg["attachments"] = attachments
        if think:
            msg["think"] = think
        if activities:
            msg["activities"] = activities[-80:]
        if error:
            msg["error"] = error
        chat["messages"].append(msg)
        # ограничиваем размер хранилища (200 сообщений на чат достаточно)
        if len(chat["messages"]) > 200:
            chat["messages"] = chat["messages"][-200:]
        self.save(chat)
        return msg

    def has(self, chat_id: str) -> bool:
        return self._path(chat_id).is_file()


class ChatService:
    """Генерация ответов: стриминг (чат) и агентный цикл (agent)."""

    def __init__(self, rt) -> None:
        self.rt = rt
        self.cfg = rt.cfg
        self.store = ChatStore(rt.cfg.chats_dir)
        self._streams: dict[str, threading.Event] = {}     # chat_id -> stop
        self._agent_task: dict[str, str] = {}              # chat_id -> task_id
        self._task_chat: dict[str, str] = {}               # task_id -> chat_id
        self._activities: dict[str, list] = {}             # chat_id -> ход агента
        self._unsub = rt.bus.subscribe(self._on_event)

    # ------------------------------------------------ события агента ->
    # транслируются в чат как chat_activity
    def _on_event(self, ev) -> None:
        tid = (ev.data or {}).get("task_id")
        chat_id = self._task_chat.get(tid)
        if not chat_id:
            return
        if ev.type in ("plan", "thought", "tool_call", "observation",
                       "progress", "error"):
            item = {"kind": ev.type, "data": ev.data, "ts": time.time()}
            acts = self._activities.setdefault(chat_id, [])
            acts.append(item)
            if len(acts) > 200:
                del acts[:100]
            self.rt.bus.emit("chat_activity", chat_id=chat_id, kind=ev.type,
                             data=ev.data)

    # ------------------------------------------------ публичное API
    def busy(self, chat_id: str) -> bool:
        return chat_id in self._streams or chat_id in self._agent_task

    def send(self, chat_id: str | None, text: str,
             attachments: list[dict] | None = None,
             agent: bool = False, mode: str = "") -> dict:
        text = (text or "").strip()
        attachments = [a for a in (attachments or [])
                       if a and a.get("path") and Path(a["path"]).is_file()]
        if not text and not attachments:
            return {"ok": False, "error": "пустое сообщение"}

        chat = self.store.get(chat_id) if chat_id else None
        created = False
        if chat is None:
            chat = self.store.create()
            created = True
        if self.busy(chat["id"]):
            return {"ok": False, "error": "этот чат уже генерирует ответ — "
                                          "дождитесь или остановите"}

        att_meta = [self._att_meta(a) for a in attachments]
        user_msg = self.store.append(chat["id"], "user", text,
                                     attachments=att_meta)
        if created or chat["title"] == "Новый чат":
            title = self._title_from(text, att_meta)
            self.store.rename(chat["id"], title)

        if agent:
            goal = self._goal_from(text, att_meta)
            self.rt.submit(self._run_agent(chat["id"], goal, mode))
        else:
            threading.Thread(target=self._worker_stream,
                             args=(chat["id"],), daemon=True,
                             name=f"chat-{chat['id']}").start()
        self.rt.bus.emit("chats_changed")
        fresh = self.store.get(chat["id"]) or chat
        return {"ok": True, "chat_id": chat["id"], "user": user_msg,
                "title": fresh.get("title", "")}

    def stop(self, chat_id: str) -> dict:
        ev = self._streams.get(chat_id)
        if ev:
            ev.set()
            return {"ok": True, "stopped": "stream"}
        tid = self._agent_task.get(chat_id)
        if tid:
            self.rt.agent.stop_task(tid)
            return {"ok": True, "stopped": "agent"}
        return {"ok": False, "error": "этот чат ничего не генерирует"}

    # ------------------------------------------------ чат: стриминг
    def _worker_stream(self, chat_id: str) -> None:
        stop = threading.Event()
        self._streams[chat_id] = stop
        message_id = _uid()
        self.rt.bus.emit("chat_start", chat_id=chat_id, message_id=message_id,
                         agent=False)
        content = ""
        think = ""
        error = ""
        try:
            messages = self._llm_messages(chat_id)
            for kind, piece in self.rt.llm.chat_stream(messages):
                if stop.is_set():
                    break
                if kind == "think":
                    think += piece
                    self.rt.bus.emit("chat_delta", chat_id=chat_id,
                                     message_id=message_id, kind="think",
                                     text=piece, pos=len(think))
                else:
                    content += piece
                    self.rt.bus.emit("chat_delta", chat_id=chat_id,
                                     message_id=message_id, kind="content",
                                     text=piece, pos=len(content))
        except Exception as e:  # noqa: BLE001
            error = self._friendly_llm_error(e)
        finally:
            self._streams.pop(chat_id, None)

        if not content.strip() and error:
            content = (f"⚠️ Не удалось получить ответ модели.\n\n`{error}`\n\n"
                       "Проверьте, что сервер запущен (LM Studio → Developer → "
                       "Start Server) и модель указана верно — ⚙ Настройки.")
        msg = self.store.append(chat_id, "assistant", content, think=think,
                                mid=message_id, error=error)
        self.rt.bus.emit("chat_done", chat_id=chat_id,
                         message_id=message_id, ok=not error,
                         content=content, think=think, error=error)
        if msg:
            self.rt.bus.emit("chats_changed")

    def _llm_messages(self, chat_id: str) -> list[dict]:
        chat = self.store.get(chat_id)
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if chat:
            hist = chat.get("messages", [])[-self.cfg.chat.history_messages:]
            for m in hist:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = self._message_content(m)
                if content is None:
                    continue
                messages.append({"role": role, "content": content})
        return messages

    def _message_content(self, msg: dict):
        """Контент сообщения для LLM: текст + inline-файлы + картинки
        (vision-моделям — как image_url-части)."""
        text = str(msg.get("content") or "")
        atts = msg.get("attachments") or []
        images = [a for a in atts if a.get("is_image")]
        others = [a for a in atts if not a.get("is_image")]

        parts_text = [text] if text else []
        for a in others:
            inline = self._inline_file(a.get("path", ""))
            if inline:
                parts_text.append(inline)
        joined = "\n\n".join(p for p in parts_text if p).strip()
        joined = joined[: self.cfg.chat.max_message_chars]

        if images and getattr(self.rt.llm, "vision", False):
            parts: list[dict] = []
            if joined:
                parts.append({"type": "text", "text": joined})
            for a in images:
                data_uri = self._data_uri(a.get("path", ""))
                if data_uri:
                    parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            return parts or None
        if images:  # модель без vision — картинки пропускаем, но упоминаем
            names = ", ".join(a.get("name", "?") for a in images)
            joined = (joined + f"\n\n[пользователь приложил изображения: {names}]").strip()
        return joined or None

    def _inline_file(self, path: str) -> str:
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in TEXT_EXTS:
            return ""
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        limit = self.cfg.chat.attach_inline_chars
        if len(raw) > limit:
            raw = raw[:limit] + "\n… [файл обрезан]"
        return f"[приложен файл: {p.name}]\n```\n{raw}\n```"

    @staticmethod
    def _data_uri(path: str) -> str:
        import base64
        try:
            data = Path(path).read_bytes()
        except OSError:
            return ""
        ext = Path(path).suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                "webp": "webp", "gif": "gif", "bmp": "bmp"}.get(ext, "png")
        return f"data:image/{mime};base64," + base64.b64encode(data).decode()

    # ------------------------------------------------ агент в чате
    async def _run_agent(self, chat_id: str, goal: str, mode: str) -> None:
        message_id = _uid()
        task_id = _uid()
        self._agent_task[chat_id] = task_id
        self._task_chat[task_id] = chat_id
        self._activities[chat_id] = []
        self.rt.bus.emit("chat_start", chat_id=chat_id, message_id=message_id,
                         agent=True)
        ok = False
        content = ""
        error = ""
        try:
            st = await self.rt.agent.run_task(goal, mode=mode, task_id=task_id)
            ok = st.status == "done"
            content = (st.summary or "").strip() or "(агент завершил без резюме)"
            if not ok and st.status == "cancelled":
                content = "⏹ Выполнение остановлено.\n\n" + content
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            content = f"💥 Сбой агента: {error}"
        finally:
            self._agent_task.pop(chat_id, None)
            self._task_chat.pop(task_id, None)

        activities = self._activities.pop(chat_id, [])
        self.store.append(chat_id, "assistant", content,
                          activities=activities, mid=message_id, error=error)
        self.rt.bus.emit("chat_done", chat_id=chat_id, message_id=message_id,
                         ok=ok, content=content, think="", error=error)
        self.rt.bus.emit("chats_changed")

    # ------------------------------------------------ утилиты
    @staticmethod
    def _att_meta(a: dict) -> dict:
        p = Path(a["path"])
        ext = p.suffix.lower()
        is_image = bool(a.get("is_image", ext in IMAGE_EXTS))
        meta = {"name": a.get("name") or p.name, "path": str(p),
                "mime": a.get("mime") or "application/octet-stream",
                "size": a.get("size") or p.stat().st_size,
                "is_image": is_image}
        if "url" in a:
            meta["url"] = a["url"]
        elif is_image:
            meta["url"] = f"/api/files/{p.name}"
        return meta

    @staticmethod
    def _title_from(text: str, atts: list[dict]) -> str:
        src = text.strip()
        if not src and atts:
            src = atts[0].get("name", "вложение")
        src = re.sub(r"\s+", " ", src).strip()
        return (src[:48] + "…") if len(src) > 48 else src or "Новый чат"

    @staticmethod
    def _goal_from(text: str, atts: list[dict]) -> str:
        files = [a["path"] for a in atts]
        if not files:
            return text
        listed = "\n".join(f"- {f}" for f in files)
        return f"{text}\n\nПриложенные пользователем файлы:\n{listed}"

    @staticmethod
    def _friendly_llm_error(e: Exception) -> str:
        s = str(e)
        if "Connection refused" in s or "urlopen error" in s and "refused" in s.lower():
            return ("сервер не отвечает (LM Studio запущен? Developer → Start Server, "
                    "порт 1234)")
        if "timed out" in s.lower() or "timeout" in s.lower():
            return "превышено время ожидания ответа модели"
        if "HTTP 404" in s:
            return "модель не найдена на сервере (404) — проверьте имя модели"
        return s[:400]
