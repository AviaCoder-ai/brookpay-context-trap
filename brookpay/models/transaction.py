"""Ledger transaction records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from brookpay.utils.money import quantize2, to_decimal


class Direction(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class Category(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    INVOICE = "invoice"
    FEE = "fee"
    FX = "fx"
    ADJUSTMENT = "adjustment"


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    user_id: str
    amount: Decimal
    currency: str
    direction: Direction
    category: Category
    created_at: datetime
    description: str = ""

    def signed_amount(self) -> Decimal:
        amt = quantize2(to_decimal(self.amount))
        return amt if self.direction == Direction.CREDIT else -amt


def net_total(transactions) -> Decimal:
    """Signed sum of transactions, assumed same currency."""
    total = Decimal("0.00")
    for txn in transactions:
        total += txn.signed_amount()
    return quantize2(total)


def by_category(transactions) -> dict[str, Decimal]:
    buckets: dict[str, Decimal] = {}
    for txn in transactions:
        key = txn.category.value
        buckets[key] = quantize2(buckets.get(key, Decimal("0")) + txn.signed_amount())
    return buckets
