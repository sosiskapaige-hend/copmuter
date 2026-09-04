"""Ядро: агентный цикл.

Цель → план → действия → наблюдения → проверка прогресса → самоисправление
→ (перепланирование) → ФАКТИЧЕСКАЯ проверка результата → отчёт.

Цикл останавливается не по «последний шаг выполнен», а по оценке LLM
(assess): цель достигнута и подтверждена. Ошибки анализируются, повторы
одного и того же действия ограничены, есть перепланирование.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..events import EventBus, InteractionGateway
from ..llm.base import LLM
from ..memory.longterm import LongTermMemory
from ..memory.session import SessionStore, TaskState, StepRecord
from ..platform import PlatformInfo
from ..safety.journal import Journal
from ..safety.policy import SafetyPolicy
from ..tools.base import ToolContext
from ..tools.registry import ToolRegistry
from .planner import Planner
from .tool_select import select_tools, find_tool_names


@dataclass
class Control:
    """Живое управление задачей: пауза/стоп."""
    mode: str = "run"          # run | pause | stop
    _ev: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        self._ev.set()

    def pause(self) -> None:
        self.mode = "pause"
        self._ev.clear()

    def resume(self) -> None:
        if self.mode == "pause":
            self.mode = "run"
        self._ev.set()

    def stop(self) -> None:
        self.mode = "stop"
        self._ev.set()

    async def wait_if_paused(self) -> None:
        if self.mode == "pause":
            self._ev.clear()
            await self._ev.wait()


class Agent:
    def __init__(self, cfg: Config, llm: LLM, registry: ToolRegistry,
                 bus: EventBus, gateway: InteractionGateway, journal: Journal,
                 sessions: SessionStore, memory: LongTermMemory,
                 policy: SafetyPolicy, platform: PlatformInfo,
                 workdir: str = ".") -> None:
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.bus = bus
        self.gateway = gateway
        self.journal = journal
        self.sessions = sessions
        self.memory = memory
        self.policy = policy
        self.platform = platform
        self.workdir = workdir
        self.planner = Planner(llm)
        self.running_tasks: dict[str, Control] = {}
        self._task_tasks: dict[str, asyncio.Task | None] = {}  # task_id -> asyncio.Task
        self._ctx = ToolContext(cfg=cfg, bus=bus, gateway=gateway, journal=journal,
                                memory=memory, platform=platform, llm=llm, workdir=workdir)

    # ------------------------- основной цикл -------------------------
    async def run_task(self, goal: str, mode: str = "", task_id: str | None = None,
                       resume: bool = False) -> TaskState:
        from ..safety.policy import validate_mode
        mode = validate_mode(mode or self.cfg.safety.mode)
        st = self.sessions.load(task_id) if (task_id and resume) else None
        if st is None or st.goal != goal:
            st = TaskState(task_id=task_id or __import__("uuid").uuid4().hex[:10],
                           goal=goal, mode=mode, status="planning")
        control = Control()
        self.running_tasks[st.task_id] = control
        self._task_tasks[st.task_id] = asyncio.current_task()

        try:
            # ---- 1. план ----
            if not st.plan:
                st.status = "planning"
                self.sessions.save(st)
                context = self._context(st)
                plan = await self.planner.make_plan(st.goal, context, self.registry.brief_list())
                if plan.needs_user:
                    answer = await self.gateway.ask(plan.needs_user, timeout=self.cfg.agent.ask_user_timeout)
                    st.goal = f"{st.goal}\n(Ответ пользователя: {answer})"
                    plan = await self.planner.make_plan(st.goal, context, self.registry.brief_list())
                st.plan = [{"title": s.title, "detail": s.detail} for s in plan.steps]
                self.bus.emit("plan", task_id=st.task_id, steps=st.plan, summary=plan.summary)
                self.sessions.save(st)

            if mode == "plan_only":
                st.status = "done"
                st.summary = ("План (режим plan_only — ничего не выполнялось):\n"
                              + "\n".join(f"{i+1}. {s['title']} — {s['detail']}"
                                          for i, s in enumerate(st.plan)))
                self.sessions.save(st)
                self.bus.emit("task_done", task_id=st.task_id, ok=True,
                              summary=st.summary, plan_only=True)
                return st

            # ---- 2. цикл ----
            st.status = "running"
            self.sessions.save(st)
            history: list[dict] = []
            consecutive_errors: dict[str, int] = {}
            replans = 0
            final_checks = 0
            last_assess = 0
            idle_replies = 0          # ответы модели текстом без действия подряд
            actions_done = 0          # сколько инструментов реально выполнено
            pinned: set[str] = set()  # инструменты, упомянутые моделью/ошибками
            done_summary_lines: list[str] = []
            max_iter = self.cfg.agent.max_iterations

            while st.iteration < max_iter:
                control = self.running_tasks.get(st.task_id, control)
                await control.wait_if_paused()
                if control.mode == "stop":
                    st.status = "cancelled"
                    st.summary = "Задача остановлена пользователем."
                    self.sessions.save(st)
                    self.bus.emit("task_failed", task_id=st.task_id, error="отменено пользователем")
                    return st

                st.iteration += 1
                decision = await self.llm.next_action(st.goal, self._context(st),
                                                      history, self._tool_schemas(st, pinned))
                if decision.thought:
                    self.bus.emit("thought", task_id=st.task_id,
                                  text=decision.thought[:400])
                    # инструменты, о которых модель «думает», попадут в набор
                    # на следующем ходу, даже если их отсеял бюджет
                    pinned |= find_tool_names(decision.thought, self.registry.names())

                # --- вопрос пользователю ---
                if decision.ask:
                    st.status = "waiting_user"
                    self.sessions.save(st)
                    answer = await self.gateway.ask(decision.ask, timeout=self.cfg.agent.ask_user_timeout)
                    history.append({"role": "user", "content": f"Ответ пользователя: {answer}"})
                    continue

                # --- завершение по решению LLM ---
                if decision.final is not None:
                    final_checks += 1
                    assess = await self.llm.assess(st.goal,
                                                   "\n".join(done_summary_lines)[-3000:],
                                                   st.last_error or None)
                    # Завершаем, если проверка подтвердила цель, либо модель
                    # настаивает повторно — но ТОЛЬКО если хоть что-то реально
                    # сделано. «Финал» без единого действия = отказ модели
                    # работать; такую задачу нельзя отмечать как выполненную.
                    if assess.achieved or (final_checks >= 2 and actions_done > 0):
                        st.status = "done"
                        st.progress = 100
                        st.summary = decision.final
                        self.sessions.save(st)
                        self.bus.emit("task_done", task_id=st.task_id, ok=True,
                                      summary=decision.final,
                                      details=f"progress={assess.progress}% {assess.message}")
                        return st
                    if actions_done == 0 and final_checks >= 3:
                        st.status = "failed"
                        st.summary = ("Модель не выполнила ни одного действия и трижды пыталась "
                                      "завершить задачу текстом. Последний ответ модели:\n\n"
                                      f"{decision.final[:1200]}\n\n"
                                      "Проверьте в ⚙ Настройках, что модель поддерживает вызов "
                                      "инструментов (function calling), или попробуйте другую модель.")
                        self.sessions.save(st)
                        self.bus.emit("task_failed", task_id=st.task_id, error=st.summary)
                        return st
                    # LLM хочет завершить, но цель не достигнута
                    history.append({"role": "user",
                                    "content": ("ОСТАНОВИСЬ. Ты заявил готовность, но проверка показала: "
                                                f"прогресс {assess.progress}%. Осталось: "
                                                f"{'; '.join(assess.remaining) or assess.message}. "
                                                "Вызови нужный инструмент. Текст без вызова инструмента "
                                                "ничего не делает.")})
                    self.bus.emit("log", level="warn",
                                  message=f"LLM хотел завершить, но цель не достигнута ({assess.progress}%)")
                    continue

                # --- вызов инструмента ---
                tc = decision.tool_call
                if tc is None:
                    # Модель ответила текстом («сейчас создам папку…») вместо
                    # действия. Это самая частая причина «агент отвечает, но
                    # ничего не делает». Настойчиво требуем вызов инструмента,
                    # после нескольких попыток — честно завершаем с ошибкой.
                    idle_replies += 1
                    if idle_replies >= 4:
                        st.status = "failed"
                        st.summary = ("Модель не вызывает инструменты — она только описывает действия "
                                      "текстом, поэтому на компьютере ничего не выполняется.\n\n"
                                      f"Последний ответ модели:\n{(decision.thought or '')[:800]}\n\n"
                                      "Что проверить: ⚙ Настройки → включён ли «function calling»; "
                                      "поддерживает ли выбранная модель вызов инструментов "
                                      "(для LM Studio подходят Qwen3 / Qwen2.5-Instruct, Llama 3.x, "
                                      "Mistral); достаточно ли контекста (≥ 8k токенов).")
                        self.sessions.save(st)
                        self.bus.emit("task_failed", task_id=st.task_id, error=st.summary)
                        return st
                    history.append({"role": "assistant", "content": (decision.thought or "")[:800]})
                    history.append({"role": "user",
                                    "content": ("Это был текст, а не действие — на компьютере ничего не "
                                                "произошло. СЕЙЧАС вызови ОДИН подходящий инструмент "
                                                "(например fs_mkdir, terminal_run, launch_app, open_url). "
                                                "Если цель уже фактически достигнута и проверена — вызови "
                                                "finish_task(summary=...). Нужен ответ пользователя — "
                                                "ask_user(question=...).")})
                    self.bus.emit("log", level="warn",
                                  message=f"модель ответила текстом без действия ({idle_replies}/4)")
                    continue
                idle_replies = 0

                tool = self.registry.get(tc.name)
                self.bus.emit("tool_call", task_id=st.task_id, name=tc.name,
                              args=_small_args(tc.args))
                # Ход модели фиксируем в истории: без него модель «не помнит»,
                # что уже вызывала, и повторяет одно и то же действие.
                history.append({"role": "assistant",
                                "content": ((decision.thought or "").strip()[:600] + "\n"
                                            if decision.thought else "")
                                + f"Вызов инструмента: {tc.name}("
                                + json.dumps(_small_args(tc.args), ensure_ascii=False)[:600] + ")"})

                if tool is None:
                    # подсказываем ближайшие по названию инструменты
                    import difflib
                    close = difflib.get_close_matches(tc.name, self.registry.names(), n=5, cutoff=0.4)
                    pinned |= set(close)
                    obs = (f"Инструмент '{tc.name}' не существует. "
                           + (f"Похожие: {', '.join(close)}. " if close else "")
                           + f"Все доступные: {', '.join(self.registry.names())}")
                    history.append({"role": "tool", "tool": tc.name, "ok": False,
                                    "content": obs})
                    continue

                # --- безопасность ---
                sd = self.policy.decide(st.mode, tool, tc.args, self._ctx)
                if sd.needs_confirm:
                    desc = self._describe_action(tool, tc.args, sd)
                    approved, comment = await self.gateway.confirm(
                        desc, {"tool": tc.name, "args": _small_args(tc.args),
                               "reason": sd.reason, "risk": sd.risk.name.lower()},
                        timeout=self.cfg.agent.ask_user_timeout)
                    if not approved:
                        note = f"ПОЛЬЗОВАТЕЛЬ ОТКАЗАЛ. Комментарий: {comment or '—'}. Выбери другой путь."
                        history.append({"role": "tool", "tool": tc.name, "ok": False,
                                        "content": note})
                        self.bus.emit("log", level="warn",
                                      message=f"отказ пользователя: {tc.name}")
                        continue
                    if comment:
                        history.append({"role": "user", "content": f"Пользователь одобрил с комментарием: {comment}"})

                # --- выполнение ---
                t0 = time.time()
                result = await self.registry.call(tc.name, tc.args, self._ctx)
                dt = time.time() - t0
                ok = result.ok
                actions_done += 1
                out_text = result.for_llm()
                self.bus.emit("observation", task_id=st.task_id, name=tc.name, ok=ok,
                              output=out_text[:1000], error=result.error[:500],
                              elapsed=round(dt, 2),
                              data={k: v for k, v in result.data.items() if k != "undo"})

                undo = result.data.get("undo")
                self.journal.record(st.task_id, tc.name, tc.args, ok, undo=undo)
                st.steps.append(StepRecord(len(st.steps),
                                           _plan_title_for(st.plan, tc.name, tc.args),
                                           tc.name, _small_args(tc.args), ok,
                                           result.output[:500], result.error[:300]))
                if len(st.steps) > 300:
                    st.steps = st.steps[-300:]

                if ok:
                    done_summary_lines.append(f"✔ {tc.name}({_small_args(tc.args)}): {result.output[:200]}")
                    consecutive_errors[tc.name + json.dumps(tc.args, sort_keys=True, default=str)] = 0
                else:
                    st.last_error = f"{tc.name}: {result.error[:500]}"
                    done_summary_lines.append(f"✘ {tc.name}: {result.error[:200]}")
                    key = tc.name + json.dumps(tc.args, sort_keys=True, default=str)
                    consecutive_errors[key] = consecutive_errors.get(key, 0) + 1
                    self.bus.emit("error", task_id=st.task_id, tool=tc.name,
                                  error=result.error[:500])
                    if consecutive_errors[key] >= self.cfg.agent.max_retry_per_action:
                        # самонаказание за повтор: меняем стратегию
                        history.append({"role": "user",
                                        "content": ("ЭТО ЖЕ САМОЕ ДЕЙСТВИЕ ПОВТОРИЛОСЬ "
                                                    f"{consecutive_errors[key]} РАЗА. "
                                                    "Стратегия не работает. Остановись, переосмысли и "
                                                    "выбери принципиально другой инструмент/путь. "
                                                    "Если зашёл в тупик — ask_user.")})
                        replans += 1
                        if replans % 2 == 0 and replans <= 6:
                            await self._replan(st, history, consecutive_errors)

                history.append({"role": "tool", "tool": tc.name, "ok": ok,
                                "content": out_text})

                # finish_task
                if ok and result.data.get("_finish"):
                    st.status = "done"
                    st.progress = 100
                    st.summary = result.output
                    self.sessions.save(st)
                    self.bus.emit("task_done", task_id=st.task_id, ok=True,
                                  summary=result.output, details=result.data.get("details", ""))
                    return st

                # --- периодическая проверка прогресса ---
                if (st.iteration - last_assess) >= self.cfg.agent.assess_every_n_steps:
                    last_assess = st.iteration
                    assess = await self.llm.assess(st.goal,
                                                   "\n".join(done_summary_lines)[-3000:],
                                                   st.last_error or None)
                    st.progress = max(st.progress, assess.progress)
                    self.bus.emit("progress", task_id=st.task_id, progress=assess.progress,
                                  remaining=assess.remaining, message=assess.message)
                    if assess.error_analysis:
                        history.append({"role": "user",
                                        "content": f"Анализ ошибки (внутренний): {assess.error_analysis}"})
                    if assess.adjust_plan and replans < 6:
                        replans += 1
                        await self._replan(st, history, consecutive_errors)
                    if assess.achieved and st.iteration >= 2:
                        st.status = "done"
                        st.progress = 100
                        st.summary = await self._final_summary(st, done_summary_lines, assess)
                        self.sessions.save(st)
                        self.bus.emit("task_done", task_id=st.task_id, ok=True, summary=st.summary)
                        return st

                self.sessions.save(st)
                if len(history) > self.cfg.agent.context_window_messages:
                    history = history[-self.cfg.agent.context_window_messages:]

                # Жёсткое ограничение на суммарный размер истории/контекста: 
                # локальные модели (например, Qwen 4B) быстро падают по context window.
                # Оставляем только последние сообщения и режем длинные поля.
                # Режем ТОЛЬКО строковый content, не добавляя ключей (иначе у
                # сообщений появляется content: null, что ломает chat/completions).
                history = _truncate_history(history)

            # лимит итераций
            st.status = "failed"
            st.summary = f"Превышен лимит итераций ({max_iter}). Прогресс: {st.progress}%."
            self.sessions.save(st)
            self.bus.emit("task_failed", task_id=st.task_id, error=st.summary)
            return st

        except asyncio.CancelledError:
            # Задачу остановили (кнопка «Стоп»). Не пробрасываем дальше —
            # владелец (чат/очередь) должен получить нормальный статус
            # cancelled и аккуратно завершить UI. Возвращаем st, чтобы
            # CancelledError не убил внешнюю корутину (worker очереди и т.п.).
            st.status = "cancelled"
            st.summary = "Задача остановлена пользователем."
            self.sessions.save(st)
            self.bus.emit("task_failed", task_id=st.task_id, error="отменено пользователем")
            return st
        except Exception as e:  # noqa: BLE001
            st.status = "failed"
            st.summary = _friendly_agent_error(e, self.cfg)
            self.sessions.save(st)
            self.bus.emit("task_failed", task_id=st.task_id, error=st.summary)
            return st
        finally:
            self.running_tasks.pop(st.task_id, None)
            self._task_tasks.pop(st.task_id, None)

    async def _replan(self, st: TaskState, history: list[dict],
                      errors: dict[str, int]) -> None:
        top_errors = sorted(errors.items(), key=lambda kv: -kv[1])[:3]
        err_text = "; ".join(f"повторяющаяся ошибка: {k[:120]}" for k, v in top_errors if v) \
            or st.last_error[:400]
        context = self._context(st)
        plan = await self.planner.make_plan(st.goal, context, self.registry.brief_list(),
                                            error_summary=err_text)
        if plan.steps:
            st.plan = [{"title": s.title, "detail": s.detail} for s in plan.steps]
            self.bus.emit("plan", task_id=st.task_id, steps=st.plan, summary=plan.summary,
                          replan=True)
            history.append({"role": "user",
                            "content": "ПЛАН ПЕРЕСОБРАН (см. контекст). Работай по новому плану."})
            self.sessions.save(st)

    async def _final_summary(self, st: TaskState, done_lines: list[str], assess: Any) -> str:
        """Человеческий итог для пользователя, когда задача завершена по
        оценке прогресса (модель не вызвала finish_task сама).

        Раньше в чат уходило сырое «ok Последнее: fs_list({'path': ...}): <d> …».
        """
        steps = [ln for ln in done_lines if ln.startswith("✔")]
        fallback_lines = ["✅ Задача выполнена."]
        if assess.message and assess.message.strip().lower() not in ("ok", "ок", "done"):
            fallback_lines.append(assess.message.strip())
        if steps:
            fallback_lines.append("")
            fallback_lines.append("Что сделано:")
            for ln in steps[-6:]:
                # «✔ fs_mkdir({'path': 'X'}): Создана папка: /…» → «• Создана папка: /…»
                tail = ln.split("): ", 1)[1] if "): " in ln else ln[2:]
                fallback_lines.append(f"• {tail.strip()[:160]}")
        fallback = "\n".join(fallback_lines)
        try:
            text = await self.llm.answer([
                {"role": "system", "content": (
                    "Ты — AI-агент, только что выполнивший задачу на компьютере пользователя. "
                    "Напиши краткий итог (2-4 предложения, на языке пользователя): что сделано, "
                    "где результат. Только факты из списка действий, без выдумок и без markdown-заголовков.")},
                {"role": "user", "content": f"Задача: {st.goal}\n\nВыполненные действия:\n"
                                            + "\n".join(done_lines[-12:])},
            ])
            text = (text or "").strip()
            if text and len(text) < 1500 and "{" not in text[:2]:
                return text
        except Exception:  # noqa: BLE001 — итог не должен ронять задачу
            pass
        return fallback

    def _tool_schemas(self, st: TaskState, pinned: set[str] | None = None) -> list[dict]:
        """Схемы инструментов для текущего хода с учётом бюджета контекста.

        Полный набор (~84 схемы ≈ 10k токенов) не помещается в окно
        локальных моделей — отбираем релевантные задаче (ядро + по ключевым
        словам цели/плана + уже использованные + упомянутые моделью).
        """
        budget = int(getattr(self.cfg.agent, "tools_budget", 0) or 0)
        all_tools = self.registry.all()
        if budget <= 0 or budget >= len(all_tools):
            return [t.to_schema() for t in all_tools]
        plan_text = " ".join(f"{s.get('title','')} {s.get('detail','')}" for s in (st.plan or []))
        recent = [s.tool for s in st.steps[-20:]]
        chosen = select_tools(st.goal, plan_text, recent, pinned or (), all_tools,
                              platform=self.platform, budget=budget)
        return [t.to_schema() for t in chosen]

    def _context(self, st: TaskState) -> str:
        from .prompts import build_context
        plan_text = "\n".join(f"{i+1}. {s['title']} — {s.get('detail','')}"
                              for i, s in enumerate(st.plan)) if st.plan else "(нет)"
        done_text = "\n".join(
            f"{'✔' if s.ok else '✘'} {s.tool}({_small_args(s.args)}): {s.output[:150]}"
            for s in st.steps[-15:])
        memory_ctx = self.memory.relevant_context(st.goal)
        return build_context(self.platform.summary(), memory_ctx,
                             self.registry.brief_list(), plan_text, done_text)

    @staticmethod
    def _describe_action(tool: Any, args: dict, sd: Any) -> str:
        risk_name = {"none": "низкий", "low": "низкий", "medium": "средний",
                     "high": "ВЫСОКИЙ", "critical": "КРИТИЧЕСКИЙ"}.get(str(sd.risk).split(".")[-1].lower(), "?")
        return (f"Агент собирается выполнить: {tool.name}\nАргументы: "
                f"{json.dumps(_small_args(args), ensure_ascii=False)[:400]}\n"
                f"Уровень риска: {risk_name}. {sd.reason}")

    # ------------------------- управление -------------------------
    def pause_task(self, task_id: str) -> bool:
        c = self.running_tasks.get(task_id)
        if c:
            c.pause()
            self.sessions.save(self.sessions.active.get(task_id)) if task_id in self.sessions.active else None
            self.bus.emit("task_state", task_id=task_id, state="paused")
            return True
        return False

    def resume_task(self, task_id: str) -> bool:
        c = self.running_tasks.get(task_id)
        if c:
            c.resume()
            self.bus.emit("task_state", task_id=task_id, state="running")
            return True
        return False

    def stop_task(self, task_id: str, force: bool = False) -> bool:
        """Остановить задачу.

        Сначала ставится мягкий флаг stop (цикл остановится на безопасной
        границе). При `force=True` (кнопка «Стоп» в UI) дополнительно
        отменяется текущее ожидание — в т.ч. длинный запрос к LLM, чтобы
        остановка срабатывала сразу, а не «со второго раза».
        """
        c = self.running_tasks.get(task_id)
        if c:
            c.stop()
            if force:
                t = self._task_tasks.get(task_id)
                if t is not None and not t.done():
                    t.cancel()
            return True
        return False

    def list_running(self) -> list[str]:
        return list(self.running_tasks.keys())


def _friendly_agent_error(e: Exception, cfg: Any) -> str:
    """Понятное объяснение сбоя вместо «LLMError: Сеть: [Errno 111]»."""
    s = str(e)
    low = s.lower()
    url = getattr(getattr(cfg, "llm", None), "base_url", "")
    model = getattr(getattr(cfg, "llm", None), "model", "")
    if "connection refused" in low or "errno 111" in low or "10061" in low or "errno 61" in low:
        return (f"Не удалось подключиться к серверу модели ({url}).\n\n"
                "Агент не может действовать без модели. Что проверить:\n"
                "• LM Studio запущен, модель загружена, включён Developer → Start Server (порт 1234);\n"
                "• адрес сервера в ⚙ Настройках совпадает с тем, что показывает LM Studio;\n"
                "• кнопка «Проверить связь» в ⚙ Настройках отвечает «сервер доступен».")
    if "timed out" in low or "timeout" in low:
        return (f"Модель ({model}) не ответила за отведённое время.\n\n"
                "Обычно это слишком большая модель для вашего железа или зависший сервер. "
                "Попробуйте модель поменьше (например Qwen3-4B/8B) или перезапустите сервер.")
    if "http 404" in low:
        return (f"Сервер {url} отвечает 404 — модель «{model}» не найдена.\n\n"
                "В ⚙ Настройках нажмите «Обновить» и выберите модель из списка сервера.")
    if "http 401" in low or "http 403" in low:
        return (f"Сервер {url} отклонил запрос (нет доступа).\n\n"
                "Проверьте API-ключ в ⚙ Настройках.")
    if "http 400" in low and ("context" in low or "token" in low or "length" in low):
        return ("Запрос не поместился в контекст модели.\n\n"
                "Увеличьте контекст (LM Studio → загрузка модели → Context Length ≥ 8192) "
                "или уменьшите `agent.tools_budget` в config.json (например 25).")
    if "http 400" in low and ("tool" in low or "function" in low):
        return ("Сервер не принимает вызовы инструментов (function calling) для этой модели.\n\n"
                "Выключите «function calling» в ⚙ Настройках — агент перейдёт на JSON-протокол, "
                "либо выберите модель с поддержкой tools (Qwen2.5/Qwen3-Instruct, Llama 3.x).")
    return f"Критическая ошибка агента: {type(e).__name__}: {s[:600]}"


def _small_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        else:
            out[k] = v
    return out


def _truncate_history(history: list[dict], limit: int = 1200) -> list[dict]:
    """Обрезает длинные строковые `content` в истории, не добавляя новых ключей.

    Раньше здесь в каждое сообщение принудительно вставлялись `content` и
    `text` (иногда со значением None), что давало `content: null` у
    tool-сообщений и ломало валидацию chat/completions на стороне сервера.
    """
    out: list[dict] = []
    for msg in history:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > limit:
            msg = {**msg, "content": content[:limit] + "... [усечено]"}
        out.append(msg)
    return out


def _plan_title_for(plan: list[dict], tool: str, args: dict) -> str:
    parts = tool.split("_")
    stem = parts[1] if len(parts) > 1 else tool
    for s in plan:
        t = (s.get("detail") or "") + " " + (s.get("title") or "")
        if stem[:3].lower() in t.lower() or (len(stem) >= 3 and stem.lower() in t.lower()):
            return s.get("title", "")
    return ""
