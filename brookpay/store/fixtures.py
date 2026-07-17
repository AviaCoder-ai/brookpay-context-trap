"""Deterministic seed data for development, demos and end-to-end runs."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from brookpay.config.constants import (
    STATUS_ACTIVE,
    STATUS_DORMANT,
    STATUS_FROZEN,
)
from brookpay.models.account import Account
from brookpay.models.transaction import Category, Direction, Transaction
from brookpay.store.repository import accounts, transactions

_SEEDED = False


def _dt(y: int, m: int, d: int, hh: int = 9, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def seed(force: bool = False) -> None:
    """Idempotent. Populates the repositories with a small realistic book."""
    global _SEEDED
    if _SEEDED and not force:
        return
    if force:
        accounts.clear()
        transactions.clear()

    accounts.add(Account(
        user_id="alice",
        raw_balance=Decimal("1250.40"),
        currency="EUR",
        status=STATUS_ACTIVE,
        opened_at=_dt(2023, 3, 14),
        updated_at=_dt(2026, 7, 1, 9, 30),
        kyc_level=2,
    ))
    accounts.add(Account(
        user_id="kenji",
        raw_balance=Decimal("100000"),
        currency="JPY",
        status=STATUS_ACTIVE,
        opened_at=_dt(2024, 1, 8),
        updated_at=_dt(2026, 6, 28, 17, 5),
        kyc_level=2,
    ))
    accounts.add(Account(
        user_id="freya",
        raw_balance=Decimal("900.00"),
        currency="USD",
        status=STATUS_FROZEN,
        opened_at=_dt(2022, 11, 2),
        updated_at=_dt(2026, 6, 30, 8, 0),
        kyc_level=3,
        metadata={"frozen_reason": "chargeback_investigation", "case": "CB-2214"},
    ))
    accounts.add(Account(
        user_id="dora",
        raw_balance=Decimal("4.90"),
        currency="EUR",
        status=STATUS_ACTIVE,
        opened_at=_dt(2025, 9, 19),
        updated_at=_dt(2026, 7, 2, 12, 45),
        kyc_level=1,
    ))
    accounts.add(Account(
        user_id="otto",
        raw_balance=Decimal("82.15"),
        currency="GBP",
        status=STATUS_DORMANT,
        opened_at=_dt(2021, 5, 30),
        updated_at=_dt(2025, 4, 11, 10, 20),
        kyc_level=1,
    ))
    accounts.add(Account(
        user_id="mira",
        raw_balance=Decimal("5200.00"),
        currency="THB",
        status=STATUS_ACTIVE,
        opened_at=_dt(2025, 2, 3),
        updated_at=_dt(2026, 7, 3, 6, 10),
        kyc_level=2,
    ))

    rows = [
        ("txn-000101", "alice", "1500.00", "EUR", Direction.CREDIT, Category.DEPOSIT, _dt(2026, 5, 4), "SEPA transfer in"),
        ("txn-000102", "alice", "230.10", "EUR", Direction.DEBIT, Category.WITHDRAWAL, _dt(2026, 6, 6), "ATM withdrawal"),
        ("txn-000103", "alice", "19.50", "EUR", Direction.DEBIT, Category.FEE, _dt(2026, 6, 30), "Account fee"),
        ("txn-000104", "kenji", "120000", "JPY", Direction.CREDIT, Category.DEPOSIT, _dt(2026, 5, 22), "Payroll"),
        ("txn-000105", "kenji", "20000", "JPY", Direction.DEBIT, Category.WITHDRAWAL, _dt(2026, 6, 12), "Transfer out"),
        ("txn-000106", "freya", "900.00", "USD", Direction.CREDIT, Category.DEPOSIT, _dt(2026, 4, 2), "Card top-up"),
        ("txn-000107", "dora", "25.00", "EUR", Direction.CREDIT, Category.DEPOSIT, _dt(2026, 6, 1), "Gift card redemption"),
        ("txn-000108", "dora", "20.10", "EUR", Direction.DEBIT, Category.INVOICE, _dt(2026, 6, 20), "Subscription"),
        ("txn-000109", "otto", "82.15", "GBP", Direction.CREDIT, Category.DEPOSIT, _dt(2024, 12, 24), "Legacy migration"),
        ("txn-000110", "mira", "6000.00", "THB", Direction.CREDIT, Category.DEPOSIT, _dt(2026, 6, 3), "PromptPay in"),
        ("txn-000111", "mira", "800.00", "THB", Direction.DEBIT, Category.WITHDRAWAL, _dt(2026, 6, 25), "QR payment"),
    ]
    for txn_id, uid, amount, ccy, direction, category, created_at, desc in rows:
        transactions.add(Transaction(
            txn_id=txn_id,
            user_id=uid,
            amount=Decimal(amount),
            currency=ccy,
            direction=direction,
            category=category,
            created_at=created_at,
            description=desc,
        ))

    _SEEDED = True
