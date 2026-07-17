"""Price list and fee schedule."""

from __future__ import annotations

from decimal import Decimal

from brookpay.utils.money import quantize2, to_decimal

# Subscription and one-off SKUs, priced in EUR reference.
PRICE_LIST: dict[str, Decimal] = {
    "plan_basic": Decimal("9.99"),
    "plan_standard": Decimal("19.99"),
    "plan_premium": Decimal("39.99"),
    "card_replacement": Decimal("7.50"),
    "instant_payout": Decimal("1.20"),
    "statement_paper": Decimal("2.00"),
}

# Processing fee: percentage in basis points with a floor, per category.
FEE_SCHEDULE: dict[str, dict[str, Decimal]] = {
    "invoice": {"bps": Decimal("200"), "min": Decimal("0.30")},
    "payout": {"bps": Decimal("90"), "min": Decimal("0.20")},
    "fx": {"bps": Decimal("35"), "min": Decimal("0.00")},
}


def price_for(sku: str) -> Decimal:
    try:
        return PRICE_LIST[sku]
    except KeyError:
        raise KeyError(f"unknown SKU '{sku}'") from None


def fee_for(category: str, amount) -> Decimal:
    """Processing fee for a category on a given base amount."""
    schedule = FEE_SCHEDULE.get(category)
    if schedule is None:
        return Decimal("0.00")
    base = to_decimal(amount)
    fee = base * schedule["bps"] / Decimal(10000)
    return quantize2(max(fee, schedule["min"]))


def line_total(sku: str, quantity: int) -> Decimal:
    if quantity <= 0:
        raise ValueError("quantity must be >= 1")
    return quantize2(price_for(sku) * quantity)
