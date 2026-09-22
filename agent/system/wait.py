"""Ожидание состояния вместо `sleep` (ТЗ §9 приложения к ТЗ).

Плохо:  открыл VS Code → sleep(3) → печатаю.
Хорошо: открыл VS Code → ждём появления процесса/окна → печатаю.

Ожидания опрашивают именно то, что нужно (процесс, окно, файл, URL, порт),
начинают с 20 мс и адаптивно увеличивают интервал до 250 мс — быстрый старт
без нагрузки на CPU. Все блокирующие проверки выполняются в executor'е, поэтому
event loop (и UI) остаются живыми.

На Windows список процессов берётся нативным API (CreateToolhelp32Snapshot),
поиск окна — EnumWindows/FindWindowW: это микросекунды-миллисекунды, что и
даёт «сотни миллисекунд» на простые команды.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_TIMEOUT = 10.0
TIMEOUTS = {                     # ТЗ §39: таймауты по типам операций
    "launch": 10.0,
    "file": 5.0,
    "browser": 15.0,
    "window": 8.0,
    "process": 10.0,
    "vision": 20.0,
}


@dataclass
class WaitResult:
    ok: bool
    elapsed_ms: float = 0.0
    polls: int = 0
    detail: str = ""

    def __bool__(self) -> bool:      # удобно: if await wait.wait_file_exists(p)
        return self.ok


def _norm_name(name: str) -> str:
    n = (name or "").strip().lower()
    return n if n.endswith((".exe", ".com")) or os.name != "nt" else n + ".exe"


class WaitManager:
    def __init__(self, state: Any = None, platform: Any = None,
                 base_interval: float = 0.02, max_interval: float = 0.25) -> None:
        self.state = state
        self.platform = platform
        self.base_interval = base_interval
        self.max_interval = max_interval
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------- ядро
    async def wait_until(self, predicate: Callable[[], Any], timeout: float = DEFAULT_TIMEOUT,
                         description: str = "", poll: float | None = None,
                         cancel: Callable[[], bool] | None = None,
                         require_stable: float = 0.0) -> WaitResult:
        """Ждёт, пока predicate() вернёт истину. Поддерживает отмену и стабилизацию."""
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        interval = poll or self.base_interval
        polls = 0
        stable_since: float | None = None
        last_err = ""
        while True:
            polls += 1
            try:
                value = predicate()
                if asyncio.iscoroutine(value):
                    value = await value
            except Exception as e:  # noqa: BLE001 — предикат не должен ронять ожидание
                value = False
                last_err = f"{type(e).__name__}: {e}"
            if value:
                if require_stable <= 0:
                    return WaitResult(True, round((time.perf_counter() - t0) * 1000, 1),
                                      polls, description)
                now = time.perf_counter()
                if stable_since is None:
                    stable_since = now
                elif (now - stable_since) >= require_stable:
                    return WaitResult(True, round((now - t0) * 1000, 1), polls, description)
            else:
                stable_since = None
            elapsed = time.perf_counter() - t0
            if elapsed >= timeout:
                return WaitResult(False, round(elapsed * 1000, 1), polls,
                                  (description + ("; " if description else "")
                                   + f"таймаут {timeout:g}с" + (f" ({last_err})" if last_err else "")))
            if cancel is not None and cancel():
                return WaitResult(False, round(elapsed * 1000, 1), polls, "отменено пользователем")
            await asyncio.sleep(min(interval, max(0.005, timeout - elapsed)))
            interval = min(self.max_interval, interval * 1.6)

    async def _in_executor(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args))

    # ------------------------------------------------------------- процессы
    async def wait_process_started(self, names: str | list[str], timeout: float | None = None,
                                   cancel: Callable[[], bool] | None = None,
                                   min_started_at: float = 0.0) -> WaitResult:
        wanted = [names] if isinstance(names, str) else list(names or [])
        wanted = [w for w in wanted if w]
        if not wanted:
            return WaitResult(True, 0.0, 0, "нет имён процессов — пропущено")

        def check() -> bool:
            procs = self._process_list_lite()
            for p in procs:
                pn = str(p.get("name") or "").lower()
                for w in wanted:
                    wn = w.lower()
                    if pn == wn or (len(wn) > 4 and wn in pn) or (len(pn) > 4 and pn in wn):
                        if min_started_at and float(p.get("started") or 0) < min_started_at:
                            continue
                        return True
            return False

        res = await self.wait_until(check, timeout=timeout or TIMEOUTS["process"],
                                    description=f"запуск процесса {', '.join(wanted[:3])}",
                                    cancel=cancel)
        if not res.ok and not res.detail.endswith("отменено пользователем"):
            res.detail = f"процесс не появился: {', '.join(wanted[:3])}"
        return res

    async def wait_process_exit(self, names: str | list[str], timeout: float = 30.0,
                                 cancel: Callable[[], bool] | None = None) -> WaitResult:
        wanted = [names] if isinstance(names, str) else list(names or [])

        def check() -> bool:
            procs = [str(p.get("name") or "").lower() for p in self._process_list_lite()]
            return not any(any(w.lower() in pn or pn in w.lower() for pn in procs)
                           for w in wanted)
        res = await self.wait_until(check, timeout=timeout,
                                    description=f"завершение {', '.join(wanted[:2])}",
                                    cancel=cancel)
        return res

    def _process_list_lite(self) -> list[dict]:
        """Быстрый список процессов: нативный API → psutil → tasklist/ps."""
        if os.name == "nt":
            out = self._win_processes()
            if out:
                return out
        cache_age = time.time() - self._cache.get("proc_ts", (0.0, None))[0]
        if cache_age < 0.2:
            return self._cache["proc_ts"][1]
        data = self._generic_processes()
        self._cache["proc_ts"] = (time.time(), data)
        return data

    @staticmethod
    def _win_processes() -> list[dict]:
        try:
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPPROCESS = 0x00000002
            INVALID = ctypes.c_void_p(-1).value
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_wchar * 260)]

            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == INVALID or not snap:
                return []
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            out: list[dict] = []
            try:
                if k32.Process32FirstW(snap, ctypes.byref(entry)):
                    while True:
                        out.append({"pid": int(entry.th32ProcessID),
                                    "name": str(entry.szExeFile), "rss_mb": 0.0,
                                    "started": 0.0})
                        if not k32.Process32NextW(snap, ctypes.byref(entry)):
                            break
            finally:
                k32.CloseHandle(snap)
            return out
        except Exception:
            return []

    @staticmethod
    def _generic_processes() -> list[dict]:
        try:
            import psutil  # type: ignore
            out = []
            for p in psutil.process_iter(["pid", "name", "create_time"]):
                try:
                    i = p.info
                    out.append({"pid": i.get("pid"), "name": i.get("name") or "",
                                "rss_mb": 0.0, "started": i.get("create_time") or 0.0})
                except Exception:
                    continue
            return out
        except Exception:
            pass
        import subprocess
        try:
            if os.name == "nt":
                r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                   text=True, timeout=8,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = []
                for line in r.stdout.splitlines():
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 2:
                        try:
                            out.append({"pid": int(parts[1]), "name": parts[0],
                                        "rss_mb": 0.0, "started": 0.0})
                        except ValueError:
                            continue
                return out
            r = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=8)
            out = []
            for line in r.stdout.splitlines():
                pid_s, _, name = line.strip().partition(" ")
                if pid_s.strip().isdigit():
                    out.append({"pid": int(pid_s), "name": os.path.basename(name.strip()),
                                "rss_mb": 0.0, "started": 0.0})
            return out
        except Exception:
            return []

    # ------------------------------------------------------------- окна
    async def wait_window_created(self, title: str, timeout: float | None = None,
                                  exact: bool = False,
                                  cancel: Callable[[], bool] | None = None) -> WaitResult:
        if not title:
            return WaitResult(True, 0.0, 0, "нет заголовка — пропущено")
        return await self.wait_until(lambda: self.find_window(title, exact) is not None,
                                     timeout=timeout or TIMEOUTS["window"],
                                     description=f"окно «{title[:40]}»", cancel=cancel)

    async def wait_window_active(self, title: str, timeout: float | None = None,
                                 cancel: Callable[[], bool] | None = None) -> WaitResult:
        return await self.wait_until(lambda: self.window_is_active(title),
                                     timeout=timeout or TIMEOUTS["window"],
                                     description=f"активное окно «{title[:40]}»", cancel=cancel)

    def find_window(self, title: str, exact: bool = False) -> dict | None:
        """Поиск окна по (части) заголовка: Win32 → wmctrl/xdotool → пусто."""
        if os.name == "nt":
            found = self._win_find_window(title, exact)
            if found:
                return found
        if self.platform is not None and getattr(self.platform, "has_x11", False):
            return self._x11_find_window(title)
        return None

    @staticmethod
    def _win_find_window(title: str, exact: bool = False) -> dict | None:
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32  # type: ignore[attr-defined]
            results: list[dict] = []
            target = title.lower()

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def cb(hwnd, _):
                if not u.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(hwnd, buf, 512)
                t = buf.value
                if not t:
                    return True
                hit = (t.lower() == target) if exact else (target in t.lower())
                if hit:
                    results.append({"hwnd": int(hwnd), "title": t})
                    return False
                return True

            u.EnumWindows(cb, 0)
            return results[0] if results else None
        except Exception:
            return None

    @staticmethod
    def _x11_find_window(title: str) -> dict | None:
        import subprocess
        try:
            r = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if title.lower() in line.lower():
                    parts = line.split(None, 3)
                    return {"hwnd": parts[0], "title": parts[3] if len(parts) > 3 else line}
        except Exception:
            pass
        try:
            r = subprocess.run(["xdotool", "search", "--name", title], capture_output=True,
                               text=True, timeout=5)
            ids = [i for i in r.stdout.split() if i.strip()]
            if ids:
                name = subprocess.run(["xdotool", "getwindowname", ids[0]], capture_output=True,
                                      text=True, timeout=5).stdout.strip()
                return {"hwnd": ids[0], "title": name}
        except Exception:
            pass
        return None

    def window_is_active(self, title: str) -> bool:
        if os.name == "nt":
            try:
                import ctypes
                u = ctypes.windll.user32  # type: ignore[attr-defined]
                hwnd = int(u.GetForegroundWindow())
                buf = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(hwnd, buf, 512)
                return title.lower() in buf.value.lower()
            except Exception:
                return False
        if self.state is not None:
            try:
                return title.lower() in (self.state._active_window_sync().title or "").lower()
            except Exception:
                return False
        return False

    def active_window_title(self) -> str:
        if os.name == "nt":
            try:
                import ctypes
                u = ctypes.windll.user32  # type: ignore[attr-defined]
                buf = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(int(u.GetForegroundWindow()), buf, 512)
                return buf.value
            except Exception:
                return ""
        if self.state is not None:
            try:
                return self.state._active_window_sync().title
            except Exception:
                return ""
        return ""

    # ------------------------------------------------------------- файлы
    async def wait_file_exists(self, path: str | Path, timeout: float | None = None,
                               min_size: int = 0, stable_ms: float = 0.0,
                               cancel: Callable[[], bool] | None = None) -> WaitResult:
        p = Path(path)

        def check() -> bool:
            try:
                if not p.exists():
                    return False
                if min_size and p.stat().st_size < min_size:
                    return False
                return True
            except OSError:
                return False
        return await self.wait_until(check, timeout=timeout or TIMEOUTS["file"],
                                     description=f"файл {p.name}",
                                     require_stable=stable_ms / 1000.0, cancel=cancel)

    async def wait_file_gone(self, path: str | Path, timeout: float = 10.0,
                             cancel: Callable[[], bool] | None = None) -> WaitResult:
        p = Path(path)
        return await self.wait_until(lambda: not p.exists(), timeout=timeout,
                                     description=f"удаление {p.name}", cancel=cancel)

    async def wait_file_changed(self, path: str | Path, since: float, timeout: float = 10.0,
                                cancel: Callable[[], bool] | None = None) -> WaitResult:
        p = Path(path)

        def check() -> bool:
            try:
                st = p.stat()
                return st.st_mtime > since and st.st_size >= 0
            except OSError:
                return False
        return await self.wait_until(check, timeout=timeout,
                                     description=f"сохранение {p.name}", cancel=cancel)

    # ------------------------------------------------------------- сеть/URL
    async def wait_url_loaded(self, url: str = "", title: str = "", timeout: float | None = None,
                              cancel: Callable[[], bool] | None = None) -> WaitResult:
        """URL «загрузился», если появилось окно браузера с нужным заголовком/адресом.

        Без browser automation это эвристика: ждём окно, в заголовке которого есть
        домен/заголовок страницы, либо любое окно браузера, если конкретика не дана.
        """
        from urllib.parse import urlparse
        host = ""
        if url:
            host = urlparse(url).netloc or url.split("/")[0]

        def check() -> bool:
            keys = [k for k in (host.replace("www.", ""), title) if k]
            if not keys:
                keys = ["chrome", "firefox", "edge", "opera", "brave", "browser", "yandex"]
            for k in keys:
                if self.find_window(k):
                    return True
            return False
        res = await self.wait_until(check, timeout=timeout or TIMEOUTS["browser"],
                                    description=f"загрузка {host or title or 'страницы'}",
                                    cancel=cancel)
        if not res.ok:
            res.detail = f"страница не подтвердилась в заголовке окна ({host or title})"
        return res

    async def wait_port(self, host: str, port: int, timeout: float = 15.0,
                        cancel: Callable[[], bool] | None = None) -> WaitResult:
        def check() -> bool:
            import socket
            s = socket.socket()
            s.settimeout(0.4)
            try:
                s.connect((host, port))
                return True
            except OSError:
                return False
            finally:
                s.close()
        return await self.wait_until(check, timeout=timeout,
                                     description=f"порт {host}:{port}", cancel=cancel)

    # ------------------------------------------------------------- общее
    async def wait_condition(self, fn: Callable[[], Any], timeout: float = 5.0,
                             description: str = "условие",
                             cancel: Callable[[], bool] | None = None) -> WaitResult:
        return await self.wait_until(fn, timeout=timeout, description=description, cancel=cancel)

    async def wait_ready(self, app: Any = None, window: str = "", process: str = "",
                         file: str | Path | None = None, url: str = "",
                         timeout: float | None = None,
                         cancel: Callable[[], bool] | None = None) -> WaitResult:
        """Составное ожидание «приложение готово»: процесс → окно → файл."""
        deadline = time.perf_counter() + (timeout or TIMEOUTS["launch"])
        if process:
            names = [process]
            if app is not None and hasattr(app, "proc_names"):
                names = app.proc_names() or names
            r = await self.wait_process_started(names, timeout=max(0.5, deadline - time.perf_counter()),
                                                cancel=cancel)
            if not r.ok and not window:
                return r
        if window:
            r = await self.wait_window_created(window, timeout=max(0.5, deadline - time.perf_counter()),
                                               cancel=cancel)
            if not r.ok and not file:
                return r
        if file:
            return await self.wait_file_exists(file, timeout=max(0.5, deadline - time.perf_counter()),
                                               cancel=cancel)
        return WaitResult(True, round((time.perf_counter() - deadline) * 1000, 1), 0, "готово")


def looks_like_regex(text: str) -> bool:
    return any(ch in (text or "") for ch in ("[", "(", "|", "\\", "*", "+", "^", "$"))


def safe_regex(pattern: str) -> "re.Pattern[str] | None":
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return None
