"""WinAPI-ориентированные инструменты: питание, обои, рабочий стол, настройки,
терминал со структурным результатом, процессы, ожидания, состояние ПК, реестр
приложений и метрики (ТЗ §32, §33, §34, §37, §38, §40, §42, §43, §51, §52).

Принцип лестницы: сначала нативный WinAPI/шелл, потом PowerShell, и только
потом — интерфейс пользователя или зрение. Здесь собраны «нативные» шаги,
поэтому эти инструменты не требуют модели вообще: они и есть быстрый путь.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .base import Risk, Tool, ToolContext, ToolResult
from .terminal import assess_command


def _prop(t: str, desc: str) -> dict:
    return {"type": t, "description": desc}


def _is_windows() -> bool:
    return os.name == "nt"


async def _run_async(argv: list[str], timeout: float, cwd: str | None = None,
                     shell: bool = False, env: dict | None = None) -> dict:
    """Запуск процесса со структурным результатом: код, stdout, stderr, время."""
    t0 = time.perf_counter()
    kwargs = {"cwd": cwd or None, "env": {**os.environ, **(env or {})} if env else None}
    if shell:
        proc = await asyncio.create_subprocess_shell(" ".join(argv) if isinstance(argv, list) else argv,
                                                     stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, **kwargs)
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        out, err = await proc.communicate()
    ms = (time.perf_counter() - t0) * 1000
    return {"ok": proc.returncode == 0 and not timed_out,
            "exit_code": proc.returncode if proc.returncode is not None else -1,
            "stdout": (out or b"").decode("utf-8", "replace"),
            "stderr": (err or b"").decode("utf-8", "replace"),
            "ms": round(ms, 1), "timeout": timed_out}


def register_win_system_tools(reg) -> None:
    # ------------------------------------------------------------------ питание
    @reg.tool("system_power",
              "Питание и сессия: shutdown (выключить), restart, sleep, hibernate, "
              "lock (заблокировать), logoff, monitor_off. WinAPI/нативные команды, "
              "без имитации клавиш. Критичные действия требуют подтверждения.",
              risk=Risk.CRITICAL, category="os",
              parameters={"type": "object", "properties": {
                  "action": _prop("string", "shutdown | restart | sleep | hibernate | lock | logoff | monitor_off"),
                  "delay": _prop("integer", "Задержка в секундах (по умолчанию 0)"),
                  "force": _prop("boolean", "Принудительно закрыть программы")},
                  "required": ["action"]})
    class SystemPower(Tool):
        def estimate_risk(self, ctx, args):
            act = str(args.get("action", "")).lower()
            if act in ("lock", "monitor_off"):
                return Risk.LOW, "блокировка экрана"
            if act in ("sleep", "hibernate"):
                return Risk.HIGH, "переход в спящий режим"
            return Risk.CRITICAL, "выключение/перезагрузка компьютера"

        async def execute(self, ctx: ToolContext, action: str, delay: int = 0,
                          force: bool = False) -> ToolResult:
            act = (action or "").lower().strip()
            delay = max(0, int(delay or 0))
            try:
                if _is_windows():
                    return await self._windows(ctx, act, delay, force)
                return await self._unix(ctx, act, delay)
            except Exception as exc:                       # noqa: BLE001
                return ToolResult.fail(f"не удалось выполнить «{act}»: {exc}")

        async def _windows(self, ctx, act: str, delay: int, force: bool) -> ToolResult:
            import ctypes
            u = ctypes.windll.user32
            if act == "lock":
                ok = bool(u.LockWorkStation())
                return (ToolResult.ok_result("Экран заблокирован.", method="winapi_lock")
                        if ok else ToolResult.fail("LockWorkStation не сработал"))
            if act == "monitor_off":
                HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER = 0xFFFF, 0x0112, 0xF170
                u.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
                return ToolResult.ok_result("Монитор выключен.", method="winapi_monitor")
            if act == "sleep":
                code = subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                                      capture_output=True).returncode
                return ToolResult.ok_result("Ухожу в спящий режим.", method="winapi_sleep",
                                            exit_code=code)
            if act == "hibernate":
                subprocess.run(["shutdown", "/h"], capture_output=True)
                return ToolResult.ok_result("Ухожу в гибернацию.", method="shutdown_exe")
            flags = {  # noqa: C420
                "shutdown": ["/s"], "restart": ["/r"], "logoff": ["/l"],
            }.get(act)
            if not flags:
                return ToolResult.fail(f"неизвестное действие: {act}")
            t = 0 if act == "logoff" else delay
            argv = ["shutdown", *flags, "/t", str(t)]
            if force:
                argv.append("/f")
            code = subprocess.run(argv, capture_output=True, text=True)
            if code.returncode == 0:
                return ToolResult.ok_result(f"Команда «{act}» принята (задержка {t} с).",
                                            method="shutdown_exe", action=act, delay=t)
            return ToolResult.fail(code.stderr.strip() or f"shutdown вернул {code.returncode}")

        async def _unix(self, ctx, act: str, delay: int) -> ToolResult:
            if act == "lock":
                for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"],
                            ["gnome-screensaver-command", "-l"]):
                    if shutil.which(cmd[0]):
                        r = await _run_async(cmd, 5)
                        if r["ok"]:
                            return ToolResult.ok_result("Экран заблокирован.", method="loginctl", **r)
                return ToolResult.fail("нет доступного способа блокировки экрана")
            if act == "monitor_off":
                if shutil.which("xset"):
                    r = await _run_async(["xset", "dpms", "force", "off"], 5)
                    return ToolResult.ok_result("Монитор выключен.", method="xset", **r)
                return ToolResult.fail("xset недоступен")
            if act == "logoff" and shutil.which("loginctl"):
                return ToolResult.ok_result("Завершаю сеанс.", method="loginctl",
                                            **(await _run_async(["loginctl", "terminate-user",
                                                                 os.environ.get("USER", "")], 5)))
            if act == "sleep" and shutil.which("systemctl"):
                return ToolResult.ok_result("Ухожу в сон.", method="systemctl",
                                            **(await _run_async(["systemctl", "suspend"], 5)))
            if act == "restart" and shutil.which("systemctl"):
                return ToolResult.ok_result("Перезагружаю.", method="systemctl",
                                            **(await _run_async(["systemctl", "reboot"], 5)))
            if act == "shutdown" and shutil.which("systemctl"):
                argv = ["systemctl", "poweroff"]
                if delay:
                    argv = ["shutdown", "-h", f"+{delay // 60 or 1}"]
                return ToolResult.ok_result("Выключаю.", method="systemctl",
                                            **(await _run_async(argv, 5)))
            return ToolResult.fail(f"действие «{act}» не поддерживается на этой системе")

    # ------------------------------------------------------------------ обои
    @reg.tool("set_wallpaper",
              "Сменить обои рабочего стола. Windows — SystemParametersInfo (WinAPI), "
              "Linux — gsettings/dconf/feh, macOS — osascript. Без пути выбирается "
              "случайная картинка из «Изображений» (или с совпадением по названию).",
              risk=Risk.LOW, category="os",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь к картинке (необязательно)"),
                  "query": _prop("string", "Слово для выбора картинки по имени, напр. «кот»"),
                  "monitor_index": _prop("integer", "Монитор (для мультимониторных утилит)")},
                  "required": []})
    class SetWallpaper(Tool):
        SUPPORTED = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tiff"}

        async def execute(self, ctx: ToolContext, path: str = "", query: str = "",
                          monitor_index: int = 0) -> ToolResult:
            target = self._pick(ctx, path, query)
            if target is None:
                return ToolResult.fail("не нашёл подходящую картинку для обоев")
            if not Path(target).is_file():
                return ToolResult.fail(f"файл не найден: {target}")
            if _is_windows():
                return self._windows(target)
            return await self._unix(target)

        def _pick(self, ctx, path: str, query: str) -> str | None:
            if path:
                p = Path(os.path.expanduser(os.path.expandvars(path.strip().strip('"'))))
                if not p.is_absolute():
                    p = Path(ctx.workdir or ".") / p
                return str(p)
            dirs = []
            try:
                from ..system.paths import system_dirs
                d = system_dirs()
                for key in ("pictures",):
                    if isinstance(d.get(key), Path):
                        dirs.append(d[key])
            except Exception:
                dirs.append(Path.home() / "Pictures")
            if _is_windows():
                profile = Path(os.environ.get("USERPROFILE") or Path.home())
                dirs.append(profile / "Pictures" / "Wallpapers")
                dirs.append(Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Pictures")
            candidates: list[Path] = []
            for d in dirs:
                if not isinstance(d, Path) or not d.is_dir():
                    continue
                for p in d.rglob("*"):
                    if p.suffix.lower() in self.SUPPORTED and "screens" not in str(p).lower():
                        candidates.append(p)
            if not candidates:
                return None
            if query:
                q = query.strip().lower()
                hits = [c for c in candidates if q in c.name.lower()]
                if hits:
                    candidates = hits
            return str(random.choice(candidates))

        def _windows(self, target: str) -> ToolResult:
            try:
                import ctypes
            except Exception as exc:                       # noqa: BLE001
                return ToolResult.fail(f"ctypes недоступен: {exc}")
            SPI_SETDESKWALLPAPER = 0x0014
            SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 0x01, 0x02
            ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
                SPI_SETDESKWALLPAPER, 0, str(target),
                SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
            if ok:
                return ToolResult.ok_result(f"Обои изменены: {Path(target).name}",
                                            method="winapi_spi", path=str(target))
            return ToolResult.fail("SystemParametersInfoW вернул 0")

        async def _unix(self, target: str) -> ToolResult:
            uri = f"file://{Path(target).resolve()}"
            candidates = [
                ("gsettings", ["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri]),
                ("gsettings", ["gsettings", "set", "org.gnome.desktop.background",
                               "picture-uri-dark", uri]),
                ("feh", ["feh", "--bg-scale", str(target)]),
                ("xfconf-query", ["xfconf-query", "-c", "xfce4-desktop", "-p",
                                  "/backdrop/screen0/monitor0/workspace0/last-image", "-s", str(target)]),
            ]
            for name, argv in candidates:
                if not shutil.which(argv[0]):
                    continue
                r = await _run_async(argv, 8)
                if r["ok"]:
                    return ToolResult.ok_result(f"Обои изменены: {Path(target).name}",
                                                method=name, path=str(target), **r)
            return ToolResult.fail("не нашёл доступного способа сменить обои (gsettings/feh/xfconf)")

    @reg.tool("show_desktop", "Показать рабочий стол (свернуть все окна), аналог Win+D.",
              risk=Risk.LOW, category="os", is_gui=True,
              parameters={"type": "object", "properties": {}})
    class ShowDesktop(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            if _is_windows():
                try:
                    import ctypes
                    u = ctypes.windll.user32
                    VK_LWIN, VK_D, KEYEVENTF_KEYUP = 0x5B, 0x44, 0x0002
                    u.keybd_event(VK_LWIN, 0, 0, 0)
                    u.keybd_event(VK_D, 0, 0, 0)
                    u.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, 0)
                    u.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
                    return ToolResult.ok_result("Показал рабочий стол.", method="winapi_keys")
                except Exception as exc:                   # noqa: BLE001
                    return ToolResult.fail(f"не удалось: {exc}")
            inputs = ctx.service("inputs")
            if inputs is not None:
                res = await inputs.hotkey("super+d")
                if getattr(res, "ok", False):
                    return ToolResult.ok_result("Показал рабочий стол.", method="hotkey")
            return ToolResult.fail("показ рабочего стола не поддерживается на этой системе")

    @reg.tool("open_windows_settings",
              "Открыть раздел настроек Windows по ms-settings-ссылке (display, sound, "
              "bluetooth, apps, windowsupdate, network, privacy, personalization...).",
              risk=Risk.LOW, category="os",
              parameters={"type": "object", "properties": {
                  "page": _prop("string", "Раздел настроек или готовая ms-settings-ссылка")},
                  "required": []})
    class OpenWindowsSettings(Tool):
        PAGES = {
            "display": "ms-settings:display", "screen": "ms-settings:display",
            "sound": "ms-settings:sound", "audio": "ms-settings:sound",
            "bluetooth": "ms-settings:bluetooth", "devices": "ms-settings:bluetooth",
            "apps": "ms-settings:appsfeatures", "installed": "ms-settings:appsfeatures",
            "update": "ms-settings:windowsupdate", "windowsupdate": "ms-settings:windowsupdate",
            "network": "ms-settings:network", "wifi": "ms-settings:network-wifi",
            "privacy": "ms-settings:privacy", "personalization": "ms-settings:personalization",
            "background": "ms-settings:personalization-background",
            "datetime": "ms-settings:dateandtime", "time": "ms-settings:dateandtime",
            "language": "ms-settings:regionlanguage", "region": "ms-settings:regionlanguage",
            "storage": "ms-settings:storagesense", "power": "ms-settings:powersleep",
            "battery": "ms-settings:batterysaver", "mouse": "ms-settings:mousetouchpad",
            "keyboard": "ms-settings:keyboard", "about": "ms-settings:about",
            "default_apps": "ms-settings:defaultapps", "notifications": "ms-settings:notifications",
            "gaming": "ms-settings:gaming-gamebar", "printers": "ms-settings:printers",
            "taskbar": "ms-settings:taskbar", "startup": "ms-settings:startupapps",
        }

        async def execute(self, ctx: ToolContext, page: str = "") -> ToolResult:
            key = (page or "").strip().lower()
            uri = key if key.startswith("ms-settings:") else self.PAGES.get(key, "")
            if not uri:
                if not key:
                    uri = "ms-settings:"
                else:
                    from ..apps.aliases import similarity
                    best = max(((k, similarity(key, k)) for k in self.PAGES), key=lambda kv: kv[1],
                               default=("", 0.0))
                    if best[1] >= 0.7:
                        uri = self.PAGES[best[0]]
                    else:
                        return ToolResult.fail(f"неизвестный раздел настроек: «{page}». "
                                               f"Известные: {', '.join(sorted(self.PAGES)[:12])}…")
            if _is_windows():
                try:
                    os.startfile(uri)                       # type: ignore[attr-defined]
                    return ToolResult.ok_result(f"Открываю настройки: {uri}", method="ms_settings", uri=uri)
                except Exception as exc:                    # noqa: BLE001
                    r = await _run_async(["cmd", "/c", "start", "", uri], 8, shell=False)
                    return (ToolResult.ok_result(f"Открываю настройки: {uri}", method="cmd_start", uri=uri)
                            if r["ok"] else ToolResult.fail(f"{exc}; {r['stderr'][:200]}"))
            if shutil.which("gnome-control-center"):
                r = await _run_async(["gnome-control-center", key or "info-overview"], 8)
                return ToolResult.ok_result("Открываю настройки системы.", method="gnome_control_center", **r)
            return ToolResult.fail("это инструмент Windows (ms-settings)")

    # ------------------------------------------------------------------ терминал
    @reg.tool("execute_powershell",
              "Выполнить PowerShell-скрипт и вернуть структуру: exit code, stdout, "
              "stderr, время. Опасные команды требуют подтверждения.",
              risk=Risk.MEDIUM, category="shell",
              parameters={"type": "object", "properties": {
                  "script": _prop("string", "Скрипт или команда PowerShell"),
                  "cwd": _prop("string", "Рабочая папка"),
                  "timeout": _prop("number", "Таймаут, сек (по умолчанию 30)"),
                  "json": _prop("boolean", "Пытаться разобрать вывод как JSON")},
                  "required": ["script"]})
    class ExecutePowerShell(Tool):
        def estimate_risk(self, ctx, args):
            return assess_command(str(args.get("script", "")))

        async def execute(self, ctx: ToolContext, script: str, cwd: str = "",
                          timeout: float = 30.0, json: bool = False) -> ToolResult:
            if not script.strip():
                return ToolResult.fail("пустой скрипт")
            exe = "powershell" if shutil.which("powershell") else ("pwsh" if shutil.which("pwsh") else "")
            if _is_windows() and not exe:
                exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                   "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            if not _is_windows():
                if not exe:
                    return ToolResult.fail("PowerShell не найден (на этой системе нет pwsh)")
            argv = [exe or "pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", script]
            timeout = max(1.0, min(float(timeout or 30), 600))
            r = await _run_async(argv, timeout, cwd=cwd or ctx.workdir)
            if r["timeout"]:
                return ToolResult.fail(f"PowerShell не успел за {timeout:.0f} с (убит)", **r)
            parsed = None
            if json and r["stdout"].strip():
                try:
                    parsed = json.loads(r["stdout"])
                except json.JSONDecodeError:
                    parsed = None
            out = r["stdout"].strip() or "(пусто)"
            text = f"exit={r['exit_code']} за {r['ms']} мс\n{out[:2000]}"
            if r["stderr"].strip():
                text += f"\n[stderr] {r['stderr'][:600]}"
            data = {"exit_code": r["exit_code"], "stdout": r["stdout"], "stderr": r["stderr"],
                    "ms": r["ms"], "method": "powershell"}
            if parsed is not None:
                data["json"] = parsed
            if r["ok"]:
                return ToolResult(ok=True, output=text, data=data)
            return ToolResult(ok=False, output=text, error=r["stderr"].strip()[:400] or
                              f"exit code {r['exit_code']}", data=data)

    @reg.tool("execute_command",
              "Выполнить команду в оболочке (PowerShell/CMD/bash) со структурным "
              "результатом: exit code, stdout, stderr, время, рабочая папка.",
              risk=Risk.MEDIUM, category="shell",
              parameters={"type": "object", "properties": {
                  "command": _prop("string", "Команда"),
                  "shell": _prop("string", "auto | powershell | cmd | bash | sh"),
                  "cwd": _prop("string", "Рабочая папка"),
                  "timeout": _prop("number", "Таймаут, сек (30 по умолчанию)"),
                  "env": _prop("object", "Дополнительные переменные окружения")},
                  "required": ["command"]})
    class ExecuteCommand(Tool):
        def estimate_risk(self, ctx, args):
            return assess_command(str(args.get("command", "")))

        async def execute(self, ctx: ToolContext, command: str, shell: str = "auto",
                          cwd: str = "", timeout: float = 30.0, env: dict | None = None) -> ToolResult:
            cmd = (command or "").strip()
            if not cmd:
                return ToolResult.fail("пустая команда")
            shell = (shell or "auto").lower()
            if shell == "auto":
                shell = "powershell" if _is_windows() else ("bash" if shutil.which("bash") else "sh")
            if shell == "powershell":
                ps = shutil.which("powershell") or shutil.which("pwsh")
                if _is_windows() and not ps:
                    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                      "WindowsPowerShell", "v1.0", "powershell.exe")
                argv = [ps or "pwsh", "-NoProfile", "-Command", cmd]
            elif shell == "cmd":
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", cmd]
            else:
                argv = [shutil.which(shell) or shell, "-c", cmd]
            timeout = max(1.0, min(float(timeout or 30), 900))
            r = await _run_async(argv, timeout, cwd=cwd or ctx.workdir, env=env or None)
            head = f"[{shell}] exit={r['exit_code']} за {r['ms']} мс"
            body = (r["stdout"] or "").strip()
            if r["stderr"].strip():
                body += f"\n[stderr] {r['stderr'].strip()[:600]}"
            text = f"{head}\n{body[:2000] or '(нет вывода)'}"
            data = {"exit_code": r["exit_code"], "stdout": r["stdout"], "stderr": r["stderr"],
                    "ms": r["ms"], "cwd": cwd or ctx.workdir, "shell": shell, "method": "shell_exec"}
            if r["timeout"]:
                return ToolResult(ok=False, output=text, error=f"таймаут {timeout:.0f} с", data=data)
            if r["ok"]:
                return ToolResult(ok=True, output=text, data=data)
            return ToolResult(ok=False, output=text,
                              error=(r["stderr"] or "").strip()[:400] or f"exit code {r['exit_code']}",
                              data=data)

    # ------------------------------------------------------------------ процессы
    @reg.tool("read_processes",
              "Список процессов с фильтром и сортировкой: имя, PID, CPU, память. "
              "Быстрый нативный обход (Toolhelp32Snapshot/psutil/tasklist).",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Фильтр по имени (подстрока)"),
                  "limit": _prop("integer", "Сколько строк вернуть (20 по умолчанию)"),
                  "sort": _prop("string", "cpu | mem | name")},
                  "required": []})
    class ReadProcesses(Tool):
        async def execute(self, ctx: ToolContext, name: str = "", limit: int = 20,
                          sort: str = "cpu") -> ToolResult:
            state = ctx.service("state")
            rows: list[dict] = []
            if state is not None:
                try:
                    rows = await asyncio.to_thread(state.processes, False)
                except Exception:
                    rows = []
            if not rows:
                rows = self._fallback()
            if name:
                needle = name.lower()
                rows = [r for r in rows if needle in str(r.get("name", "")).lower()]
            key = {"cpu": "cpu", "mem": "mem", "name": "name"}.get(
                (sort or "cpu").lower(), "cpu")
            rows.sort(key=lambda r: (str(r.get(key, "")) if key == "name" else -float(r.get(key) or 0)))
            limit = max(1, min(int(limit or 20), 200))
            rows = rows[:limit]
            lines = [f"{r.get('pid', '?'):>6}  {str(r.get('name', ''))[:28]:28} "
                     f"cpu={float(r.get('cpu') or 0):5.1f}%  mem={float(r.get('mem') or 0):5.1f}%"
                     for r in rows]
            return ToolResult.ok_result(f"Процессов: {len(rows)}\n" + "\n".join(lines),
                                        method="native_processes", rows=rows)

        def _fallback(self) -> list[dict]:
            rows: list[dict] = []
            try:
                if _is_windows():
                    r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                       text=True, timeout=15)
                    for line in r.stdout.splitlines():
                        parts = [p.strip('"') for p in line.split('","')]
                        if len(parts) >= 5:
                            rows.append({"pid": int(parts[1]) if parts[1].isdigit() else 0,
                                         "name": parts[0], "cpu": 0.0, "mem": float(
                                             (parts[4] or "0").replace(",", "").replace(" K", "") or 0) / 1024})
                else:
                    r = subprocess.run(["ps", "-eo", "pid,pcpu,pmem,comm", "--no-headers"],
                                       capture_output=True, text=True, timeout=15)
                    for line in r.stdout.splitlines():
                        parts = line.split(None, 3)
                        if len(parts) == 4 and parts[0].isdigit():
                            rows.append({"pid": int(parts[0]), "name": Path(parts[3]).name,
                                         "cpu": float(parts[1]), "mem": float(parts[2])})
            except Exception:
                pass
            return rows

    # ------------------------------------------------------------------ ожидания
    @reg.tool("wait_for",
              "Ожидание события вместо «sleep»: process_started, process_finished, "
              "window_created, window_active, file_exists, file_gone, url_loaded, "
              "port_open, condition. Возвращает, сколько реально ждал.",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {
                  "kind": _prop("string", "process_started | process_finished | window_created | "
                                          "window_active | file_exists | file_gone | url_loaded | "
                                          "port_open | seconds"),
                  "target": _prop("string", "Имя процесса/окна/файла/URL/порт"),
                  "timeout": _prop("number", "Максимум ожидания, сек (10 по умолчанию)"),
                  "stable_ms": _prop("number", "Требовать стабильность столько мс")},
                  "required": ["kind"]})
    class WaitFor(Tool):
        async def execute(self, ctx: ToolContext, kind: str, target: str = "",
                          timeout: float = 10.0, stable_ms: float = 0) -> ToolResult:
            wait = ctx.service("wait")
            k = (kind or "").strip().lower()
            timeout = max(0.1, min(float(timeout or 10), 600))
            if k == "seconds":
                secs = float(target or timeout)
                await asyncio.sleep(max(0.0, min(secs, 600)))
                return ToolResult.ok_result(f"Подождал {secs:.1f} с.", method="sleep", skipped=False)
            if wait is None:
                return ToolResult.fail("подсистема ожиданий недоступна")
            try:
                if k == "process_started":
                    res = await wait.wait_process_started(target, timeout=timeout)
                elif k == "process_finished":
                    res = await wait.wait_process_finished(target, timeout=timeout)
                elif k == "window_created":
                    res = await wait.wait_window_created(target or None, timeout=timeout)
                elif k == "window_active":
                    res = await wait.wait_window_active(target, timeout=timeout)
                elif k == "file_exists":
                    res = await wait.wait_file_exists(target, timeout=timeout,
                                                      stable_ms=float(stable_ms or 0))
                elif k == "file_gone":
                    res = await wait.wait_file_gone(target, timeout=timeout)
                elif k == "url_loaded":
                    res = await wait.wait_url_loaded(target, timeout=timeout)
                elif k == "port_open":
                    res = await wait.wait_port(target, timeout=timeout)
                elif k == "condition":
                    res = await wait.wait_condition(target, timeout=timeout)
                else:
                    return ToolResult.fail(f"неизвестный тип ожидания: {kind}")
            except Exception as exc:                        # noqa: BLE001
                return ToolResult.fail(f"ожидание не удалось: {exc}")
            detail = getattr(res, "detail", "") or ""
            if getattr(res, "ok", False):
                return ToolResult.ok_result(
                    f"Дождался: {k} {target} за {getattr(res, 'elapsed_ms', 0):.0f} мс. {detail}".strip(),
                    method="wait", polls=getattr(res, "polls", 0))
            return ToolResult(ok=False, output=f"Не дождался: {k} {target} ({detail})",
                              error=detail or "таймаут ожидания",
                              data={"polls": getattr(res, "polls", 0)})

    # ------------------------------------------------------------------ состояние ПК
    @reg.tool("computer_state",
              "Снимок состояния ПК: активное окно и процесс, мониторы и DPI, позиция "
              "мыши, буфер обмена, открытые приложения, рабочий каталог, платформа.",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {
                  "include_clipboard": _prop("boolean", "Читать буфер обмена (по умолчанию да)"),
                  "parts": _prop("array", "Какие части нужны: active_window, monitors, mouse, "
                                          "clipboard, apps, env")},
                  "required": []})
    class ComputerStateTool(Tool):
        async def execute(self, ctx: ToolContext, include_clipboard: bool = True,
                          parts: list | None = None) -> ToolResult:
            state = ctx.service("state")
            if state is None:
                info = {"platform": os.name, "cwd": ctx.workdir,
                        "python": sys.version.split()[0]}
                return ToolResult.ok_result(json.dumps(info, ensure_ascii=False, indent=1),
                                            state=info, method="minimal")
            snap = await state.snapshot(parts=parts, include_clipboard=include_clipboard)
            formatter = getattr(state, "format_summary", None)
            text = (formatter(snap, include_clipboard=include_clipboard) if callable(formatter)
                    else json.dumps(snap, ensure_ascii=False, indent=1))
            return ToolResult.ok_result(text, method="computer_state", **{"state": snap})

    # ------------------------------------------------------------------ реестр приложений
    @reg.tool("find_app",
              "Найти приложение в реестре: ключ, отображаемое имя, путь к exe, "
              "протоколы, способы запуска, статистика использования. Понимает "
              "русские алиасы («телега», «вс код», «проводник»).",
              risk=Risk.NONE, category="apps",
              parameters={"type": "object", "properties": {
                  "query": _prop("string", "Название/алиас приложения"),
                  "suggestions": _prop("boolean", "Показать похожие варианты")},
                  "required": ["query"]})
    class FindApp(Tool):
        async def execute(self, ctx: ToolContext, query: str, suggestions: bool = True) -> ToolResult:
            apps = ctx.service("apps")
            if apps is None:
                return ToolResult.fail("реестр приложений недоступен")
            res = apps.find(query)
            if res.ok and res.record is not None:
                r = res.record
                path = r.confirmed_path() or "не найден"
                info = {"key": r.key, "display_name": r.display_name, "path": path,
                        "protocols": r.protocols, "appids": r.appids,
                        "launch_methods": r.launch_methods[:5], "matches": res.matches,
                        "confidence": round(res.score, 3), "exe": r.exe,
                        "uses": r.stats.get("uses", 0)}
                text = (f"{r.display_name} ({r.key}) — уверенность {res.score:.0%}\n"
                        f"Путь: {path}\nСовпало: {', '.join(res.matches[:4])}")
                return ToolResult.ok_result(text, app=info, method="app_registry")
            lines = [f"Не нашёл «{query}» в реестре."]
            sugg = apps.suggest(query, limit=5)
            if suggestions and sugg:
                lines.append("Похожие: " + ", ".join(f"{name} ({score:.0%})"
                                                     for name, score in sugg))
            return ToolResult(ok=False, output="\n".join(lines), error="приложение не найдено",
                              data={"suggestions": sugg})

    # ------------------------------------------------------------------ метрики
    @reg.tool("agent_metrics",
              "Метрики и бенчмарк агента: время до результата (TTC), латентность "
              "инструментов, успешность, фоллбэки, статистика маршрутов и способов "
              "запуска приложений.",
              risk=Risk.NONE, category="meta",
              parameters={"type": "object", "properties": {
                  "what": _prop("string", "summary | tools | methods | routes | all"),
                  "reset": _prop("boolean", "Сбросить метрики (осторожно)")},
                  "required": []})
    class AgentMetrics(Tool):
        async def execute(self, ctx: ToolContext, what: str = "summary",
                          reset: bool = False) -> ToolResult:
            metrics = ctx.service("metrics")
            if metrics is None:
                return ToolResult.fail("метрики недоступны")
            if reset:
                metrics.reset()
                return ToolResult.ok_result("Метрики сброшены.")
            what = (what or "summary").lower()
            data: dict = {}
            if what in ("summary", "all"):
                data["summary"] = metrics.summary()
            if what in ("tools", "all"):
                data["tools"] = {k: v for k, v in (metrics.to_dict().get("tools") or {}).items()}
            if what in ("methods", "all"):
                optimizer = ctx.service("optimizer")
                data["methods"] = optimizer.benchmark() if optimizer is not None else {}
            text = json.dumps(data, ensure_ascii=False, indent=1)[:3500]
            return ToolResult.ok_result(text, metrics=data, method="metrics")

    @reg.tool("plan_preview",
              "Показать, что агент собирается сделать, не выполняя действий "
              "(режим PLAN ONLY / dry run). Если задача требует модели — сообщит об этом.",
              risk=Risk.NONE, category="meta",
              parameters={"type": "object", "properties": {
                  "text": _prop("string", "Фраза пользователя")},
                  "required": ["text"]})
    class PlanPreview(Tool):
        async def execute(self, ctx: ToolContext, text: str) -> ToolResult:
            fast = ctx.service("fast")
            if fast is None:
                return ToolResult.fail("быстрый слой недоступен")
            preview = fast.preview(text)
            if not preview.get("known"):
                return ToolResult.ok_result(
                    f"«{text}» — для этой задачи нужен разбор моделью (план составит она).",
                    preview=preview, method="plan_preview")
            lines = [f"План без выполнения («{text}»):"]
            for i, step in enumerate(preview.get("steps", []), 1):
                lines.append(f"{i}. {step}")
            lines.append(f"Маршрут: {preview.get('route')} · действий: {preview.get('count')} · "
                         f"подтверждение: {'да' if preview.get('needs_confirm') else 'нет'}")
            return ToolResult.ok_result("\n".join(lines), preview=preview, method="plan_preview")


__all__ = ["register_win_system_tools"]
