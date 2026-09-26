"""Архитектура коммерческих лицензий и подписок (ТЗ §72-§76, §241-§244).

Предоставляет:
  * Трёхуровневую модель прав: Free, Pro, Enterprise;
  * Graceful degradation: при отсутствии сети или истечении лицензии локальные
    инструменты и локальная модель остаются доступными (Offline Mode);
  * Проверку разрешений для функций (Entitlements: облачные LLM, безлимитные шаги и т.д.).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SubscriptionTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


@dataclass
class LicenseStatus:
    tier: SubscriptionTier = SubscriptionTier.FREE
    active: bool = True
    license_key_masked: str = ""
    expires_at_ms: int = 0
    offline_grace_hours: int = 720  # 30 дней автономной работы
    entitlements: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "active": self.active,
            "key": self.license_key_masked,
            "expires_at_ms": self.expires_at_ms,
            "offline_grace_hours": self.offline_grace_hours,
            "entitlements": self.entitlements,
        }


DEFAULT_ENTITLEMENTS = {
    SubscriptionTier.FREE: {
        "native_execution": True,
        "local_models": True,
        "basic_tools": True,
        "cloud_models": False,
        "advanced_automation": False,
        "unlimited_steps": False,
        "priority_support": False,
    },
    SubscriptionTier.PRO: {
        "native_execution": True,
        "local_models": True,
        "basic_tools": True,
        "cloud_models": True,
        "advanced_automation": True,
        "unlimited_steps": True,
        "priority_support": False,
    },
    SubscriptionTier.ENTERPRISE: {
        "native_execution": True,
        "local_models": True,
        "basic_tools": True,
        "cloud_models": True,
        "advanced_automation": True,
        "unlimited_steps": True,
        "priority_support": True,
    },
}


class LicenseManager:
    """Менеджер лицензий и подписок."""

    def __init__(self, storage_path: str | Path | None = None) -> None:
        if storage_path is None:
            storage_path = Path.home() / ".local" / "share" / "copmuter" / "license.json"
        self.storage_path = Path(storage_path)
        self.status = LicenseStatus()
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            self._set_tier(SubscriptionTier.FREE)
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            tier_str = data.get("tier", SubscriptionTier.FREE.value)
            tier = SubscriptionTier(tier_str) if tier_str in [t.value for t in SubscriptionTier] else SubscriptionTier.FREE
            self._set_tier(tier, key_masked=data.get("key", ""), expires_at=data.get("expires_at_ms", 0))
        except Exception:
            self._set_tier(SubscriptionTier.FREE)

    def _save(self) -> None:
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(json.dumps(self.status.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass

    def _set_tier(self, tier: SubscriptionTier, key_masked: str = "", expires_at: int = 0) -> None:
        self.status.tier = tier
        self.status.active = True
        self.status.license_key_masked = key_masked
        self.status.expires_at_ms = expires_at
        self.status.entitlements = dict(DEFAULT_ENTITLEMENTS.get(tier, DEFAULT_ENTITLEMENTS[SubscriptionTier.FREE]))

    def can_access(self, feature: str) -> bool:
        """Проверяет доступность функции по текущей лицензии."""
        # Всегда разрешаем нативные действия и локальные модели независимо от статуса сети
        if feature in ("native_execution", "local_models", "basic_tools"):
            return True
        return bool(self.status.entitlements.get(feature, False))

    def activate(self, license_key: str) -> tuple[bool, str]:
        """Активирует лицензионный ключ."""
        key = license_key.strip()
        if not key:
            return False, "Пустой лицензионный ключ"

        masked = key[:4] + "..." + key[-4:] if len(key) >= 8 else "****"

        # Детерминированная валидация формата ключей (PRO-..., ENT-...)
        if key.startswith("PRO-"):
            self._set_tier(SubscriptionTier.PRO, key_masked=masked, expires_at=int(time.time() * 1000) + 365 * 86400 * 1000)
            self._save()
            return True, "Лицензия Pro успешно активирована (действительна 1 год)"
        elif key.startswith("ENT-"):
            self._set_tier(SubscriptionTier.ENTERPRISE, key_masked=masked, expires_at=int(time.time() * 1000) + 365 * 86400 * 1000)
            self._save()
            return True, "Лицензия Enterprise успешно активирована"
        else:
            return False, "Неверный формат ключа. Ожидается ключ формата PRO-XXXX или ENT-XXXX"

    def reset_to_free(self) -> None:
        """Сбрасывает подписку на Free."""
        self._set_tier(SubscriptionTier.FREE)
        self._save()
