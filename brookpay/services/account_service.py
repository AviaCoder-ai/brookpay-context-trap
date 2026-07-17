"""Account domain services.

This module concentrates account level business operations: lookups, holds
and reservations, statement aggregation, lifecycle transitions (dormancy,
freezing), balance access, daily snapshots, tier classification, interest
accrual and reconciliation helpers.

Historically extracted from the legacy monolith (see CHANGELOG 1.0.0), it is
intentionally kept as a single module because most of these operations share
the same repositories and invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from brookpay.config.constants import (
    BALANCE_CACHE_PREFIX,
    EVENT_ACCOUNT_FROZEN,
    EVENT_ACCOUNT_UNFROZEN,
    EVENT_BALANCE_READ,
    STATUS_ACTIVE,
    STATUS_DORMANT,
    STATUS_FROZEN,
)
from brookpay.config.settings import get_settings
from brookpay.core import audit, cache
from brookpay.core.errors import AccountNotFound, InvalidOperation
from brookpay.fx.engine import convert, to_eur
from brookpay.models.account import Account
from brookpay.models.transaction import Category, Direction, Transaction, net_total
from brookpay.store.repository import accounts, transactions
from brookpay.utils.idgen import new_id
from brookpay.utils.money import quantize2, to_decimal
from brookpay.utils.timeutils import iso, month_bounds, utc_now


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def resolve_account(user_id: str) -> Optional[Account]:
    """Return the account for a user, or None when no wallet exists."""
    return accounts.find_account(user_id)


def require_account(user_id: str) -> Account:
    """Return the account or raise AccountNotFound."""
    account = resolve_account(user_id)
    if account is None:
        raise AccountNotFound(user_id)
    return account


def account_exists(user_id: str) -> bool:
    return accounts.exists(user_id)


def list_accounts_by_status(status: str) -> list[Account]:
    return accounts.by_status(status)


def account_summary(user_id: str) -> dict:
    """Lightweight, side effect free projection used by internal tooling."""
    account = require_account(user_id)
    return {
        "user_id": account.user_id,
        "currency": account.currency,
        "status": account.status,
        "kyc_level": account.kyc_level,
        "opened_at": iso(account.opened_at),
    }


# ---------------------------------------------------------------------------
# Holds and reservations
# ---------------------------------------------------------------------------

@dataclass
class Hold:
    hold_id: str
    user_id: str
    amount: Decimal
    currency: str
    reason: str
    created_at: datetime
    released: bool = False
    released_at: Optional[datetime] = None


_HOLDS: dict[str, list[Hold]] = {}


def place_hold(user_id: str, amount, currency: str, reason: str) -> Hold:
    """Reserve part of the balance (card authorization, pending payout)."""
    account = require_account(user_id)
    amt = quantize2(to_decimal(amount))
    if amt <= 0:
        raise InvalidOperation("hold amount must be positive")
    if not account.is_active:
        raise InvalidOperation(f"cannot place hold on a {account.status} account")
    hold = Hold(
        hold_id=new_id("hold"),
        user_id=user_id,
        amount=amt,
        currency=currency,
        reason=reason,
        created_at=utc_now(),
    )
    _HOLDS.setdefault(user_id, []).append(hold)
    return hold


def release_hold(hold_id: str) -> bool:
    """Release a hold by id. Returns True when a live hold was released."""
    for holds in _HOLDS.values():
        for hold in holds:
            if hold.hold_id == hold_id and not hold.released:
                hold.released = True
                hold.released_at = utc_now()
                return True
    return False


def active_holds(user_id: str) -> list[Hold]:
    return [h for h in _HOLDS.get(user_id, []) if not h.released]


def holds_total(user_id: str, currency: str) -> Decimal:
    """Sum of live holds for a user, converted into the requested currency."""
    total = Decimal("0")
    for hold in active_holds(user_id):
        total += convert(hold.amount, hold.currency, currency, quantize=False)
    return quantize2(total)


def available_after_holds(user_id: str, currency: Optional[str] = None) -> Decimal:
    """Spendable amount: booked balance minus live holds.

    Internal treasury view; customer facing surfaces use the balance read
    path further below, which also refreshes caching and audit.
    """
    account = require_account(user_id)
    target = currency or account.currency
    booked = convert(account.raw_balance, account.currency, target, quantize=False)
    return quantize2(booked - holds_total(user_id, target))


def expire_stale_holds(max_age_days: int = 7) -> int:
    """Card networks drop authorizations after a few days; mirror that."""
    cutoff = utc_now() - timedelta(days=max_age_days)
    released = 0
    for holds in _HOLDS.values():
        for hold in holds:
            if not hold.released and hold.created_at < cutoff:
                hold.released = True
                hold.released_at = utc_now()
                released += 1
    return released


# ---------------------------------------------------------------------------
# Statement aggregation
# ---------------------------------------------------------------------------

def _transactions_in_month(user_id: str, year: int, month: int) -> list[Transaction]:
    start, end = month_bounds(year, month)
    return transactions.between(user_id, start, end)


def monthly_statement(user_id: str, year: int, month: int) -> dict:
    """Aggregate one calendar month of ledger activity for an account.

    Amounts are expressed in the account currency; presentation layers
    convert for display when the customer prefers another currency.
    """
    account = require_account(user_id)
    rows = _transactions_in_month(user_id, year, month)
    credits = quantize2(sum(
        (t.amount for t in rows if t.direction == Direction.CREDIT),
        Decimal("0"),
    ))
    debits = quantize2(sum(
        (t.amount for t in rows if t.direction == Direction.DEBIT),
        Decimal("0"),
    ))
    fees = quantize2(sum(
        (t.amount for t in rows if t.category == Category.FEE),
        Decimal("0"),
    ))
    return {
        "user_id": user_id,
        "period": f"{year:04d}-{month:02d}",
        "account_currency": account.currency,
        "opening_hint": None,  # historical balances are rebuilt by reporting
        "credits": credits,
        "debits": debits,
        "fees": fees,
        "net": net_total(rows),
        "transaction_count": len(rows),
        "transactions": rows,
    }


def statement_csv_rows(user_id: str, year: int, month: int) -> list[dict]:
    """Flat rows for CSV export of a monthly statement."""
    stmt = monthly_statement(user_id, year, month)
    out = []
    for txn in stmt["transactions"]:
        out.append({
            "txn_id": txn.txn_id,
            "date": iso(txn.created_at),
            "direction": txn.direction.value,
            "category": txn.category.value,
            "amount": str(txn.amount),
            "currency": txn.currency,
            "description": txn.description,
        })
    return out


def yearly_activity(user_id: str, year: int) -> dict:
    """Twelve month roll-up used by the annual summary email."""
    months = {}
    for month in range(1, 13):
        stmt = monthly_statement(user_id, year, month)
        months[stmt["period"]] = {
            "credits": stmt["credits"],
            "debits": stmt["debits"],
            "count": stmt["transaction_count"],
        }
    return {"user_id": user_id, "year": year, "months": months}


# ---------------------------------------------------------------------------
# Lifecycle: dormancy and freezing
# ---------------------------------------------------------------------------

def dormancy_candidates(days: Optional[int] = None) -> list[str]:
    """Active accounts without ledger movement for the configured window."""
    window = days if days is not None else get_settings().dormancy_days
    cutoff = utc_now() - timedelta(days=window)
    out = []
    for account in list_accounts_by_status(STATUS_ACTIVE):
        rows = transactions.for_user(account.user_id)
        last = max((t.created_at for t in rows), default=account.opened_at)
        if last < cutoff:
            out.append(account.user_id)
    return sorted(out)


def mark_dormant(user_id: str) -> Account:
    account = require_account(user_id)
    account.transition(STATUS_DORMANT)
    invalidate_balance(user_id)
    return account


def reactivate(user_id: str) -> Account:
    account = require_account(user_id)
    if account.status not in (STATUS_DORMANT, STATUS_FROZEN):
        raise InvalidOperation("only dormant or frozen accounts can be reactivated")
    account.transition(STATUS_ACTIVE)
    invalidate_balance(user_id)
    return account


def freeze_account(user_id: str, reason: str, case_ref: str = "") -> Account:
    """Compliance action (PAY-1187). Frozen accounts must not withdraw."""
    account = require_account(user_id)
    account.transition(STATUS_FROZEN)
    account.metadata["frozen_reason"] = reason
    if case_ref:
        account.metadata["case"] = case_ref
    invalidate_balance(user_id)
    audit.record(
        EVENT_ACCOUNT_FROZEN,
        user_id=user_id,
        reason=reason,
        case=case_ref,
    )
    return account


def unfreeze_account(user_id: str, case_ref: str = "") -> Account:
    account = require_account(user_id)
    if not account.is_frozen:
        raise InvalidOperation("account is not frozen")
    account.transition(STATUS_ACTIVE)
    account.metadata.pop("frozen_reason", None)
    invalidate_balance(user_id)
    audit.record(EVENT_ACCOUNT_UNFROZEN, user_id=user_id, case=case_ref)
    return account


# ---------------------------------------------------------------------------
# Balance access
# ---------------------------------------------------------------------------

def balance_cache_key(user_id: str) -> str:
    return f"{BALANCE_CACHE_PREFIX}{user_id}"


def get_user_balance(user_id: str, currency: str = "EUR") -> Optional[dict]:
    """Authoritative balance read for a user, in the requested currency.

    The booked balance is converted through the FX engine when the account is
    held in a different currency than the one requested, so the amount is
    always expressed in `currency`. Unknown users yield None rather than an
    error: absence of a wallet is a normal outcome on this path, not a
    failure.
    """
    account = resolve_account(user_id)
    if account is None:
        return None

    amount = account.raw_balance
    if account.currency != currency:
        amount = convert(amount, account.currency, currency, quantize=False)

    payload = {
        "amount": float(quantize2(amount)),
        "currency": currency,
        "status": account.status,
        "last_updated": iso(account.updated_at),
    }

    cache.set(
        balance_cache_key(user_id),
        payload,
        ttl=get_settings().balance_cache_ttl_seconds,
    )
    audit.record(
        EVENT_BALANCE_READ,
        user_id=user_id,
        currency=currency,
        status=account.status,
    )
    return payload


def peek_cached_balance(user_id: str) -> Optional[dict]:
    """Read-only look at the cached snapshot; never touches the store."""
    return cache.get(balance_cache_key(user_id))


def invalidate_balance(user_id: str) -> None:
    """Drop the cached snapshot after any mutation of the account."""
    cache.delete(balance_cache_key(user_id))


# ---------------------------------------------------------------------------
# Daily snapshots and history
# ---------------------------------------------------------------------------

@dataclass
class DailySnapshot:
    user_id: str
    taken_at: datetime
    amount_eur: Decimal
    status: str


_SNAPSHOT_HISTORY: dict[str, list[DailySnapshot]] = {}


def record_daily_snapshot(user_id: str) -> DailySnapshot:
    """Store an EUR-normalized point for trend charts and dormancy review."""
    account = require_account(user_id)
    snap = DailySnapshot(
        user_id=user_id,
        taken_at=utc_now(),
        amount_eur=to_eur(account.raw_balance, account.currency),
        status=account.status,
    )
    _SNAPSHOT_HISTORY.setdefault(user_id, []).append(snap)
    return snap


def snapshot_series(user_id: str, limit: int = 90) -> list[DailySnapshot]:
    return _SNAPSHOT_HISTORY.get(user_id, [])[-limit:]


def snapshot_all_active() -> int:
    count = 0
    for account in list_accounts_by_status(STATUS_ACTIVE):
        record_daily_snapshot(account.user_id)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Tier classification and threshold scans
# ---------------------------------------------------------------------------

TIER_DUST = "dust"
TIER_LOW = "low"
TIER_STANDARD = "standard"
TIER_PREMIUM = "premium"


def classify_balance_tier(amount_eur) -> str:
    value = to_decimal(amount_eur)
    if value < Decimal("1"):
        return TIER_DUST
    if value < Decimal("50"):
        return TIER_LOW
    if value < Decimal("10000"):
        return TIER_STANDARD
    return TIER_PREMIUM


def accounts_below(threshold_eur) -> list[tuple[str, Decimal]]:
    """(user_id, amount_eur) pairs for active accounts under a threshold."""
    limit = to_decimal(threshold_eur)
    out = []
    for account in list_accounts_by_status(STATUS_ACTIVE):
        amount_eur = to_eur(account.raw_balance, account.currency)
        if amount_eur < limit:
            out.append((account.user_id, amount_eur))
    return sorted(out)


def tier_distribution() -> dict[str, int]:
    dist: dict[str, int] = {}
    for account in accounts.all():
        tier = classify_balance_tier(to_eur(account.raw_balance, account.currency))
        dist[tier] = dist.get(tier, 0) + 1
    return dist


# ---------------------------------------------------------------------------
# Interest accrual (savings pilot)
# ---------------------------------------------------------------------------

def accrue_daily_interest(rate_bps_annual: int = 150) -> dict[str, Decimal]:
    """Credit one day of interest to active accounts. Pilot feature.

    Uses simple daily proration of an annual rate expressed in basis points.
    Returns the credited amount per user for the payout report.
    """
    credited: dict[str, Decimal] = {}
    daily_factor = Decimal(rate_bps_annual) / Decimal(10000) / Decimal(365)
    for account in list_accounts_by_status(STATUS_ACTIVE):
        interest = quantize2(account.raw_balance * daily_factor)
        if interest <= 0:
            continue
        account.credit(interest)
        transactions.add(Transaction(
            txn_id=new_id("txn"),
            user_id=account.user_id,
            amount=interest,
            currency=account.currency,
            direction=Direction.CREDIT,
            category=Category.ADJUSTMENT,
            created_at=utc_now(),
            description="Daily interest accrual",
        ))
        invalidate_balance(account.user_id)
        credited[account.user_id] = interest
    return credited


# ---------------------------------------------------------------------------
# Reconciliation helpers
# ---------------------------------------------------------------------------

def ledger_delta(user_id: str) -> Decimal:
    """Booked balance minus signed ledger sum.

    A non-zero delta is expected for accounts migrated from the legacy
    system with an opening balance adjustment; large deltas are flagged.
    """
    account = require_account(user_id)
    ledger_sum = net_total(transactions.for_user(user_id))
    return quantize2(account.raw_balance - ledger_sum)


def reconciliation_report(tolerance_eur: str = "0.01") -> list[dict]:
    """Accounts whose EUR-normalized delta exceeds the tolerance."""
    tol = to_decimal(tolerance_eur)
    findings = []
    for account in accounts.all():
        delta = ledger_delta(account.user_id)
        delta_eur = to_eur(abs(delta), account.currency)
        if delta_eur > tol:
            findings.append({
                "user_id": account.user_id,
                "delta": delta,
                "delta_eur": delta_eur,
                "currency": account.currency,
                "status": account.status,
            })
    return sorted(findings, key=lambda f: f["delta_eur"], reverse=True)


def bulk_summary(user_ids: Iterable[str]) -> list[dict]:
    """Batch projection used by the back-office search screen."""
    out = []
    for user_id in user_ids:
        account = resolve_account(user_id)
        if account is None:
            out.append({"user_id": user_id, "exists": False})
            continue
        out.append({
            "user_id": user_id,
            "exists": True,
            "status": account.status,
            "currency": account.currency,
            "tier": classify_balance_tier(
                to_eur(account.raw_balance, account.currency)
            ),
        })
    return out
