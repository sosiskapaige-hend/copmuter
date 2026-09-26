"""Защита от Prompt Injection и изоляция ненадёжного контента (ТЗ §141, §142).

Любые внешние данные (HTML страниц, прочитанные файлы, вывод команд, имена файлов)
считаются UNTRUSTED и оборачиваются в изолирующие теги. Попытки перехвата управления
(jailbreak, command override, exfiltration) распознаются и нейтрализуются.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

# Известные шаблоны prompt injection (EN и RU)
INJECTION_PATTERNS = [
    # Direct instruction overrides
    re.compile(r"(?i)\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions|rules|prompts|directions)\b"),
    re.compile(r"(?i)\b(?:забудь|игнорируй|отмени|сбрось)\s+(?:все\s+)?(?:(?:предыдущие|прошлые|системные)\s+)?(?:инструкции|правила|указания|промпты)(?:\s+системы)?\b"),
    # Roleplay / Jailbreak / DAN exploits
    re.compile(r"(?i)\b(?:you\s+are\s+now|act\s+as)\s+(?:an?\s+unrestricted|in\s+developer\s+mode|DAN|jailbreak|maintenance\s+mode)\b"),
    re.compile(r"(?i)\b(?:ты\s+теперь|действуй\s+как)\s+(?:режим\s+разработчика|без\s+ограничений|jailbreak)\b"),
    # System boundary injection
    re.compile(r"(?i)<\s*(?:system|im_start|im_end|tool_call|instructions?)[^>]*>"),
    # Exfiltration commands inside untrusted content
    re.compile(r"(?i)\b(?:send|post|curl|wget|fetch)\s+(?:all\s+)?(?:passwords?|keys?|tokens?|env|credentials?|secrets?)\s+(?:to|http)\b"),
    re.compile(r"(?i)\b(?:выведи|отправь|отошли)\s+(?:пароли|токены|ключи|переменные\s+окружения|секреты)\b"),
    # Destructive sabotage commands
    re.compile(r"(?i)\b(?:format\s+[a-z]:|rm\s+-rf\s+[/~]|del\s+/[fsq]\s+[a-z]:\\windows)\b"),
]


@dataclass
class InjectionCheckResult:
    is_injected: bool
    confidence: float
    matched_patterns: list[str] = field(default_factory=list)
    sanitized_text: str = ""


def detect_prompt_injection(text: str) -> InjectionCheckResult:
    """Проверяет текст на наличие признаков prompt injection."""
    if not text:
        return InjectionCheckResult(is_injected=False, confidence=0.0, sanitized_text="")

    matches = []
    for pat in INJECTION_PATTERNS:
        found = pat.findall(text)
        if found:
            for item in found:
                matches.append(item if isinstance(item, str) else str(item))

    if not matches:
        return InjectionCheckResult(
            is_injected=False,
            confidence=0.0,
            matched_patterns=[],
            sanitized_text=text,
        )

    # Заменяем опасные конструкции безопасными маркерами
    sanitized = text
    for pat in INJECTION_PATTERNS:
        sanitized = pat.sub("[ПОПЫТКА_ИНЪЕКЦИИ_НЕЙТРАЛИЗОВАНА]", sanitized)

    confidence = min(1.0, len(matches) * 0.35 + 0.3)
    return InjectionCheckResult(
        is_injected=True,
        confidence=confidence,
        matched_patterns=matches,
        sanitized_text=sanitized,
    )


def wrap_untrusted_content(content: str, source: str = "external") -> str:
    """Изолирует ненадёжный контент в безопасный контейнер с явным указанием источника."""
    check = detect_prompt_injection(content)
    body = check.sanitized_text if check.is_injected else content
    # Защита от закрытия тега внутри контента
    body_escaped = body.replace("</untrusted_content>", "&lt;/untrusted_content&gt;")
    warning = ""
    if check.is_injected:
        warning = ' injection_detected="true"'

    return f'<untrusted_content source="{html.escape(source)}"{warning}>\n{body_escaped}\n</untrusted_content>'
