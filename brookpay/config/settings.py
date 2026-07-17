"""Runtime settings.

Values come from environment variables prefixed with BROOKPAY_ and fall back
to safe development defaults. The Settings object is immutable and cached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from brookpay.config.constants import (
    BALANCE_CACHE_TTL_SECONDS,
    DEFAULT_CURRENCY,
)

_PREFIX = "BROOKPAY_"


def _env_str(name: str, default: str) -> str:
    return os.environ.get(_PREFIX + name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(_PREFIX + name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.environ.get(_PREFIX + name, default)
    try:
        return Decimal(raw)
    except Exception:
        return Decimal(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(_PREFIX + name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    environment: str
    default_currency: str
    balance_cache_ttl_seconds: int
    low_balance_threshold_eur: Decimal
    dormancy_days: int
    velocity_max_withdrawals_per_hour: int
    velocity_max_daily_eur: Decimal
    audit_retention_days: int
    compliance_export_enabled: bool
    exports_dir: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        environment=_env_str("ENV", "development"),
        default_currency=_env_str("DEFAULT_CURRENCY", DEFAULT_CURRENCY),
        balance_cache_ttl_seconds=_env_int(
            "BALANCE_CACHE_TTL", BALANCE_CACHE_TTL_SECONDS
        ),
        low_balance_threshold_eur=_env_decimal("LOW_BALANCE_THRESHOLD_EUR", "10.00"),
        dormancy_days=_env_int("DORMANCY_DAYS", 365),
        velocity_max_withdrawals_per_hour=_env_int("VELOCITY_MAX_PER_HOUR", 5),
        velocity_max_daily_eur=_env_decimal("VELOCITY_MAX_DAILY_EUR", "5000.00"),
        audit_retention_days=_env_int("AUDIT_RETENTION_DAYS", 400),
        compliance_export_enabled=_env_bool("COMPLIANCE_EXPORT", True),
        exports_dir=_env_str("EXPORTS_DIR", "var/exports"),
    )
