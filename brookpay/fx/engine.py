"""Currency conversion through EUR cross rates."""

from __future__ import annotations

from decimal import Decimal

from brookpay.core.errors import UnsupportedCurrency
from brookpay.fx.rates import RATES_PER_EUR
from brookpay.utils.money import quantize2, to_decimal


def _rate_per_eur(currency: str) -> Decimal:
    try:
        return RATES_PER_EUR[currency]
    except KeyError:
        raise UnsupportedCurrency(currency) from None


def rate(src: str, dst: str) -> Decimal:
    """How many units of dst one unit of src buys."""
    return _rate_per_eur(dst) / _rate_per_eur(src)


def convert(amount, src: str, dst: str, quantize: bool = True) -> Decimal:
    """Convert an amount between two supported currencies."""
    value = to_decimal(amount)
    if src == dst:
        return quantize2(value) if quantize else value
    out = value / _rate_per_eur(src) * _rate_per_eur(dst)
    return quantize2(out) if quantize else out


def to_eur(amount, src: str) -> Decimal:
    return convert(amount, src, "EUR")


def spread_adjusted(amount, src: str, dst: str, spread_bps: int = 35) -> Decimal:
    """Conversion including the retail spread, used by quote previews."""
    mid = convert(amount, src, dst, quantize=False)
    factor = Decimal(1) - Decimal(spread_bps) / Decimal(10000)
    return quantize2(mid * factor)
