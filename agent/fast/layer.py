"""Fast Layer — сборка быстрого пути целиком (ТЗ §1, §5, §45, §52).

Здесь связываются движок намерений, маршрутизатор, оптимизатор, реестр
приложений, состояние ПК, ожидания, ввод, зрение и исполнитель. Наружу слой
отдаёт три вещи:

  * `route(text)` — куда пойдёт задача (простое/сложное/вопрос);
  * `run(...)` — выполнить простую задачу без модели (или с одним вызовом для
    генерации кода); `None`, если задача уходит агентному циклу;
  * `preview(text)` — «что будет сделано» для PLAN ONLY и dry-run.

Слой создаётся в рантайме (`AgentRuntime`) и передаётся агенту, поэтому
инструменты получают все подсистемы через `ctx.services`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .executor import ExecutionResult, FastExecutor
from .intent import Intent, IntentEngine, IntentMemory
from .optimizer import ExecutionOptimizer
from .router import AGENT, CHAT, DIRECT, LLM_TEXT, VISION, FastRouter, Route

EXT_BY_LANG = {
    "python": ".py", "питон": ".py", "javascript": ".js", "js": ".js", "typescript": ".ts",
    "ts": ".ts", "java": ".java", "kotlin": ".kt", "cpp": ".cpp", "c++": ".cpp",
    "c#": ".cs", "csharp": ".cs", "go": ".go", "golang": ".go", "rust": ".rs",
    "html": ".html", "css": ".css", "php": ".php", "ruby": ".rb", "bash": ".sh",
    "sh": ".sh", "sql": ".sql", "swift": ".swift",
}

CODE_SYSTEM = ("Ты — опытный программист. Пишешь короткий, рабочий, "
               "самодостаточный код. Отвечай ТОЛЬКО кодом: без пояснений и без "
               "markdown-обрамления (без ```), чтобы ответ можно было сразу "
               "сохранить в файл.")

CHAT_SYSTEM = ("Ты — локальный ассистент, который управляет компьютером пользователя. "
               "Отвечай кратко, по делу, на русском языке. Если пользователь просит "
               "действие — скажи, что нужно выполнить его инструментом.")


class FastLayer:
    def __init__(self, cfg: Any = None, registry: Any = None, llm: Any = None,
                 bus: Any = None, metrics: Any = None, log: Any = None, cache: Any = None,
                 apps: Any = None, launcher: Any = None, state: Any = None, wait: Any = None,
                 inputs: Any = None, screens: Any = None, vision: Any = None,
                 optimizer: Any = None, policy: Any = None, gateway: Any = None,
                 platform: Any = None, workdir: str = ".") -> None:
        self.cfg = cfg
        self.registry = registry
        self.llm = llm
        self.bus = bus
        self.metrics = metrics
        self.log = log
        self.cache = cache
        self.apps = apps
        self.launcher = launcher
        self.state = state
        self.wait = wait
        self.inputs = inputs
        self.screens = screens
        self.vision = vision
        self.platform = platform
        self.workdir = workdir
        self.optimizer = optimizer or ExecutionOptimizer(metrics=metrics, log=log, cfg=cfg)
        memory_path = None
        try:
            if cfg is not None and getattr(cfg, "state_dir", None):
                memory_path = Path(cfg.state_dir) / "intent_memory.json"
        except Exception:
            memory_path = None
        self.memory = IntentMemory(memory_path)
        self.engine = IntentEngine(registry=apps, memory=self.memory, cfg=cfg)
        self.executor = FastExecutor(cfg=cfg, registry=registry, metrics=metrics, log=log,
                                     optimizer=self.optimizer, wait=wait, state=state,
                                     screens=screens, vision=vision, inputs=inputs,
                                     apps=apps, launcher=launcher, policy=policy,
                                     gateway=gateway, bus=bus)
        self.router = FastRouter(engine=self.engine, executor=self.executor, cfg=cfg,
                                 metrics=metrics, log=log, platform=platform)

    # ------------------------------------------------------------------ сервисы
    def services(self) -> dict:
        """Словарь подсистем для ToolContext (инструменты достают их через ctx.service)."""
        return {
            "cache": self.cache, "metrics": self.metrics, "log": self.log,
            "apps": self.apps, "launcher": self.launcher, "state": self.state,
            "wait": self.wait, "inputs": self.inputs, "screens": self.screens,
            "vision": self.vision, "optimizer": self.optimizer, "intent": self.engine,
            "router": self.router, "fast": self, "registry": self.registry,
            "llm": self.llm, "platform": self.platform,
        }

    # ------------------------------------------------------------------ маршрут
    def route(self, text: str, mode: str = "auto") -> Route:
        return self.router.route(text, mode=mode)

    def preview(self, text: str, mode: str = "") -> dict:
        """План без выполнения (PLAN ONLY / dry run)."""
        route = self.router.route(text, mode="plan_only")
        # подтверждения оцениваем по «боевому» режиму: в plan_only политика
        # разрешает всё (ничего же не выполняется), а показать нужно честно
        check_mode = mode or getattr(getattr(self.cfg, "safety", None), "mode", "auto")
        if check_mode == "plan_only":
            check_mode = "auto"
        intent = route.intent
        steps: list[str] = []
        known = route.kind != AGENT
        if known and intent is not None:
            steps = self.executor.preview(intent)
        needs_confirm = False
        if self.executor.policy is not None and self.registry is not None and intent is not None:
            tool_name = _main_tool(intent)
            tool = self.registry.get(tool_name) if tool_name else None
            if tool is not None:
                try:
                    args = _tool_args(intent, tool_name)
                    needs_confirm = self.executor.policy.decide(
                        check_mode, tool, args).needs_confirm or tool.risk >= 3
                except Exception:
                    needs_confirm = False
        return {"known": known, "route": route.kind, "reason": route.reason,
                "intent": getattr(intent, "name", ""), "steps": steps,
                "count": len(steps), "needs_confirm": needs_confirm,
                "confidence": round(route.confidence, 3),
                "label": intent.label() if hasattr(intent, "label") else "",
                "launch_plan": self._launch_preview(intent)}

    def _launch_preview(self, intent: Any) -> list[dict]:
        if self.launcher is None or getattr(intent, "name", "") != "launch_app":
            return []
        key = (intent.slots or {}).get("app_key") or (intent.slots or {}).get("target") or ""
        try:
            dry = self.launcher.dry_run(str(key))
            return dry.get("plan", []) if isinstance(dry, dict) else []
        except Exception:
            return []

    # ------------------------------------------------------------------ запуск
    async def run(self, text: str, ctx: Any, control: Any = None, mode: str = "auto",
                  record: bool = True) -> ExecutionResult | None:
        """Выполнить задачу быстрым путём.

        Возвращает результат или None, если задача должна уйти агентному циклу
        (тогда вызывающий код работает с моделью как обычно).
        """
        route = self.router.route(text, mode=mode)
        if not route.is_fast or route.kind == AGENT:
            if self.log is not None:
                self.log.event("route_agent", reason=route.reason,
                               intent=getattr(route.intent, "name", ""))
            return None
        if mode == "plan_only":
            return self._plan_only(route, text)
        if route.kind == CHAT:
            return await self._chat(route, text, ctx)
        if route.kind == LLM_TEXT:
            return await self._code_task(route, text, ctx, control=control, mode=mode)
        if route.kind == VISION:
            return await self._vision(route, ctx, control=control, mode=mode)
        if route.kind == DIRECT and getattr(route.intent, "name", "") == "compound" \
                and any(getattr(p, "name", "") == "code_task" for p in route.intent.parts):
            return await self._compound_with_code(route, text, ctx, control=control, mode=mode)
        res = await self.executor.execute(route.intent, ctx, control=control, mode=mode)
        res.route = DIRECT
        if record:
            self._learn(text, route.intent, res)
        return res

    # ------------------------------------------------------------------ частные маршруты
    def _plan_only(self, route: Route, text: str) -> ExecutionResult:
        preview = self.preview(text)
        lines = [f"План (режим plan_only — ничего не выполнялось): «{text}»"]
        for i, step in enumerate(preview.get("steps", []), 1):
            lines.append(f"{i}. {step}")
        lines.append(f"Маршрут: {preview.get('route')} · действий: {preview.get('count')} · "
                     f"подтверждение: {'да' if preview.get('needs_confirm') else 'нет'}")
        return ExecutionResult(True, "\n".join(lines), getattr(route.intent, "name", ""),
                               route=DIRECT, data={"preview": preview})

    async def _chat(self, route: Route, text: str, ctx: Any) -> ExecutionResult:
        if self.llm is None:
            return ExecutionResult(False, "модель недоступна", "chat", error="нет LLM")
        try:
            answer = await self.llm.answer([{"role": "system", "content": CHAT_SYSTEM},
                                            {"role": "user", "content": text}])
        except Exception as exc:                            # noqa: BLE001
            return ExecutionResult(False, f"Не удалось получить ответ: {exc}", "chat",
                                   error=str(exc), needs_agent=True)
        return ExecutionResult(True, (answer or "").strip(), "chat", route=CHAT,
                               data={"answer": answer})

    async def _vision(self, route: Route, ctx: Any, control: Any = None,
                      mode: str = "auto") -> ExecutionResult:
        res = await self.executor.execute(route.intent, ctx, control=control, mode=mode)
        res.route = VISION
        return res

    async def _code_task(self, route: Route, text: str, ctx: Any, control: Any = None,
                         mode: str = "auto") -> ExecutionResult:
        """Сценарий 5 ТЗ: код → файл → (VS Code) → запуск → проверка.

        Модель вызывается ровно один раз — за содержимым файла; всё остальное
        делают инструменты.
        """
        if self.llm is None:
            return ExecutionResult(False, "модель недоступна для генерации кода", "code_task",
                                   error="нет LLM", needs_agent=True)
        intent = route.intent
        prompt = (f"Напиши {intent.slots.get('what') or 'программу'} на "
                  f"{intent.slots.get('language') or 'python'}.")
        run_after = bool(re.search(r"(запусти|выполни|проверь|протестируй|и запусти)",
                                   text, re.I))
        if control is not None:
            try:
                await control.wait_if_paused()
            except Exception:
                pass
        try:
            code = await self.llm.answer([{"role": "system", "content": CODE_SYSTEM},
                                          {"role": "user", "content": prompt}])
        except Exception as exc:                            # noqa: BLE001
            return ExecutionResult(False, f"Модель не ответила: {exc}", "code_task",
                                   error=str(exc), needs_agent=True)
        code = _clean_code(code or "")
        if not code.strip():
            return ExecutionResult(False, "Модель вернула пустой код", "code_task",
                                   error="пустой код", needs_agent=True)
        lang = str(intent.slots.get("language") or "python").lower()
        ext = EXT_BY_LANG.get(lang, ".txt")
        what = str(intent.slots.get("what") or "code")
        target = str(intent.slots.get("path") or "") or str(
            Path(self.workdir or os.getcwd()) / f"{_slug(what)}{ext}")
        steps: list[dict] = []
        r = await self.executor._tool(ctx, "fs_write", {"path": target, "content": code})
        steps.append({"tool": "fs_write", "ok": r.ok, "output": (r.output or r.error)[:200]})
        if not r.ok:
            return ExecutionResult(False, r.error, "code_task", actions=steps,
                                   error=r.error, needs_agent=True)
        opened = await self.executor._tool(ctx, "open_vscode", {"path": target}, timeout=20)
        steps.append({"tool": "open_vscode", "ok": opened.ok,
                      "output": (opened.output or opened.error)[:160]})
        lines = [f"Готово: {Path(target).name} ({len(code)} символов) — {target}"]
        if opened.ok:
            lines.append("Открыл в VS Code.")
        verified = None
        if run_after:
            run = await self.executor._tool(ctx, "run_file", {"path": target}, timeout=90)
            steps.append({"tool": "run_file", "ok": run.ok, "output": (run.output or run.error)[:400]})
            verified = bool(run.ok)
            if run.ok:
                out = (run.output or "").strip().splitlines()
                lines.append("Запустил, вывод:\n" + "\n".join(out[:12]))
            else:
                lines.append(f"Не удалось запустить: {run.error[:200]}")
        self._learn(text, intent, ExecutionResult(True, "", "code_task"))
        return ExecutionResult(True, "\n".join(lines), "code_task", actions=steps,
                               route=LLM_TEXT, data={"path": target, "bytes": len(code),
                                                     "verified": verified, "code": code})

    async def _compound_with_code(self, route: Route, text: str, ctx: Any,
                                  control: Any = None, mode: str = "auto") -> ExecutionResult:
        """Составная команда, в которой одна из частей требует генерации кода.

        Пример ТЗ: «открой VS Code и напиши калькулятор на Python» — остальные
        шаги делают инструменты, а текст кода просит модель ровно один раз.
        """
        results: list[ExecutionResult] = []
        for part in route.intent.parts:
            if getattr(part, "name", "") == "code_task":
                sub = Route(LLM_TEXT, part, "шаг требует генерации", part.confidence)
                res = await self._code_task(sub, text, ctx, control=control, mode=mode)
            else:
                res = await self.executor.execute(part, ctx, control=control, mode=mode)
            results.append(res)
        ok = all(r.ok for r in results)
        return ExecutionResult(ok, "\n".join(r.text for r in results if r.text), "compound",
                               actions=[r.to_dict() for r in results],
                               error=next((r.error for r in results if r.error), ""),
                               needs_agent=any(r.needs_agent for r in results))

    # ------------------------------------------------------------------ обучение
    def _learn(self, text: str, intent: Any, res: ExecutionResult) -> None:
        if not res.ok or intent is None:
            return
        self.engine.remember_success(text, intent)
        if self.apps is not None and getattr(intent, "name", "") == "launch_app":
            key = (intent.slots or {}).get("app_key")
            if key:
                try:
                    self.apps.mark_used(key, True, res.ms, res.data.get("method", ""))
                except Exception:
                    pass

    def save(self) -> None:
        self.engine.save()

    def stats(self) -> dict:
        return {"router": self.router.summary(),
                "intent": self.engine.stats_summary(),
                "optimizer": self.optimizer.capabilities(),
                "learned_phrases": self.memory.size()}


def _clean_code(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```(?:[a-zA-Z0-9+.#-]*)\n(.*?)```", t, re.S)
    if m:
        return m.group(1).strip()
    t = re.sub(r"^```[a-zA-Z0-9+.#-]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _slug(text: str) -> str:
    try:
        from ..apps.aliases import translit
        base = translit(text)
    except Exception:
        base = text
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base or "").strip("_").lower()
    return (base or "code")[:40]


def _main_tool(intent: Any) -> str:
    mapping = {
        "launch_app": "launch_app", "open_url": "open_url", "open_folder": "open_path",
        "open_path": "open_path", "web_search": "search_google", "youtube_search": "search_youtube",
        "create_folder": "fs_mkdir", "create_file": "fs_write", "write_file": "fs_write",
        "read_file": "fs_read", "list_dir": "fs_list", "find_files": "fs_search",
        "delete_path": "fs_delete", "move_path": "fs_move", "copy_path": "fs_copy",
        "rename_path": "fs_move", "system_power": "system_power", "volume": "os_settings",
        "screenshot": "take_screenshot", "set_wallpaper": "set_wallpaper",
        "show_desktop": "show_desktop", "send_keys": "keyboard_hotkey",
        "type_text": "keyboard_type", "click_element": "click_element",
        "run_command": "execute_command", "kill_process": "process_kill",
        "clipboard_get": "clip_get", "clipboard_set": "clip_set",
    }
    return mapping.get(getattr(intent, "name", ""), "")


def _tool_args(intent: Any, tool: str) -> dict:
    slots = getattr(intent, "slots", {}) or {}
    if tool == "fs_delete":
        return {"path": slots.get("path", ""), "recursive": True}
    if tool in ("fs_write", "create_file"):
        return {"path": slots.get("path", ""), "content": slots.get("content", "")}
    if tool in ("fs_mkdir", "open_path", "fs_read", "fs_list"):
        return {"path": slots.get("path", "")}
    if tool in ("fs_move", "fs_copy"):
        return {"src": slots.get("src_path", ""), "dst": slots.get("dst_path", "")}
    if tool == "system_power":
        return {"action": slots.get("action", "")}
    if tool == "os_settings":
        return {"area": "volume", "action": slots.get("action", "get")}
    if tool == "execute_command":
        return {"command": slots.get("command", "")}
    if tool == "launch_app":
        return {"name": slots.get("app_key") or slots.get("target", "")}
    if tool in ("open_url",):
        return {"url": slots.get("url", "")}
    return {}


__all__ = ["FastLayer", "ExecutionResult", "Intent"]
