"""Модуль безопасности: защита от инъекций, маскирование секретов, защищённое хранилище."""

from .injection import detect_prompt_injection, wrap_untrusted_content
from .secrets import redact_secrets, contains_secrets
from .vault import CredentialVault

__all__ = [
    "detect_prompt_injection",
    "wrap_untrusted_content",
    "redact_secrets",
    "contains_secrets",
    "CredentialVault",
]
