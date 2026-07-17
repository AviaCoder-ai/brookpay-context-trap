"""Book-level analytics for the ops dashboard.

Everything here is read-only over the repositories; EUR normalization goes
through the FX engine so numbers line up with the treasury view.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from brookpay.config.constants import STATUS_ACTIVE
from brookpay.fx.engine import to_eur
from brookpay.models.transaction import Category, Direction
from brookpay.store.repository import accounts, transactions
from brookpay.utils.money import quantize2
from brookpay.utils.timeutils import utc_now


def total_book_value_eur() -> Decimal:
    """Sum of all booked balances, normalized to EUR."""
    total = Decimal("0")
    for account in accounts.all():
        total += to_eur(account.raw_balance, account.currency)
    return quantize2(total)


def active_ratio() -> float:
    all_accounts = accounts.all()
    if not all_accounts:
        return 0.0
    active = sum(1 for a in all_accounts if a.status == STATUS_ACTIVE)
    return round(active / len(all_accounts), 4)


def currency_exposure() -> dict[str, Decimal]:
    """EUR value held per account currency."""
    exposure: dict[str, Decimal] = {}
    for account in accounts.all():
        eur = to_eur(account.raw_balance, account.currency)
        exposure[account.currency] = quantize2(
            exposure.get(account.currency, Decimal("0")) + eur
        )
    return dict(sorted(exposure.items()))


def flow_summary(days: int = 30) -> dict[str, Decimal]:
    """Inbound vs outbound EUR volume over a trailing window."""
    since = utc_now() - timedelta(days=days)
    inbound = Decimal("0")
    outbound = Decimal("0")
    for txn in transactions.all():
        if txn.created_at < since:
            continue
        eur = to_eur(txn.amount, txn.currency)
        if txn.direction == Direction.CREDIT:
            inbound += eur
        else:
            outbound += eur
    return {
        "inbound_eur": quantize2(inbound),
        "outbound_eur": quantize2(outbound),
        "net_eur": quantize2(inbound - outbound),
    }


def fee_revenue_eur(days: int = 30) -> Decimal:
    since = utc_now() - timedelta(days=days)
    total = Decimal("0")
    for txn in transactions.all():
        if txn.created_at < since or txn.category != Category.FEE:
            continue
        total += to_eur(txn.amount, txn.currency)
    return quantize2(total)


def kpi_snapshot() -> dict:
    """One-call bundle for the dashboard landing page."""
    flows = flow_summary()
    return {
        "book_value_eur": str(total_book_value_eur()),
        "active_ratio": active_ratio(),
        "currency_exposure": {k: str(v) for k, v in currency_exposure().items()},
        "inbound_eur_30d": str(flows["inbound_eur"]),
        "outbound_eur_30d": str(flows["outbound_eur"]),
        "accounts": len(accounts.all()),
        "transactions": len(transactions.all()),
    }


# ---------------------------------------------------------------------------
# Distribution statistics
# ---------------------------------------------------------------------------

def _percentile(values: list[Decimal], fraction: float) -> Decimal:
    """Nearest-rank percentile over a sorted copy of the values.

    Nearest-rank rather than interpolated: the ops dashboard reports real
    observed values, and an interpolated "balance" that no account actually
    holds confuses the finance team more than it informs them.
    """
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    rank = max(1, int(round(fraction * len(ordered))))
    return ordered[min(rank, len(ordered)) - 1]


def balance_distribution() -> dict[str, str]:
    """Percentile spread of account balances, normalized to EUR."""
    values = [to_eur(a.raw_balance, a.currency) for a in accounts.all()]
    return {
        "p10": str(quantize2(_percentile(values, 0.10))),
        "p50": str(quantize2(_percentile(values, 0.50))),
        "p90": str(quantize2(_percentile(values, 0.90))),
        "p99": str(quantize2(_percentile(values, 0.99))),
    }


def mean_balance_eur() -> Decimal:
    """Arithmetic mean of booked balances in EUR."""
    values = [to_eur(a.raw_balance, a.currency) for a in accounts.all()]
    if not values:
        return Decimal("0")
    return quantize2(sum(values, Decimal("0")) / len(values))


def gini_coefficient() -> float:
    """Concentration of the book across accounts, 0 = even, 1 = concentrated.

    Reported to the treasury team as a liquidity risk signal: a book whose
    value sits in a handful of accounts behaves very differently under a
    withdrawal wave than an evenly spread one.
    """
    values = sorted(to_eur(a.raw_balance, a.currency) for a in accounts.all())
    n = len(values)
    total = sum(values, Decimal("0"))
    if n == 0 or total == 0:
        return 0.0
    cumulative = Decimal("0")
    for index, value in enumerate(values, start=1):
        cumulative += Decimal(index) * value
    ratio = (Decimal(2) * cumulative) / (Decimal(n) * total) - (Decimal(n + 1) / Decimal(n))
    return round(float(ratio), 4)


def concentration_top_n(n: int = 10) -> dict:
    """Share of the book held by the largest N accounts."""
    values = sorted(
        (to_eur(a.raw_balance, a.currency) for a in accounts.all()),
        reverse=True,
    )
    total = sum(values, Decimal("0"))
    if total == 0:
        return {"top_n": n, "share": 0.0, "value_eur": "0.00"}
    top = sum(values[:n], Decimal("0"))
    return {
        "top_n": n,
        "share": round(float(top / total), 4),
        "value_eur": str(quantize2(top)),
    }


# ---------------------------------------------------------------------------
# Status and lifecycle breakdown
# ---------------------------------------------------------------------------

def status_breakdown() -> dict[str, int]:
    """Account count per lifecycle status."""
    counts: dict[str, int] = {}
    for account in accounts.all():
        counts[account.status] = counts.get(account.status, 0) + 1
    return dict(sorted(counts.items()))


def value_by_status() -> dict[str, str]:
    """EUR value held per lifecycle status.

    Frozen and dormant value matters to treasury: it is on the balance sheet
    but cannot leave, so it behaves differently from active liquidity.
    """
    totals: dict[str, Decimal] = {}
    for account in accounts.all():
        eur = to_eur(account.raw_balance, account.currency)
        totals[account.status] = totals.get(account.status, Decimal("0")) + eur
    return {k: str(quantize2(v)) for k, v in sorted(totals.items())}


def kyc_breakdown() -> dict[int, int]:
    """Account count per KYC level."""
    counts: dict[int, int] = {}
    for account in accounts.all():
        counts[account.kyc_level] = counts.get(account.kyc_level, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Transaction analytics
# ---------------------------------------------------------------------------

def category_volumes(days: int = 30) -> dict[str, str]:
    """EUR volume per transaction category over a trailing window."""
    since = utc_now() - timedelta(days=days)
    totals: dict[str, Decimal] = {}
    for txn in transactions.all():
        if txn.created_at < since:
            continue
        eur = to_eur(txn.amount, txn.currency)
        key = txn.category.value
        totals[key] = totals.get(key, Decimal("0")) + eur
    return {k: str(quantize2(v)) for k, v in sorted(totals.items())}


def transaction_counts(days: int = 30) -> dict[str, int]:
    """Transaction count per category over a trailing window."""
    since = utc_now() - timedelta(days=days)
    counts: dict[str, int] = {}
    for txn in transactions.all():
        if txn.created_at < since:
            continue
        key = txn.category.value
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def average_transaction_eur(days: int = 30) -> Decimal:
    """Mean EUR size of a transaction over a trailing window."""
    since = utc_now() - timedelta(days=days)
    values = [
        to_eur(t.amount, t.currency)
        for t in transactions.all()
        if t.created_at >= since
    ]
    if not values:
        return Decimal("0")
    return quantize2(sum(values, Decimal("0")) / len(values))


def largest_transactions(days: int = 30, top: int = 5) -> list[dict]:
    """The biggest movements in a trailing window, for the ops review."""
    since = utc_now() - timedelta(days=days)
    rows = []
    for txn in transactions.all():
        if txn.created_at < since:
            continue
        rows.append({
            "txn_id": txn.txn_id,
            "user_id": txn.user_id,
            "eur": to_eur(txn.amount, txn.currency),
            "category": txn.category.value,
            "direction": txn.direction.value,
        })
    rows.sort(key=lambda r: r["eur"], reverse=True)
    return [
        {**r, "eur": str(quantize2(r["eur"]))}
        for r in rows[:top]
    ]


# ---------------------------------------------------------------------------
# Activity cohorts
# ---------------------------------------------------------------------------

def active_accounts(days: int = 30) -> set[str]:
    """User ids with at least one transaction in the trailing window."""
    since = utc_now() - timedelta(days=days)
    return {t.user_id for t in transactions.all() if t.created_at >= since}


def dormant_candidates(days: int = 90) -> list[str]:
    """Active accounts with no movement in a long window.

    Candidates only: the lifecycle transition to dormant is the account
    service's call, not this module's. Reporting flags, it does not act.
    """
    recent = active_accounts(days)
    return sorted(
        a.user_id
        for a in accounts.all()
        if a.status == STATUS_ACTIVE and a.user_id not in recent
    )


def engagement_ratio(days: int = 30) -> float:
    """Fraction of accounts that moved money in the trailing window."""
    all_accounts = accounts.all()
    if not all_accounts:
        return 0.0
    return round(len(active_accounts(days)) / len(all_accounts), 4)


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

def daily_flow_series(days: int = 7) -> list[dict]:
    """Per-day inbound/outbound EUR over a trailing window, oldest first.

    Buckets by calendar date in UTC. Days with no movement still appear, with
    zeros, so the chart has no gaps and a flat line reads as "nothing
    happened" rather than "data missing".
    """
    now = utc_now()
    buckets: dict[str, dict[str, Decimal]] = {}
    for offset in range(days - 1, -1, -1):
        key = (now - timedelta(days=offset)).date().isoformat()
        buckets[key] = {"inbound": Decimal("0"), "outbound": Decimal("0")}

    since = now - timedelta(days=days)
    for txn in transactions.all():
        if txn.created_at < since:
            continue
        key = txn.created_at.date().isoformat()
        if key not in buckets:
            continue
        eur = to_eur(txn.amount, txn.currency)
        if txn.direction == Direction.CREDIT:
            buckets[key]["inbound"] += eur
        else:
            buckets[key]["outbound"] += eur

    return [
        {
            "date": key,
            "inbound_eur": str(quantize2(values["inbound"])),
            "outbound_eur": str(quantize2(values["outbound"])),
            "net_eur": str(quantize2(values["inbound"] - values["outbound"])),
        }
        for key, values in buckets.items()
    ]


def trend_direction(series: list[dict], field: str = "net_eur") -> str:
    """Compare the last two points of a series: up, down or flat."""
    if len(series) < 2:
        return "flat"
    previous = Decimal(series[-2][field])
    latest = Decimal(series[-1][field])
    if latest > previous:
        return "up"
    if latest < previous:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# Treasury view
# ---------------------------------------------------------------------------

def liquidity_view() -> dict:
    """What treasury needs to size the float, in one call.

    Splits the book into value that can move (active accounts) and value that
    cannot (everything else), then reports concentration, because a
    concentrated book needs a bigger buffer for the same nominal value.
    """
    by_status = value_by_status()
    active_value = Decimal(by_status.get(STATUS_ACTIVE, "0"))
    total = total_book_value_eur()
    locked = quantize2(total - active_value)
    return {
        "book_value_eur": str(total),
        "movable_eur": str(quantize2(active_value)),
        "locked_eur": str(locked),
        "locked_ratio": round(float(locked / total), 4) if total else 0.0,
        "concentration_top10": concentration_top_n(10),
        "gini": gini_coefficient(),
    }


def dashboard_bundle() -> dict:
    """Everything the ops dashboard renders, in a single aggregation pass."""
    return {
        "kpis": kpi_snapshot(),
        "distribution": balance_distribution(),
        "status": status_breakdown(),
        "value_by_status": value_by_status(),
        "kyc": kyc_breakdown(),
        "categories_30d": category_volumes(30),
        "engagement_30d": engagement_ratio(30),
        "liquidity": liquidity_view(),
    }


# ---------------------------------------------------------------------------
# FX exposure
# ---------------------------------------------------------------------------

def fx_exposure_share() -> dict[str, float]:
    """Share of the book held in each currency, as fractions of EUR value.

    Treasury hedges against these shares, so they are reported separately
    from the absolute exposure: a small absolute position in a volatile
    currency can still be the one worth hedging.
    """
    exposure = currency_exposure()
    total = sum(exposure.values(), Decimal("0"))
    if total == 0:
        return {}
    return {
        currency: round(float(value / total), 4)
        for currency, value in exposure.items()
    }


def non_eur_share() -> float:
    """Fraction of the book not held in EUR."""
    shares = fx_exposure_share()
    return round(1.0 - shares.get("EUR", 0.0), 4)


def dominant_currency() -> str:
    """The currency holding the largest share of the book."""
    exposure = currency_exposure()
    if not exposure:
        return ""
    return max(exposure.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Anomaly signals
# ---------------------------------------------------------------------------

# Multiple of the trailing mean above which a day's outbound volume is worth
# a human look. Not an alert threshold: this module reports, the alerting
# jobs decide what to do about it.
_OUTBOUND_SPIKE_FACTOR = Decimal("3")


def outbound_spike_days(days: int = 30) -> list[str]:
    """Days whose outbound volume far exceeds the window's mean.

    Compares each day against the mean of the whole window, which is crude
    but stable on a small book; a rolling baseline would be noise here.
    """
    series = daily_flow_series(days)
    if not series:
        return []
    volumes = [Decimal(row["outbound_eur"]) for row in series]
    mean = sum(volumes, Decimal("0")) / len(volumes)
    if mean <= 0:
        return []
    threshold = mean * _OUTBOUND_SPIKE_FACTOR
    return [
        row["date"]
        for row in series
        if Decimal(row["outbound_eur"]) > threshold
    ]


def net_outflow_days(days: int = 30) -> int:
    """How many days in the window saw more money leave than arrive."""
    return sum(
        1 for row in daily_flow_series(days) if Decimal(row["net_eur"]) < 0
    )


def health_signals() -> dict:
    """Compact set of book-health signals for the ops landing page.

    Everything here is descriptive. No signal in this bundle triggers an
    action on its own; they exist so an operator has one place to look before
    deciding whether something warrants investigation.
    """
    return {
        "gini": gini_coefficient(),
        "non_eur_share": non_eur_share(),
        "dominant_currency": dominant_currency(),
        "outbound_spike_days": outbound_spike_days(30),
        "net_outflow_days_30d": net_outflow_days(30),
        "engagement_30d": engagement_ratio(30),
        "dormant_candidates": len(dormant_candidates(90)),
    }
