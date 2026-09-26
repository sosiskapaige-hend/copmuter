"""Обнаружение и маскирование секретов (ТЗ §143).

Не допускает утечки секретов (API-ключей, паролей, токенов, приватных ключей)
в контекст LLM, файлы журнала или события UI.
"""

from __future__ import annotations

import re

# Паттерны распространённых секретов
SECRET_PATTERNS = [
    # Private Keys
    (re.compile(r"-----BEGIN\s+[A-Z0-9_\s]+PRIVATE\s+KEY-----[\s\S]+?-----END\s+[A-Z0-9_\s]+PRIVATE\s+KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    # OpenAI API Keys
    (re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b"), "[REDACTED_OPENAI_KEY]"),
    # Anthropic API Keys
    (re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
    # GitHub Tokens (ghp, gho, ghu, ghs, ghr)
    (re.compile(r"\bgh[pousr][-_][a-zA-Z0-9_]{30,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Bearer Tokens
    (re.compile(r"(?i)\bBearer\s+[a-zA-Z0-9_\-\.]{20,}\b"), "Bearer [REDACTED_TOKEN]"),
    # Key-value assignments like api_key=..., password: ..., password is ...
    (re.compile(r"""(?i)(?:api_key|access_token|secret_key|password|passwd|pwd|pass|auth_token)(?:\s*[:=]\s*|\s+(?:is|это|=)\s*)["']?([a-zA-Z0-9_\-\.\$\@\#\%\&\*]{6,})["']?"""), r"\1"),
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Находит и маскирует конфиденциальные данные.

    Возвращает (очищенный_текст, количество_заменённых_секретов).
    """
    if not text:
        return text, 0

    count = 0
    result = text

    # 1. Приватные ключи и специфичные токены
    for pat, replacement in SECRET_PATTERNS[:-1]:
        matches = pat.findall(result)
        if matches:
            count += len(matches)
            result = pat.sub(replacement, result)

    # 2. Именованные секреты (password = "...", api_key = "...")
    named_pat = SECRET_PATTERNS[-1][0]
    def _mask_named(match: re.Match) -> str:
        nonlocal count
        count += 1
        full = match.group(0)
        val = match.group(1)
        return full.replace(val, "[REDACTED_SECRET]")

    result = named_pat.sub(_mask_named, result)
    return result, count


def contains_secrets(text: str) -> bool:
    """Проверяет, содержит ли строка очевидные секреты."""
    _, count = redact_secrets(text)
    return count > 0
