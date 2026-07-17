"""Input validation helpers shared by handlers and services."""

from __future__ import annotations

import re

from brookpay.config.constants import SUPPORTED_CURRENCIES

_USER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def is_valid_user_id(user_id) -> bool:
    return isinstance(user_id, str) and bool(_USER_ID_RE.match(user_id))


def normalize_currency(currency) -> str:
    if not isinstance(currency, str):
        raise ValueError("currency must be a string")
    return currency.strip().upper()


def is_supported_currency(currency) -> bool:
    try:
        return normalize_currency(currency) in SUPPORTED_CURRENCIES
    except ValueError:
        return False


def is_valid_email(email) -> bool:
    return isinstance(email, str) and bool(_EMAIL_RE.match(email))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
