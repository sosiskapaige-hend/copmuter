"""Автообнаружение установленных программ (ТЗ §8).

Что сканируется на Windows (в порядке скорости — от быстрого к медленному):

  1. типовые пути каталога приложений (мгновенно: просто проверка файлов);
  2. реестр: App Paths (`...\\CurrentVersion\\App Paths`), список установленных
     программ (`...\\Uninstall`), зарегистрированные приложения
     (`SOFTWARE\\RegisteredApplications` → Capabilities), обработчики
     URL-протоколов (`HKCR\\<proto>\\shell\\open\\command`);
  3. меню «Пуск» (`%ProgramData%` и `%APPDATA%` → `.lnk`) + рабочий стол;
  4. PATH;
  5. браузер по умолчанию (`UrlAssociations\\http\\UserChoice`) и список
     браузеров из `StartMenuInternet`;
  6. Store/UWP-приложения (PowerShell `Get-StartApps`, самый медленный шаг —
     выполняется один раз и кэшируется).

На Linux сканируются `.desktop`-файлы (включая flatpak/snap), на macOS —
`/Applications`. Результат кэшируется в `AGENT_HOME/cache_apps.json`, поэтому
повторный запуск приложения не тратит время на поиск.

Обнаружение — фоновая операция (ТЗ §2 приложения к ТЗ): при старте агента
показывается кэш, а скан при необходимости идёт параллельно.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from . import catalog
from .aliases import normalize, similarity
from .registry import AppRecord, AppRegistry, expand, platform_key, slug, command_of


@dataclass
class Discovered:
    key: str
    display_name: str
    path: str = ""            # exe / .lnk / .desktop / shell-команда / URI
    exe: str = ""
    source: str = "unknown"   # candidate|catalog_path|app_paths|uninstall|registered|protocol|start_menu|path|store|desktop_app
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- реестр Windows
def _winreg():
    import winreg  # noqa: PLC0415 — только на Windows
    return winreg


def _reg_subkeys(root: int, path: str) -> list[str]:
    try:
        wr = _winreg()
        with wr.OpenKey(root, path) as k:
            names = []
            i = 0
            while True:
                try:
                    names.append(wr.EnumKey(k, i))
                except OSError:
                    break
                i += 1
            return names
    except Exception:
        return []


def _reg_value(root: int, path: str, name: str = "") -> str:
    try:
        wr = _winreg()
        with wr.OpenKey(root, path) as k:
            val, _ = wr.QueryValueEx(k, name)
            return str(val) if val is not None else ""
    except Exception:
        return ""


def _reg_values(root: int, path: str) -> dict[str, str]:
    try:
        wr = _winreg()
        with wr.OpenKey(root, path) as k:
            out, i = {}, 0
            while True:
                try:
                    nm, val, _ = wr.EnumValue(k, i)
                except OSError:
                    break
                out[str(nm)] = str(val)
                i += 1
            return out
    except Exception:
        return {}


def _trim_command(cmd: str) -> str:
    """«C:\\...\\chrome.exe» --single-argument %1 → C:\\...\\chrome.exe"""
    c = (cmd or "").strip()
    if not c:
        return ""
    m = re.match(r'^"([^"]+)"', c)
    if m:
        return m.group(1)
    # без кавычек: до .exe
    m = re.search(r"^(.*?\.exe)", c, re.I)
    if m:
        return m.group(1).strip()
    return c.split(" ")[0]


class AppDiscovery:
    """Сканер установленного ПО. Все шаги безопасны и не падают."""

    SOURCES_WINDOWS = ("catalog_path", "app_paths", "path", "registered", "uninstall",
                       "start_menu", "protocol", "store", "default_browser")

    def __init__(self, cache: Any = None, log: Any = None) -> None:
        self.cache = cache
        self.log = log
        self._last_stats: dict = {}

    # ---------------------------------------------------------------- API
    def run(self, registry: AppRegistry, quick: bool = False, use_cache: bool = True,
            max_age: float = 7 * 24 * 3600.0) -> dict:
        """Обнаружить и влить в реестр. Возвращает сводку."""
        cached = None
        if self.cache is not None and use_cache:
            if not self.cache.is_stale("discovery", max_age):
                cached = self.cache.get("discovery")
        if isinstance(cached, list) and cached:
            applied = self.apply(registry, cached)
            applied["from_cache"] = True
            self._last_stats = applied
            return applied
        entries = self.discover(quick=quick)
        if self.cache is not None:
            self.cache.set("discovery", [d.to_dict() for d in entries])
        applied = self.apply(registry, entries)
        applied["from_cache"] = False
        self._last_stats = applied
        return applied

    # ---------------------------------------------------------------- скан
    def discover(self, quick: bool = False) -> list[Discovered]:
        t0 = time.perf_counter()
        plat = platform_key()
        found: dict[str, Discovered] = {}

        def put(d: Discovered) -> None:
            if not d.path or not d.display_name:
                return
            cur = found.get(d.display_name.lower())
            if cur is None:
                found[d.display_name.lower()] = d
            elif not cur.path and d.path:
                found[d.display_name.lower()] = d

        # 1) типовые пути из каталога — самый быстрый и надёжный шаг
        for spec in catalog.specs_for_discovery():
            for tpl in spec.win_paths if plat == "windows" else ():
                p = expand(tpl)
                if "*" in p:
                    import glob
                    hits = sorted(glob.glob(p), reverse=True)
                    p = hits[0] if hits else ""
                if p and os.path.isfile(command_of(p)):
                    put(Discovered(key=spec.key, display_name=spec.display_name,
                                   path=p, exe=os.path.basename(command_of(p)),
                                   source="catalog_path"))
                    break

        if plat == "windows":
            self._scan_windows(found, quick=quick)
        elif plat == "linux":
            self._scan_linux(found)
        elif plat == "macos":
            self._scan_macos(found)

        # PATH из каталога (linux/macos + Windows-утилиты)
        for spec in catalog.specs_for_discovery():
            if plat == "windows":
                continue
            for b in list(spec.linux_bins) + list(spec.mac_bins):
                w = shutil.which(b)
                if w:
                    put(Discovered(key=spec.key, display_name=spec.display_name,
                                   path=w, exe=os.path.basename(w), source="path"))
                    break

        entries = list(found.values())
        self._last_stats = {"found": len(entries), "ms": round((time.perf_counter() - t0) * 1000, 1),
                            "quick": quick, "platform": plat}
        if self.log:
            self.log.info(f"Обнаружение программ: найдено {len(entries)} за "
                          f"{self._last_stats['ms']} мс", source="discovery", quick=quick)
        return entries

    # ---------------------------------------------------------------- Windows
    def _scan_windows(self, found: dict[str, Discovered], quick: bool = False) -> None:
        wr = None
        try:
            wr = _winreg()
        except Exception:
            return
        HKCU, HKLM, HKCR = wr.HKEY_CURRENT_USER, wr.HKEY_LOCAL_MACHINE, wr.HKEY_CLASSES_ROOT

        # --- App Paths: exe → полный путь (Chrome, Edge, Office, …) ---
        for root, base in ((HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                           (HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
                           (HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")):
            for exe in _reg_subkeys(root, base):
                p = _trim_command(_reg_value(root, f"{base}\\{exe}") or
                                  _reg_value(root, f"{base}\\{exe}", "Path"))
                if not p or not p.lower().endswith((".exe", ".com")):
                    continue
                name = Path(exe).stem
                spec = self._match_spec(name)
                put(Discovered(key=spec.key if spec else slug(name),
                               display_name=spec.display_name if spec else name,
                               path=p, exe=exe, source="app_paths"))

        # --- Установленные программы (Uninstall) ---
        for root, base in ((HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                           (HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                           (HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
            for sub in _reg_subkeys(root, base):
                key_path = f"{base}\\{sub}"
                name = _reg_value(root, key_path, "DisplayName")
                if not name or _reg_value(root, key_path, "SystemComponent") in ("1", "0x1"):
                    continue
                icon = _trim_command(_reg_value(root, key_path, "DisplayIcon"))
                loc = _reg_value(root, key_path, "InstallLocation")
                exe = ""
                if icon and icon.lower().endswith((".exe", ".com")) and os.path.isfile(icon):
                    exe = icon
                elif loc and os.path.isdir(loc):
                    cand = [os.path.join(loc, f) for f in os.listdir(loc)
                            if f.lower().endswith(".exe")][:1]
                    exe = cand[0] if cand else ""
                if not exe:
                    continue
                spec = self._match_spec(name)
                put(Discovered(key=spec.key if spec else slug(name),
                               display_name=spec.display_name if spec else name,
                               path=exe, exe=os.path.basename(exe), source="uninstall"))

        # --- Зарегистрированные приложения + их протоколы ---
        for root, base in ((HKLM, r"SOFTWARE\RegisteredApplications"),
                           (HKCU, r"SOFTWARE\RegisteredApplications")):
            for name, sub in _reg_values(root, base).items():
                caps_path = sub.strip("\\")
                app_name = _reg_value(HKLM, caps_path, "ApplicationName") or name
                urls = _reg_values(HKLM, f"{caps_path}\\URLAssociations") or \
                    _reg_values(HKCU, f"{caps_path}\\URLAssociations")
                spec = self._match_spec(app_name)
                if not spec:
                    continue
                for proto in list(urls.keys())[:10]:
                    put(Discovered(key=spec.key, display_name=spec.display_name,
                                   path=self._protocol_default(proto), exe="",
                                   source="registered", extra={"protocol": proto}))
                    if spec:
                        found.setdefault(spec.display_name.lower(),
                                         Discovered(key=spec.key, display_name=spec.display_name,
                                                    path="", source="registered",
                                                    extra={"protocol": proto}))

        # --- Меню «Пуск» и рабочий стол: ярлыки ---
        roots = [
            Path(os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs")),
            Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")),
            Path(os.path.expandvars(r"%USERPROFILE%\Desktop")),
            Path(os.path.expandvars(r"%PUBLIC%\Desktop")),
        ]
        for base_dir in roots:
            if not base_dir.is_dir():
                continue
            try:
                for lnk in base_dir.rglob("*.lnk"):
                    name = lnk.stem
                    spec = self._match_spec(name)
                    put(Discovered(key=spec.key if spec else slug(name),
                                   display_name=spec.display_name if spec else name,
                                   path=str(lnk), exe="", source="start_menu"))
            except OSError:
                continue

        # --- PATH ---
        for spec in catalog.specs_for_discovery():
            for e in spec.exe:
                w = shutil.which(e)
                if w:
                    put(Discovered(key=spec.key, display_name=spec.display_name,
                                   path=w, exe=e, source="path"))
                    break

        # --- браузер по умолчанию ---
        default_browser = self._default_browser()
        if default_browser:
            put(Discovered(key="browser", display_name=catalog.spec("browser").display_name,
                           path=default_browser, exe=os.path.basename(default_browser),
                           source="default_browser", extra={"default": True}))
            spec = self._match_spec(Path(default_browser).stem)
            if spec:
                put(Discovered(key=spec.key, display_name=spec.display_name,
                               path=default_browser, exe=os.path.basename(default_browser),
                               source="default_browser"))

        # --- Store/UWP (медленно — только когда не quick) ---
        if not quick:
            for name, appid in self._store_apps():
                spec = self._match_spec(name)
                put(Discovered(key=spec.key if spec else slug(name),
                               display_name=spec.display_name if spec else name,
                               path=f"shell:AppsFolder\\{appid}", exe="",
                               source="store", extra={"appid": appid}))

    def _protocol_default(self, proto: str) -> str:
        wr = _winreg()
        for base in (r"SOFTWARE\Classes", r"SOFTWARE\WOW6432Node\Classes"):
            cmd = _reg_value(wr.HKEY_CLASSES_ROOT, f"{proto}\\shell\\open\\command")
            if not cmd:
                cmd = _reg_value(wr.HKLM, f"{base}\\{proto}\\shell\\open\\command")
            if not cmd:
                cmd = _reg_value(wr.HKCU, f"{base}\\{proto}\\shell\\open\\command")
            if cmd:
                return _trim_command(cmd)
        return ""

    def _default_browser(self) -> str:
        wr = _winreg()
        progid = _reg_value(wr.HKCU,
                            r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
                            "ProgId")
        if progid:
            cmd = _reg_value(wr.HKCR, f"{progid}\\shell\\open\\command") or \
                _reg_value(wr.HKLM, rf"SOFTWARE\Classes\{progid}\shell\open\command")
            p = _trim_command(cmd)
            if p and os.path.isfile(p):
                return p
        # запасной путь: перечень браузеров
        for base in (r"SOFTWARE\Clients\StartMenuInternet", r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"):
            for name in _reg_subkeys(wr.HKLM, base):
                cmd = _reg_value(wr.HKLM, f"{base}\\{name}\\shell\\open\\command")
                p = _trim_command(cmd)
                if p and os.path.isfile(p):
                    return p
        return ""

    def _store_apps(self, timeout: float = 25.0) -> list[tuple[str, str]]:
        """Store-приложения через Get-StartApps (кэшируется на неделю)."""
        if self.cache is not None:
            cached = self.cache.get("store_apps")
            if isinstance(cached, list):
                return [(str(a), str(b)) for a, b in cached]
        out: list[tuple[str, str]] = []
        try:
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if not ps:
                return out
            r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                                "Get-StartApps | ConvertTo-Json -Compress"],
                               capture_output=True, text=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            import json
            data = json.loads((r.stdout or "[]").strip() or "[]")
            if isinstance(data, dict):
                data = [data]
            for item in data:
                if isinstance(item, dict) and item.get("AppID"):
                    out.append((str(item.get("Name") or item["AppID"]), str(item["AppID"])))
        except Exception as e:  # noqa: BLE001 — Store не обязателен
            if self.log:
                self.log.warn(f"Get-StartApps недоступен: {e}")
        if self.cache is not None and out:
            self.cache.set("store_apps", out)
        return out

    # ---------------------------------------------------------------- Linux
    def _scan_linux(self, found: dict[str, Discovered]) -> None:
        dirs = [
            Path("/usr/share/applications"),
            Path("/usr/local/share/applications"),
            Path(os.path.expanduser("~/.local/share/applications")),
            Path("/var/lib/flatpak/exports/share/applications"),
            Path(os.path.expanduser("~/.local/share/flatpak/exports/share/applications")),
            Path("/var/lib/snapd/desktop/applications"),
        ]
        for d in dirs:
            if not d.is_dir():
                continue
            try:
                files = sorted(d.glob("*.desktop"))
            except OSError:
                continue
            for f in files:
                info = self._parse_desktop(f)
                if not info:
                    continue
                name, exec_cmd = info["name"], info["exec"]
                spec = self._match_spec(name) or self._match_spec(Path(exec_cmd.split()[0]).name)
                found.setdefault(name.lower(), Discovered(
                    key=spec.key if spec else slug(name),
                    display_name=spec.display_name if spec else name,
                    path=str(f), exe=exec_cmd.split()[0] if exec_cmd else "",
                    source="desktop_app", extra={"exec": exec_cmd}))

    @staticmethod
    def _parse_desktop(path: Path) -> dict | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if "[Desktop Entry]" not in text:
            return None
        name = exec_cmd = ""
        no_display = terminal = False
        in_entry = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                in_entry = line == "[Desktop Entry]"
                continue
            if not in_entry or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "Name" and not name:
                name = v
            elif k == "Exec":
                exec_cmd = re.sub(r"%[fFuUdDnNickvm]", "", v).strip()
            elif k == "NoDisplay":
                no_display = v.lower() == "true"
            elif k == "Terminal":
                terminal = v.lower() == "true"
        if not name or not exec_cmd or no_display:
            return None
        if terminal:
            return None
        return {"name": name, "exec": exec_cmd}

    # ---------------------------------------------------------------- macOS
    def _scan_macos(self, found: dict[str, Discovered]) -> None:
        for root in ("/Applications", "/System/Applications",
                     os.path.expanduser("~/Applications")):
            p = Path(root)
            if not p.is_dir():
                continue
            try:
                items = sorted(p.glob("*.app"))
            except OSError:
                continue
            for app in items:
                name = app.stem
                spec = self._match_spec(name)
                found.setdefault(name.lower(), Discovered(
                    key=spec.key if spec else slug(name),
                    display_name=spec.display_name if spec else name,
                    path="open -a " + name, exe="", source="bundle"))

    # ---------------------------------------------------------------- единое
    def _match_spec(self, name: str) -> catalog.AppSpec | None:
        """Сопоставление имени программы с каталогом (алиасы + нечёткость)."""
        if not name:
            return None
        norm = normalize(name)
        best: tuple[catalog.AppSpec | None, float] = (None, 0.0)
        for spec in catalog.specs_for_discovery():
            for alias in (spec.display_name, spec.key, *spec.aliases):
                if not alias:
                    continue
                r = similarity(norm, alias)
                if r > best[1]:
                    best = (spec, r)
        if best[0] is not None and best[1] >= 0.82:
            return best[0]
        return None

    # ---------------------------------------------------------------- применение
    def apply(self, registry: AppRegistry, entries: Iterable[Discovered | dict]) -> dict:
        """Вливает результаты скана в реестр приложений."""
        plat = platform_key()
        n_new = n_paths = n_proto = 0
        by_source: dict[str, int] = {}
        for e in entries:
            if isinstance(e, dict):
                try:
                    e = Discovered(**e)
                except TypeError:
                    continue
            if not e or not e.display_name:
                continue
            by_source[e.source] = by_source.get(e.source, 0) + 1
            rec = registry.get(e.key)
            if rec is None:
                rec = registry.upsert(AppRecord(key=e.key, display_name=e.display_name,
                                                source=e.source, installed=bool(e.path)))
                n_new += 1
            if e.path and e.path.startswith(("shell:AppsFolder", "ms-")):
                # Store/URI-приложение: путь — команда оболочки, а не файл
                if rec.paths.get(plat) != e.path:
                    registry.set_path(e.key, e.path, plat, source=e.source)
                    n_paths += 1
            elif e.path and e.path.startswith("open -a"):
                if rec.paths.get(plat) != e.path:
                    registry.set_path(e.key, e.path, plat, source=e.source)
                    n_paths += 1
            elif e.path and os.path.exists(command_of(e.path)):
                cur = rec.paths.get(plat) or ""
                # предпочитаем exe/ярлык из меню «Пуск» «пустому» и не-exe пути
                if not cur or not cur.lower().endswith((".exe", ".com", ".lnk")):
                    registry.set_path(e.key, e.path, plat, source=e.source)
                    n_paths += 1
            elif e.path and e.path.endswith((".desktop", ".lnk")):
                if not rec.paths.get(plat):
                    registry.set_path(e.key, e.path, plat, source=e.source)
                    n_paths += 1
            if e.exe and e.exe.lower() not in rec.exe:
                rec.exe.append(e.exe.lower())
            proto = (e.extra or {}).get("protocol")
            if proto:
                if proto not in rec.protocols:
                    rec.protocols.append(str(proto))
                    n_proto += 1
            appid = (e.extra or {}).get("appid")
            if appid and appid not in rec.appids:
                rec.appids.append(str(appid))
            if e.source == "default_browser" and e.path:
                rec.extra["is_default"] = True
            rec.installed = rec.installed or bool(e.path)
            if e.display_name and e.display_name not in rec.aliases:
                rec.aliases.append(e.display_name)
                registry.aliases.register(e.key, [e.display_name])
        total = sum(by_source.values())
        registry.set_discovery_meta({"found": total, "by_source": by_source, "new": n_new,
                                     "paths": n_paths, "protocols": n_proto,
                                     "platform": plat, "stats": self._last_stats})
        registry.save()
        summary = {"ok": True, "found": total,
                   "new_apps": n_new, "new_paths": n_paths, "protocols": n_proto,
                   "by_source": by_source, "platform": plat,
                   "installed": len(registry.installed()),
                   "stats": self._last_stats}
        return summary
