"""Runtime: композиция всех подсистем в единый управляемый объект.

Владельцы: asyncio-луп (поток), EventBus, InteractionGateway, Agent,
TaskManager (очередь), Scheduler (расписание), TriggerManager (триггеры),
BackgroundRunner. UI (CLI/Web) работает через runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .agent.core import Agent
from .chats import ChatService
from .config import Config
from .events import EventBus, InteractionGateway
from .llm import create_llm
from .memory.longterm import LongTermMemory
from .memory.session import SessionStore
from .platform import get_platform
from .safety.journal import Journal
from .safety.policy import SafetyPolicy
from .tasks.background import BackgroundRunner
from .tasks.queue import TaskManager
from .tasks.scheduler import Scheduler
from .tasks.triggers import TriggerManager
from .tools import build_registry


class AgentRuntime:
    def __init__(self, cfg: Config, workdir: str = ".") -> None:
        self.cfg = cfg
        self.cfg.ensure_dirs()
        self.workdir = workdir
        # относительные пути инструментов отсчитываются от рабочей папки
        try:
            wd = os.path.abspath(workdir)
            if os.path.isdir(wd) and os.getcwd() != wd:
                os.chdir(wd)
        except OSError:
            pass
        self.bus = EventBus()
        self.gateway = InteractionGateway(self.bus)
        self.platform = get_platform()
        self.llm = create_llm(cfg)
        self.registry = build_registry()
        self.journal = Journal(cfg.journal_file)
        self.sessions = SessionStore(cfg.state_dir)
        self.memory = LongTermMemory(cfg.memory_dir)
        self.policy = SafetyPolicy(confirm_from=cfg.safety.confirm_from,
                                   bulk_delete_threshold=cfg.safety.bulk_delete_threshold)
        self.agent = Agent(cfg, self.llm, self.registry, self.bus, self.gateway,
                           self.journal, self.sessions, self.memory, self.policy,
                           self.platform, workdir=workdir)
        self.tasks = TaskManager(self.agent, self.bus, self.sessions)
        self.chats = ChatService(self)
        self.schedules = Scheduler(cfg.state_dir, self._schedule_fire)
        self.triggers = TriggerManager(cfg.state_dir, self._trigger_fire, poll=3.0)
        self.bg = BackgroundRunner(self.bus)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # глобальное голосовое управление (хоткеи вне окна, wake-word «Джарвис»)
        self.gvoice = None

    # ---------------- луп ----------------
    def start(self) -> None:
        if self.loop is not None:
            return
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="agent-loop")
        self._thread.start()
        self._wait_loop()

    def _run_loop(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._bootstrap())
        self.loop.run_forever()   # луп живёт, пока не вызван stop

    async def _bootstrap(self) -> None:
        await self.tasks.start()
        await self.schedules.start()
        await self.triggers.start()
        self.bus.emit("log", level="info",
                      message=f"Агент запущен (LLM: {self.llm.name}, "
                              f"инструментов: {len(self.registry.names())}, "
                              f"платформа: {self.platform.system})")

    def _wait_loop(self, timeout: float = 10.0) -> None:
        t0 = time.time()
        while self.loop is None or not self.loop.is_running():
            if time.time() - t0 > timeout:
                raise RuntimeError("event loop не стартовал")
            time.sleep(0.02)

    def _ensure_loop_running(self) -> None:
        if self.loop is None or not self.loop.is_running():
            # луп мог завершиться (например, после shutdown) — перезапуск невозможен
            # в рамках одного объекта; создаём новый
            raise RuntimeError("event loop не запущен — вызовите runtime.start()")

    def submit(self, coro) -> "asyncio.Future":
        """Отправить корутину в луп, вернуть Future."""
        self._ensure_loop_running()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _schedule_fire(self, s) -> None:
        await self.tasks.enqueue(s.instruction, mode=s.mode)
        self.bus.emit("log", level="scheduler",
                      message=f"Расписание «{s.expr}» сработало: {s.instruction[:80]}")

    async def _trigger_fire(self, t, f: Path) -> None:
        instr = (t.instruction or "Обработай новый файл").replace("{file}", str(f))
        await self.tasks.enqueue(instr, mode=self.cfg.safety.mode)
        self.bus.emit("log", level="trigger",
                      message=f"Триггер (новый файл {f.name} в {t.path}): {instr[:80]}")

    # ---------------- API (для UI) ----------------
    def submit_task(self, goal: str, mode: str = "", parallel_ok: bool = False,
                    background: bool = False) -> str:
        item = self.submit(self.tasks.enqueue(goal, mode=mode,
                                              parallel_ok=parallel_ok,
                                              background=background)).result(timeout=10)
        return item.task_id

    # ---------------- чат ----------------
    def chat_send(self, chat_id: str | None, text: str,
                  attachments: list | None = None, agent: bool = False,
                  mode: str = "") -> dict:
        return self.chats.send(chat_id, text, attachments=attachments,
                               agent=agent, mode=mode or self.cfg.safety.mode)

    def chat_stop(self, chat_id: str) -> dict:
        return self.chats.stop(chat_id)

    def llm_models(self) -> dict:
        """Список моделей на сервере (для настроек UI)."""
        fn = getattr(self.llm, "list_models", None)
        if not callable(fn):
            return {"ok": False, "error": "провайдер не поддерживает список моделей"}
        try:
            return {"ok": True, "models": fn()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}

    def control(self, action: str) -> dict:
        """pause_all | resume_all | stop_all | cancel_next."""
        if action == "pause_all":
            n = self.tasks.pause_all()
        elif action == "resume_all":
            n = self.tasks.resume_all()
        elif action == "stop_all":
            n = self.tasks.stop_all()
        elif action == "cancel_next":
            n = self.submit(self.tasks.cancel_next()).result(timeout=10)
        else:
            return {"ok": False, "error": f"неизвестное действие: {action}"}
        return {"ok": True, "affected": n}

    def set_mode(self, mode: str) -> str:
        from .safety.policy import validate_mode
        m = validate_mode(mode)
        self.cfg.safety.mode = m
        self._persist_config()
        return m

    def confirm(self, confirm_id: str, approve: bool, comment: str = "") -> bool:
        return self.gateway.resolve(confirm_id, (approve, comment))

    def answer(self, ask_id: str, answer: str) -> bool:
        return self.gateway.resolve(ask_id, answer)

    def undo_last(self, steps: int = 1) -> dict:
        async def _do():
            ctx = self.agent._ctx
            res = await self.registry.call("undo_last", {"steps": steps}, ctx)
            return {"ok": res.ok, "output": res.output, "error": res.error}
        try:
            return self.submit(_do()).result(timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def memory_add(self, text: str, kind: str = "preference") -> dict:
        return self.memory.add(kind, text)

    def memory_remove(self, item_id: str) -> bool:
        return self.memory.remove(item_id)

    def memory_list(self) -> list[dict]:
        return self.memory.all()

    def schedule_add(self, expr: str, instruction: str, mode: str = "") -> dict:
        try:
            s = self.schedules.add(expr, instruction, mode)
            return {"ok": True, "id": s.id, "expr": s.expr}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def schedule_remove(self, sid: str) -> bool:
        return self.schedules.remove(sid)

    def trigger_add(self, path: str, pattern: str, instruction: str) -> dict:
        t = self.triggers.add(path, pattern, instruction)
        return {"ok": True, "id": t.id, "path": t.path, "pattern": t.pattern}

    def trigger_remove(self, tid: str) -> bool:
        return self.triggers.remove(tid)

    def state_snapshot(self) -> dict:
        return {
            "llm": self.llm.name,
            "platform": self.platform.summary(),
            "mode": self.cfg.safety.mode,
            "tools": self.registry.names(),
            "tasks": self.sessions.history_brief(30),
            "queue": self.tasks.queued_goals(),
            "running": self.agent.list_running(),
            "schedules": [{"id": s.id, "expr": s.expr, "instruction": s.instruction,
                           "enabled": s.enabled} for s in self.schedules.list()],
            "triggers": [{"id": t.id, "path": t.path, "pattern": t.pattern,
                          "instruction": t.instruction, "enabled": t.enabled}
                         for t in self.triggers.list()],
            "memory": self.memory.all()[:100],
            "journal": self.journal.stats(),
            "pending": self.gateway.pending_list(),
            "background": self.bg.all()[-20:],
            "voice": self.voice_support(),
        }

    def system_stats(self) -> dict:
        from .tools.system import _psutil, _mem_info, _cpu_info
        out: dict[str, Any] = {}
        ps = _psutil()
        if ps:
            try:
                out["cpu_percent"] = ps.cpu_percent(interval=0.2)
                vm = ps.virtual_memory()
                out["ram_percent"] = vm.percent
                out["ram_free_gb"] = round(vm.available / 1073741824, 2)
                try:
                    du = ps.disk_usage("/")
                    out["disk_percent"] = du.percent
                except Exception:
                    pass
            except Exception:
                pass
        else:
            out["cpu_info"] = _cpu_info()
            out["ram_info"] = _mem_info()
        out["uptime_hint"] = time.strftime("%H:%M:%S")
        return out

    def voice_support(self) -> dict:
        from .voice import Voice
        v = Voice(self.cfg)
        out = {
            "stt": v.stt_available(),
            "stt_reason": v.stt_reason(),
            "tts": v.tts_available(),
            "tts_provider": v.tts_provider(),
        }
        if self.gvoice is not None:
            try:
                out.update(self.gvoice.status())
            except Exception:  # noqa: BLE001
                pass
        out.update({"config": dict(self.cfg.voice)})
        return out

    # ---------------- голос: глобальное управление ----------------
    def start_global_voice(self) -> bool:
        """Поднимает глобальные хоткеи и wake-word «Джарвис» (если можно)."""
        from .voice import GlobalVoice
        if self.gvoice is None:
            self.gvoice = GlobalVoice(self)
        try:
            started = self.gvoice.start()
        except Exception as e:  # noqa: BLE001
            self.bus.emit("log", level="warn",
                          message=f"Глобальный голос не запустился: {e}")
            return False
        if not started:
            self.bus.emit("log", level="info",
                          message="Глобальный голос выключен (настройки/нет "
                                  "библиотек: pip install keyboard sounddevice "
                                  "faster-whisper numpy)")
        return started

    def stop_global_voice(self) -> None:
        if self.gvoice is not None:
            try:
                self.gvoice.stop()
            except Exception:  # noqa: BLE001
                pass

    def voice_settings_apply(self, patch: dict) -> dict:
        """Обновляет настройки голоса из UI и применяет их на лету."""
        from .config import VOICE_DEFAULTS
        allowed = set(VOICE_DEFAULTS.keys())
        v = patch.get("voice") or {}
        if not isinstance(v, dict):
            return {"ok": False, "error": "нет раздела voice"}
        changed = False
        for k, val in v.items():
            if k not in allowed:
                continue
            if isinstance(val, bool):
                val = bool(val)
            elif k in ("stt_model", "language", "hotkey", "app_hotkey",
                       "wake_word", "mode"):
                val = str(val).strip()
            if k == "stt_model" and val not in ("tiny", "base", "small",
                                                "medium", "large-v3"):
                continue
            if k == "mode":
                from .safety.policy import validate_mode
                val = validate_mode(val)
            if self.cfg.voice.get(k) != val:
                self.cfg.voice[k] = val
                changed = True
        if "wake_aliases" in v and isinstance(v["wake_aliases"], list):
            self.cfg.voice["wake_aliases"] = [str(a) for a in v["wake_aliases"]]
            changed = True
        if changed:
            self._persist_config()
            # применяем на лету: перезапускаем глобальный слушатель
            self.stop_global_voice()
            self.start_global_voice()
        return {"ok": True, "voice": dict(self.cfg.voice)}

    # ---------------- самопроверка возможностей ----------------
    def self_check(self, run_tests: bool = False) -> dict:
        """Честная проверка: что агент реально может на этой машине.

        Без `run_tests` — только обнаружение возможностей; с `run_tests` —
        пробует скриншот, список окон и виртуальный ввод.
        """
        p = self.platform
        checks: list[dict] = []
        def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})

        # --- экран ---
        shot_ok, shot_detail = False, "нет"
        if p.has_mss:
            try:
                import mss
                with mss.mss() as sct:
                    shot = sct.grab(sct.monitors[0])
                    shot_ok = shot.width > 0 and shot.height > 0
                    shot_detail = f"{shot.width}×{shot.height}"
            except Exception as e:  # noqa: BLE001
                shot_detail = f"ошибка: {str(e)[:120]}"
        elif p.has_display:
            shot_detail = "mss не установлен (pip install mss)"
        else:
            shot_detail = "нет дисплея (headless): скриншоты недоступны"
        add("Скриншот экрана", shot_ok, shot_detail,
            "pip install mss" if not shot_ok and p.has_display else "")

        # --- мышь/клавиатура ---
        mouse_ok, mouse_detail = False, "нет"
        if p.has_display and p.has_pyautogui:
            try:
                import pyautogui  # noqa: F401
                mouse_ok, mouse_detail = True, "pyautogui доступен"
            except Exception as e:  # noqa: BLE001
                mouse_detail = f"ошибка импорта: {e}"
        elif not p.has_display:
            mouse_detail = "нет дисплея — ввод записывается в виртуальный журнал"
        else:
            mouse_detail = "pyautogui не установлен (pip install pyautogui)"
        add("Мышь и клавиатура", mouse_ok or not p.has_display, mouse_detail,
            "pip install pyautogui")

        # --- окна ---
        win_ok, win_detail = False, "нет"
        if p.system == "windows":
            win_ok, win_detail = True, "через WinAPI (ctypes)"
        elif p.system == "linux" and (p.has_wmctrl or p.has_xdotool):
            win_ok = True
            win_detail = "wmctrl/xdotool"
        elif p.system == "linux":
            win_detail = "нужны wmctrl и xdotool"
        else:
            win_ok, win_detail = True, "через AppleScript"
        add("Управление окнами", win_ok or not p.has_display, win_detail,
            "sudo apt install wmctrl xdotool" if not win_ok else "")

        # --- голос ---
        v = self.voice_support()
        add("Микрофон/STT", bool(v.get("stt")), v.get("stt_reason", "?"),
            "pip install faster-whisper sounddevice numpy")
        add("Озвучка (TTS)", bool(v.get("tts")),
            f"{v.get('tts_provider') or 'нет'}")
        add("Глобальные хоткеи (вне окна)", bool(v.get("can_hotkeys")),
            "библиотека keyboard"
            if v.get("can_hotkeys") else "pip install keyboard")

        # --- браузер ---
        add("Управляемый браузер", p.has_playwright,
            "playwright установлен" if p.has_playwright else "не установлен",
            "pip install playwright && playwright install chromium")

        # --- реальные тесты (по запросу) ---
        tests: list[dict] = []
        if run_tests:
            tests.append(self._test_virtual_input())
            tests.append(self._test_terminal())
            if p.has_display:
                tests.append(self._test_window_enum())
            else:
                tests.append({"name": "Перечень окон", "ok": None,
                              "detail": "нет дисплея — пропущено"})

        return {
            "platform": p.summary(),
            "system": p.system,
            "has_display": p.has_display,
            "monitors": p.monitors,
            "checks": checks,
            "tests": tests,
        }

    def _test_virtual_input(self) -> dict:
        """Проверяет конвейер мыши/клавиатуры (не двигая реальный курсор)."""
        import asyncio
        async def _do():
            ctx = self.agent._ctx
            r1 = await self.registry.call("mouse_click",
                                          {"x": 10, "y": 10, "clicks": 1}, ctx)
            r2 = await self.registry.call("keyboard_type", {"text": "test"}, ctx)
            return r1, r2
        try:
            r1, r2 = asyncio.run_coroutine_threadsafe(_do(), self.loop).result(timeout=15)
            ok = r1.ok and r2.ok
            return {"name": "Конвейер мыши/клавиатуры", "ok": ok,
                    "detail": (r1.output[:80] + " | " + r2.output[:80])[:220]}
        except Exception as e:  # noqa: BLE001
            return {"name": "Конвейер мыши/клавиатуры", "ok": False,
                    "detail": f"ошибка: {e}"}

    def _test_terminal(self) -> dict:
        import asyncio
        async def _do():
            return await self.registry.call("terminal_run",
                                            {"command": "echo ok"}, self.agent._ctx)
        try:
            r = asyncio.run_coroutine_threadsafe(_do(), self.loop).result(timeout=20)
            return {"name": "Терминал", "ok": r.ok,
                    "detail": (r.output[:100] if r.ok else r.error[:100])}
        except Exception as e:  # noqa: BLE001
            return {"name": "Терминал", "ok": False, "detail": str(e)[:100]}

    def _test_window_enum(self) -> dict:
        import asyncio
        async def _do():
            return await self.registry.call("window_list", {}, self.agent._ctx)
        try:
            r = asyncio.run_coroutine_threadsafe(_do(), self.loop).result(timeout=15)
            wins = r.data.get("windows") if isinstance(r.data, dict) else None
            if isinstance(wins, list) and wins:
                titles = [w.get("title", "")[:30] for w in wins[:3]]
                return {"name": "Перечень окон", "ok": True,
                        "detail": f"окон: {len(wins)}; например: {', '.join(titles)}"}
            return {"name": "Перечень окон", "ok": r.ok,
                    "detail": r.output[:120]}
        except Exception as e:  # noqa: BLE001
            return {"name": "Перечень окон", "ok": False, "detail": str(e)[:100]}

    # ---------------- настройки модели (UI) ----------------
    def settings_view(self) -> dict:
        return {"ok": True,
                "llm": {"provider": self.cfg.llm.provider,
                        "base_url": self.cfg.llm.base_url,
                        "model": self.cfg.llm.model,
                        "api_key_set": bool(self.cfg.llm.api_key),
                        "supports_tool_calling": self.cfg.llm.supports_tool_calling,
                        "vision": self.cfg.llm.vision,
                        "max_tokens": self.cfg.llm.max_tokens,
                        "temperature": self.cfg.llm.temperature},
                "mode": self.cfg.safety.mode,
                "voice": dict(self.cfg.voice)}

    def settings_apply(self, body: dict) -> dict:
        d = (body or {}).get("llm") or {}
        llm = self.cfg.llm
        if d.get("base_url"):
            llm.base_url = str(d["base_url"]).rstrip("/")
            llm.provider = "openai"  # явный сервер: OpenAI/Ollama/LM Studio — всё совместимо
        if d.get("model"):
            llm.model = str(d["model"])
        if d.get("api_key"):
            llm.api_key = str(d["api_key"])
        elif d.get("clear_key"):
            llm.api_key = ""
        if "supports_tool_calling" in d:
            llm.supports_tool_calling = bool(d["supports_tool_calling"])
        if "vision" in d:
            llm.vision = bool(d["vision"])
        try:
            llm.max_tokens = max(256, min(int(d["max_tokens"]), 16384))
        except (TypeError, ValueError, KeyError):
            pass
        try:
            llm.temperature = max(0.0, min(float(d["temperature"]), 2.0))
        except (TypeError, ValueError, KeyError):
            pass
        try:
            new_llm = self.reload_llm()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"не удалось создать LLM: {e}"}
        self._persist_config()
        self.bus.emit("log", level="info",
                      message=f"Модель переключена: {new_llm.name} / {llm.model} @ {llm.base_url}")
        out = {"ok": True, "model": llm.model, "provider": new_llm.name}
        if isinstance((body or {}).get("voice"), dict):
            vr = self.voice_settings_apply({"voice": body["voice"]})
            out.update(vr)
        if "mode" in (body or {}):
            out["mode"] = self.set_mode(body["mode"])
        return out

    def reload_llm(self):
        """Пересоздаёт LLM из текущего config и подменяет у всех владельцев."""
        from .llm import create_llm
        new_llm = create_llm(self.cfg)
        self.llm = new_llm
        self.agent.llm = new_llm
        self.agent.planner.llm = new_llm
        self.agent._ctx.llm = new_llm
        return new_llm

    def _persist_config(self) -> None:
        """Сохраняет изменяемые настройки (модель, режим, голос) в config.json.

        Пишем в AGENT_HOME/config.json — он сливается поверх конфига
        репозитория при загрузке (см. Config.load), поэтому изменения
        переживают перезапуск в любом окружении.
        """
        try:
            p = self.cfg.agent_home / "config.json"
            data = {}
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8") or "{}")
            data["llm"] = {
                "provider": self.cfg.llm.provider,
                "base_url": self.cfg.llm.base_url,
                "model": self.cfg.llm.model,
                "api_key": self.cfg.llm.api_key,
                "supports_tool_calling": self.cfg.llm.supports_tool_calling,
                "vision": self.cfg.llm.vision,
                "max_tokens": self.cfg.llm.max_tokens,
                "temperature": self.cfg.llm.temperature,
            }
            data["safety"] = {"mode": self.cfg.safety.mode}
            data["voice"] = dict(self.cfg.voice)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    def stop(self) -> None:
        self.stop_global_voice()
        async def _stop():
            await self.tasks.stop()
            await self.schedules.stop()
            await self.triggers.stop()
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(_stop(), self.loop).result(timeout=5)
            except Exception:
                pass


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _has_cmd(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None
