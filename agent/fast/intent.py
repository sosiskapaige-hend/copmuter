"""Intent Engine — распознавание намерения БЕЗ обращения к модели (ТЗ §6, §36, §1 прил.).

«запусти телегу» должно превращаться в `launch_application(Telegram)` за
микросекунды, а не после «подумаю-ка я 20 секунд». Для этого:

  * нормализация фразы (регистр, ё→е, пунктуация) + словарь синонимов глаголов;
  * упорядоченный набор правил (regex + слоты) на русском и английском;
  * разрешение названий приложений через реестр (алиасы + нечёткое сравнение:
    «телега», «тг», «вс код», «проводник»);
  * разбор составных команд («открой VS Code и напиши калькулятор») на части;
  * память формулировок: успешно выполненные команды запоминаются и в следующий
    раз распознаются мгновенно (ТЗ §27 «обучение на действиях»).

Движок детерминированный и дешёвый: полный разбор ≈ 10–100 микросекунд.
Если уверенности мало — честно возвращается `agent_task`, и решение принимает
модель (безопасность важнее скорости).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..apps.aliases import normalize
from ..system import paths as path_tools

# --------------------------------------------------------------------------
#  Синонимы действий (для распознавания и для разбора составных команд)
# --------------------------------------------------------------------------
OPEN_VERBS = r"(?:открой|открыть|запусти|запустить|включи|вруби|стартани|подними|загрузи|запуск|open|launch|start|run)"
MAKE_VERBS = r"(?:создай|создать|сделай|сделать|сформируй|организуй|new|create|make)"
FIND_VERBS = r"(?:найди|найти|поищи|ищи|поискать|search|find)"
WRITE_VERBS = r"(?:напиши|написать|напечатай|введи|ввести|write|type)"

VERB_START_RE = re.compile(
    r"^(?:открой|открыть|запусти|запустить|включи|вруби|стартани|подними|"
    r"создай|создать|сделай|сделать|найди|найти|поищи|напиши|напечатай|введи|"
    r"удали|сотри|скопируй|перемести|переименуй|перейди|зайди|покажи|прочитай|"
    r"выключи|перезагрузи|заблокируй|выполни|закрой|убей|заверши|поставь|смени|"
    r"поменяй|установи|посмотри|проверь|нажми|кликни|щёлкни|прибавь|убавь|"
    r"open|launch|start|run|create|make|find|search|write|type|delete|copy|move|"
    r"set|show|close|kill|screenshot|mute)\b", re.I)

CUT_RE = re.compile(
    r"\s*(?:,\s*|\s+и\s+|\s+затем\s+|\s+потом\s+|\s+после\s+этого\s+|\s+после\s+чего\s+|\s+а\s+также\s+)",
    re.I)

CHAT_START_RE = re.compile(
    r"^(что|кто|где|когда|почему|зачем|как|сколько|какой|какая|какие|чем|расскажи|"
    r"объясни|подскажи|посоветуй|привет|здравствуй|hi|hello|what|who|where|why|how|"
    r"when|which|tell me|explain)\b", re.I)

# Слова, после которых задача почти наверняка требует модели (планирование)
COMPLEX_MARKERS = (
    "установи", "скачай и установи", "разберись", "разберись почему", "настрой ",
    "если ", "пока не", "проанализируй", "сравни", "исследуй", "разложи по",
    "собери информацию", "найди и исправь", "оптимизируй", "почини", "напиши отчёт",
    "составь план", "выясни", "проверь и", "автоматизируй", "перепиши все",
)

CONFIRMABLE_POWER = {"shutdown", "restart", "sleep", "logoff"}


@dataclass
class Intent:
    name: str
    slots: dict = field(default_factory=dict)
    confidence: float = 0.0
    raw: str = ""
    source: str = "rule"          # rule | registry | memory | fallback
    parts: list["Intent"] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "slots": self.slots,
                "confidence": round(self.confidence, 3), "source": self.source,
                "reason": self.reason,
                "parts": [p.to_dict() for p in self.parts]}

    # ---------- человекочитаемые подписи (для UI/логов) ----------
    def label(self) -> str:
        key = self.slots.get("app_key")
        target = self.slots.get("target") or self.slots.get("query") or key or ""
        human = {
            "launch_app": "Открываю",
            "open_url": "Открываю сайт",
            "open_folder": "Открываю папку",
            "open_path": "Открываю файл",
            "web_search": "Ищу в интернете",
            "youtube_search": "Ищу видео",
            "create_folder": "Создаю папку",
            "create_file": "Создаю файл",
            "write_file": "Записываю в файл",
            "read_file": "Читаю файл",
            "delete_path": "Удаляю",
            "move_path": "Перемещаю",
            "copy_path": "Копирую",
            "rename_path": "Переименовываю",
            "list_dir": "Смотрю папку",
            "find_files": "Ищу файлы",
            "system_power": "Выполняю системное действие",
            "volume": "Меняю звук",
            "screenshot": "Делаю снимок экрана",
            "set_wallpaper": "Меняю обои",
            "send_keys": "Нажимаю",
            "type_text": "Печатаю текст",
            "click_element": "Ищу элемент на экране",
            "code_task": "Пишу код",
            "run_command": "Выполняю команду",
            "kill_process": "Закрываю программу",
            "clipboard_get": "Читаю буфер обмена",
            "clipboard_set": "Пишу в буфер обмена",
            "show_desktop": "Показываю рабочий стол",
            "chat": "Отвечаю",
            "agent_task": "Думаю над задачей",
        }.get(self.name, self.name)
        if self.name == "launch_app":
            return f"{human} {self.slots.get('app_display') or target}"
        if self.name in ("open_url", "open_folder", "open_path", "delete_path", "create_folder",
                         "create_file", "move_path", "copy_path", "rename_path"):
            return f"{human}: {target}"
        if self.name == "youtube_search":
            return f"{human}: {self.slots.get('query')}"
        if self.name == "web_search":
            return f"{human}: {self.slots.get('query')}"
        if self.name == "send_keys":
            return f"{human}: {self.slots.get('keys')}"
        if self.name == "code_task":
            return f"{human}: {self.slots.get('what')} ({self.slots.get('language')})"
        return human


class IntentMemory:
    """Память формулировок: удачные команды распознаются моментально (ТЗ §27)."""

    MAX = 500

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._items: dict[str, dict] = {}
        self._dirty = False
        self.load()

    def get(self, phrase: str) -> dict | None:
        item = self._items.get(normalize(phrase))
        if not item:
            return None
        item["hits"] = int(item.get("hits", 0)) + 1
        item["last"] = time.time()
        self._dirty = True
        return item

    def remember(self, phrase: str, intent: Intent) -> None:
        key = normalize(phrase)
        if not key or len(key) < 3:
            return
        self._items[key] = {"intent": intent.to_dict(), "ts": time.time(), "hits": 0}
        if len(self._items) > self.MAX:
            items = sorted(self._items.items(), key=lambda kv: -float(kv[1].get("hits", 0)))
            self._items = dict(items[: self.MAX])
        self._dirty = True

    def forget(self, phrase: str) -> bool:
        ok = self._items.pop(normalize(phrase), None) is not None
        self._dirty = self._dirty or ok
        return ok

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self._items = data["items"]

    def save(self, force: bool = False) -> None:
        if not self.path or not (force or self._dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"schema": 1, "items": self._items},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
            self._dirty = False
        except OSError:
            pass

    def size(self) -> int:
        return len(self._items)


@dataclass
class Rule:
    name: str
    pattern: re.Pattern
    confidence: float = 0.9
    extract: Callable[[re.Match, str], dict] | None = None
    guard: Callable[[str, re.Match], bool] | None = None
    reason: str = ""


class IntentEngine:
    def __init__(self, registry: Any = None, memory: IntentMemory | None = None,
                 cfg: Any = None) -> None:
        self.registry = registry
        self.memory = memory
        self.cfg = cfg
        self.rules: list[Rule] = self._build_rules()
        self.stats = {"parsed": 0, "memory_hits": 0, "fallbacks": 0, "compounds": 0}

    # ------------------------------------------------------------------ сборка правил
    def _build_rules(self) -> list[Rule]:
        R = Rule
        return [
            # ---------------- питание и системные действия ----------------
            R("system_power", re.compile(rf"({OPEN_VERBS}?\s*)?выключ(?:и|ить)\s+(?:компьютер|пк|ноутбук|систему|машину)\b", re.I),
              extract=lambda m, t: {"action": "shutdown"}),
            R("system_power", re.compile(r"(перезагруз(?:и|ить)|reboot|restart)\s*(?:компьютер|пк|систему|машину)?\s*$", re.I),
              extract=lambda m, t: {"action": "restart"}),
            R("system_power", re.compile(r"(заблокируй|заблокировать|залочь|lock)\s*(?:компьютер|пк|экран|сеанс)?\s*$", re.I),
              extract=lambda m, t: {"action": "lock"}),
            R("system_power", re.compile(r"(спящий режим|усыпи|уйти в сон|sleep)\s*(?:компьютер|пк)?\s*$", re.I),
              extract=lambda m, t: {"action": "sleep"}),
            R("system_power", re.compile(r"(выйди из системы|заверши сеанс|log\s?off)\s*$", re.I),
              extract=lambda m, t: {"action": "logoff"}),
            R("system_power", re.compile(r"выключ(?:и|ить)\s+(?:монитор|экран|дисплей)\b", re.I),
              extract=lambda m, t: {"action": "monitor-off"}),
            R("show_desktop", re.compile(r"(покажи|сверни)\s+(?:мне\s+)?(?:рабочий стол|все окна)\b|show desktop", re.I),
              extract=lambda m, t: {}),

            # ---------------- звук ----------------
            R("volume", re.compile(r"(выключи|отключи|убери|замуть|mute)\s+(?:звук|громкость)\b", re.I),
              extract=lambda m, t: {"action": "mute"}),
            R("volume", re.compile(r"(включи|верни|unmute)\s+(?:звук|громкость)\b", re.I),
              extract=lambda m, t: {"action": "unmute"}),
            R("volume", re.compile(r"(?:громкость|звук)\s*(?:на|=|:)?\s*(\d{1,3})\s*%?", re.I),
              extract=lambda m, t: {"action": "set", "value": int(m.group(1))}),
            R("volume", re.compile(r"(сделай|поставь)?\s*(?:звук\s+)?(громче|тише|погромче|потише|прибавь звук|убавь звук)", re.I),
              extract=lambda m, t: {"action": "up" if "е" == m.group(2)[-2:-1] or "громче" in m.group(2) or "прибавь" in m.group(2) else "down"}),

            # ---------------- экран ----------------
            R("screenshot", re.compile(r"(сделай|сними|снять|снимок|сделать|что)\s*(?:экран\s*)?(?:скриншот|снимок экрана|скрин)|^скриншот\b|screenshot", re.I),
              extract=lambda m, t: {}),
            R("set_wallpaper", re.compile(r"(поменяй|смени|поставь|установи|сделай)\s+(?:обои|обоину|фонов(?:ое изображение|ый рисунок)|wallpaper)(.*)", re.I),
              extract=lambda m, t: {"path": (m.group(2) or "").strip(" .,:")}),

            # ---------------- интернет: поиск ----------------
            R("youtube_search", re.compile(
                rf"(?:{FIND_VERBS})\s+(?:мне\s+)?(?:видео\s+)?(?:на\s+|в\s+)?(?:ютубе|ютуб|ютьюбе|youtube)\s*(?:видео)?\s*(?:про|о|об|по|насчет|про то как)?\s*(.*)", re.I),
              confidence=0.95),
            R("youtube_search", re.compile(rf"(?:{FIND_VERBS}|включи|открой)\s+видео\s+(?:про|о|об|по)\s+(.*)", re.I),
              confidence=0.8),
            R("web_search", re.compile(rf"(?:погугли|загугли|погугли-ка|google)\s+(.*)", re.I),
              confidence=0.95),
            R("web_search", re.compile(rf"{FIND_VERBS}\s+в\s+(?:интернете|сети|поиске|поисковике)\s+(.*)", re.I),
              confidence=0.9),
            R("web_search", re.compile(rf"(?:{WRITE_VERBS})\s+в\s+(?:поисковике|поиске|гугле|яндексе|строке поиска|поисковую строку)\s*(.*)", re.I),
              confidence=0.9),
            R("web_search", re.compile(rf"{FIND_VERBS}\s+(?:в\s+)?(?:гугл|гугле|яндекс|яндексе|google|yandex|duckduckgo)\s+(.*)", re.I),
              confidence=0.88),
            R("web_search", re.compile(r"^поиск[:\s]+(.*)", re.I), confidence=0.8),

            # ---------------- интернет: сайты и папки ----------------
            R("open_url", re.compile(rf"{OPEN_VERBS}\s+(?:сайт|страниц[уеы]|ссылк[уи]|website|url)\s+(.*)", re.I),
              confidence=0.95),
            R("open_url", re.compile(rf"(?:зайди|перейди|перейти|go)\s+(?:на\s+|в\s+)?([\w\-]+\.[a-zA-Zа-я]{2,}(?:/\S*)?)", re.I),
              confidence=0.95),
            R("open_url", re.compile(rf"{OPEN_VERBS}\s+(?:на\s+|в\s+)?([\w\-]+\.(?:com|ru|org|net|io|dev|me|ua|by|kz)(?:/\S*)?)", re.I),
              confidence=0.92),
            R("open_url", re.compile(
                rf"{OPEN_VERBS}\s+(?:на\s+|в\s+)?(ютуб\w*|youtube|гугл|google|яндекс|yandex|вк|vk|вконтакте|"
                rf"github|гитхаб|википеди\w*|wikipedia|почт\w+|gmail|reddit|duckduckgo|twitch|"
                rf"кинопоиск|озон|ozon|wildberries|авито|avito|map\w*|карт\w+)\s*$", re.I),
              confidence=0.9),
            R("open_folder", re.compile(rf"{OPEN_VERBS}\s+(?:папку|каталог|директорию|folder)\s+(.*)", re.I),
              confidence=0.9),
            R("open_folder", re.compile(
                rf"{OPEN_VERBS}\s+(?:на\s+|в\s+)?(загрузки|рабочий стол|документы|картинки|"
                rf"изображения|музыку|видео|домашнюю папку|домашний каталог)\b", re.I),
              confidence=0.92),
            R("launch_app", re.compile(
                rf"{OPEN_VERBS}\s+(?:на\s+|в\s+)?(проводник|мои файлы|этот компьютер|мой компьютер|"
                rf"корзину|панель управления|реестр|диспетчер устройств|управление дисками)\b", re.I),
              confidence=0.92),

            # ---------------- файлы ----------------
            R("create_folder", re.compile(rf"{MAKE_VERBS}\s+(?:новую\s+)?(?:папку|каталог|директорию|folder)\s+(.*)", re.I),
              confidence=0.95),
            R("create_folder", re.compile(rf"{MAKE_VERBS}\s+(?:на\s+(?:рабочем столе|столе)|в\s+(?:\S+))\s+(?:папку|каталог)\s+(.*)", re.I),
              confidence=0.9),
            R("create_file", re.compile(rf"{MAKE_VERBS}\s+(?:новый\s+)?файл\s+(.*)", re.I),
              confidence=0.9),
            R("create_file", re.compile(rf"{MAKE_VERBS}\s+(?:новый\s+)?файл\s+(.+?)\s+(?:с\s+текстом|с\s+содержимым|и\s+запиши|и\s+напиши|текст)\s+(.+)", re.I),
              confidence=0.92),
            R("write_file", re.compile(r"(?:запиши|сохрани|добавь)\s+(?:в\s+)?файл\s+(.*?)\s+(?:текст|строку|содержимое|данные)\s+(.*)", re.I),
              confidence=0.9),
            R("read_file", re.compile(r"(?:прочитай|прочитать|покажи содержимое|что в файле|открой и покажи)\s+(?:файл\s+)?(.*)", re.I),
              confidence=0.85),
            R("list_dir", re.compile(r"(?:что в папке|что находится в папке|покажи содержимое папки|список файлов в|покажи папку)\s+(.*)", re.I),
              confidence=0.9),
            R("find_files", re.compile(rf"{FIND_VERBS}\s+(?:файл|файлы|папку|папки)\s+(.*)", re.I),
              confidence=0.85),
            R("delete_path", re.compile(r"(?:удали|удалить|сотри|стереть|delete)\s+(.*)", re.I),
              confidence=0.9),
            R("move_path", re.compile(r"(?:перемести|переместить|move)\s+(.*?)\s+(?:в|во|into)\s+(.*)", re.I),
              confidence=0.9),
            R("copy_path", re.compile(r"(?:скопируй|скопировать|copy)\s+(.*?)\s+(?:в|во|into)\s+(.*)", re.I),
              confidence=0.9),
            R("rename_path", re.compile(r"(?:переименуй|переименовать|rename)\s+(.*?)\s+(?:в|во|на)\s+(.*)", re.I),
              confidence=0.9),
            R("open_path", re.compile(rf"{OPEN_VERBS}\s+(?:файл|документ|file)\s+(.*)", re.I),
              confidence=0.85),

            # ---------------- буфер обмена ----------------
            R("clipboard_get", re.compile(r"(?:что|прочитай|покажи)\s+(?:сейчас\s+)?в\s+буфере(?:\s+обмена)?", re.I),
              extract=lambda m, t: {}),
            R("clipboard_set", re.compile(r"(?:положи|скопируй|запиши)\s+в\s+буфер(?:\s+обмена)?\s*(.*)", re.I),
              confidence=0.85),

            # ---------------- терминал и процессы ----------------
            R("run_command", re.compile(rf"(?:выполни|запусти|исполни)\s+команду\s+(.*)", re.I),
              confidence=0.9),
            R("run_command", re.compile(r"(?:выполни|исполни)\s+(.*?)\s+в\s+терминале", re.I),
              confidence=0.85),
            R("kill_process", re.compile(rf"(?:закрой|заверши|убей|kill)\s+(?:процесс|приложение|программу)\s+(.*)", re.I),
              confidence=0.85),
            R("kill_process", re.compile(rf"(?:закрой|заверши|убей|close|kill)\s+(?!окн\w|все|вкладк\w|документ|файл)([\w\-. ]{{2,40}})\s*$", re.I),
              confidence=0.78),

            # ---------------- генерация кода ----------------
            R("code_task", re.compile(
                rf"(?:{WRITE_VERBS}|{MAKE_VERBS}|сгенерируй)\s+(?:мне\s+)?(?:код\s+|программу\s+|скрипт\s+)?(.*?)\s+на\s+(python|питоне|питон|javascript|js|typescript|ts|java|kotlin|c\+\+|cplusplus|c#|csharp|go|golang|rust|html|css|php|ruby|bash|swift)\b", re.I),
              confidence=0.9),
            R("code_task", re.compile(
                rf"(?:{WRITE_VERBS}|{MAKE_VERBS})\s+(?:мне\s+)?(калькулятор|программу|скрипт|сайт|бота|таймер|"
                rf"телеграм[- ]?бота|парсер|игру|todo|заметк\w+)\b(?:\s+(.*))?", re.I),
              confidence=0.8),

            # ---------------- ввод: клавиши, текст, клики ----------------
            R("send_keys", re.compile(
                r"(?:нажми|нажать|press)\s+(?:клавиш[уи]|сочетание|hotkey)?\s*"
                r"((?:ctrl|control|alt|shift|win|cmd|meta|fn)(?:\s*\+\s*[\wа-яё]+)+|"
                r"enter|escape|esc|tab|space|пробел|backspace|delete|del|home|end|pageup|pagedown|"
                r"f[1-9]|f1[0-2]|up|down|left|right|вверх|вниз|влево|вправо)$", re.I),
              confidence=0.9),
            R("click_element", re.compile(
                r"(?:нажми|кликни|щёлкни|тыкни|click)\s+(?:на\s+)?(кнопк[уеаи]|ссылк[уе]|иконк[уе]|элемент|пункт|"
                r"поле|вкладк[уе]|меню|чекбокс|галочку)?\s*(.*)", re.I),
              confidence=0.85),
            R("type_text", re.compile(rf"{WRITE_VERBS}\s+в\s+([\w\-. ]+?)\s+(?:текст\s+|текст:\s*)?(.+)", re.I),
              confidence=0.8),
            R("type_text", re.compile(rf"(?:напечатай|введи текст|напиши текст)\s+(.*)", re.I),
              confidence=0.8),

            # ---------------- запуск приложений (после всех уточняющих правил) ----------------
            R("launch_app", re.compile(rf"{OPEN_VERBS}\s+(?:мне\s+)?(?:приложение|программу|игру|утилиту|app|program)?\s*(.*)", re.I),
              confidence=0.9),
            R("launch_app", re.compile(r"^(?:браузер|проводник|терминал|блокнот|калькулятор|настройки|"
                                       r"диспетчер задач|телеграм|телега|дискорд|хром|код)$", re.I),
              confidence=0.85),
        ]

    # ------------------------------------------------------------------ разбор
    def parse(self, text: str, use_memory: bool = True) -> Intent:
        raw = (text or "").strip()
        self.stats["parsed"] += 1
        if not raw:
            return Intent("agent_task", {}, 0.0, raw, source="fallback")
        norm = normalize(raw)

        # 0) точное совпадение с выученной формулировкой
        if use_memory and self.memory is not None:
            hit = self.memory.get(norm)
            if hit and isinstance(hit.get("intent"), dict):
                self.stats["memory_hits"] += 1
                d = hit["intent"]
                return Intent(d.get("name", "agent_task"), dict(d.get("slots") or {}),
                              0.99, raw, source="memory", reason="learned")

        # 1) составная команда
        parts_text = self.split_compound(raw)
        if len(parts_text) > 1:
            parts = [self.parse(p, use_memory=False) for p in parts_text]
            if all(p.name not in ("agent_task",) for p in parts):
                self.stats["compounds"] += 1
                conf = min(p.confidence for p in parts)
                return Intent("compound", {"count": len(parts)}, conf, raw,
                              source="rule", parts=parts, reason="compound")
            # часть непонятна — пусть решает модель, но сохраним подсказки
            return Intent("agent_task", {"parts": [p.to_dict() for p in parts]},
                          min(p.confidence for p in parts), raw, source="fallback",
                          reason="compound_part_unknown")

        # 2) правила
        for rule in self.rules:
            m = rule.pattern.search(raw)
            if not m:
                continue
            if rule.guard and not rule.guard(raw, m):
                continue
            slots = rule.extract(m, raw) if rule.extract else self._default_slots(rule.name, m, raw)
            intent = self._finalize(rule, slots, raw)
            if intent is not None:
                return intent

        # 3) вопрос/беседа без действия
        if CHAT_START_RE.search(raw) and not VERB_START_RE.search(raw):
            return Intent("chat", {"text": raw}, 0.7, raw, source="rule", reason="question")

        # 4) неизвестно — решает модель
        self.stats["fallbacks"] += 1
        return Intent("agent_task", {"text": raw}, 0.3, raw, source="fallback",
                      reason="no_rule_matched")

    # ------------------------------------------------------------------ слоты
    def _default_slots(self, name: str, m: re.Match, raw: str) -> dict:
        groups = [g for g in m.groups() if g]
        tail = groups[-1].strip(" .,:;") if groups else ""
        slots: dict[str, Any] = {}
        if name in ("youtube_search", "web_search"):
            slots["query"] = tail or raw
            slots["engine"] = "youtube" if name == "youtube_search" else "google"
        elif name == "open_url":
            slots["url"] = tail or raw
        elif name in ("open_folder", "open_path", "read_file", "list_dir", "create_folder",
                      "create_file", "delete_path", "find_files"):
            slots["target"] = tail or raw
        elif name in ("move_path", "copy_path", "rename_path"):
            g = [x for x in m.groups() if x]
            slots["src"] = g[-2].strip() if len(g) >= 2 else ""
            slots["dst"] = g[-1].strip() if len(g) >= 2 else ""
        elif name == "write_file":
            g = [x for x in m.groups() if x]
            slots["target"] = g[-2].strip() if len(g) >= 2 else ""
            slots["content"] = g[-1] if len(g) >= 2 else ""
        elif name == "launch_app":
            slots["target"] = tail or raw
        elif name == "type_text":
            slots["text"] = tail or raw
        elif name == "kill_process":
            slots["name"] = tail or raw
        elif name == "run_command":
            slots["command"] = tail or raw
        elif name == "send_keys":
            slots["keys"] = tail
        elif name == "click_element":
            slots["description"] = tail or raw
        elif name == "clipboard_set":
            slots["text"] = tail
        return slots

    def _finalize(self, rule: Rule, slots: dict, raw: str) -> Intent | None:
        """Дополняет слоты (реестр приложений, пути) и проверяет применимость."""
        name = rule.name
        conf = rule.confidence

        if name == "launch_app":
            target = str(slots.get("target") or "").strip(" .,:;")
            if not target:
                return None
            res = self.registry.find(target) if self.registry is not None else None
            if res is not None and res.ok:
                slots["app_key"] = res.record.key
                slots["app_display"] = res.record.display_name
                slots["target"] = target
                conf = min(0.98, max(conf, res.score))
            else:
                # приложение не опознано: отдаём модели (возможно, это не приложение)
                if looks_like_url(target):
                    return Intent("open_url", {"url": target}, 0.8, raw, source="rule")
                if self.registry is not None:
                    sugg = self.registry.suggest(target, limit=3)
                    if sugg:
                        key, disp, score = sugg[0]
                        if score >= 0.75:
                            slots["app_key"], slots["app_display"] = key, disp
                            return Intent(name, slots, score, raw, source="registry",
                                          reason=f"suggest:{key}")
                return None

        if name in ("open_url",):
            url = str(slots.get("url") or "").strip(" .,:;")
            if not url:
                return None
            slots["url"] = url

        if name in ("youtube_search", "web_search"):
            q = str(slots.get("query") or "").strip(" .,:;")
            # «найди видео про котиков на ютубе» → запрос «котиков» (платформу не ищем)
            q = re.sub(r"\s+(?:на|в|по)\s+(?:ютуб\w*|youtube|гугл\w*|google|яндекс\w*|"
                       r"интернет\w*|сети|поиске|поисковике|википеди\w*)\s*$", "", q,
                       flags=re.I).strip(" .,:;")
            q = re.sub(r"\s+(?:ютуб\w*|youtube)\s*$", "", q, flags=re.I).strip(" .,:;")
            q = re.sub(r"^(?:видео|ролик\w*)\s+", "", q, flags=re.I).strip(" .,:;")
            q = re.sub(r"^(?:про|о|об|по|насч[её]т)\s+", "", q, flags=re.I).strip(" .,:;")
            if not q or normalize(q) in ("мне", "что-нибудь", "все"):
                return None
            slots["query"] = q

        if name == "create_file":
            target = strip_filler(str(slots.get("target") or ""))
            if not target:
                return None
            content = slots.get("content")
            m2 = re.match(r"(.+?)\s+(?:с\s+текстом|с\s+содержимым|и\s+запиши|и\s+напиши|текст)\s+(.+)",
                          target, re.I)
            if m2:
                target, content = m2.group(1).strip(), m2.group(2)
            slots["target"] = target
            if content is not None:
                slots["content"] = str(content)
            place, place_label = path_tools.find_place(raw)
            slots["place"] = place_label
            path, _ = path_tools.resolve_path(target, base=place, fuzzy=False)
            if path is None:
                return None
            slots["path"] = str(path)
            return Intent(name, slots, conf, raw, source="rule", reason=rule.name)

        if name in ("delete_path", "open_path", "read_file", "list_dir", "find_files",
                    "create_folder", "open_folder", "write_file"):
            target = strip_filler(str(slots.get("target") or ""))
            if not target:
                return None
            place, place_label = path_tools.find_place(raw)
            base = None
            if name in ("create_folder", "create_file", "write_file") and place is not None:
                base = place
            path, found_place = path_tools.resolve_path(target, base=base, fuzzy=False)
            slots["target"] = target
            slots["place"] = found_place or place_label
            if name == "find_files":
                slots["target"] = target
                slots["pattern"] = clean_pattern(target)
                place, place_label = path_tools.find_place(raw)
                slots["place"] = place_label
                return Intent(name, slots, conf, raw, source="rule", reason=rule.name)
            if name == "open_folder":
                # «открой папку загрузки» — попытка трактовать как известное место
                known = path_tools.place_dir(target)
                if known is not None and not looks_like_path(target):
                    slots["path"] = str(known)
                elif path is not None:
                    slots["path"] = str(path)
                else:
                    return None
            else:
                if path is None:
                    return None
                slots["path"] = str(path)

        if name in ("move_path", "copy_path", "rename_path"):
            src, dst = strip_filler(str(slots.get("src") or "")), strip_filler(str(slots.get("dst") or ""))
            if not src or not dst:
                return None
            sp, _ = path_tools.resolve_path(src, fuzzy=False)
            if name == "rename_path":
                dp = path_tools.resolve_path(dst, base=sp.parent if sp else None, fuzzy=False)[0]
            else:
                known = path_tools.place_dir(dst)
                if known is not None and not path_tools.is_absolute_like(dst):
                    dp = known / sp.name
                else:
                    dp = path_tools.resolve_path(dst, fuzzy=False)[0]
            if sp is None or dp is None:
                return None
            slots["src_path"], slots["dst_path"] = str(sp), str(dp)

        if name == "click_element":
            desc = str(slots.get("description") or "").strip(" .,:;")
            if not desc:
                return None
            slots["description"] = desc
            conf = max(conf, 0.7)

        if name == "send_keys":
            keys = str(slots.get("keys") or "").strip().lower()
            if not keys:
                return None
            slots["keys"] = keys

        if name == "kill_process":
            nm = str(slots.get("name") or "").strip(" .,:;")
            if not nm:
                return None
            slots["name"] = nm
            if self.registry is not None:
                res = self.registry.find(nm)
                if res.ok and res.record is not None:
                    slots["app_key"] = res.record.key
                    slots["app_display"] = res.record.display_name
                    slots["proc_names"] = res.record.proc_names()
                    conf = max(conf, max(0.8, res.score))

        if name == "code_task":
            what = str(slots.get("what") or "").strip(" .,:;")
            lang = str(slots.get("language") or "python").strip().lower()
            slots["language"] = {"питоне": "python", "питон": "python", "js": "javascript",
                                 "ts": "typescript", "cplusplus": "cpp", "c++": "cpp",
                                 "csharp": "c#", "golang": "go"}.get(lang, lang)
            slots["what"] = what or "программу"
            conf = max(conf, 0.8)

        return Intent(name, slots, conf, raw, source="rule", reason=rule.name)

    # ------------------------------------------------------------------ составные
    def split_compound(self, text: str) -> list[str]:
        """Разбор составных команд с учётом реестра приложений.

        «открой Discord и Telegram» делится, потому что Telegram — известное
        приложение; «найди видео про котиков и собак» — нет (это один запрос).
        """
        parts: list[str] = []
        last = 0
        for m in CUT_RE.finditer(text or ""):
            head = (text[last:m.start()] or "").strip(" ,;")
            tail = (text[m.end():] or "").strip(" ,;")
            if not head or not tail:
                continue
            if not VERB_START_RE.search(tail) and not self._is_bare_target(tail):
                continue
            parts.append(head)
            last = m.end()
        if not parts:
            return [text]
        parts.append((text[last:] or "").strip(" ,;"))
        parts = [p for p in parts if p]
        # Переносим глагол: «открой Discord и Telegram» → «открой Discord», «открой Telegram»
        head_verb = None
        m = VERB_START_RE.search(parts[0] if parts else "")
        if m:
            head_verb = m.group(0).strip()
        if head_verb:
            for i in range(1, len(parts)):
                if not VERB_START_RE.search(parts[i]):
                    parts[i] = f"{head_verb} {parts[i]}"
        return parts

    def _is_bare_target(self, tail: str) -> bool:
        """«Telegram» / «телегу» / «браузер» без глагола — это тоже команда открытия."""
        if self.registry is None or len(tail) > 40:
            return False
        if re.search(r"\b(про|о|об|для|чтобы|котор\w+|как|что)\b", tail, re.I):
            return False
        res = self.registry.find(tail)
        return bool(res.ok and res.record is not None)

    # ------------------------------------------------------------------ обучение
    def remember_success(self, text: str, intent: Intent) -> None:
        if self.memory is None or intent.name in ("agent_task", "chat", "compound"):
            return
        self.memory.remember(text, intent)

    def save(self) -> None:
        if self.memory is not None:
            self.memory.save()

    def stats_summary(self) -> dict:
        return {**self.stats, "rules": len(self.rules),
                "learned": self.memory.size() if self.memory else 0}


# --------------------------------------------------------------------------
#  Составные команды
# --------------------------------------------------------------------------
def split_compound(text: str) -> list[str]:
    """«открой VS Code и напиши калькулятор» → две команды.

    Делим только там, где справа начинается НОВОЕ действие — иначе «найди видео
    про котиков и собак» развалилось бы на бессмысленные части.
    """
    t = (text or "").strip()
    if not t:
        return []
    parts: list[str] = []
    last = 0
    for m in CUT_RE.finditer(t):
        head = t[last:m.start()].strip(" ,;")
        tail = t[m.end():].strip(" ,;")
        if not head or not tail:
            continue
        if not VERB_START_RE.search(tail):
            continue
        parts.append(head)
        last = m.end()
    if not parts:
        return [t]
    parts.append(t[last:].strip(" ,;"))
    return [p for p in parts if p]


_TRAILING_FILLER = re.compile(
    r"\s*(?:,|\s)\s*(?:через\s+[\w\-.]+|с\s+помощью\s+[\w\-.]+|при\s+помощи\s+[\w\-.]+|"
    r"используя\s+[\w\-.]+|пожалуйста|плиз|please|сам|сама|сейчас|быстро|"
    r"по\s+имени\s+[\w\-.]+)\s*$", re.I)


def strip_filler(text: str) -> str:
    """Убрать служебный хвост из имени объекта («… через веб-интерфейс»)."""
    out = (text or "").strip(" .,:;")
    for _ in range(4):
        m = _TRAILING_FILLER.search(out)
        if not m:
            break
        out = out[:m.start()].strip(" .,:;")
    return out


def clean_pattern(text: str) -> str:
    """«файлы отчёт» → «отчёт» (то, что ищем по имени)."""
    t = re.sub(r"\b(файл|файлы|файла|папк\w*|каталог\w*|документ\w*|все|всю|мне|пожалуйста|на|в)\b",
               " ", text or "", flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .,:;")


def looks_like_url(text: str) -> bool:
    t = (text or "").strip()
    if t.startswith(("http://", "https://", "www.")):
        return True
    return bool(re.match(r"^[\w\-]+\.(com|ru|org|net|io|dev|me|ua|by|kz|ai|app|xyz)(/\S*)?$", t, re.I))


def looks_like_path(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.search(r"[\\/]|^[A-Za-z]:", t)) or t.startswith("~")


def is_complex(text: str) -> bool:
    """Требует ли задача полноценного планирования моделью."""
    low = normalize(text)
    if any(marker in low for marker in COMPLEX_MARKERS):
        return True
    # длинная цепочка действий без явных глаголов-команд
    if low.count(" и ") >= 3:
        return True
    return False
