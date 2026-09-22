"""Системные папки и разбор путей из естественной речи (ТЗ §34).

«рабочий стол» → ~/Desktop, «загрузки» → ~/Downloads, «документы» → ~/Documents,
«картинки» → ~/Pictures, «музыка» → ~/Music, «видео» → ~/Videos.

Дополнительно решается главная практическая задача: понять формулировку
«удали папку 123 с рабочего стола» — то есть вытащить имя объекта, место и
собрать настоящий путь; если имени нет в папке, включить нечёткий поиск
(«удали папку с отчётами» → «Отчёты за 2026»).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from ..apps.aliases import normalize, similarity

# Место → (имя переменной окружения Windows, имя папки внутри профиля)
PLACES: dict[str, tuple[str, str]] = {
    "рабочий стол": ("", "Desktop"),
    "рабочем столе": ("", "Desktop"),
    "рабочего стола": ("", "Desktop"),
    "десктоп": ("", "Desktop"),
    "desktop": ("", "Desktop"),
    "документы": ("", "Documents"),
    "документах": ("", "Documents"),
    "мои документы": ("", "Documents"),
    "documents": ("", "Documents"),
    "загрузки": ("", "Downloads"),
    "загрузках": ("", "Downloads"),
    "загрузок": ("", "Downloads"),
    "downloads": ("", "Downloads"),
    "изображения": ("", "Pictures"),
    "изображениях": ("", "Pictures"),
    "картинки": ("", "Pictures"),
    "картинках": ("", "Pictures"),
    "pictures": ("", "Pictures"),
    "музыка": ("", "Music"),
    "музыке": ("", "Music"),
    "music": ("", "Music"),
    "видео": ("", "Videos"),
    "видеофайлы": ("", "Videos"),
    "videos": ("", "Videos"),
    "профиль": ("USERPROFILE", ""),
    "домашняя папка": ("USERPROFILE", ""),
    "домашний каталог": ("USERPROFILE", ""),
    "home": ("USERPROFILE", ""),
    "скачивания": ("", "Downloads"),
}

_PLACE_RE = re.compile(
    r"\b(на|в|из|с|со|по)\s+(" + "|".join(sorted((re.escape(k) for k in PLACES), key=len, reverse=True)) + r")\b",
    re.I)

_QUOTED_RE = re.compile(r"[\"'«»“”]([^\"'«»“”]{1,120})[\"'«»“”]")

_EXT_RE = re.compile(r"\b[\w\-. ]{1,80}\.(txt|md|py|json|csv|docx?|xlsx?|pptx?|pdf|png|jpe?g|gif|webp|mp[34]|avi|mkv|zip|rar|7z|exe|lnk|html?|xml|ya?ml|ini|log|bat|cmd|ps1|sh)\b", re.I)


def _profile() -> Path:
    return Path(os.path.expanduser("~"))


def system_dirs() -> dict[str, Path]:
    """Известные системные папки (существующие)."""
    home = _profile()
    out: dict[str, Path] = {"home": home}
    mapping = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
               "pictures": "Pictures", "music": "Music", "videos": "Videos"}
    if os.name == "nt":
        # OneDrive-профили часто держат рабочий стол в облаке
        onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        for key, folder in mapping.items():
            direct = home / folder
            cloud = Path(onedrive) / folder if onedrive else None
            out[key] = cloud if (cloud and cloud.is_dir() and not direct.is_dir()) else direct
    else:
        try:
            import subprocess
            for key in mapping:
                r = subprocess.run(["xdg-user-dir", key.upper()], capture_output=True,
                                   text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip() and Path(r.stdout.strip()).is_dir():
                    out[key] = Path(r.stdout.strip())
                    continue
                out[key] = home / mapping[key]
        except Exception:
            for key, folder in mapping.items():
                out[key] = home / folder
    out.setdefault("temp", Path(os.environ.get("TEMP") or "/tmp"))
    out.setdefault("projects", _first_existing(home / "source" / "repos", home / "Projects",
                                               home / "Documents" / "Projects") or home / "Projects")
    return out


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.is_dir():
            return p
    return None


def place_dir(place: str) -> Path | None:
    """Каталог по названию места («рабочий стол» → ~/Desktop)."""
    key = normalize(place)
    if key not in PLACES:
        # нечётко: «рабочему столу» → «рабочий стол»
        best = max(((k, similarity(key, k)) for k in PLACES), key=lambda kv: kv[1], default=("", 0.0))
        if best[1] < 0.75:
            return None
        key = best[0]
    env_name, folder = PLACES[key]
    if env_name == "USERPROFILE" or (not folder and not env_name):
        return _profile()
    if env_name and os.environ.get(env_name):
        p = Path(os.environ[env_name]) / folder if folder else Path(os.environ[env_name])
        return p
    dirs = system_dirs()
    mapped = {"Desktop": "desktop", "Documents": "documents", "Downloads": "downloads",
              "Pictures": "pictures", "Music": "music", "Videos": "videos"}
    return dirs.get(mapped.get(folder, ""), _profile() / folder) if folder else _profile()


def find_place(text: str) -> tuple[Path | None, str]:
    """Находит упоминание места в фразе. Возвращает (путь, название места)."""
    m = _PLACE_RE.search(text or "")
    if not m:
        return None, ""
    place = m.group(2)
    return place_dir(place), place


def is_absolute_like(text: str) -> bool:
    t = (text or "").strip().strip('"«»')
    if not t:
        return False
    if t.startswith(("~", "/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", t):
        return True
    return os.sep in t and len(t) > 2


def quoted(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTED_RE.finditer(text or "")]


def looks_like_file(text: str) -> bool:
    return bool(_EXT_RE.search(text or ""))


def clean_name(name: str) -> str:
    """Убирает служебные слова из названия объекта."""
    n = re.sub(r"\s+", " ", (name or "").strip().strip("\"'«»"))
    n = re.sub(r"^(папк[уиа]?|каталог|директори[юия]|файл|документ|ярлык|todo)\s+", "", n, flags=re.I)
    n = re.sub(r"\s+(папк[уа]?|каталог|директори[юия]?|файл|документ)$", "", n, flags=re.I)
    return n.strip(" .,;:")


def resolve_path(text: str, base: Path | None = None, default_place: str | None = None,
                 fuzzy: bool = True) -> tuple[Path | None, str]:
    """Собирает путь из естественной фразы.

    «папку 123 с рабочего стола» → (~/Desktop/123, «рабочий стол»)
    «report.docx в документах»   → (~/Documents/report.docx)
    «D:\\work\\app»              → как есть
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""
    place, place_label = find_place(raw)
    # прямой путь?
    cand = raw.strip().strip("\"'«»")
    if is_absolute_like(cand):
        p = Path(os.path.expanduser(os.path.expandvars(cand)))
        return p, ""
    names = quoted(raw)
    name = ""
    if names:
        name = clean_name(names[0])
    else:
        # всё, что осталось после удаления глаголов/предлогов/места
        t = _PLACE_RE.sub(" ", raw)
        t = re.sub(r"\b(удали|удалить|сотри|создай|создать|сделай|сделать|открой|открыть|"
                   r"перемести|переместить|скопируй|скопировать|переименуй|переименовать|"
                   r"прочитай|прочитать|покажи|показать|найди|найти|в|во|на|из|с|со|мне|"
                   r"пожалуйста|папку|папка|папке|файл|файла|файле|документ|каталог|"
                   r"директорию|ярлык|который|которая|лежит|находится)\b", " ", t, flags=re.I)
        name = clean_name(t)
    if not name:
        return None, place_label
    target_base = base or place or (place_dir(default_place) if default_place else None) or Path.cwd()
    p = target_base / name
    if p.exists() or not fuzzy:
        return p, place_label
    # 1) ищем внутри выбранного места нечётко
    if fuzzy and (place or base):
        found = find_similar(target_base, name)
        if found is not None:
            return found, place_label
    # 2) ищем в других известных местах
    if fuzzy:
        for key, d in system_dirs().items():
            if not d.is_dir():
                continue
            found = find_similar(d, name, threshold=0.86)
            if found is not None:
                return found, key
    return p, place_label


def find_similar(folder: Path, name: str, threshold: float = 0.72) -> Path | None:
    """Нечёткий поиск файла/папки в каталоге («отчёт» → «Отчёты за 2026»)."""
    if not folder or not folder.is_dir():
        return None
    try:
        items = list(folder.iterdir())
    except OSError:
        return None
    norm = normalize(name)
    best: tuple[Path | None, float] = (None, 0.0)
    for it in items:
        stem = it.stem if it.is_file() else it.name
        r = similarity(norm, normalize(stem))
        if r > best[1]:
            best = (it, r)
    if best[0] is not None and best[1] >= threshold:
        return best[0]
    return None


def ensure_suffix(name: str, suffix: str = ".txt") -> str:
    return name if Path(name).suffix else name + suffix


def list_places() -> Iterable[tuple[str, Path]]:
    for key, path in system_dirs().items():
        yield key, path
