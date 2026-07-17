"""Decimal helpers for monetary amounts.

All internal arithmetic uses Decimal. Floats are accepted at the edges and
converted through str() to avoid binary representation noise.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation as _DecInvalid

TWO_PLACES = Decimal("0.01")


def to_decimal(value) -> Decimal:
    """Coerce int, float, str or Decimal into a Decimal."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (_DecInvalid, ValueError, TypeError) as exc:
        raise ValueError(f"not a monetary amount: {value!r}") from exc


def quantize2(value) -> Decimal:
    """Round to 2 decimal places, half up (banker style is not used here)."""
    return to_decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def is_positive(value) -> bool:
    try:
        return to_decimal(value) > 0
    except ValueError:
        return False


def basis_points(value, bps: int) -> Decimal:
    """Return value * bps / 10000, quantized to cents."""
    return quantize2(to_decimal(value) * Decimal(bps) / Decimal(10000))


def clamp(value, low, high) -> Decimal:
    v = to_decimal(value)
    return max(to_decimal(low), min(to_decimal(high), v))


def split_even(total, parts: int) -> list[Decimal]:
    """Split an amount into `parts` cent-exact shares (largest remainder)."""
    if parts <= 0:
        raise ValueError("parts must be >= 1")
    total_d = quantize2(total)
    cents = int(total_d * 100)
    base, remainder = divmod(cents, parts)
    shares = []
    for i in range(parts):
        c = base + (1 if i < remainder else 0)
        shares.append(Decimal(c) / Decimal(100))
    return shares
