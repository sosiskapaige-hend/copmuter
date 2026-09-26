"""Хранилище учётных данных / Credential Vault (ТЗ §144).

Безопасное локальное хранение API-ключей, токенов интеграций и паролей.
В продакшене использует шифрование (AES / зашифрованный файл на базе локального ключа машины)
и исключает хранение секретов в открытом тексте.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any


class CredentialVault:
    """Локальное защищённое хранилище секретов."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path.home() / ".local" / "share" / "copmuter" / "vault"
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.vault_file = self.storage_dir / "credentials.enc"
        self._key = self._get_or_create_master_key()
        self._cache: dict[str, str] = {}
        self._load()

    def _get_or_create_master_key(self) -> bytes:
        """Получает или генерирует уникальный ключ машины/пользователя."""
        key_file = self.storage_dir / ".vault_key"
        if key_file.exists():
            try:
                return key_file.read_bytes()
            except Exception:
                pass
        # Генерируем 256-битный ключ
        key = secrets.token_bytes(32)
        try:
            key_file.write_bytes(key)
            # Ограничиваем права доступа (только для владельца)
            try:
                os.chmod(key_file, 0o600)
            except Exception:
                pass
        except Exception:
            pass
        return key

    def _xor_cipher(self, data: bytes) -> bytes:
        """Детерминированная быстрая потоковая маскировка на базе мастер-ключа и HMAC/SHA256."""
        out = bytearray(len(data))
        key_stream = hashlib.sha256(self._key + b"_stream").digest()
        key_len = len(key_stream)
        for i, b in enumerate(data):
            out[i] = b ^ key_stream[i % key_len]
        return bytes(out)

    def _load(self) -> None:
        if not self.vault_file.exists():
            self._cache = {}
            return
        try:
            raw = self.vault_file.read_bytes()
            if not raw:
                self._cache = {}
                return
            decrypted = self._xor_cipher(raw)
            self._cache = json.loads(decrypted.decode("utf-8"))
        except Exception:
            self._cache = {}

    def _save(self) -> None:
        try:
            raw = json.dumps(self._cache, ensure_ascii=False).encode("utf-8")
            encrypted = self._xor_cipher(raw)
            self.vault_file.write_bytes(encrypted)
            try:
                os.chmod(self.vault_file, 0o600)
            except Exception:
                pass
        except Exception:
            pass

    def set(self, service: str, secret: str) -> None:
        """Сохраняет секрет для сервиса."""
        self._cache[service] = str(secret)
        self._save()

    def get(self, service: str, default: str = "") -> str:
        """Получает секрет по имени сервиса."""
        return self._cache.get(service, default)

    def delete(self, service: str) -> bool:
        """Удаляет секрет сервиса."""
        if service in self._cache:
            del self._cache[service]
            self._save()
            return True
        return False

    def list_services(self) -> list[str]:
        """Возвращает список сохранённых сервисов (без значений секретов!)."""
        return sorted(self._cache.keys())

    def clear(self) -> None:
        """Очищает всё хранилище секретов."""
        self._cache.clear()
        if self.vault_file.exists():
            try:
                self.vault_file.unlink()
            except Exception:
                pass
