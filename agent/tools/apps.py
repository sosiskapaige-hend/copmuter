"""Запуск/закрытие приложений.

Регистр известных приложений (config.apps) + поиск в PATH + startfile на
Windows / xdg-open на Linux. Это НЕ закрытый список: при отсутствии в
регистре агент может воспользоваться terminal_run или GUI-поиском.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry

_DEFAULTS = {
    "vscode": {"win32": "code", "linux": "code", "darwin": "code"},
    "code": {"win32": "code", "linux": "code", "darwin": "code"},
    "chrome": {"win32": "chrome", "linux": "google-chrome", "darwin": "open -a 'Google Chrome'"},
    "firefox": {"win32": "firefox", "linux": "firefox", "darwin": "open -a Firefox"},
    "edge": {"win32": "msedge", "linux": "microsoft-edge", "darwin": "open -a 'Microsoft Edge'"},
    "browser": {"win32": "__default_browser__", "linux": "__default_browser__", "darwin": "__default_browser__"},
    "explorer": {"win32": "explorer", "linux": "xdg-open", "darwin": "open"},
    "terminal": {"win32": "wt", "linux": "x-terminal-emulator", "darwin": "open -a Terminal"},
    "cmd": {"win32": "cmd", "linux": "x-terminal-emulator", "darwin": "open -a Terminal"},
    "powershell": {"win32": "powershell", "linux": "pwsh", "darwin": "pwsh"},
    "notepad": {"win32": "notepad", "linux": "gedit", "darwin": "open -a TextEdit"},
    "calc": {"win32": "calc", "linux": "gnome-calculator", "darwin": "open -a Calculator"},
    "calculator": {"win32": "calc", "linux": "gnome-calculator", "darwin": "open -a Calculator"},
    "paint": {"win32": "mspaint", "linux": "gimp", "darwin": "open -a Preview"},
    "settings": {"win32": "ms-settings:", "linux": "gnome-control-center", "darwin": "open 'x-apple.systempreferences:'"},
    "taskmgr": {"win32": "taskmgr", "linux": "gnome-system-monitor", "darwin": "open -a 'Activity Monitor'"},
    "word": {"win32": "winword", "linux": "libreoffice --writer", "darwin": "open -a 'Microsoft Word'"},
    "excel": {"win32": "excel", "linux": "libreoffice --calc", "darwin": "open -a 'Microsoft Excel'"},
    "telegram": {"win32": "telegram", "linux": "telegram-desktop", "darwin": "open -a Telegram"},
    "discord": {"win32": "discord", "linux": "discord", "darwin": "open -a Discord"},
    "steam": {"win32": "steam", "linux": "steam", "darwin": "open -a Steam"},
    "spotify": {"win32": "spotify", "linux": "spotify", "darwin": "open -a Spotify"},
}

# Русские названия → ключи реестра
_ALIASES = {
    "браузер": "browser", "хром": "chrome", "гугл хром": "chrome", "google chrome": "chrome",
    "файрфокс": "firefox", "мозилла": "firefox", "эдж": "edge", "microsoft edge": "edge",
    "проводник": "explorer", "терминал": "terminal", "командная строка": "cmd",
    "консоль": "cmd", "блокнот": "notepad", "калькулятор": "calc", "калькулятора": "calc",
    "настройки": "settings", "параметры": "settings", "диспетчер задач": "taskmgr",
    "ворд": "word", "эксель": "excel", "телеграм": "telegram", "телега": "telegram",
    "дискорд": "discord", "стим": "steam", "спотифай": "spotify", "vs code": "vscode",
    "visual studio code": "vscode", "пейнт": "paint",
}

# Где на Windows обычно лежат приложения, которых нет в PATH
_WIN_KNOWN_PATHS = {
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
    "msedge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
               r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
    "firefox": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"],
    "code": [r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
             r"%ProgramFiles%\Microsoft VS Code\Code.exe"],
    "telegram": [r"%AppData%\Telegram Desktop\Telegram.exe"],
    "discord": [r"%LocalAppData%\Discord\Update.exe --processStart Discord.exe"],
    "steam": [r"%ProgramFiles(x86)%\Steam\steam.exe"],
    "spotify": [r"%AppData%\Spotify\Spotify.exe"],
    "winword": [r"%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE"],
    "excel": [r"%ProgramFiles%\Microsoft Office\root\Office16\EXCEL.EXE"],
}


def _win_known_path(cmd: str) -> str | None:
    """Ищет exe в типичных местах установки Windows (когда его нет в PATH)."""
    base = os.path.basename(cmd).lower().replace(".exe", "")
    for tpl in _WIN_KNOWN_PATHS.get(base, []):
        p = os.path.expandvars(tpl)
        exe = p.split(" --")[0]
        if os.path.isfile(exe):
            return p
    return None


def _resolve(ctx: ToolContext, name: str) -> str | None:
    plat = {"windows": "win32", "linux": "linux", "macos": "darwin"}[ctx.platform.system]
    apps = {**_DEFAULTS, **(ctx.cfg.apps or {})}
    key = name.lower().strip()
    key = _ALIASES.get(key, key)
    entry = apps.get(key)
    cmd = None
    if isinstance(entry, dict):
        cmd = entry.get(plat) or entry.get("linux")
    elif entry is not None:
        cmd = str(entry)
    if cmd:
        return cmd
    in_path = shutil.which(key) or shutil.which(key.replace(" ", ""))
    if in_path:
        return in_path
    if plat == "win32":
        known = _win_known_path(key)
        if known:
            return known
    return None


def _open_in_default_browser(url: str) -> bool:
    """Системный браузер по умолчанию — без Playwright и без поиска exe."""
    import webbrowser
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _normalize_url(url: str) -> str:
    u = (url or "").strip().strip("«»\"'")
    if not u:
        return u
    if u.startswith(("http://", "https://", "file://", "mailto:", "about:")):
        return u
    # «открой ютуб» → youtube.com; «сайт google.com» → https://google.com
    known = {"ютуб": "youtube.com", "youtube": "youtube.com", "гугл": "google.com",
             "google": "google.com", "яндекс": "yandex.ru", "yandex": "yandex.ru",
             "вк": "vk.com", "vk": "vk.com", "вконтакте": "vk.com", "github": "github.com",
             "гитхаб": "github.com", "телеграм": "web.telegram.org", "почта": "mail.google.com",
             "gmail": "mail.google.com", "википедия": "ru.wikipedia.org", "wikipedia": "wikipedia.org"}
    low = u.lower()
    if low in known:
        return "https://" + known[low]
    if "." in u and " " not in u:
        return "https://" + u
    # поисковый запрос
    import urllib.parse
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(u)


def register_app_tools(reg: ToolRegistry) -> None:

    @reg.tool("open_url",
              "Открыть ссылку/сайт в СИСТЕМНОМ браузере пользователя (по умолчанию). "
              "Не требует Playwright. «Открой ютуб», «зайди на google.com», «открой браузер» — сюда. "
              "Если нужно ЧИТАТЬ или КЛИКАТЬ страницу — используйте browser_open.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "URL, домен или название сайта (пусто = просто открыть браузер)"),
                  "browser": _prop("string", "Конкретный браузер: chrome | firefox | edge (опционально)")},
                  "required": []})
    class OpenUrl(Tool):
        async def execute(self, ctx: ToolContext, url: str = "", browser: str = "") -> ToolResult:
            target = _normalize_url(url) if url else "about:blank"
            if not ctx.platform.has_display:
                return ToolResult.fail("нет дисплея (headless) — системный браузер открыть нельзя. "
                                       "Используйте browser_open (управляемый браузер) для чтения страницы.")
            if browser:
                cmd = _resolve(ctx, browser)
                if cmd and cmd != "__default_browser__":
                    ok, why = _spawn(ctx, cmd, [target])
                    if ok:
                        return ToolResult.ok_result(f"Открыто в {browser}: {target}", url=target)
                    # браузер не найден — падаем на системный по умолчанию
            if _open_in_default_browser(target):
                return ToolResult.ok_result(f"Открыто в браузере по умолчанию: {target}", url=target)
            # последний шанс: через ОС
            if ctx.platform.system == "windows":
                r = subprocess.run(["cmd", "/c", "start", "", target], capture_output=True, text=True)
                if r.returncode == 0:
                    return ToolResult.ok_result(f"Открыто: {target}", url=target)
            else:
                opener = "open" if ctx.platform.system == "macos" else "xdg-open"
                if shutil.which(opener):
                    subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                    return ToolResult.ok_result(f"Открыто: {target}", url=target)
            return ToolResult.fail(f"не удалось открыть браузер для {target}")

    def _spawn(ctx: ToolContext, cmd: str, args: list[str]) -> tuple[bool, str]:
        """Запуск процесса в фоне; возвращает (ok, причина)."""
        try:
            if ctx.platform.system == "windows":
                if " --" in cmd:                      # "Update.exe --processStart Discord.exe"
                    head, tail = cmd.split(" --", 1)
                    argv = [head, "--" + tail.split(" ", 1)[0]] + tail.split(" ")[1:] + args
                else:
                    exe = shutil.which(cmd) or (_win_known_path(cmd) or cmd)
                    if not os.path.isfile(exe) and not shutil.which(exe):
                        # start "" <cmd>: находит приложения по App Paths реестра
                        # (chrome, msedge, winword …), даже если их нет в PATH
                        r = subprocess.run(["cmd", "/c", "start", "", cmd, *args],
                                           capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            return True, ""
                        return False, (r.stderr or r.stdout or "").strip()[:200]
                    argv = [exe, *args]
                subprocess.Popen(argv, shell=False,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                 | getattr(subprocess, "DETACHED_PROCESS", 0))
                return True, ""
            import shlex
            parts = shlex.split(cmd)
            exe = shutil.which(parts[0])
            if not exe:
                return False, f"команда не найдена: {parts[0]}"
            subprocess.Popen([exe, *parts[1:], *args], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    @reg.tool("launch_app",
              "Запустить приложение по имени (chrome, browser, vscode, terminal, explorer, notepad, "
              "calc, telegram, ...) или командой. Понимает русские названия («блокнот», «браузер»). "
              "Открывает файл/папку в приложении, если передан path.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Имя приложения или команда"),
                  "path": _prop("string", "Файл/папка/URL, которую открыть в приложении")},
                  "required": ["name"]})
    class LaunchApp(Tool):
        async def execute(self, ctx: ToolContext, name: str, path: str = "") -> ToolResult:
            if not ctx.platform.has_display:
                return ToolResult.fail("нет дисплея (headless) — GUI-приложение запустить нельзя. "
                                       "Для консольных команд используйте terminal_run.")
            cmd = _resolve(ctx, name) or name
            args: list[str] = []
            if path:
                p = path if path.startswith(("http://", "https://")) else os.path.expanduser(path)
                if ctx.platform.system == "windows" and cmd.lower() in ("explorer", "explorer.exe"):
                    return await _open_windows(p)
                args = [p]
            # «открой браузер» → браузер по умолчанию (какой бы он ни был)
            if cmd == "__default_browser__":
                target = _normalize_url(path) if path else "about:blank"
                if _open_in_default_browser(target):
                    return ToolResult.ok_result(f"Открыт браузер по умолчанию: {target}")
                return ToolResult.fail("не удалось открыть браузер по умолчанию")
            if ctx.platform.system == "windows" and (cmd.startswith("ms-settings:") or cmd.endswith(":")):
                r = subprocess.run(["cmd", "/c", "start", "", cmd], capture_output=True, text=True)
                return ToolResult.ok_result(f"Запущено: {name}") if r.returncode == 0 \
                    else ToolResult.fail(r.stderr or f"не удалось запустить {name}")
            ok, why = _spawn(ctx, cmd, args)
            if ok:
                return ToolResult.ok_result(f"Запущено: {name}" + (f" ({' '.join(args)})" if args else ""))
            # браузеры: если конкретный не найден — открываем системный по умолчанию
            if name.lower() in ("chrome", "firefox", "edge", "хром", "браузер", "browser") or \
                    (path and path.startswith(("http://", "https://"))):
                target = _normalize_url(path) if path else "about:blank"
                if _open_in_default_browser(target):
                    return ToolResult.ok_result(f"«{name}» не найден — открыт браузер по умолчанию: {target}")
            return ToolResult.fail(f"не удалось запустить «{name}»: {why or 'не найдено'}. "
                                   f"Попробуйте terminal_run с полным путём к программе или list_apps.")

    async def _open_windows(p: str) -> ToolResult:
        try:
            os.startfile(p)  # type: ignore[attr-defined]
            return ToolResult.ok_result(f"Открыто: {p}")
        except Exception as e:
            return ToolResult.fail(f"не удалось открыть: {e}")

    @reg.tool("open_path", "Открыть файл/папку стандартным приложением ОС.",
              risk=Risk.LOW, category="apps",
              parameters={"type": "object", "properties": {
                  "path": _prop("string", "Путь")}, "required": ["path"]})
    class OpenPath(Tool):
        async def execute(self, ctx: ToolContext, path: str) -> ToolResult:
            p = os.path.expanduser(path)
            if not os.path.exists(p):
                return ToolResult.fail(f"не существует: {p}")
            if ctx.platform.system == "windows":
                return await _open_windows(p)
            cmd = ["open"] if ctx.platform.system == "macos" else ["xdg-open"]
            r = subprocess.run(cmd + [p], capture_output=True, text=True)
            return ToolResult.ok_result(f"Открыто: {p}") if r.returncode == 0 \
                else ToolResult.fail(f"нет xdg-open/open: {r.stderr.strip()}")

    @reg.tool("list_apps",
              "Список приложений, которые агент умеет запускать по имени (реестр приложений + PATH).",
              risk=Risk.NONE, category="apps",
              parameters={"type": "object", "properties": {
                  "query": _prop("string", "Фильтр по названию/алиасу (необязательно)"),
                  "limit": _prop("integer", "Сколько строк показать")}, "required": []})
    class ListApps(Tool):
        async def execute(self, ctx: ToolContext, query: str = "", limit: int = 60) -> ToolResult:
            registry = ctx.service("apps")
            if registry is not None:
                recs = registry.all()
                q = (query or "").strip().lower()
                if q:
                    recs = [r for r in recs
                            if q in r.key.lower() or q in (r.display_name or "").lower()
                            or any(q in a.lower() for a in r.aliases)]
                installed = [r for r in recs if r.installed]
                known = [r for r in recs if not r.installed]
                lines = [f"Установленные ({len(installed)}):"]
                for r in sorted(installed, key=lambda x: x.display_name)[:max(1, limit)]:
                    lines.append(f"- {r.display_name} ({r.key}) — {r.confirmed_path() or 'путь не найден'}")
                if known:
                    lines.append(f"Известные в каталоге ({len(known)}):")
                    lines += [f"- {r.display_name} ({r.key})" for r in
                              sorted(known, key=lambda x: x.display_name)[:max(1, limit)]]
                lines.append("Подсказка: find_app(\"телега\") покажет путь и способы запуска.")
                return ToolResult.ok_result("\n".join(lines), method="app_registry",
                                            counts={"installed": len(installed),
                                                    "known": len(known)})
            apps = {**_DEFAULTS, **(ctx.cfg.apps or {})}
            lines = [f"- {k}" for k in sorted(apps)]
            extra = []
            for d in (os.environ.get("PATH", "").split(os.pathsep) if ctx.platform.system != "windows"
                      else [r"C:\Program Files", r"C:\Program Files (x86)"]):
                try:
                    for e in os.listdir(d):
                        if e.lower().endswith((".exe", ".desktop")):
                            extra.append(e)
                except OSError:
                    continue
                if len(extra) > 200:
                    break
            if extra:
                lines.append("В PATH найдено:")
                lines += [f"- {e}" for e in sorted(set(extra))[:100]]
            return ToolResult.ok_result("\n".join(lines))

    @reg.tool("kill_app", "Закрыть приложение по имени (мягко, затем принудительно).",
              risk=Risk.HIGH, category="apps",
              parameters={"type": "object", "properties": {
                  "name": _prop("string", "Имя процесса"), "force": _prop("boolean", "Сразу принудительно")},
                  "required": ["name"]})
    class KillApp(Tool):
        async def execute(self, ctx: ToolContext, name: str, force: bool = False) -> ToolResult:
            from .system import _psutil
            ps = _psutil()
            if ps:
                killed = []
                for p in ps.process_iter(["pid", "name"]):
                    try:
                        nm = (p.info.get("name") or "").lower()
                        if name.lower() in nm or nm in name.lower():
                            if force:
                                p.kill()
                            else:
                                p.terminate()
                            killed.append(f"{p.info.get('name')} ({p.info.get('pid')})")
                    except Exception:
                        continue
                if killed:
                    return ToolResult.ok_result(f"Закрыто: {', '.join(killed[:10])}")
                return ToolResult.fail(f"процесс '{name}' не найден")
            # fallback: taskkill/pkill
            cmd = ["taskkill", "/IM", name] + (["/F"] if force else []) if ctx.platform.system == "windows" \
                else ["pkill", "-f" if force else "", name]
            r = subprocess.run([c for c in cmd if c], capture_output=True, text=True)
            return ToolResult.ok_result(r.stdout or "закрыто") if r.returncode == 0 \
                else ToolResult.fail(r.stderr or "не удалось закрыть")
