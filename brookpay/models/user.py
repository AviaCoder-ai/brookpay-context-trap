"""Directory record for a person. Wallet state lives in Account."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from brookpay.utils.validation import is_valid_email


@dataclass
class User:
    user_id: str
    email: str
    display_name: str
    locale: str = "en_GB"
    marketing_opt_in: bool = False
    created_at: datetime | None = None
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not is_valid_email(self.email):
            raise ValueError(f"invalid email for user '{self.user_id}'")

    @property
    def language(self) -> str:
        return self.locale.split("_", 1)[0]
