"""Отбор инструментов под конкретную задачу (бюджет контекста).

Зачем: полный набор из ~84 схем — это ≈10k токенов на русском. Локальные
модели (LM Studio, Ollama) с контекстом 4–8k на таком payload'е либо
получают ошибку, либо сервер молча выкидывает середину промпта — и модель
«не видит» инструментов, отвечая обычным текстом.

Раньше отправлялись просто ПЕРВЫЕ 8 по алфавиту (ask_user + browser_*), что
лишало агента fs/terminal/apps/finish_task. Здесь — отбор по релевантности:

  * ядро (всегда): файлы, терминал, запуск приложений/ссылок, finish/ask;
  * явно упомянутые в цели/плане инструменты и категории (по ключевым словам);
  * инструменты, уже использованные в этой задаче, и «закреплённые» (pinned —
    модель упомянула их в рассуждениях);
  * недоступные на платформе категории (GUI без дисплея, браузер без
    Playwright) уходят в конец.
"""
from __future__ import annotations

import re
from typing import Iterable

# Всегда в наборе: покрывают 90 % бытовых задач.
CORE_TOOLS: tuple[str, ...] = (
    "finish_task", "ask_user", "notify_user", "wait",
    "fs_mkdir", "fs_write", "fs_read", "fs_list", "fs_search", "fs_move",
    "fs_copy", "fs_delete", "fs_info",
    "terminal_run", "terminal_check",
    "launch_app", "open_url", "open_path",
    "system_info", "process_list", "process_kill",
    "screen_capture", "screen_describe",
)

# Ключевые слова → категория инструментов (рус/англ, по основам слов).
CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "browser": ("сайт", "браузер", "url", "http", "страниц", "ссылк", "youtube", "ютуб",
                "google", "гугл", "яндекс", "скачай", "скачать", "вкладк", "веб", "web",
                "login", "войди", "залогин", "форм", "заполни", "таблиц с сайта", "парс"),
    "window": ("окно", "окна", "окон", "сверни", "разверни", "window", "весь экран",
               "монитор", "перемести окно", "закрой все", "закрой всё", "фокус"),
    "input": ("клик", "кликни", "нажми", "введи", "напечатай", "мыш", "клавиш", "горяч",
              "hotkey", "перетащи", "прокрути", "скролл", "выдели", "ctrl", "alt"),
    "clipboard": ("буфер", "clipboard", "скопируй текст", "вставь"),
    "git": ("git", "гит", "репозитор", "клонируй", "коммит", "commit", "push", "pull",
            "branch", "ветк", "github", "гитхаб"),
    "vision": ("ocr", "распознай", "текст с картинки", "изображен", "картинк", "фото",
               "что на экране", "лучш", "выбери фото"),
    "screen": ("экран", "скриншот", "снимок", "монитор"),
    "os": ("звук", "громк", "wifi", "wi-fi", "вай-фай", "bluetooth", "блютуз", "яркост",
           "микрофон", "автозагруз", "служб", "уведомлен", "выключи звук", "включи звук"),
    "system": ("тормоз", "процесс", "cpu", "память", "ram", "диск", "место", "убей",
               "kill", "загрузк", "медлен", "диагност", "почему", "зависл", "виснет"),
    "apps": ("запусти", "открой", "программ", "приложен", "закрой", "vscode", "chrome",
             "блокнот", "калькулятор", "проводник", "telegram", "телеграм", "discord",
             "steam", "spotify", "word", "excel"),
    "fs": ("файл", "папк", "директор", "каталог", "переименуй", "скопируй", "перемести",
           "удали", "найди", "архив", "zip", "распакуй", "прочитай", "напиши", "создай",
           "рассортируй", "разложи", "документ", "pdf", "docx", "xlsx", "рабочий стол",
           "desktop", "downloads", "загрузк", "бэкап", "восстанови", "верни"),
    "terminal": ("команд", "терминал", "консол", "установи", "pip", "npm", "python",
                 "скрипт", "выполни", "собери", "запусти проект", "тест"),
    "meta": ("отмени", "undo", "верни как было", "подожди", "жди"),
}

# Базовый приоритет категорий при добивке бюджета.
CATEGORY_BASE: dict[str, int] = {
    "fs": 30, "terminal": 28, "apps": 26, "meta": 24, "system": 20, "screen": 12,
    "browser": 16, "vision": 9, "git": 8, "window": 13, "input": 6, "os": 5,
    "clipboard": 4, "shell": 18,
}

# Инструменты-«синонимы»: у них есть рабочий основной аналог
# (fs_mkdir ↔ create_folder, keyboard_type ↔ type_text, open_url ↔ open_default_browser…).
# Они полезны быстрому слою и человеку, но не должны вытеснять основные инструменты
# из бюджета схем, который уходит модели.
DUPLICATE_TOOLS: frozenset[str] = frozenset({
    "create_folder", "create_file", "write_code", "save_file", "open_file", "open_folder",
    "delete_file", "delete_folder", "copy_file", "move_file", "clipboard_get",
    "clipboard_set", "kill_process", "read_processes", "search_web", "open_default_browser",
    "launch_application", "type_text", "press_key", "move_mouse", "double_click",
    "click_on_screen", "find_on_screen", "take_screenshot", "read_screen", "open_browser",
    "browser_scroll", "run_terminal_command",
})
DUPLICATE_PENALTY = 55


def _hint_hit(text: str, hint: str) -> bool:
    """Подсказка категории ищется с начала слова: «верни» не должно находиться
    внутри «сверни», «папк» — находиться в «папку» (это основа слова)."""
    return re.search(rf"(?<![а-яёa-z0-9]){re.escape(hint)}", text) is not None


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text) is not None


def find_tool_names(text: str, names: Iterable[str]) -> set[str]:
    """Имена инструментов, встречающиеся в произвольном тексте (план, мысли)."""
    low = (text or "").lower()
    return {n for n in names if _mentions(low, n)}


def select_tools(goal: str, plan_text: str, recent: Iterable[str], pinned: Iterable[str],
                 tools: list, platform=None, budget: int = 40) -> list:
    """Возвращает подмножество объектов Tool (не больше `budget`), отсортированное
    по убыванию релевантности. Если бюджет ≥ числа инструментов — вернёт все."""
    tools = list(tools)
    if budget <= 0 or budget >= len(tools):
        return tools
    text = f"{goal}\n{plan_text}".lower()
    recent = set(recent or ())
    pinned = set(pinned or ())
    has_display = bool(getattr(platform, "has_display", True))
    has_pw = bool(getattr(platform, "has_playwright", True))

    cat_hits: dict[str, int] = {}
    for cat, hints in CATEGORY_HINTS.items():
        cat_hits[cat] = sum(1 for h in hints if _hint_hit(text, h))

    scored: list[tuple[int, str, object]] = []
    for t in tools:
        s = CATEGORY_BASE.get(t.category, 0)
        if t.name in CORE_TOOLS:
            s += 100
        if _mentions(text, t.name):
            s += 95
        if t.name in pinned:
            s += 90
        if t.name in recent:
            s += 80
        s += 12 * min(cat_hits.get(t.category, 0), 4)
        if getattr(t, "is_alias", False) or t.name in DUPLICATE_TOOLS:
            s -= DUPLICATE_PENALTY
        if not has_display and t.category in ("window", "input", "screen") \
                and t.name not in ("screen_capture", "screen_describe"):
            s -= 60
        if not has_pw and t.category == "browser":
            s -= 40
        scored.append((s, t.name, t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, _, t in scored[:budget]]
