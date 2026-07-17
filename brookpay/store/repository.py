"""In-memory repositories.

Production runs against PostgreSQL through the same interfaces; the memory
implementation backs local development, tests and demo scenarios.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from brookpay.core.errors import AccountNotFound
from brookpay.models.account import Account
from brookpay.models.transaction import Transaction


class AccountRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, Account] = {}

    def add(self, account: Account) -> Account:
        self._by_id[account.user_id] = account
        return account

    def find_account(self, user_id: str) -> Optional[Account]:
        """Return the account or None when the user has no wallet."""
        return self._by_id.get(user_id)

    def get(self, user_id: str) -> Account:
        account = self.find_account(user_id)
        if account is None:
            raise AccountNotFound(user_id)
        return account

    def all(self) -> list[Account]:
        return list(self._by_id.values())

    def by_status(self, status: str) -> list[Account]:
        return [a for a in self._by_id.values() if a.status == status]

    def exists(self, user_id: str) -> bool:
        return user_id in self._by_id

    def clear(self) -> None:
        self._by_id.clear()


class TransactionRepository:
    def __init__(self) -> None:
        self._rows: list[Transaction] = []

    def add(self, txn: Transaction) -> Transaction:
        self._rows.append(txn)
        return txn

    def for_user(self, user_id: str) -> list[Transaction]:
        return [t for t in self._rows if t.user_id == user_id]

    def between(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[Transaction]:
        return [
            t
            for t in self._rows
            if t.user_id == user_id and start <= t.created_at < end
        ]

    def all(self) -> list[Transaction]:
        return list(self._rows)

    def extend(self, txns: Iterable[Transaction]) -> None:
        self._rows.extend(txns)

    def clear(self) -> None:
        self._rows.clear()


# Module singletons, shared across the process (mirrors a connection pool).
accounts = AccountRepository()
transactions = TransactionRepository()


def reset_all() -> None:
    """Test helper."""
    accounts.clear()
    transactions.clear()
