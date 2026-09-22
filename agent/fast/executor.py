"""Fast Executor: намерение → действия (ТЗ §7, §10, §20, §22, §28).

Здесь заканчивается «быстрый путь»: роутер решил, что задача простая, движок
намерений разобрал фразу — исполнитель делает работу инструментами. Модель в
этом процессе не участвует вообще (кроме случаев, когда роутер явно попросил
её сгенерировать текст — это отдельный шаг).

Что обеспечивается:

  * проверка результата после каждого действия (файл существует, процесс
    появился, экран изменился);
  * запасные способы при отказе (лестница методов, ТЗ §28);
  * подтверждения для опасных операций (политика безопасности + шлюз);
  * запись в метрики/лог и обучение алиасов.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.base import ToolResult


@dataclass
class ExecutionResult:
    ok: bool
    text: str
    intent_name: str = ""
    actions: list = field(default_factory=list)
    error: str = ""
    ms: float = 0.0
    route: str = "fast"
    needs_agent: bool = False
    needs_llm_text: str = ""      # запрос к модели (для задач с генерацией текста)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "intent": self.intent_name,
                "error": self.error, "ms": round(self.ms, 1), "route": self.route,
                "needs_agent": self.needs_agent,
                "actions": [a.to_dict() if hasattr(a, "to_dict") else a for a in self.actions]}


class FastExecutor:
    """Исполнение простых команд без обращения к модели."""

    def __init__(self, cfg: Any = None, registry: Any = None, metrics: Any = None,
                 log: Any = None, optimizer: Any = None, wait: Any = None, state: Any = None,
                 screens: Any = None, vision: Any = None, inputs: Any = None,
                 apps: Any = None, launcher: Any = None, policy: Any = None,
                 gateway: Any = None, bus: Any = None) -> None:
        self.cfg = cfg
        self.registry = registry
        self.metrics = metrics
        self.log = log
        self.optimizer = optimizer
        self.wait = wait
        self.state = state
        self.screens = screens
        self.vision = vision
        self.inputs = inputs
        self.apps = apps
        self.launcher = launcher
        self.policy = policy
        self.gateway = gateway
        self.bus = bus

    # ------------------------------------------------------------------ вход
    async def execute(self, intent: Any, ctx: Any, control: Any = None,
                      mode: str = "auto") -> ExecutionResult:
        t0 = time.perf_counter()
        name = getattr(intent, "name", "")
        if name == "compound":
            return await self._compound(intent, ctx, control, mode)
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            return ExecutionResult(False, f"быстрый путь не умеет «{name}»",
                                   intent_name=name, needs_agent=True,
                                   error="нет обработчика",
                                   ms=(time.perf_counter() - t0) * 1000)
        try:
            res = await handler(intent, ctx, control, mode)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                            # noqa: BLE001
            res = ExecutionResult(False, f"Ошибка при выполнении: {exc}", intent_name=name,
                                  error=f"{type(exc).__name__}: {exc}")
        res.ms = (time.perf_counter() - t0) * 1000
        if self.log is not None:
            try:
                self.log.event("fast_exec", intent=name, ok=res.ok, ms=round(res.ms, 1),
                               route=res.route, error=res.error[:160])
            except Exception:
                pass
        return res

    # ------------------------------------------------------------------ помощь
    async def _tool(self, ctx: Any, name: str, args: dict, timeout: float = 30.0,
                    fallbacks: list[tuple[str, dict]] | None = None) -> ToolResult:
        if self.registry is None:
            return ToolResult.fail("реестр инструментов недоступен")
        attempts: list[tuple[str, dict]] = [(name, args or {})] + list(fallbacks or [])
        last: ToolResult = ToolResult.fail("не выполнено")
        for i, (tool_name, tool_args) in enumerate(attempts):
            t0 = time.perf_counter()
            try:
                res = await asyncio.wait_for(self.registry.call(tool_name, tool_args, ctx), timeout=timeout)
            except asyncio.TimeoutError:
                res = ToolResult.fail(f"таймаут {timeout:.0f} с")
            except Exception as exc:                        # noqa: BLE001
                res = ToolResult.fail(f"{type(exc).__name__}: {exc}")
            ms = (time.perf_counter() - t0) * 1000
            if self.metrics is not None:
                try:
                    self.metrics.tool_call(tool_name, ms, res.ok, timeout="таймаут" in res.error)
                except Exception:
                    pass
            if res.ok:
                if i and res.data is not None:
                    res.data = {**res.data, "fallback_from": attempts[0][0]}
                return res
            last = res
            if self.log is not None:
                try:
                    self.log.event("fallback", tool=tool_name, next=attempts[i + 1][0]
                                   if i + 1 < len(attempts) else "",
                                   error=(res.error or "")[:160])
                except Exception:
                    pass
        return last

    async def _confirm(self, ctx: Any, tool_name: str, args: dict, description: str,
                       mode: str = "auto") -> bool:
        """Нужно ли подтверждение и получено ли оно."""
        if self.policy is None or self.gateway is None or self.registry is None:
            return True
        tool = self.registry.get(tool_name)
        if tool is None:
            return True
        try:
            decision = self.policy.decide(mode, tool, args, ctx)
        except Exception:
            return True
        if not decision.needs_confirm:
            return True
        timeout = float(getattr(getattr(self.cfg, "agent", None), "ask_user_timeout", 300) or 300)
        ok, comment = await self.gateway.confirm(description, {"tool": tool_name, "args": args}, timeout)
        if self.log is not None:
            self.log.event("confirm", tool=tool_name, approved=ok, comment=comment[:80])
        return bool(ok)

    async def _verify_path(self, path: str, kind: str = "exists", timeout: float = 3.0) -> tuple[bool, str]:
        p = Path(os.path.expanduser(os.path.expandvars(str(path))))
        if self.wait is not None and kind in ("exists", "gone"):
            try:
                if kind == "exists" and p.suffix:
                    res = await self.wait.wait_file_exists(str(p), timeout=timeout)
                elif kind == "exists":
                    res = await self.wait.wait_condition(lambda: p.exists(), timeout=timeout)
                else:
                    res = await self.wait.wait_file_gone(str(p), timeout=timeout)
                if res.ok:
                    return True, res.detail or "подтверждено"
                return False, res.detail or "не подтвердилось"
            except Exception:
                pass
        ok = p.exists() if kind == "exists" else not p.exists()
        return ok, ("файл на месте" if ok else "не найден") if kind == "exists" else \
            ("удалено" if ok else "ещё существует")

    def _emit(self, ctx: Any, kind: str, **kw: Any) -> None:
        bus = getattr(ctx, "bus", None) or self.bus
        if bus is None:
            return
        try:
            bus.emit(kind, **kw)
        except Exception:
            pass

    # ================================================================== приложения
    async def _do_launch_app(self, intent, ctx, control, mode) -> ExecutionResult:
        slots = intent.slots
        key = slots.get("app_key") or ""
        spoken = slots.get("target") or key
        args = str(slots.get("args") or "")
        rec = None
        if self.apps is not None and key:
            rec = self.apps.get(key)
        if self.launcher is not None:
            res = await self.launcher.launch(spoken or key, target=args, rec=rec, verify=True,
                                             timeout=float(getattr(getattr(self.cfg, "fast", None),
                                                                  "launch_timeout", 12) or 12))
            text = res.message if res.ok else (res.error or res.message)
            if res.ok:
                if self.optimizer is not None:
                    self.optimizer.note("launch_app", res.method, True, res.ms)
                return ExecutionResult(True, text, intent.name,
                                       actions=[a.to_dict() for a in res.attempts],
                                       data={"method": res.method, "pid": res.pid,
                                             "verified": res.verified, "app": key})
            if self.optimizer is not None:
                self.optimizer.note("launch_app", res.method or "unknown", False, res.ms)
            return ExecutionResult(False, text, intent.name, error=text,
                                   actions=[a.to_dict() for a in res.attempts], data={"app": key})
        r = await self._tool(ctx, "launch_app", {"name": spoken or key, "path": args},
                             timeout=20, fallbacks=[("open_path", {"path": spoken})])
        return ExecutionResult(r.ok, r.output or (r.error or ""), intent.name, error=r.error,
                               data=dict(r.data or {}))

    async def _do_open_url(self, intent, ctx, control, mode) -> ExecutionResult:
        from ..tools.browser_agent import normalize_target
        raw = str(intent.slots.get("url") or intent.slots.get("target") or "").strip()
        url = normalize_target(raw)
        if not url:
            return ExecutionResult(False, "не понял, какую ссылку открыть", intent.name,
                                   error="пустой url", needs_agent=True)
        if self.launcher is not None and url.startswith(("http", "file")):
            res = await self.launcher.open_url(ctx, url)
            if res.ok:
                if self.optimizer is not None:
                    self.optimizer.note("open_url", res.method, True, res.ms)
                return ExecutionResult(True, res.message, intent.name, data={"url": url,
                                                                             "method": res.method})
        r = await self._tool(ctx, "open_url", {"url": url},
                             fallbacks=[("open_url", {"url": url, "browser": "chrome"})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"url": url})

    async def _do_open_folder(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        if not path:
            from ..system import paths as path_tools
            target = str(intent.slots.get("target") or "")
            known = path_tools.place_dir(target)
            if known is None:
                return ExecutionResult(False, f"не знаю такую папку: {target}", intent.name,
                                       error="папка не найдена", needs_agent=True)
            path = str(known)
        r = await self._tool(ctx, "open_path", {"path": path},
                             fallbacks=[("open_path", {"path": str(Path(path).parent)})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"path": path})

    async def _do_open_path(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or intent.slots.get("target") or "")
        r = await self._tool(ctx, "open_path", {"path": path},
                             fallbacks=[("open_vscode", {"path": path}),
                                        ("open_folder", {"path": str(Path(path).parent)})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"path": path})

    # ================================================================== поиск
    async def _do_web_search(self, intent, ctx, control, mode) -> ExecutionResult:
        from ..tools.browser_agent import search_url
        q = str(intent.slots.get("query") or "")
        engine = str(intent.slots.get("engine") or "google")
        if not q:
            return ExecutionResult(False, "пустой запрос", intent.name, needs_agent=True,
                                   error="нет запроса")
        url = search_url(q, engine)
        r = await self._tool(ctx, "search_google", {"query": q, "engine": engine, "open": True})
        if not r.ok:
            r = await self._tool(ctx, "open_url", {"url": url})
        text = r.output if r.ok else r.error
        return ExecutionResult(r.ok, text or f"Открыл поиск: {url}", intent.name, error=r.error,
                               data={"url": url, "query": q})

    async def _do_youtube_search(self, intent, ctx, control, mode) -> ExecutionResult:
        from ..tools.browser_agent import search_url
        q = str(intent.slots.get("query") or "")
        if not q:
            return ExecutionResult(False, "не понял, что искать", intent.name, needs_agent=True,
                                   error="нет запроса")
        url = search_url(q, "youtube")
        r = await self._tool(ctx, "search_youtube", {"query": q, "open": True})
        if not r.ok:
            r = await self._tool(ctx, "open_url", {"url": url})
        return ExecutionResult(r.ok, r.output or r.error or f"Открыл YouTube: {url}", intent.name,
                               error=r.error, data={"url": url, "query": q})

    # ================================================================== файлы
    async def _do_create_folder(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        r = await self._tool(ctx, "fs_mkdir", {"path": path})
        if r.ok:
            ok, detail = await self._verify_path(path, "exists")
            if not ok:
                return ExecutionResult(False, f"Папка не появилась: {path}", intent.name,
                                       error=detail, needs_agent=True)
            return ExecutionResult(True, f"Создал папку: {Path(path).name} ({path})", intent.name,
                                   data={"path": path, "verified": True})
        return ExecutionResult(False, r.error, intent.name, error=r.error, data={"path": path})

    async def _do_create_file(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        content = str(intent.slots.get("content") or "")
        r = await self._tool(ctx, "fs_write", {"path": path, "content": content})
        if r.ok:
            ok, detail = await self._verify_path(path, "exists")
            return ExecutionResult(ok, f"Создал файл: {Path(path).name}" +
                                   (f" ({len(content)} символов)" if content else ""),
                                   intent.name, error="" if ok else detail,
                                   data={"path": path, "verified": ok})
        return ExecutionResult(False, r.error, intent.name, error=r.error)

    async def _do_write_file(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        content = str(intent.slots.get("content") or "")
        mode_slot = str(intent.slots.get("mode") or "w")
        tool = "fs_append" if mode_slot == "a" else "fs_write"
        r = await self._tool(ctx, tool, {"path": path, "content": content})
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"path": path})

    async def _do_read_file(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        r = await self._tool(ctx, "fs_read", {"path": path, "max_chars": 4000})
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"path": path, "text": r.output})

    async def _do_list_dir(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        r = await self._tool(ctx, "fs_list", {"path": path})
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"path": path})

    async def _do_find_files(self, intent, ctx, control, mode) -> ExecutionResult:
        pattern = str(intent.slots.get("pattern") or intent.slots.get("target") or "")
        place = str(intent.slots.get("place") or "")
        root = ""
        if place:
            from ..system import paths as path_tools
            known = path_tools.place_dir(place)
            root = str(known) if known else ""
        r = await self._tool(ctx, "fs_search", {"root": root, "pattern": f"*{pattern}*",
                                                "max_results": 25})
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data={"pattern": pattern, "root": root})

    async def _do_delete_path(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        target = str(intent.slots.get("target") or path)
        p = Path(path) if path else None
        if not p or not p.exists():
            resolved, place = _resolve_with_place(path, intent)
            if resolved is not None:
                p, path = resolved, str(resolved)
        if p is None:
            return ExecutionResult(False, "не понял, что удалять", intent.name,
                                   error="путь не определён", needs_agent=True)
        if not p.exists():
            return ExecutionResult(False, f"Не нашёл: {path}", intent.name,
                                   error="объект не существует", needs_agent=True,
                                   data={"path": path})
        is_dir = p.is_dir()
        approx = 0
        if is_dir:
            try:
                approx = sum(1 for _ in p.rglob("*")) + 1
            except OSError:
                approx = 0
        desc = (f"Удалить {'папку' if is_dir else 'файл'} «{p.name}»"
                f" ({path})" + (f", внутри ~{approx} объектов" if approx else ""))
        approved = await self._confirm(ctx, "fs_delete",
                                       {"path": path, "recursive": is_dir}, desc, mode)
        if not approved:
            return ExecutionResult(False, "Удаление отменено пользователем.", intent.name,
                                   error="отменено", data={"path": path})
        r = await self._tool(ctx, "fs_delete", {"path": path, "recursive": is_dir},
                             fallbacks=[("delete_folder", {"path": path, "recursive": True,
                                                           "permanent": False})])
        if not r.ok:
            return ExecutionResult(False, r.error, intent.name, error=r.error, data={"path": path})
        ok, detail = await self._verify_path(path, "gone")
        if not ok:
            return ExecutionResult(False, f"Не удалось удалить: {path}", intent.name,
                                   error=detail, needs_agent=True, data={"path": path})
        return ExecutionResult(True, f"Удалил {'папку' if is_dir else 'файл'}: {p.name}",
                               intent.name, data={"path": path, "verified": True, "items": approx})

    async def _do_move_path(self, intent, ctx, control, mode) -> ExecutionResult:
        return await self._transfer(intent, ctx, "fs_move", "Переместил")

    async def _do_copy_path(self, intent, ctx, control, mode) -> ExecutionResult:
        return await self._transfer(intent, ctx, "fs_copy", "Скопировал")

    async def _transfer(self, intent, ctx, tool: str, verb: str) -> ExecutionResult:
        src = str(intent.slots.get("src_path") or "")
        dst = str(intent.slots.get("dst_path") or "")
        if not src or not dst:
            return ExecutionResult(False, "не понял источник или назначение", intent.name,
                                   error="нужны оба пути", needs_agent=True)
        r = await self._tool(ctx, tool, {"src": src, "dst": dst})
        if not r.ok:
            return ExecutionResult(False, r.error, intent.name, error=r.error,
                                   data={"src": src, "dst": dst})
        ok, detail = await self._verify_path(dst, "exists")
        name = Path(src).name
        return ExecutionResult(ok, f"{verb}: {name} → {Path(dst).parent}",
                               intent.name, error="" if ok else detail,
                               data={"src": src, "dst": dst, "verified": ok})

    async def _do_rename_path(self, intent, ctx, control, mode) -> ExecutionResult:
        return await self._transfer(intent, ctx, "fs_move", "Переименовал в")

    # ================================================================== система
    async def _do_system_power(self, intent, ctx, control, mode) -> ExecutionResult:
        action = str(intent.slots.get("action") or "")
        labels = {"shutdown": "Выключить компьютер", "restart": "Перезагрузить компьютер",
                  "sleep": "Перевести в спящий режим", "hibernate": "Гибернация",
                  "lock": "Заблокировать экран", "logoff": "Завершить сеанс",
                  "monitor-off": "Выключить монитор"}
        desc = labels.get(action, f"Системное действие «{action}»")
        approved = await self._confirm(ctx, "system_power", {"action": action}, desc, mode)
        if not approved:
            return ExecutionResult(False, "Действие отменено пользователем.", intent.name,
                                   error="отменено")
        r = await self._tool(ctx, "system_power", {"action": action}, timeout=15)
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error)

    async def _do_volume(self, intent, ctx, control, mode) -> ExecutionResult:
        action = str(intent.slots.get("action") or "get")
        value = intent.slots.get("value")
        if action == "set" and value is not None:
            name, args = "os_settings", {"area": "volume", "action": "set", "value": str(value)}
        elif action in ("mute", "unmute"):
            name, args = "os_settings", {"area": "volume", "action": "mute" if action == "mute" else "unmute"}
        elif action == "up":
            name, args = "keyboard_hotkey", {"keys": "volumeup"}
        elif action == "down":
            name, args = "keyboard_hotkey", {"keys": "volumedown"}
        else:
            name, args = "os_settings", {"area": "volume", "action": "get"}
        r = await self._tool(ctx, name, args, fallbacks=[("os_settings",
                                                          {"area": "volume", "action": "get"})])
        text = r.output or r.error
        if action == "set" and value is None:
            text = "Не понял уровень громкости."
        return ExecutionResult(r.ok, text, intent.name, error=r.error)

    async def _do_show_desktop(self, intent, ctx, control, mode) -> ExecutionResult:
        r = await self._tool(ctx, "show_desktop", {})
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error)

    async def _do_screenshot(self, intent, ctx, control, mode) -> ExecutionResult:
        r = await self._tool(ctx, "take_screenshot", {}, fallbacks=[("screen_capture", {})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data=dict(r.data or {}))

    async def _do_set_wallpaper(self, intent, ctx, control, mode) -> ExecutionResult:
        path = str(intent.slots.get("path") or "")
        query = str(intent.slots.get("query") or "")
        r = await self._tool(ctx, "set_wallpaper", {"path": path, "query": query})
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data=dict(r.data or {}))

    async def _do_kill_process(self, intent, ctx, control, mode) -> ExecutionResult:
        name = str(intent.slots.get("app_display") or intent.slots.get("name") or "")
        key = str(intent.slots.get("app_key") or "")
        proc_names = list(intent.slots.get("proc_names") or [])
        args = {"force": False}
        if proc_names:
            args["name"] = proc_names[0]
        elif name:
            args["name"] = name
        desc = f"Завершить процесс «{name or key}»"
        approved = await self._confirm(ctx, "process_kill", args, desc, mode)
        if not approved:
            return ExecutionResult(False, "Завершение отменено пользователем.", intent.name,
                                   error="отменено")
        r = await self._tool(ctx, "process_kill", args,
                             fallbacks=[("kill_process", {**args}),
                                        ("kill_app", {"name": key or name, "force": True})])
        if r.ok and self.wait is not None and proc_names:
            try:
                res = await self.wait.wait_process_finished(proc_names[0], timeout=3.0)
                return ExecutionResult(res.ok, f"Закрыл: {name or key}",
                                       intent.name, error="" if res.ok else "процесс ещё жив",
                                       data={"verified": res.ok})
            except Exception:
                pass
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error)

    async def _do_run_command(self, intent, ctx, control, mode) -> ExecutionResult:
        command = str(intent.slots.get("command") or "")
        if not command:
            return ExecutionResult(False, "пустая команда", intent.name, error="нет команды",
                                   needs_agent=True)
        approved = await self._confirm(ctx, "execute_command", {"command": command},
                                       f"Выполнить команду: {command}", mode)
        if not approved:
            return ExecutionResult(False, "Команда отменена пользователем.", intent.name,
                                   error="отменено")
        r = await self._tool(ctx, "execute_command", {"command": command, "timeout": 60},
                             fallbacks=[("terminal_run", {"command": command})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data=dict(r.data or {}))

    # ================================================================== ввод
    async def _do_send_keys(self, intent, ctx, control, mode) -> ExecutionResult:
        keys = str(intent.slots.get("keys") or "")
        if not keys:
            return ExecutionResult(False, "не понял сочетание клавиш", intent.name,
                                   error="нет клавиш", needs_agent=True)
        if self.inputs is not None:
            res = (await self.inputs.hotkey(keys) if "+" in keys
                   else await self.inputs.press_key(keys))
            if getattr(res, "ok", False):
                return ExecutionResult(True, f"Нажал {keys}", intent.name,
                                       data={"method": "input_controller"})
        r = await self._tool(ctx, "keyboard_hotkey", {"keys": keys},
                             fallbacks=[("press_key", {"key": keys})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error)

    async def _do_type_text(self, intent, ctx, control, mode) -> ExecutionResult:
        text = str(intent.slots.get("text") or "")
        if not text:
            return ExecutionResult(False, "нечего печатать", intent.name, error="пустой текст",
                                   needs_agent=True)
        if self.inputs is not None:
            res = await self.inputs.type_text(text)
            if getattr(res, "ok", False):
                return ExecutionResult(True, getattr(res, "message", "Текст введён"), intent.name,
                                       data={"chars": len(text), "method": "input_controller"})
        r = await self._tool(ctx, "keyboard_type", {"text": text},
                             fallbacks=[("type_text", {"text": text})])
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error)

    async def _do_click_element(self, intent, ctx, control, mode) -> ExecutionResult:
        desc = str(intent.slots.get("description") or "")
        if self.vision is None or self.inputs is None:
            return ExecutionResult(False, "для поиска элемента на экране нужно зрение", intent.name,
                                   error="зрение недоступно", needs_agent=True)
        r = await self._tool(ctx, "click_element", {"target": desc}, timeout=60)
        return ExecutionResult(r.ok, r.output or r.error, intent.name, error=r.error,
                               data=dict(r.data or {}))

    async def _do_code_task(self, intent, ctx, control, mode) -> ExecutionResult:
        # Содержимое кода генерирует модель — но ровно один вызов, без агентного цикла.
        return ExecutionResult(True, "Нужно сгенерировать код.", intent.name,
                               needs_llm_text=_code_prompt(intent),
                               data={"what": intent.slots.get("what"),
                                     "language": intent.slots.get("language")})

    # ================================================================== составные
    async def _compound(self, intent, ctx, control, mode) -> ExecutionResult:
        results: list[ExecutionResult] = []
        for part in intent.parts:
            if control is not None and getattr(control, "mode", "") == "stop":
                results.append(ExecutionResult(False, "остановлено пользователем",
                                               getattr(part, "name", ""), error="отменено"))
                break
            # части составной команды независимы: не прерываем цепочку из-за
            # одной ошибки, а честно собираем результат по всем (ТЗ §24)
            results.append(await self.execute(part, ctx, control=control, mode=mode))
        needs_llm = next((r.needs_llm_text for r in results if r.needs_llm_text), "")
        # Если в цепочке есть шаг, требующий генерации (код/текст), «успехом» это
        # не считается: без модели задача не выполнена — её берёт на себя LLM-ветка.
        ok = all(r.ok for r in results) and not needs_llm
        texts = [r.text for r in results if r.text]
        err = next((r.error for r in results if r.error), "")
        needs_agent = any(r.needs_agent for r in results) or bool(needs_llm)
        return ExecutionResult(ok, "\n".join(texts), "compound",
                               actions=[r.to_dict() for r in results], error=err,
                               needs_agent=needs_agent, needs_llm_text=needs_llm)

    # ------------------------------------------------------------------ предпросмотр
    def preview(self, intent: Any) -> list[str]:
        """Что будет сделано (для PLAN ONLY), без выполнения."""
        name = getattr(intent, "name", "")
        slots = getattr(intent, "slots", {}) or {}
        if name == "compound":
            out: list[str] = []
            for part in getattr(intent, "parts", []):
                out.extend(self.preview(part))
            return out
        label = intent.label() if hasattr(intent, "label") else name
        details: list[str] = []
        if name == "launch_app":
            details.append(f"приложение: {slots.get('app_display') or slots.get('target')}")
        elif name in ("open_url", "web_search", "youtube_search"):
            details.append(f"ссылка: {slots.get('url') or slots.get('query')}")
        elif name in ("create_folder", "create_file", "delete_path", "move_path",
                      "copy_path", "rename_path", "read_file", "find_files"):
            details.append(f"объект: {slots.get('path') or slots.get('pattern') or slots.get('target')}")
        elif name == "system_power":
            details.append(f"действие: {slots.get('action')}")
        elif name == "run_command":
            details.append(f"команда: {slots.get('command')}")
        elif name == "kill_process":
            details.append(f"процесс: {slots.get('app_display') or slots.get('name')}")
        elif name in ("send_keys", "type_text"):
            details.append(f"{slots.get('keys') or slots.get('text')}")
        if details:
            return [f"{label} — {', '.join(d for d in details if d)}"]
        return [label]


def _resolve_with_place(path: str, intent: Any) -> tuple[Path | None, str]:
    """«папка 123 с рабочего стола» → ~/Desktop/123 (с нечётким поиском)."""
    from ..system import paths as path_tools
    place = str(intent.slots.get("place") or "") or None
    base = path_tools.place_dir(place) if place else None
    resolved, place_label = path_tools.resolve_path(path, base=base, default_place=place)
    return (resolved, place_label) if resolved is not None else (None, "")


def _code_prompt(intent: Any) -> str:
    what = str(intent.slots.get("what") or "программу")
    lang = str(intent.slots.get("language") or "python")
    return (f"Напиши {what} на {lang}. Верни только код, без пояснений и без markdown-"
            f"обрамления, чтобы его можно было сразу сохранить в файл.")


__all__ = ["FastExecutor", "ExecutionResult"]
