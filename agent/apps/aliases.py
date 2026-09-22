"""Умный резолвер названий приложений (ТЗ §36, §13 приложения к ТЗ).

Что решает: «запусти телегу», «открой тг», «вс код», «код», «вижуал студио»,
«проводник», «стим» → конкретные ключи реестра приложений (telegram, vscode,
explorer, steam…).

Как: словарь алиасов (русские, английские, транслит, сетевой сленг) +
нормализация + лёгкий стемминг (телегу→телег) + нечёткое сравнение
(difflib) + автообучение: если пользователь сказал новое слово, и приложение
запустилось — слово запоминается и в следующий раз срабатывает мгновенно.
"""
from __future__ import annotations

import json
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from functools import lru_cache
from typing import Iterable

# ru → lat (транслит): «телега» → «telega», «вс код» → «vs kod»
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

# Окончания русских слов, которые можно срезать при поиске (телегу → телег)
_RU_ENDINGS = ("иями", "ями", "ами", "ией", "иях", "ов", "ев", "ей", "ой", "ый", "ий",
               "ах", "ях", "ам", "ям", "ию", "ия", "ье", "ья", "ы", "и", "у", "ю", "а",
               "е", "о", "я", "ь")

# Слова, которые не являются названием приложения
STOPWORDS = {
    "открой", "открыть", "запусти", "запустить", "включи", "вруби", "стартани", "покажи",
    "зайди", "перейди", "запуск", "мне", "пожалуйста", "плиз", "быстро", "окно", "окошко",
    "программу", "программа", "приложение", "прилу", "апп", "приложеньку", "меня",
    "open", "launch", "start", "run", "please", "the", "app", "application", "program",
    "на", "в", "по", "и", "мне", "надо", "хочу", "нужно", "давай", "сейчас", "бы",
}

_SPLIT_RE = re.compile(r"[^\w\s.+/\\:\-]+", re.UNICODE)


@lru_cache(maxsize=50000)
def normalize(text: str) -> str:
    """Нижний регистр, ё→е, без лишней пунктуации, одиночные пробелы."""
    s = (text or "").strip().lower().replace("ё", "е")
    s = _SPLIT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=50000)
def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in normalize(text))


@lru_cache(maxsize=50000)
def stem(word: str) -> str:
    """Очень лёгкий стеммер: срезает частые русские/английские окончания."""
    w = word
    if len(w) > 5:
        for end in _RU_ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 4:
                return w[: -len(end)]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str, drop_stopwords: bool = False) -> list[str]:
    toks = [t for t in normalize(text).split() if t]
    if drop_stopwords:
        toks = [t for t in toks if t not in STOPWORDS] or toks
    return toks


def levenshtein_ratio(a: str, b: str, limit: float = 0.0) -> float:
    """Быстрая похожесть 0..1 на Левенштейне с ранним выходом.

    difflib.SequenceMatcher точен, но в ~20 раз медленнее: на 800 алиасах и
    десятках правил это превращало разбор фразы в десятки миллисекунд.
    """
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    if abs(la - lb) / max(la, lb) > 1.0 - max(limit, 0.55):
        return 0.0
    if la > lb:
        a, b, la, lb = b, a, lb, la
    prev = list(range(la + 1))
    for j in range(1, lb + 1):
        cur = [j] + [0] * la
        bj = b[j - 1]
        for i in range(1, la + 1):
            cur[i] = min(prev[i] + 1, cur[i - 1] + 1,
                         prev[i - 1] + (0 if a[i - 1] == bj else 1))
        prev = cur
    dist = prev[la]
    return max(0.0, 1.0 - dist / max(la, lb))


def similarity(a: str, b: str) -> float:
    """Похожесть двух строк 0..1 (учитывает стемминг и транслит)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 0.0
    if a[0] != b[0] and len(a) > 4 and len(b) > 4:
        return 0.0
    sa, sb = stem(a), stem(b)
    if sa == sb:
        return 0.97
    best = levenshtein_ratio(a, b, limit=0.75)
    if sa != a or sb != b:
        best = max(best, levenshtein_ratio(sa, sb, limit=0.75) * 0.98)
    ta, tb = translit(a), translit(b)
    if ta != a or tb != b:
        best = max(best, levenshtein_ratio(ta, tb, limit=0.75) * 0.95)
    if len(sb) >= 4 and (sa.startswith(sb) or sb.startswith(sa)):
        best = max(best, 0.9)
    return best


class AliasResolver:
    """Алиасы → ключи приложений, с обучением и персистентностью."""

    def __init__(self, path: Path | str | None = None,
                 builtin: dict[str, Iterable[str]] | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._map: dict[str, str] = {}          # нормализованный алиас → ключ
        self._learned: dict[str, dict] = {}     # для сохранения
        self._index: dict[str, list[tuple[str, str]]] | None = None  # буква → [(алиас, ключ)]
        self._phrases: list[tuple[str, str]] = []                    # многословные алиасы
        if builtin:
            for key, aliases in builtin.items():
                self.register(key, aliases)
        self.load()

    # ---------------- регистрация ----------------
    def register(self, key: str, aliases: Iterable[str]) -> None:
        with self._lock:
            for a in list(aliases) + [key]:
                n = normalize(a)
                if n:
                    self._map.setdefault(n, key)
                t = translit(a)
                if t and t != n:
                    self._map.setdefault(t, key)
            self._index = None

    def learn(self, phrase: str, key: str, source: str = "agent") -> bool:
        """Запомнить, что такая формулировка = это приложение."""
        n = normalize(phrase)
        if not n or len(n) < 3 or len(n) > 60:
            return False
        with self._lock:
            if self._map.get(n) == key:
                return False
            self._map[n] = key
            self._learned[n] = {"key": key, "ts": time.time(), "hits": 0,
                               "source": source}
            self._dirty = True
            self._index = None
        self.save()
        return True

    # ---------------- индекс ----------------
    def _build_index(self) -> None:
        index: dict[str, list[tuple[str, str]]] = {}
        phrases: list[tuple[str, str]] = []
        for alias, key in self._map.items():
            if " " in alias:
                phrases.append((alias, key))
                continue
            index.setdefault(alias[:1], []).append((alias, key))
        self._index = index
        self._phrases = phrases

    def _bucket(self, token: str) -> list[tuple[str, str]]:
        if self._index is None:
            self._build_index()
        assert self._index is not None
        letters = {token[:1]}
        tr = translit(token)
        if tr:
            letters.add(tr[:1])
        st = stem(token)
        if st:
            letters.add(st[:1])
        out: list[tuple[str, str]] = []
        for ch in letters:
            out.extend(self._index.get(ch, ()))
        return out

    def index_size(self) -> dict:
        if self._index is None:
            self._build_index()
        return {"aliases": len(self._map), "phrases": len(self._phrases),
                "buckets": len(self._index or {}), "learned": len(self._learned)}

    # ---------------- поиск ----------------
    def resolve(self, text: str, candidates: Iterable[str] | None = None) -> tuple[str | None, float, str]:
        """Возвращает (ключ, уверенность, чем совпало)."""
        raw = normalize(text)
        if not raw:
            return None, 0.0, ""
        with self._lock:
            m = dict(self._map)
        # 1) точное совпадение всей фразы
        if raw in m:
            return m[raw], 1.0, "exact:" + raw
        tr = translit(raw)
        if tr in m:
            return m[tr], 0.98, "translit:" + tr
        # 2) по значимым словам
        toks = tokens(raw, drop_stopwords=True)
        allowed = {normalize(c) for c in candidates} if candidates else None
        best: tuple[str | None, float, str] = (None, 0.0, "")

        def consider(key: str, score: float, why: str) -> None:
            nonlocal best
            if allowed is not None and normalize(key) not in allowed:
                return
            if score > best[1]:
                best = (key, score, why)

        for t in toks:
            if t in m:
                consider(m[t], 0.94, f"word:{t}")
            else:
                st = stem(t)
                if st in m:
                    consider(m[st], 0.9, f"stem:{st}")
        # 3) нечёткое сравнение по каждому слову и по всей фразе
        for t in toks:
            for alias, key in self._bucket(t):
                if abs(len(alias) - len(t)) > 3:
                    continue
                r = similarity(t, alias)
                if r >= 0.78:
                    consider(key, round(r * 0.88, 3), f"fuzzy:{alias}~{t}")
        if self._index is None:
            self._build_index()
        for alias, key in self._phrases:
            if alias in raw:
                consider(key, 0.86, f"phrase:{alias}")
        return best

    def suggest(self, text: str, limit: int = 3) -> list[tuple[str, float]]:
        """Варианты для UI/подсказок: [(ключ, уверенность)]."""
        scores: dict[str, float] = {}
        for t in tokens(text, drop_stopwords=True) or tokens(text):
            for alias, key in self._bucket(t):
                if abs(len(alias) - len(t)) > 3:
                    continue
                r = similarity(t, alias)
                if r >= 0.6:
                    scores[key] = max(scores.get(key, 0.0), round(r, 3))
        out = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return out

    def hit(self, phrase: str) -> None:
        n = normalize(phrase)
        with self._lock:
            item = self._learned.get(n)
            if item:
                item["hits"] = int(item.get("hits", 0)) + 1
                self._dirty = True

    # ---------------- персистентность ----------------
    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        learned = data.get("learned") if isinstance(data, dict) else None
        if not isinstance(learned, dict):
            return
        with self._lock:
            for phrase, item in learned.items():
                if isinstance(item, dict) and item.get("key"):
                    self._map[normalize(phrase)] = str(item["key"])
                    self._learned[normalize(phrase)] = item

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            payload = {"schema": 1, "saved": time.time(),
                       "learned": {k: v for k, v in self._learned.items()
                                   if int(v.get("hits", 0)) >= 0}}
            if len(payload["learned"]) > 800:      # не разрастаемся бесконечно
                items = sorted(payload["learned"].items(),
                               key=lambda kv: (-int(kv[1].get("hits", 0)), -float(kv[1].get("ts", 0))))
                payload["learned"] = dict(items[:800])
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def learned_count(self) -> int:
        with self._lock:
            return len(self._learned)

    def all_pairs(self) -> dict[str, str]:
        with self._lock:
            return dict(self._map)
