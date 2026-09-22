"""Кэш: память (TTL) + диск (JSON) + «cached()»-обёртка.

Кэшируем то, что дорого добывать и медленно меняется (ТЗ §45):
пути приложений, результаты обнаружения программ, системные директории,
сведения о браузере, схемы инструментов, успешные маршруты выполнения.

Никогда не кэшируем «горячие» данные без проверки: у каждого значения есть
TTL, а у файлового кэша — версия схемы (при апгрейде кэш инвалидируется).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_TTL = 300.0          # 5 минут — для «быстрых» данных (процессы, окна)
LONG_TTL = 7 * 24 * 3600.0   # неделя — для путей приложений, директорий


class TTLCache:
    """Потокобезопасный in-memory кэш с TTL и статистикой попаданий."""

    def __init__(self, default_ttl: float = DEFAULT_TTL, max_items: int = 512) -> None:
        self.default_ttl = default_ttl
        self.max_items = max_items
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return default
            expires, value = item
            if expires and expires < time.time():
                self._data.pop(key, None)
                self.misses += 1
                return default
            self.hits += 1
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        t = self.default_ttl if ttl is None else ttl
        expires = (time.time() + t) if t else 0.0
        with self._lock:
            self._data[key] = (expires, value)
            if len(self._data) > self.max_items:
                # выкидываем самые «старые» по сроку годности
                for k in sorted(self._data, key=lambda k: self._data[k][0])[:max(1, self.max_items // 8)]:
                    self._data.pop(k, None)

    def invalidate(self, prefix: str = "") -> int:
        with self._lock:
            keys = [k for k in self._data if not prefix or k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
            return len(keys)

    def stats(self) -> dict:
        with self._lock:
            n = len(self._data)
        total = self.hits + self.misses
        return {"items": n, "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}

    def get_or_set(self, key: str, producer: Callable[[], Any], ttl: float | None = None) -> Any:
        """Возвращает значение из кэша, иначе вычисляет producer() и сохраняет.

        Важно: producer может кинуть исключение — тогда кэш не отравляется.
        """
        sentinel = object()
        val = self.get(key, sentinel)
        if val is not sentinel:
            return val
        val = producer()
        if val is not None:
            self.set(key, val, ttl)
        return val


class JsonCache:
    """Дисковый кэш-словарь (JSON) с версией схемы и TTL на запись.

    Файл держим маленьким и человекочитаемым: его можно посмотреть глазами,
    починить руками и удалить для «сброса кэша».
    """

    def __init__(self, path: Path | str, schema: int = 1, ttl: float = LONG_TTL) -> None:
        self.path = Path(path)
        self.schema = schema
        self.ttl = ttl
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._loaded = False

    # ---------- низкий уровень ----------
    def load(self) -> None:
        with self._lock:
            self._loaded = True
            self._data = {}
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict) or raw.get("schema") != self.schema:
                return
            items = raw.get("items")
            if isinstance(items, dict):
                self._data = {k: v for k, v in items.items() if isinstance(v, dict)}

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if not (self._dirty or force):
                return
            payload = {"schema": self.schema, "saved": time.time(), "items": self._data}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False
            except OSError:
                pass

    # ---------- API ----------
    def get(self, key: str, default: Any = None, max_age: float | None = None) -> Any:
        self._ensure()
        with self._lock:
            item = self._data.get(key)
        if not isinstance(item, dict):
            return default
        age_limit = self.ttl if max_age is None else max_age
        ts = float(item.get("ts") or 0)
        if age_limit and ts and (time.time() - ts) > age_limit:
            return default
        return item.get("value", default)

    def set(self, key: str, value: Any, **meta: Any) -> None:
        self._ensure()
        with self._lock:
            self._data[key] = {"ts": time.time(), "value": value, **meta}
            self._dirty = True

    def delete(self, key: str) -> bool:
        self._ensure()
        with self._lock:
            ok = self._data.pop(key, None) is not None
            self._dirty = self._dirty or ok
            return ok

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._dirty = True
        self.flush(force=True)

    def items(self) -> dict[str, dict]:
        self._ensure()
        with self._lock:
            return dict(self._data)

    def age_of(self, key: str) -> float | None:
        self._ensure()
        with self._lock:
            item = self._data.get(key)
        if not isinstance(item, dict):
            return None
        return time.time() - float(item.get("ts") or 0)

    def is_stale(self, key: str, max_age: float) -> bool:
        age = self.age_of(key)
        return age is None or age > max_age


class CacheHub:
    """Сборник кэшей агента: единая точка для кода и UI."""

    def __init__(self, state_dir: Path) -> None:
        state_dir = Path(state_dir)
        self.memory = TTLCache()
        self.short = TTLCache(default_ttl=30.0, max_items=256)      # окна/процессы/курсор
        self.apps = JsonCache(state_dir / "cache_apps.json", schema=2, ttl=LONG_TTL)
        self.routes = JsonCache(state_dir / "cache_routes.json", schema=1, ttl=30 * 24 * 3600.0)
        self.misc = JsonCache(state_dir / "cache_misc.json", schema=1, ttl=LONG_TTL)
        self._pairs: tuple[tuple[str, TTLCache | JsonCache], ...] = (
            ("memory", self.memory), ("short", self.short), ("apps", self.apps),
            ("routes", self.routes), ("misc", self.misc),
        )

    # ---- прокси к «долговременному» кэшу (обнаружение, пути, фичи) ----
    def get(self, key: str, default: Any = None, max_age: float | None = None) -> Any:
        return self.apps.get(key, default, max_age=max_age)

    def set(self, key: str, value: Any, **meta: Any) -> None:
        self.apps.set(key, value, **meta)

    def is_stale(self, key: str, max_age: float) -> bool:
        return self.apps.is_stale(key, max_age)

    def flush(self) -> None:
        for _, c in self._pairs:
            if isinstance(c, JsonCache):
                c.flush()

    def clear_all(self) -> None:
        for name, c in self._pairs:
            if isinstance(c, JsonCache):
                c.clear()
            else:
                c.invalidate()

    def stats(self) -> dict:
        out: dict[str, Any] = {}
        for name, c in self._pairs:
            if isinstance(c, JsonCache):
                out[name] = {"items": len(c.items()), "kind": "disk"}
            else:
                out[name] = {**c.stats(), "kind": "memory"}
        return out
