"""Запуск приложений: один вход, восемь способов, автоматический fallback.

Порядок способов (ТЗ §9, §29, §51) — от самого быстрого и надёжного к
самому медленному:

    1. cached_executable   — путь, уже найденный и сохранённый ранее
    2. registered_app      — реестр приложений / shell-цель (ms-settings:, папки)
    3. shortcut            — ярлык из меню «Пуск» (.lnk) / .desktop на Linux
    4. protocol            — URI-схема (tg:, discord:, steam://, vscode://)
    5. shell               — Windows Shell / xdg-open / open (App Paths)
    6. powershell          — Start-Process (когда Shell не справился)
    7. start_menu          — поиск в меню «Пуск», Store-приложения (AppsFolder)
    8. ui_automation       — клавиша Win → имя → Enter (крайний случай)

Каждая попытка измеряется (мс + успех/неудача). Успешные пути запоминаются в
реестре (`record.launch_method`), неудачные — деградируют в приоритете, так что
со временем приложение открывается первым же способом (ТЗ §28, §45).

Проверка результата обязательна: после запуска агент ждёт появления процесса
или окна (agent/system/wait.py), а не «спит 3 секунды».
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import catalog
from .registry import AppRecord, AppRegistry, expand, platform_key, command_of

# Базовый приоритет методов (меньше = пробуем раньше).
METHOD_RANK: dict[str, int] = {
    "cached_executable": 1,
    "registered_app": 2,
    "shortcut": 3,
    "protocol": 4,
    "shell": 5,
    "powershell": 6,
    "start_menu": 7,
    "ui_automation": 8,
}

METHOD_TITLES = {
    "cached_executable": "сохранённый путь",
    "registered_app": "реестр приложений",
    "shortcut": "ярлык",
    "protocol": "протокол",
    "shell": "командная оболочка",
    "powershell": "PowerShell",
    "start_menu": "меню «Пуск»",
    "ui_automation": "управление интерфейсом",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


@dataclass
class LaunchAttempt:
    method: str
    ok: bool
    ms: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {"method": self.method, "ok": self.ok, "ms": round(self.ms, 1),
                "detail": self.detail[:200]}


@dataclass
class LaunchResult:
    ok: bool
    key: str = ""
    display_name: str = ""
    method: str = ""
    ms: float = 0.0
    pid: int | None = None
    message: str = ""
    error: str = ""
    attempts: list[LaunchAttempt] = field(default_factory=list)
    verified: bool = False

    @property
    def fallbacks(self) -> int:
        return max(0, len(self.attempts) - 1)


# --------------------------------------------------------------------------
#  Абстракция ОС (легко подменяется в тестах)
# --------------------------------------------------------------------------
class OSLaunchEnv:
    """Реальные системные вызовы запуска."""

    # Можно ли подтвердить запуск наблюдением (процесс/окно). Тестовые
    # окружения ставят False — иначе проверка ловила бы реальную систему.
    can_verify: bool = True

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or platform_key()

    # --- примитивы ---
    def spawn_detached(self, argv: list[str], cwd: str | None = None) -> tuple[bool, str, int | None]:
        try:
            kw: dict[str, Any] = {"cwd": cwd or None,
                                  "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL}
            if self.platform == "windows":
                kw["creationflags"] = _DETACHED | _NEW_GROUP
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen(argv, **kw)  # noqa: S603 — argv формируется нами
            return True, "", p.pid
        except FileNotFoundError:
            return False, f"не найдено: {argv[0]}", None
        except OSError as e:
            return False, f"{type(e).__name__}: {e}", None

    def startfile(self, path: str) -> tuple[bool, str, int | None]:
        if self.platform == "windows":
            try:
                os.startfile(path)  # type: ignore[attr-defined]
                return True, "", None
            except OSError as e:
                return False, str(e), None
        if self.platform == "macos":
            return self.spawn_detached(["open", path])
        return self.spawn_detached(["xdg-open", path])

    def shell_start(self, target: str, args: str = "") -> tuple[bool, str, int | None]:
        """Windows Shell: cmd /c start "" <target> (ищет в App Paths)."""
        if self.platform == "windows":
            cmd = f'start "" "{target}"{(" " + args) if args else ""}'
            try:
                r = subprocess.run(["cmd", "/c", cmd], capture_output=True, text=True,
                                   timeout=15, creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    return True, "", None
                return False, (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200], None
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, str(e)[:200], None
        opener = "open" if self.platform == "macos" else "xdg-open"
        if shutil.which(opener):
            return self.spawn_detached([opener, target])
        return False, f"{opener} недоступен", None

    def powershell_start(self, target: str, args: list[str] | None = None) -> tuple[bool, str, int | None]:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return False, "PowerShell не найден", None
        arg_list = " ".join(f"'{a}'" for a in (args or []))
        script = (f"Start-Process -FilePath '{target}'"
                  + (f" -ArgumentList {arg_list}" if arg_list else ""))
        try:
            r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                return True, "", None
            return False, (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200], None
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)[:200], None

    def open_app_bundle(self, name: str) -> tuple[bool, str, int | None]:
        return self.spawn_detached(["open", "-a", name])

    def open_uri(self, uri: str) -> tuple[bool, str, int | None]:
        if self.platform == "windows":
            try:
                r = subprocess.run(["cmd", "/c", "start", "", uri], capture_output=True,
                                   text=True, timeout=15, creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    return True, "", None
                return False, (r.stderr or "").strip()[:200], None
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, str(e)[:200], None
        if self.platform == "macos":
            return self.spawn_detached(["open", uri])
        return self.spawn_detached(["xdg-open", uri])

    def apps_folder(self, appid: str) -> tuple[bool, str, int | None]:
        """Store/UWP: explorer.exe shell:AppsFolder\\<AppID>."""
        if self.platform != "windows":
            return False, "AppsFolder доступен только в Windows", None
        try:
            r = subprocess.run(["explorer.exe", f"shell:AppsFolder\\{appid}"],
                               capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
            return True, "", None
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)[:200], None

    def desktop_launch(self, desktop_file: str) -> tuple[bool, str, int | None]:
        if shutil.which("gtk-launch"):
            return self.spawn_detached(["gtk-launch", Path(desktop_file).stem])
        if shutil.which("gio"):
            return self.spawn_detached(["gio", "launch", desktop_file])
        return self.startfile(desktop_file)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def exists(self, path: str) -> bool:
        return bool(path) and (os.path.exists(expand(path)) or os.path.exists(command_of(path)))


class FakeLaunchEnv(OSLaunchEnv):
    """Тестовое окружение: ничего не запускает, всё записывает.

    Используется юнит-тестами fallback-цепочки и «сухого прогона» в UI.
    """

    can_verify = False

    def __init__(self, platform: str = "windows", fail: set[str] | None = None,
                 existing: set[str] | None = None, which: set[str] | None = None,
                 can_verify: bool | None = None) -> None:
        super().__init__(platform)
        if can_verify is not None:
            self.can_verify = bool(can_verify)
        self.calls: list[tuple[str, str]] = []
        self.fail = {m.lower() for m in (fail or set())}
        self._existing = {p.lower() for p in (existing or set())}
        self._which = {w.lower() for w in (which or set())}

    def _rec(self, kind: str, target: str) -> tuple[bool, str, int | None]:
        self.calls.append((kind, target))
        if kind.lower() in self.fail or any(f in target.lower() for f in self.fail):
            return False, f"имитация отказа: {kind}", None
        return True, "", 4242

    def spawn_detached(self, argv, cwd=None):
        return self._rec(argv[0], " ".join(argv))

    def startfile(self, path):
        return self._rec("startfile", path)

    def shell_start(self, target, args=""):
        return self._rec("shell", target)

    def powershell_start(self, target, args=None):
        return self._rec("powershell", target)

    def open_app_bundle(self, name):
        return self._rec("open_app", name)

    def open_uri(self, uri):
        return self._rec("protocol", uri)

    def apps_folder(self, appid):
        return self._rec("start_menu", appid)

    def desktop_launch(self, desktop_file):
        return self._rec("desktop", desktop_file)

    def which(self, name):
        return f"/fake/{name}" if name.lower() in self._which else None

    def exists(self, path):
        return bool(path) and path.lower() in self._existing


# --------------------------------------------------------------------------
#  Launcher
# --------------------------------------------------------------------------
class AppLauncher:
    def __init__(self, registry: AppRegistry, env: OSLaunchEnv | None = None,
                 metrics: Any = None, log: Any = None, optimizer: Any = None,
                 wait: Any = None, state: Any = None, inputs: Any = None,
                 cfg: Any = None) -> None:
        self.registry = registry
        self.env = env or OSLaunchEnv()
        self.metrics = metrics
        self.log = log
        self.optimizer = optimizer
        self.wait = wait
        self.state = state
        self.inputs = inputs
        self.cfg = cfg
        self._resolved_path = ""    # путь, подтверждённый окружением в текущем плане

    def _confirmed_path(self, rec: AppRecord, plat: str) -> str:
        """Путь, который реально существует — с учётом окружения запуска.

        `AppRecord.confirmed_path` проверяет файловую систему напрямую; в тестах
        (и при «сухом прогоне» под другой ОС) проверку делает env.exists().
        """
        from .registry import command_of
        p = (rec.paths or {}).get(plat) or ""
        if not p:
            return ""
        if p.startswith(("shell:", "ms-", "http:", "https:")) or p.endswith(":") \
                or "*" in p or "?" in p:
            return p
        try:
            if self.env.exists(command_of(p)):
                return p
        except Exception:
            pass
        return ""

    def _plat(self) -> str:
        """Платформа, для которой работает окружение запуска (важно для тестов)."""
        return str(getattr(self.env, "platform", "") or platform_key())

    # ------------------------------------------------------------ план
    def build_plan(self, rec: AppRecord, target: str = "",
                   allow_ui: bool = False) -> list[str]:
        """Список методов, применимых к этому приложению (в базовом порядке)."""
        plat = self._plat()
        methods: list[str] = []
        self._resolved_path = self._confirmed_path(rec, plat)
        if self._resolved_path or rec.confirmed_path(plat):
            methods.append("cached_executable")
        if rec.shell or rec.protocols or rec.kind in ("settings", "folder", "shell"):
            methods.append("registered_app")
        if rec.shortcuts:
            methods.append("shortcut")
        if rec.protocols:
            methods.append("protocol")
        if plat == "windows":
            methods.append("shell")
            methods.append("powershell")
            if rec.appids:
                methods.append("start_menu")
        elif plat == "linux":
            if rec.extra.get("linux_desktop"):
                methods.insert(0, "shortcut")
            methods.append("shell")
        else:
            methods.append("shell")
            if rec.extra.get("mac_app"):
                methods.insert(0, "registered_app")
        if rec.appids and "start_menu" not in methods:
            methods.append("start_menu")
        if allow_ui and plat == "windows":
            methods.append("ui_automation")
        # адаптивный порядок: успешный в прошлом метод идёт первым
        if self.optimizer is not None:
            try:
                methods = self.optimizer.rank_methods("launch_app", methods)
            except Exception:
                pass
        elif rec.launch_method in methods and rec.methods_tried.get(rec.launch_method, 0) >= 1 \
                and rec.fail_count == 0:
            methods.remove(rec.launch_method)
            methods.insert(0, rec.launch_method)
        return methods

    # ------------------------------------------------------------ запуск
    async def launch(self, name: str, target: str = "", rec: AppRecord | None = None,
                     allow_ui: bool | None = None, verify: bool = True,
                     timeout: float = 12.0) -> LaunchResult:
        t_start = time.perf_counter()
        lookup = None
        if rec is None:
            lookup = self.registry.find(name)
            rec = lookup.record
        if rec is None:
            # совсем неизвестное приложение — пробуем как команду/путь
            res = await self._launch_unknown(name, target, timeout=timeout)
            res.ms = (time.perf_counter() - t_start) * 1000
            return res
        if allow_ui is None:
            allow_ui = bool(getattr(self.cfg, "allow_ui_fallback", False)) or self._ui_allowed()
        plan = self.build_plan(rec, target, allow_ui=allow_ui)
        attempts: list[LaunchAttempt] = []
        last_error = ""
        for method in plan:
            t0 = time.perf_counter()
            ok, detail, pid = await self._attempt(method, rec, target)
            ms = (time.perf_counter() - t0) * 1000
            verified = False
            if ok and verify and self._verify_possible(rec) \
                    and getattr(self.env, "can_verify", True):
                verified = await self._verify(rec, method, timeout=timeout)
                if not verified:
                    ok, detail = False, detail or "запуск не подтвердился (процесс/окно не найдены)"
            attempts.append(LaunchAttempt(method, ok, ms, detail))
            self._record(rec, method, ok, ms, name, target, detail)
            if ok:
                total = (time.perf_counter() - t_start) * 1000
                rec.record_result(True, total, method)
                self.registry.save()
                self._learn(rec, name)
                return LaunchResult(True, rec.key, rec.display_name, method,
                                    ms=round(total, 1), pid=pid,
                                    message=f"{rec.display_name}: запущено ({METHOD_TITLES.get(method, method)})"
                                            + (f", проверено" if verified else ""),
                                    attempts=attempts, verified=verified)
            last_error = detail
        total = (time.perf_counter() - t_start) * 1000
        rec.record_result(False, total, "")
        self.registry.save()
        msg = (f"не удалось запустить «{rec.display_name}». "
               f"Пробовали: {', '.join(METHOD_TITLES.get(m, m) for m in plan)}. "
               f"Последняя ошибка: {last_error or 'неизвестно'}")
        if self.log:
            self.log.warn(f"Запуск не удался: {rec.display_name}", attempts=[a.to_dict() for a in attempts])
        return LaunchResult(False, rec.key, rec.display_name, ms=round(total, 1),
                            error=msg, message=msg, attempts=attempts)

    # ------------------------------------------------------------ методы
    async def _attempt(self, method: str, rec: AppRecord, target: str) -> tuple[bool, str, int | None]:
        fn = getattr(self, f"_do_{method}", None)
        if fn is None:
            return False, f"метод {method} не поддерживается", None
        if self.log:
            self.log.event("launch_try", tool="launch_app", method=method, app=rec.key)
        try:
            res = fn(rec, target)
            if asyncio.iscoroutine(res):
                res = await res
            return res
        except Exception as e:  # noqa: BLE001 — метод не должен ронять агента
            return False, f"{type(e).__name__}: {e}", None

    def _do_cached_executable(self, rec: AppRecord, target: str):
        path = rec.confirmed_path() or getattr(self, "_resolved_path", "")
        if not path:
            return False, "сохранённый путь устарел", None
        args = list(rec.args) + ([target] if target else [])
        if path.startswith("shell:") or path.endswith(":"):
            return self.env.shell_start(path, " ".join(args))
        if path.lower().endswith((".lnk", ".desktop")) or os.path.isdir(expand(path)):
            return self.env.startfile(expand(path))
        if " --" in path:
            head, tail = path.split(" --", 1)
            argv = [expand(head)] + tail.split() + args
            return self.env.spawn_detached(argv)
        return self.env.spawn_detached([expand(path), *args])

    def _do_registered_app(self, rec: AppRecord, target: str):
        plat = self._plat()
        if rec.kind in ("settings", "folder", "shell") and rec.shell:
            if plat == "windows":
                if rec.shell.startswith("shell:") or rec.shell.endswith(":"):
                    return self.env.shell_start(rec.shell)
                return self.env.spawn_detached(["explorer.exe", rec.shell])
            # Linux/macOS: системная папка через xdg-open/open
            p = rec.confirmed_path(plat) or ""
            if p:
                return self.env.startfile(expand(p))
            return False, "нет цели для открытия", None
        if rec.extra.get("mac_app") and plat == "macos":
            return self.env.open_app_bundle(str(rec.extra["mac_app"]))
        if rec.protocols:
            proto = rec.protocols[0]
            uri = target if target and "://" in target else f"{proto}:"
            return self.env.open_uri(uri)
        if self.cfg is not None:
            apps = getattr(self.cfg, "apps", {}) or {}
            cmd = apps.get(rec.key)
            if isinstance(cmd, dict):
                cmd = cmd.get(plat)
            if cmd:
                return self.env.shell_start(str(cmd)) if plat == "windows" \
                    else self.env.spawn_detached(str(cmd).split())
        return False, "нет зарегистрированного способа", None

    def _do_shortcut(self, rec: AppRecord, target: str):
        plat = self._plat()
        if rec.shortcuts:
            for s in rec.shortcuts:
                if not os.path.exists(expand(s)):
                    continue
                if str(s).endswith(".desktop") and plat == "linux":
                    return self.env.desktop_launch(str(s))
                return self.env.startfile(expand(s))
        if plat == "linux":
            for name in (rec.extra.get("linux_desktop") or []):
                for d in ("/usr/share/applications", "/usr/local/share/applications",
                          os.path.expanduser("~/.local/share/applications")):
                    p = os.path.join(d, name)
                    if os.path.exists(p):
                        return self.env.desktop_launch(p)
        if plat == "windows":
            for menu_name in (rec.display_name, *(rec.extra.get("start_menu") or []), rec.key):
                lnk = self._find_start_menu_shortcut(menu_name)
                if lnk:
                    if lnk not in rec.shortcuts:
                        rec.shortcuts.append(lnk)
                    return self.env.startfile(lnk)
        return False, "ярлык не найден", None

    def _find_start_menu_shortcut(self, name: str) -> str | None:
        from .aliases import similarity
        roots = [os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
                 os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")]
        best: tuple[str, float] = ("", 0.0)
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                for lnk in Path(root).rglob("*.lnk"):
                    r = similarity(lnk.stem, name)
                    if r > best[1]:
                        best = (str(lnk), r)
            except OSError:
                continue
        return best[0] if best[1] >= 0.7 else None

    def _do_protocol(self, rec: AppRecord, target: str):
        if not rec.protocols:
            return False, "протокол не зарегистрирован", None
        uri = target or f"{rec.protocols[0]}:"
        if uri and "://" not in uri and not uri.endswith(":"):
            uri = f"{rec.protocols[0]}://{uri}"
        return self.env.open_uri(uri)

    def _do_shell(self, rec: AppRecord, target: str):
        plat = self._plat()
        if plat == "windows":
            if rec.shell:
                return self.env.shell_start(rec.shell)
            for cand in (rec.exe or [rec.key]):
                res = self.env.shell_start(cand, f'"{target}"' if target else "")
                if res[0]:
                    return res
            return self.env.shell_start(rec.display_name, f'"{target}"' if target else "")
        if rec.kind == "folder":
            p = rec.confirmed_path(plat) or ""
            if p:
                return self.env.startfile(expand(p))
        for b in (rec.extra.get("linux_bins") or []) + (rec.extra.get("mac_bins") or []):
            exe = self.env.which(b)
            if exe:
                return self.env.spawn_detached([exe, *([target] if target else [])])
        return False, "shell-команда недоступна", None

    def _do_powershell(self, rec: AppRecord, target: str):
        args = list(rec.args) + ([target] if target else [])
        candidates = ([rec.confirmed_path()] if rec.confirmed_path() else []) + \
            [expand(p) for p in rec.path_candidates] + list(rec.exe)
        for cand in candidates:
            if not cand:
                continue
            if cand.lower().endswith((".exe", ".com")) and not self.env.exists(cand):
                continue
            ok = self.env.powershell_start(cand, args)
            if ok[0]:
                return ok
        # последняя попытка — по имени приложения
        return self.env.powershell_start(rec.display_name, args)

    def _do_start_menu(self, rec: AppRecord, target: str):
        if rec.appids:
            for appid in rec.appids:
                ok = self.env.apps_folder(appid)
                if ok[0]:
                    return ok
        if self._plat() == "windows":
            lnk = self._find_start_menu_shortcut(rec.display_name)
            if lnk:
                return self.env.startfile(lnk)
        return False, "приложение не найдено в меню «Пуск»", None

    async def _do_ui_automation(self, rec: AppRecord, target: str):
        """Win → поиск по имени → Enter. Крайний случай, когда ничего не помогло."""
        if self.inputs is None:
            return False, "нет контроллера ввода", None
        if self._plat() != "windows":
            return False, "UI-автоматизация реализована для Windows", None
        try:
            await self.inputs.hotkey("win")
            await asyncio.sleep(0.15)
            await self.inputs.type_text(rec.display_name, use_clipboard=False)
            await asyncio.sleep(0.35)
            await self.inputs.press_key("enter")
            return True, "запуск через поиск «Пуск»", None
        except Exception as e:  # noqa: BLE001
            return False, f"UI-автоматизация: {e}", None

    # ------------------------------------------------------------ неизвестное
    async def _launch_unknown(self, name: str, target: str, timeout: float) -> LaunchResult:
        raw = (name or "").strip().strip('"')
        attempts: list[LaunchAttempt] = []
        candidates: list[tuple[str, str]] = []
        if os.path.exists(expand(raw)):
            candidates.append(("cached_executable", expand(raw)))
        w = self.env.which(raw)
        if w:
            candidates.append(("shell", w))
        candidates.append(("shell", raw))
        if self._plat() == "windows":
            candidates.append(("powershell", raw))
        for method, cand in candidates:
            t0 = time.perf_counter()
            if method == "cached_executable":
                ok, detail, pid = self.env.startfile(cand)
            elif method == "powershell":
                ok, detail, pid = self.env.powershell_start(cand, [target] if target else [])
            else:
                ok, detail, pid = self.env.spawn_detached(
                    [cand, *([target] if target else [])]) if self.env.exists(cand) \
                    else self.env.shell_start(cand, target)
            ms = (time.perf_counter() - t0) * 1000
            attempts.append(LaunchAttempt(method, ok, ms, detail))
            if self.log:
                self.log.tool("launch_app", {"name": name, "method": method}, ms, ok,
                              method=method, error=detail if not ok else "")
            if ok:
                return LaunchResult(True, key="", display_name=raw, method=method,
                                    ms=round(ms, 1), pid=pid,
                                    message=f"Запущено: {raw} ({method})", attempts=attempts)
        msg = (f"не удалось запустить «{raw}» — программа не найдена. "
               f"Попробуйте уточнить название или полный путь.")
        return LaunchResult(False, display_name=raw, error=msg, message=msg, attempts=attempts)

    # ------------------------------------------------------------ проверка
    def _verify_possible(self, rec: AppRecord) -> bool:
        """Есть ли чем подтвердить запуск.

        Если менеджер ожиданий не подключён (минимальная сборка, тесты, «сухой
        прогон»), успешный вызов запуска считается успехом, но помечается как
        непроверенный — падать из-за отсутствия наблюдателя неправильно.
        """
        if self.wait is None:
            return False
        return bool(rec.proc_names() or rec.display_name
                    or rec.kind in ("settings", "folder", "shell"))

    async def _verify(self, rec: AppRecord, method: str, timeout: float = 12.0) -> bool:
        if self.wait is None:
            return False
        names = rec.proc_names()
        window_hint = rec.display_name
        try:
            if rec.kind in ("settings", "folder", "shell") or method in ("protocol", "shell", "start_menu",
                                                                        "ui_automation"):
                if rec.kind == "settings":
                    return await self.wait.wait_process_started(
                        ["SystemSettings.exe", "ApplicationFrameHost.exe", "explorer.exe"],
                        timeout=min(timeout, 6.0))
                if names:
                    ok = await self.wait.wait_process_started(names, timeout=min(timeout, 8.0))
                    if ok:
                        return True
                return await self.wait.wait_window_created(window_hint, timeout=min(timeout, 5.0))
            if names:
                ok = await self.wait.wait_process_started(names, timeout=min(timeout, 8.0))
                if ok:
                    return True
            return await self.wait.wait_window_created(window_hint, timeout=min(timeout, 4.0))
        except Exception:
            return False

    # ------------------------------------------------------------ служебное
    def _record(self, rec: AppRecord, method: str, ok: bool, ms: float,
                name: str, target: str, detail: str) -> None:
        if self.metrics is not None:
            self.metrics.method_call("launch_app", method, ms, ok)
            if not ok:
                self.metrics.fallback("launch_app", method)
        if self.log is not None:
            self.log.tool("launch_app", {"app": rec.key, "target": target or None},
                          ms, ok, method=method,
                          error="" if ok else detail, fallback=not ok)

    def _note(self, capability: str, method: str, ok: bool, ms: float, detail: str = "") -> None:
        """Запись попытки способа в метрики (для адаптивного выбора методов)."""
        if self.metrics is not None:
            try:
                self.metrics.method_call(capability, method, ms, ok)
                if not ok:
                    self.metrics.fallback(capability, method)
            except Exception:
                pass
        if self.log is not None and not ok:
            try:
                self.log.warn(f"{capability}: способ {method} не сработал", detail=detail[:160])
            except Exception:
                pass

    def _learn(self, rec: AppRecord, spoken: str) -> None:
        """Обучение алиасу (ТЗ §13 приложения к ТЗ): «телега» → telegram."""
        from .aliases import normalize
        phrase = normalize(spoken)
        if not phrase or len(phrase) < 3:
            return
        if phrase in (rec.key, normalize(rec.display_name)):
            return
        if self.registry.aliases.learn(phrase, rec.key, source="launch"):
            if self.log:
                self.log.info(f"Запомнено название: «{phrase}» → {rec.display_name}",
                              alias=phrase, app=rec.key)

    def _ui_allowed(self) -> bool:
        try:
            if self.cfg is not None:
                return bool(getattr(self.cfg.fast if hasattr(self.cfg, "fast") else self.cfg,
                                    "allow_ui_fallback", False))
        except Exception:
            pass
        return False

    async def open_url(self, ctx: Any = None, url: str = "") -> LaunchResult:
        """Открыть ссылку штатным браузером (без адресной строки и кликов)."""
        t0 = time.perf_counter()
        attempts: list[LaunchAttempt] = []
        order = ["native_shell", "spawn"]
        for method in order:
            t1 = time.perf_counter()
            if method == "native_shell":
                ok, detail, pid = self.env.open_uri(url)
            else:
                opened = False
                detail, pid = "", None
                for argv in (["xdg-open", url], ["open", url]):
                    if self.env.which(argv[0]):
                        ok2, detail, pid = self.env.spawn_detached(argv)
                        opened = ok2
                        break
                ok, opened = (False, opened) if not opened else (True, detail)
            ms = (time.perf_counter() - t1) * 1000
            attempts.append(LaunchAttempt(method, ok, ms, detail))
            self._note("open_url", method, ok, ms, detail)
            if ok:
                return LaunchResult(True, "browser", "браузер", method,
                                    ms=round((time.perf_counter() - t0) * 1000, 1), pid=pid,
                                    message=f"Открыл ссылку ({METHOD_TITLES.get(method, method)}): {url}",
                                    attempts=attempts)
        return LaunchResult(False, "browser", "браузер",
                            ms=round((time.perf_counter() - t0) * 1000, 1),
                            error="не удалось открыть ссылку", attempts=attempts)

    def dry_run(self, name: str, target: str = "") -> dict:
        """Что будет сделано, без запуска (полезно для UI и отладки)."""
        lookup = self.registry.find(name)
        if lookup.record is None:
            return {"ok": False, "name": name, "error": "приложение не найдено в реестре",
                    "suggestions": [{"key": k, "name": n, "score": s}
                                    for k, n, s in self.registry.suggest(name)]}
        rec = lookup.record
        plan = self.build_plan(rec, target, allow_ui=self._ui_allowed())
        return {"ok": True, "key": rec.key, "display_name": rec.display_name,
                "matched": lookup.matched, "confidence": round(lookup.score, 3),
                "plan": [{"method": m, "title": METHOD_TITLES.get(m, m),
                          "rank": METHOD_RANK.get(m, 99)} for m in plan],
                "confirmed_path": rec.confirmed_path(),
                "argv": (rec.exe or [])[:3],
                "stats": {"use_count": rec.use_count, "success_rate": rec.success_rate(),
                          "avg_ms": rec.avg_ms, "last_method": rec.launch_method}}
