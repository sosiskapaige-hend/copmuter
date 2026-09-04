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
        return {"stt": _has("faster_whisper") and _has("sounddevice"),
                "tts": _has_cmd("espeak-ng") or _has_cmd("espeak") or _has_cmd("say")}

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
                "mode": self.cfg.safety.mode}

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
        return {"ok": True, "model": llm.model, "provider": new_llm.name}

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
        """Сохраняет настройки LLM в config.json внутри AGENT_HOME."""
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
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    def stop(self) -> None:
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
