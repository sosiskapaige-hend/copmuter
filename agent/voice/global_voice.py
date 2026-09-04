"""Глобальное голосовое управление компьютером.

Работает на стороне сервера, поэтому не зависит от того, какое окно сейчас
активно у пользователя:

* **Глобальный хоткей** (по умолчанию Ctrl+Alt+M, настраивается) — «нажать и
  говорить»: клавиша переключает запись микрофона, фраза распознаётся
  локальным Whisper и уходит агенту, ответ озвучивается локальным TTS.

* **Wake-word «Джарвис»** — когда включено в настройках, сервер постоянно
  слушает микрофон (VAD): фраза, начинающаяся с «Джарвис …», выполняется
  агентом автоматически, без каких-либо нажатий.

Зависимости (опциональные): `keyboard` (глобальные хоткеи), `sounddevice`,
`numpy`, `faster-whisper` (STT). Если чего-то нет — соответствующий режим
аккуратно отключается, остальное продолжает работать.
"""
from __future__ import annotations

import re
import threading
import time

from .stt_tts import Voice, clean_speech_text

_WAKE_EVENT = "voice_wake"
_HEARD_EVENT = "voice_heard"
_REPLY_EVENT = "voice_reply"
_STATE_EVENT = "voice_state"


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def normalize_wake_aliases(cfg_voice: dict) -> list[str]:
    """Список слов-активаторов в нижнем регистре, без знаков препинания."""
    word = str(cfg_voice.get("wake_word") or "джарвис").strip().lower()
    aliases = [a.lower() for a in (cfg_voice.get("wake_aliases") or []) if str(a).strip()]
    if word and word not in aliases:
        aliases.insert(0, word)
    return aliases or ["джарвис"]


def strip_wake_word(text: str, aliases: list[str]) -> str | None:
    """Если фраза начинается с одного из слов-активаторов — возвращает
    остаток команды (без слова). Иначе None."""
    t = (text or "").strip()
    if not t:
        return None
    # отрезаем пунктуацию/пробелы в начале исходной строки (не lower-копии,
    # чтобы индексы совпадали) и сравниваем первое слово без учёта регистра
    head = re.match(r"^[\s\d—–,.:!?\"'«»()\-_]*", t)
    body = t[head.end():] if head else t
    m = re.match(r"^([а-яa-zё]+)", body, flags=re.IGNORECASE)
    if not m:
        return None
    if m.group(1).lower() not in aliases:
        return None
    rest = body[m.end():].strip(" —–-:,")
    return rest


class GlobalVoice:
    """Сервис глобального голосового управления (фоновый поток)."""

    def __init__(self, rt) -> None:
        self.rt = rt
        self.cfg = rt.cfg
        self.voice = Voice(rt.cfg)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = "idle"          # idle | listening | processing | speaking
        self._threads: list[threading.Thread] = []
        self._hotkey_hook = None
        self._wake_thread = None
        self._rec_abort = threading.Event()   # остановить текущую запись
        self._speak_next_chat = False   # озвучить следующий ответ этого чата
        self._unsub = None
        # тихий голосовой чат, куда падают команды с хоткея/wake-word
        self._voice_chat_id = None

    # ---------------- статус ----------------
    def status(self) -> dict:
        cfg = self.cfg.voice
        kb = _has("keyboard")
        mic = self.voice.stt_available()
        return {
            "available": bool(kb and mic and self.voice.tts_available()),
            "can_hotkeys": kb,
            "can_mic": mic,
            "can_tts": self.voice.tts_available(),
            "tts_provider": self.voice.tts_provider(),
            "hotkey": cfg.get("hotkey", "ctrl+alt+m"),
            "wake_enabled": bool(cfg.get("wake_enabled", False)),
            "wake_word": cfg.get("wake_word", "джарвис"),
            "state": self._state,
            "global_enabled": bool(cfg.get("global_enabled", True)),
        }

    # ---------------- запуск/останов ----------------
    def start(self) -> bool:
        """Регистрирует глобальные хоткеи и (если включено) wake-слушатель.

        Вызов можно повторять (в т.ч. после stop()): подписка и хоткеи не
        дублируются, wake-поток перезапускается.
        """
        cfg = self.cfg.voice
        if not cfg.get("global_enabled", True):
            return False
        self._stop.clear()
        if self._unsub is None:
            self._unsub = self.rt.bus.subscribe(self._on_bus)
        started = False
        if _has("keyboard"):
            try:
                self._start_hotkey_listener()
                started = True
            except Exception as e:  # noqa: BLE001
                self.rt.bus.emit("log", level="warn",
                                 message=f"Глобальные хоткеи недоступны: {e}")
        if cfg.get("wake_enabled", False):
            self._start_wake_listener()
            started = True
        if not started and self._unsub is not None:
            # ничего не запустилось — подписка на события не нужна
            try:
                self._unsub()
            except Exception:
                pass
            self._unsub = None
        return started

    def stop(self) -> None:
        self._stop.set()
        self._rec_abort.set()   # разблокировать запись с микрофона при выходе
        if self._hotkey_hook is not None:
            try:
                self._hotkey_hook()
            except Exception:
                pass
            self._hotkey_hook = None
        if self._wake_thread is not None:
            try:
                self._wake_thread.join(timeout=3.0)
            except Exception:
                pass
            self._wake_thread = None
        if self._unsub:
            try:
                self._unsub()
            except Exception:
                pass
            self._unsub = None

    # ---------------- события агента -> озвучка ответа ----------------
    def _on_bus(self, ev) -> None:
        d = ev.data or {}
        if ev.type == "chat_done" and self._speak_next_chat:
            if d.get("chat_id") == self._voice_chat_id:
                content = (d.get("content") or "").strip()
                self._speak_next_chat = False
                if content and d.get("ok"):
                    threading.Thread(target=self._speak_safe, args=(content,),
                                     daemon=True).start()

    def _speak_safe(self, text: str) -> None:
        text = clean_speech_text(text)
        if not text:
            return
        self._set_state("speaking")
        self.rt.bus.emit(_REPLY_EVENT, text=text)
        try:
            self.voice.speak(text)
        finally:
            self._set_state("idle")

    # ---------------- хоткей ----------------
    def _start_hotkey_listener(self) -> None:
        import keyboard
        hotkey = self.cfg.voice.get("hotkey") or "ctrl+alt+m"
        try:
            keyboard.remove_hotkey  # убеждаемся, что API современный
        except Exception:
            pass
        self._hotkey_hook = keyboard.add_hotkey(hotkey, self._on_hotkey,
                                                suppress=False)
        self.rt.bus.emit("log", level="info",
                         message=f"Глобальный хоткей микрофона: {hotkey}")

    def _on_hotkey(self) -> None:
        # hotkey callback приходит из потока keyboard — работаем в своём
        threading.Thread(target=self._toggle_record, daemon=True,
                         name="gv-hotkey").start()

    def _toggle_record(self) -> None:
        with self._lock:
            if self._state == "listening":
                # повторное нажатие — остановить запись (push-to-talk)
                self._rec_abort.set()
                return
            if self._state in ("processing", "speaking"):
                return
            self._rec_abort.clear()
            self._set_state("listening")
        try:
            text = self._record_once()
        except Exception as e:  # noqa: BLE001
            text = ""
            self.rt.bus.emit("log", level="warn", message=f"Голос: {e}")
        aborted = self._rec_abort.is_set()
        self._rec_abort.clear()
        self._set_state("idle")
        if aborted:
            return
        if text.strip():
            self._run_command(text, source="hotkey")

    # ---------------- wake-word «Джарвис» ----------------
    def _start_wake_listener(self) -> None:
        if not self.voice.stt_available():
            self.rt.bus.emit("log", level="warn",
                             message="Wake-word «Джарвис» требует: pip install "
                                     "faster-whisper sounddevice numpy")
            return
        self._wake_thread = threading.Thread(target=self._wake_loop, daemon=True,
                                             name="gv-wake")
        self._wake_thread.start()
        self.rt.bus.emit("log", level="info",
                         message=f"Wake-word включён: «{self.cfg.voice.get('wake_word')}»")

    def _wake_loop(self) -> None:
        aliases = normalize_wake_aliases(self.cfg.voice)
        while not self._stop.is_set():
            try:
                if self._state in ("processing", "speaking", "listening"):
                    time.sleep(0.5)
                    continue
                # ждём фразу через VAD-запись с небольшим лимитом
                text = self._record_once(max_seconds=8.0)
            except Exception:  # noqa: BLE001 — микрофон мог пропасть
                time.sleep(1.5)
                continue
            if not text or not text.strip():
                continue
            cmd = strip_wake_word(text, aliases)
            if cmd is None:
                continue
            self.rt.bus.emit(_WAKE_EVENT, text=text, command=cmd)
            self._run_command(cmd, source="wake")

    def _record_once(self, max_seconds: float = 15.0) -> str:
        self._rec_abort.clear()
        self.rt.bus.emit(_STATE_EVENT, state="listening")
        try:
            return self.voice.record_and_transcribe(max_seconds,
                                                    abort=self._rec_abort)
        finally:
            self.rt.bus.emit(_STATE_EVENT, state="idle")

    # ---------------- исполнение команды ----------------
    def _run_command(self, command: str, source: str) -> None:
        self._set_state("processing")
        cfg = self.cfg.voice
        try:
            self._speak_next_chat = True
            chat_id = self._ensure_chat()
            use_agent = bool(cfg.get("send_agent", True))
            mode = str(cfg.get("mode") or self.cfg.safety.mode) or "auto"
            res = self.rt.chats.send(chat_id, command, attachments=None,
                                     agent=use_agent, mode=mode)
            if not res.get("ok"):
                self._speak_next_chat = False
                err = str(res.get("error") or "не удалось выполнить")
                self.rt.bus.emit(_REPLY_EVENT, text=err, error=True)
                self._speak_safe("Не удалось выполнить команду: " + err)
                return
            self.rt.bus.emit(_HEARD_EVENT, text=command, chat_id=chat_id,
                             source=source)
            self.rt.bus.emit("log", level="voice",
                             message=f"[{source}] {command[:120]}")
        finally:
            self._set_state("idle")

    def _ensure_chat(self) -> str:
        chat = None
        if self._voice_chat_id:
            chat = self.rt.chats.store.get(self._voice_chat_id)
        if chat is None:
            # используем последний активный чат или создаём «Голосовое управление»
            chats = self.rt.chats.store.list()
            if chats:
                self._voice_chat_id = chats[0]["id"]
            else:
                c = self.rt.chats.store.create("Голосовое управление")
                self._voice_chat_id = c["id"]
            self.rt.bus.emit("chats_changed")
        return self._voice_chat_id

    def _set_state(self, state: str) -> None:
        self._state = state
        self.rt.bus.emit(_STATE_EVENT, state=state)
