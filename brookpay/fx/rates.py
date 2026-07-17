"""Static FX rate table, quoted against EUR.

Ops refresh this table from the treasury desk once per day. Rates are mid
market and only used for balance display, thresholds and risk conversion;
settlement uses the payment processor's own rates.
"""

from __future__ import annotations

from decimal import Decimal

AS_OF = "2026-07-10T06:00:00+00:00"

# 1 EUR buys this many units of the quoted currency.
RATES_PER_EUR: dict[str, Decimal] = {
    "EUR": Decimal("1"),
    "USD": Decimal("1.0850"),
    "GBP": Decimal("0.8460"),
    "JPY": Decimal("161.20"),
    "CHF": Decimal("0.9420"),
    "THB": Decimal("39.40"),
    "SGD": Decimal("1.4610"),
}


def known_currencies() -> tuple[str, ...]:
    return tuple(sorted(RATES_PER_EUR))
