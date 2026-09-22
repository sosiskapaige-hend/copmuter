"""Реестр приложений (ТЗ §8, §27, §45).

Хранит всё, что нужно для мгновенного запуска программ:

  * подтверждённые пути (.exe/.lnk), найденные при первом запуске и
    сохранённые локально, — дальше запуск идёт без повторного поиска;
  * альтернативные способы запуска: ярлык, URI-протокол, Store/UWP,
    shell-команда, имя процесса;
  * статистику использования: сколько раз запускали, среднее время,
    успешность — на ней работает адаптивный выбор метода.

Каталог (agent/apps/catalog.py) задаёт «стартовые знания», автообнаружение
(agent/apps/discovery.py) дополняет их тем, что реально стоит на машине, а
результат кэшируется в `AGENT_HOME/apps.json`.

Всё, что знает реестр, доступно и LLM (tool `apps_registry`), и UI.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

from . import catalog
from .aliases import AliasResolver, normalize, similarity

SCHEMA = 3


def platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def slug(text: str) -> str:
    s = normalize(text).replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-_.+]", "", s)
    return s.strip("-") or "app"


def expand(path: str) -> str:
    """Разворачивает %VAR% (Windows) и ~ в реальный путь."""
    p = os.path.expandvars(path) if "%" in path else path
    return os.path.expanduser(p)


def command_of(path: str) -> str:
    """Из «Update.exe --processStart Discord.exe» — путь до аргументов."""
    return expand(str(path).split(" --")[0].strip().strip('"'))


@dataclass
class AppRecord:
    key: str
    display_name: str = ""
    kind: str = "app"                     # app | folder | settings | shell | power | store
    aliases: list[str] = field(default_factory=list)
    exe: list[str] = field(default_factory=list)          # имена процессов
    paths: dict[str, str] = field(default_factory=dict)   # платформа → подтверждённый путь/команда
    path_candidates: list[str] = field(default_factory=list)  # шаблоны путей (Windows)
    shortcuts: list[str] = field(default_factory=list)    # .lnk / .desktop
    protocols: list[str] = field(default_factory=list)
    appids: list[str] = field(default_factory=list)       # Store: shell:AppsFolder\<AppID>
    shell: str = ""                                       # готовая shell-команда / ms-settings:
    args: list[str] = field(default_factory=list)
    source: str = "catalog"                               # catalog | registry | start_menu | path | store | user
    installed: bool = False
    verified_at: float = 0.0
    last_used: float = 0.0
    use_count: int = 0
    ok_count: int = 0
    fail_count: int = 0
    avg_ms: float = 0.0
    methods_tried: dict[str, int] = field(default_factory=dict)
    launch_method: str = ""                               # последний успешный метод
    extra: dict = field(default_factory=dict)

    # ---------- представления ----------
    def confirmed_path(self, plat: str | None = None) -> str:
        """Подтверждённый путь, если он ещё существует (иначе пусто)."""
        plat = plat or platform_key()
        p = (self.paths or {}).get(plat) or ""
        if not p:
            return ""
        if p.startswith(("shell:", "ms-", "http:", "https:")) or p.endswith(":"):
            return p
        if any(ch in p for ch in "*?"):
            return p
        return p if os.path.exists(command_of(p)) else ""

    @property
    def stats(self) -> dict:
        """Сводка использования записи (её читают инструменты и UI)."""
        return {"uses": self.use_count, "ok": self.ok_count, "fail": self.fail_count,
                "avg_ms": round(self.avg_ms, 1), "last_used": self.last_used,
                "methods": dict(self.methods_tried or {}),
                "launch_method": self.launch_method}

    @property
    def launch_methods(self) -> list[str]:
        """Возможные способы запуска этой записи (по имеющимся данным)."""
        out: list[str] = []
        if self.paths:
            out.append("cached_executable")
        if self.shell or self.protocols or self.kind in ("settings", "folder", "shell"):
            out.append("registered_app")
        if self.shortcuts:
            out.append("shortcut")
        if self.protocols:
            out.append("protocol")
        if self.appids:
            out.append("start_menu")
        if self.extra.get("linux_desktop") or self.extra.get("mac_app"):
            out.append("shortcut")
        for m in ("shell", "powershell", "ui_automation"):
            out.append(m)
        seen: list[str] = []
        for m in out:
            if m not in seen:
                seen.append(m)
        return seen

    def candidate_list(self, plat: str | None = None) -> list[str]:
        """Кандидаты для поиска (в порядке попыток)."""
        plat = plat or platform_key()
        out: list[str] = []
        if plat == "windows":
            if self.shell:
                out.append(self.shell)
            out.extend(self.path_candidates)
            out.extend(self.shortcuts)
        elif plat == "linux":
            out.extend(self.shortcuts)
            out.extend(self.extra.get("linux_bins") or [])
            out.extend(self.extra.get("linux_desktop") or [])
        elif plat == "macos":
            out.extend(self.shortcuts)
            out.extend(self.extra.get("mac_bins") or [])
            if self.extra.get("mac_app"):
                out.append("open -a " + str(self.extra["mac_app"]))
        return [c for c in out if c]

    def proc_names(self) -> list[str]:
        names = {str(e).lower() for e in self.exe if e}
        for p in list((self.paths or {}).values()) + list(self.path_candidates):
            if not p or p.startswith(("shell:", "ms-", "http")):
                continue
            base = os.path.basename(command_of(p))
            if base.lower().endswith((".exe", ".com", ".app")):
                names.add(base.lower())
        return sorted(n for n in names if n)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppRecord":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def record_result(self, ok: bool, ms: float, method: str = "") -> None:
        self.use_count += 1
        self.last_used = time.time()
        if ok:
            self.ok_count += 1
            if method:
                self.launch_method = method
        else:
            self.fail_count += 1
        if method:
            self.methods_tried[method] = self.methods_tried.get(method, 0) + 1
        if ms > 0:
            n = max(1, self.ok_count + self.fail_count)
            self.avg_ms = round((self.avg_ms * (n - 1) + ms) / n, 1)

    def success_rate(self) -> float:
        total = self.ok_count + self.fail_count
        return round(self.ok_count / total, 3) if total else 0.0


@dataclass
class LookupResult:
    record: AppRecord | None
    score: float
    matched: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None and self.score >= 0.62

    @property
    def matches(self) -> str:
        """Совместимый алиас `matched` (инструменты и UI читают это имя)."""
        return self.matched


class AppRegistry:
    """Реестр приложений: каталог + обнаруженное + выученное."""

    def __init__(self, path: Path | str | None = None,
                 aliases: AliasResolver | None = None) -> None:
        self.path = Path(path) if path else None
        self.aliases = aliases or AliasResolver(None, catalog.builtin_aliases())
        self._lock = threading.RLock()
        self._apps: dict[str, AppRecord] = {}
        self._by_exe: dict[str, str] = {}
        self._discovered_at: float = 0.0
        self._discovery_stats: dict = {}
        self._user_overrides: dict[str, dict] = {}
        self._dirty = False
        for spec in catalog.ALL_SPECS:
            self._apps[spec.key] = self._from_spec(spec)
        self._reindex()
        self.load()

    # ---------------- построение ----------------
    @staticmethod
    def _from_spec(spec: catalog.AppSpec) -> AppRecord:
        return AppRecord(
            key=spec.key, display_name=spec.display_name, kind=spec.kind,
            aliases=list(spec.aliases),
            exe=list(spec.exe),
            path_candidates=list(spec.win_paths),
            protocols=list(spec.protocols), appids=list(spec.appids),
            shell=spec.shell, args=list(spec.args), source="catalog",
            extra={"linux_bins": list(spec.linux_bins), "linux_desktop": list(spec.linux_desktop),
                   "mac_app": spec.mac_app, "mac_bins": list(spec.mac_bins),
                   "start_menu": list(spec.in_start_menu)},
        )

    def _reindex(self) -> None:
        by_exe: dict[str, str] = {}
        for key, rec in self._apps.items():
            for n in rec.proc_names():
                by_exe.setdefault(n, key)
        self._by_exe = by_exe

    # ---------------- доступ ----------------
    def get(self, key: str) -> AppRecord | None:
        return self._apps.get(key)

    def all(self) -> list[AppRecord]:
        return [self._apps[k] for k in sorted(self._apps)]

    def keys(self) -> list[str]:
        return sorted(self._apps)

    def installed(self) -> list[AppRecord]:
        return [r for r in self.all() if r.installed or r.paths.get(platform_key())]

    def by_process(self, name: str) -> AppRecord | None:
        n = (name or "").lower()
        if not n:
            return None
        key = self._by_exe.get(n)
        if key:
            return self._apps.get(key)
        for rec in self.all():
            for pn in rec.proc_names():
                if pn and (pn in n or n in pn):
                    return rec
        return None

    # ---------------- поиск по имени ----------------
    def find(self, name: str) -> LookupResult:
        """Естественное имя → запись реестра («телега», «вс код», «диспетчер задач»)."""
        raw = (name or "").strip()
        if not raw:
            return LookupResult(None, 0.0)
        norm = normalize(raw)
        apps = self._apps
        if norm in apps:
            return LookupResult(apps[norm], 1.0, "key")
        key, score, why = self.aliases.resolve(raw)
        if key and key in apps and score >= 0.62:
            return LookupResult(apps[key], score, why)
        best: tuple[AppRecord | None, float, str] = (None, 0.0, "")
        for k, rec in apps.items():
            for alias in [rec.display_name, k, *rec.aliases]:
                if not alias:
                    continue
                r = similarity(norm, alias)
                if r > best[1]:
                    best = (rec, r, f"cmp:{alias}")
        if best[0] is not None and best[1] >= 0.72:
            return LookupResult(best[0], round(best[1], 3), best[2])
        return LookupResult(None, round(best[1], 3), best[2])

    def suggest(self, text: str, limit: int = 5) -> list[tuple[str, str, float]]:
        """[(ключ, название, уверенность)] — подсказки для UI и модели."""
        raw = (text or "").strip()
        out: list[tuple[str, str, float]] = []
        seen: set[str] = set()
        r = self.find(raw)
        if r.record is not None:
            out.append((r.record.key, r.record.display_name, round(r.score, 3)))
            seen.add(r.record.key)
        for key, score in self.aliases.suggest(raw, limit=limit * 2):
            rec = self._apps.get(key)
            if rec and key not in seen:
                out.append((key, rec.display_name, round(score, 3)))
                seen.add(key)
        return out[:limit]

    # ---------------- наполнение ----------------
    def upsert(self, rec: AppRecord, merge: bool = True) -> AppRecord:
        with self._lock:
            cur = self._apps.get(rec.key)
            if cur is None or not merge:
                self._apps[rec.key] = rec
            else:
                cur.display_name = rec.display_name or cur.display_name
                if rec.kind and rec.kind != "app":
                    cur.kind = rec.kind
                for a in rec.aliases:
                    if a and a not in cur.aliases:
                        cur.aliases.append(a)
                for e in rec.exe:
                    if e and e not in cur.exe:
                        cur.exe.append(e)
                for p in rec.path_candidates:
                    if p and p not in cur.path_candidates:
                        cur.path_candidates.append(p)
                for plat, p in (rec.paths or {}).items():
                    cur.paths.setdefault(plat, p)
                for s in rec.shortcuts:
                    if s and s not in cur.shortcuts:
                        cur.shortcuts.append(s)
                for p in rec.protocols:
                    if p and p not in cur.protocols:
                        cur.protocols.append(p)
                for a in rec.appids:
                    if a and a not in cur.appids:
                        cur.appids.append(a)
                if rec.shell and not cur.shell:
                    cur.shell = rec.shell
                if rec.args and not cur.args:
                    cur.args = rec.args
                if rec.installed:
                    cur.installed = True
                if rec.source and cur.source == "catalog":
                    cur.source = rec.source
                for k, v in (rec.extra or {}).items():
                    if v and not cur.extra.get(k):
                        cur.extra[k] = v
            self._reindex()
            self._dirty = True
        return self._apps[rec.key]

    def add_aliases(self, key: str, aliases: Iterable[str]) -> int:
        rec = self._apps.get(key)
        if rec is None:
            return 0
        n = 0
        for a in aliases:
            a = (a or "").strip()
            if a and a not in rec.aliases:
                rec.aliases.append(a)
                self.aliases.register(key, [a])
                n += 1
        if n:
            self._dirty = True
        return n

    def set_path(self, key: str, path: str, plat: str | None = None,
                 source: str = "discovery", display_name: str = "") -> None:
        plat = plat or platform_key()
        rec = self._apps.get(key)
        if rec is None:
            rec = self.upsert(AppRecord(key=key, display_name=display_name or key,
                                        source=source, installed=True))
        rec.paths[plat] = path
        rec.verified_at = time.time()
        if self._looks_installed(path):
            rec.installed = True
        self._dirty = True

    @staticmethod
    def _looks_installed(path: str) -> bool:
        p = (path or "").strip()
        if not p:
            return False
        if p.endswith(":") or p.startswith(("shell:", "ms-")):
            return True
        if any(ch in p for ch in "*?"):
            return True
        cs = command_of(p)
        if cs and (os.path.exists(cs) or cs.startswith(("open -a ", "cmd ", "powershell "))):
            return True
        return False

    def mark_used(self, key: str, ok: bool, ms: float, method: str = "") -> None:
        rec = self._apps.get(key)
        if rec is None:
            return
        rec.record_result(ok, ms, method)
        self._dirty = True

    def set_discovery_meta(self, stats: dict) -> None:
        self._discovered_at = time.time()
        self._discovery_stats = dict(stats or {})
        self._dirty = True

    def discovery_info(self) -> dict:
        return {"at": self._discovered_at,
                "stats": dict(self._discovery_stats),
                "age_h": round((time.time() - self._discovered_at) / 3600, 2)
                         if self._discovered_at else None,
                "apps": len(self._apps), "installed": len(self.installed())}

    # ---------------- персистентность ----------------
    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema") not in (None, SCHEMA):
            return
        with self._lock:
            for key, d in (data.get("apps") or {}).items():
                if not isinstance(d, dict):
                    continue
                try:
                    rec = AppRecord.from_dict(d)
                except TypeError:
                    continue
                if not rec.key:
                    rec.key = key
                cur = self._apps.get(key)
                if cur is not None:
                    # каталог мог обновиться: новое берём из каталога, опыт — из файла
                    cur.paths.update(rec.paths or {})
                    for s in rec.shortcuts or []:
                        if s and s not in cur.shortcuts:
                            cur.shortcuts.append(s)
                    cur.protocols = sorted(set(cur.protocols) | set(rec.protocols or []))
                    cur.appids = sorted(set(cur.appids) | set(rec.appids or []))
                    cur.installed = cur.installed or rec.installed
                    cur.use_count, cur.ok_count, cur.fail_count = \
                        rec.use_count, rec.ok_count, rec.fail_count
                    cur.avg_ms = rec.avg_ms
                    cur.launch_method = rec.launch_method or cur.launch_method
                    cur.last_used = rec.last_used
                    cur.methods_tried = dict(rec.methods_tried or {})
                    cur.verified_at = rec.verified_at
                    for a in rec.aliases or []:
                        if a and a not in cur.aliases:
                            cur.aliases.append(a)
                else:
                    self._apps[key] = rec
            self._discovered_at = float(data.get("discovered_at") or 0.0)
            self._discovery_stats = dict(data.get("discovery_stats") or {})
            self._user_overrides = dict(data.get("overrides") or {})
            self._reindex()
        for rec in self.all():
            self.aliases.register(rec.key, [rec.display_name, *rec.aliases])

    def save(self, force: bool = False) -> None:
        if not self.path:
            return
        with self._lock:
            if not (force or self._dirty):
                return
            payload = {"schema": SCHEMA, "saved": time.time(),
                       "discovered_at": self._discovered_at,
                       "discovery_stats": self._discovery_stats,
                       "overrides": self._user_overrides,
                       "apps": {k: rec.to_dict() for k, rec in self._apps.items()}}
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ---------------- подсказки для LLM/UI ----------------
    def brief_list(self, limit: int = 40) -> str:
        rows = []
        for rec in self.installed()[:limit]:
            p = rec.paths.get(platform_key()) or ""
            rows.append(f"- {rec.key} ({rec.display_name})" + (f" — {p}" if p else ""))
        return "\n".join(rows) or "(реестр пуст — запустите обнаружение программ)"

    def capability_summary(self) -> dict:
        inst = self.installed()
        groups: dict[str, list[str]] = {}
        for rec in inst:
            groups.setdefault(rec.kind, []).append(rec.display_name)
        return {"total": len(self._apps), "installed": len(inst),
                "by_kind": {k: sorted(v) for k, v in groups.items()},
                "browsers": sorted(r.display_name for r in inst
                                   if r.key in ("browser", "chrome", "firefox", "edge", "brave",
                                                "opera", "yandex-browser")),
                "discovery": self.discovery_info()}
