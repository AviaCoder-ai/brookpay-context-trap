"""Payout lifecycle (bank transfers out of the wallet).

A payout goes through: requested -> approved -> submitted -> settled, or is
rejected/cancelled along the way. Approval is delegated to the risk engine
owned by the application; this module only orchestrates state and ledger
writes once a decision exists.

The separation matters: this module never decides whether a payout may
proceed, it only records that a decision was made and moves money once it
has been. That keeps the state machine here honest (every transition is
explicit and validated) and keeps the risk rules in one place rather than
smeared across the lifecycle.

Quoting is the exception to "no decisions here": a quote is an estimate of
fees and settlement timing, not an authorisation, so it can be produced
without consulting risk at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional

from brookpay.billing.tariffs import fee_for
from brookpay.config.constants import CURRENCY_SETTLEMENT_DAYS
from brookpay.core.errors import InvalidOperation
from brookpay.models.transaction import Category, Direction, Transaction
from brookpay.store.repository import accounts, transactions
from brookpay.utils.idgen import new_id
from brookpay.utils.money import quantize2, to_decimal
from brookpay.utils.timeutils import utc_now
from brookpay.utils.validation import require


class PayoutState(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    SETTLED = "settled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


_TERMINAL = {PayoutState.SETTLED, PayoutState.REJECTED, PayoutState.CANCELLED}

_TRANSITIONS: dict[PayoutState, set[PayoutState]] = {
    PayoutState.REQUESTED: {PayoutState.APPROVED, PayoutState.REJECTED,
                            PayoutState.CANCELLED},
    PayoutState.APPROVED: {PayoutState.SUBMITTED, PayoutState.CANCELLED},
    PayoutState.SUBMITTED: {PayoutState.SETTLED, PayoutState.REJECTED},
}


@dataclass
class Payout:
    payout_id: str
    user_id: str
    amount: Decimal
    currency: str
    iban_last4: str
    state: PayoutState = PayoutState.REQUESTED
    fee: Decimal = Decimal("0.00")
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    state_log: list[tuple[str, str]] = field(default_factory=list)
    rejection_reason: str = ""

    def transition(self, new_state: PayoutState, note: str = "") -> None:
        allowed = _TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise InvalidOperation(
                f"payout {self.payout_id}: {self.state.value} -> "
                f"{new_state.value} is not allowed"
            )
        self.state = new_state
        self.updated_at = utc_now()
        self.state_log.append((new_state.value, note))


_PAYOUTS: dict[str, Payout] = {}


# ---------------------------------------------------------------------------
# Settlement calendars
# ---------------------------------------------------------------------------

# Weekday indices the local rail does not settle on (Saturday, Sunday). Bank
# holidays are handled by the calendar service and are deliberately not
# vendored here: they change per country and per year, and a stale table
# would quote confidently wrong dates.
_NON_SETTLEMENT_WEEKDAYS = (5, 6)


def is_settlement_day(day: datetime) -> bool:
    """Whether the local rail settles on a given date."""
    return day.weekday() not in _NON_SETTLEMENT_WEEKDAYS


def add_business_days(start: datetime, days: int) -> datetime:
    """Advance a date by N settlement days, skipping weekends.

    Bank holidays are out of scope here; the calendar service applies them
    on top when it has a country context. Zero or negative days returns the
    start date unchanged.
    """
    if days <= 0:
        return start
    current = start
    remaining = days
    while remaining > 0:
        current = current + timedelta(days=1)
        if is_settlement_day(current):
            remaining -= 1
    return current


def settlement_eta(currency: str, submitted_at: Optional[datetime] = None) -> datetime:
    """Estimated settlement date for a currency's local rail."""
    submitted_at = submitted_at or utc_now()
    days = CURRENCY_SETTLEMENT_DAYS.get(currency.upper(), 2)
    if days == 0:
        return submitted_at
    return add_business_days(submitted_at, days)


def settlement_days_for(currency: str) -> int:
    """Business days a payout in this currency needs to settle."""
    return CURRENCY_SETTLEMENT_DAYS.get(currency.upper(), 2)


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PayoutQuote:
    """A non-binding estimate of what a payout would cost and when it lands."""

    user_id: str
    amount: Decimal
    fee: Decimal
    total: Decimal
    currency: str
    settlement_days: int
    destination_kind: str

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "amount": str(self.amount),
            "fee": str(self.fee),
            "total": str(self.total),
            "currency": self.currency,
            "settlement_days": self.settlement_days,
            "destination_kind": self.destination_kind,
        }


def classify_destination(destination: str) -> str:
    """Best-effort classification of a payout destination identifier.

    Display metadata only: the rail is chosen by the banking integration
    from the full destination details, not from this label.
    """
    compact = "".join(destination.split()).upper()
    if not compact:
        return "unknown"
    if compact.startswith("CARD"):
        return "card"
    if len(compact) >= 15 and compact[:2].isalpha():
        return "iban"
    return "account_ref"


def quote_payout(user_id: str, amount, currency: str, destination: str = "") -> dict:
    """Estimate fees and settlement timing for a payout, without creating one.

    A quote is not an authorisation: it consults no risk rules, moves no
    money and records nothing. It exists so the UI can show "you will
    receive X on day Y" before the customer commits. The fee is the same one
    request_payout would apply, so the quoted total matches what a real
    request would charge.
    """
    amt = quantize2(to_decimal(amount))
    require(amt > 0, "quoted amount must be positive")
    fee = fee_for("payout", amt)
    return PayoutQuote(
        user_id=user_id,
        amount=amt,
        fee=fee,
        total=quantize2(amt + fee),
        currency=currency.upper(),
        settlement_days=settlement_days_for(currency),
        destination_kind=classify_destination(destination),
    ).as_dict()


def quote_many(user_id: str, amounts: list, currency: str) -> list[dict]:
    """Quote a ladder of amounts, for the "how much to send" picker."""
    return [quote_payout(user_id, a, currency) for a in amounts]


def effective_rate(quote: dict) -> Decimal:
    """Fee as a fraction of the payout amount, for the fee transparency line."""
    amount = to_decimal(quote["amount"])
    if amount <= 0:
        return Decimal("0")
    return to_decimal(quote["fee"]) / amount


def request_payout(user_id: str, amount, currency: str, iban_last4: str) -> Payout:
    """Register a payout request. No funds move at this stage."""
    amt = quantize2(to_decimal(amount))
    require(amt > 0, "payout amount must be positive")
    require(len(iban_last4) == 4 and iban_last4.isdigit(),
            "iban_last4 must be 4 digits")
    payout = Payout(
        payout_id=new_id("po"),
        user_id=user_id,
        amount=amt,
        currency=currency,
        iban_last4=iban_last4,
        fee=fee_for("payout", amt),
    )
    payout.state_log.append((PayoutState.REQUESTED.value, "created"))
    _PAYOUTS[payout.payout_id] = payout
    return payout


def apply_decision(payout_id: str, allowed: bool, reason: str = "") -> Payout:
    """Record the risk decision on a requested payout."""
    payout = get_payout(payout_id)
    if allowed:
        payout.transition(PayoutState.APPROVED, "risk approved")
    else:
        payout.rejection_reason = reason
        payout.transition(PayoutState.REJECTED, f"risk denied: {reason}")
    return payout


def submit_to_bank(payout_id: str) -> Payout:
    """Debit the wallet and hand the transfer to the banking rail."""
    payout = get_payout(payout_id)
    if payout.state != PayoutState.APPROVED:
        raise InvalidOperation("only approved payouts can be submitted")
    account = accounts.get(payout.user_id)
    total = quantize2(payout.amount + payout.fee)
    account.debit(total)
    transactions.add(Transaction(
        txn_id=new_id("txn"),
        user_id=payout.user_id,
        amount=payout.amount,
        currency=account.currency,
        direction=Direction.DEBIT,
        category=Category.WITHDRAWAL,
        created_at=utc_now(),
        description=f"Payout {payout.payout_id} to ****{payout.iban_last4}",
    ))
    if payout.fee > 0:
        transactions.add(Transaction(
            txn_id=new_id("txn"),
            user_id=payout.user_id,
            amount=payout.fee,
            currency=account.currency,
            direction=Direction.DEBIT,
            category=Category.FEE,
            created_at=utc_now(),
            description=f"Payout fee {payout.payout_id}",
        ))
    payout.transition(PayoutState.SUBMITTED, "sent to bank rail")
    return payout


def mark_settled(payout_id: str, bank_reference: str) -> Payout:
    payout = get_payout(payout_id)
    payout.transition(PayoutState.SETTLED, f"bank ref {bank_reference}")
    return payout


def cancel(payout_id: str, note: str = "customer request") -> Payout:
    payout = get_payout(payout_id)
    if payout.state in _TERMINAL:
        raise InvalidOperation("payout already terminal")
    payout.transition(PayoutState.CANCELLED, note)
    return payout


def get_payout(payout_id: str) -> Payout:
    try:
        return _PAYOUTS[payout_id]
    except KeyError:
        raise InvalidOperation(f"unknown payout '{payout_id}'") from None


def pending_for_user(user_id: str) -> list[Payout]:
    return [
        p for p in _PAYOUTS.values()
        if p.user_id == user_id and p.state not in _TERMINAL
    ]


def daily_submitted_volume(user_id: str) -> Decimal:
    """EUR-agnostic raw sum of today's submitted payouts (ops dashboard)."""
    today = utc_now().date()
    total = Decimal("0")
    for p in _PAYOUTS.values():
        if p.user_id != user_id:
            continue
        if p.state in (PayoutState.SUBMITTED, PayoutState.SETTLED) \
                and p.updated_at.date() == today:
            total += p.amount
    return quantize2(total)


# ---------------------------------------------------------------------------
# Batch files (pain.001 assembly)
# ---------------------------------------------------------------------------

@dataclass
class PayoutBatch:
    """A set of approved payouts handed to the rail as one file."""

    batch_id: str
    currency: str
    payout_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    submitted: bool = False

    @property
    def size(self) -> int:
        return len(self.payout_ids)


def build_batch(payout_ids: list[str], currency: str) -> PayoutBatch:
    """Group approved payouts of one currency into a submission batch.

    Every payout must be approved and denominated in the batch currency; a
    mismatch is a caller bug rather than a business outcome, so it raises.
    """
    batch = PayoutBatch(batch_id=new_id("batch"), currency=currency.upper())
    for payout_id in payout_ids:
        payout = get_payout(payout_id)
        if payout.state != PayoutState.APPROVED:
            raise InvalidOperation(
                f"payout {payout_id} is {payout.state.value}, not approved"
            )
        if payout.currency.upper() != batch.currency:
            raise InvalidOperation(
                f"payout {payout_id} is {payout.currency}, batch is {batch.currency}"
            )
        batch.payout_ids.append(payout_id)
    return batch


def batch_total(batch: PayoutBatch) -> Decimal:
    """Sum of amounts plus fees across a batch."""
    total = Decimal("0")
    for payout_id in batch.payout_ids:
        payout = get_payout(payout_id)
        total += payout.amount + payout.fee
    return quantize2(total)


def submit_batch(batch: PayoutBatch) -> list[Payout]:
    """Submit every payout in a batch, in order.

    Best effort per payout: one failing submission does not abort the rest,
    because a partially submitted file is still a valid file at the rail. The
    caller reconciles the returned list against the batch.
    """
    submitted: list[Payout] = []
    for payout_id in batch.payout_ids:
        try:
            submitted.append(submit_to_bank(payout_id))
        except InvalidOperation:
            continue
    batch.submitted = True
    return submitted


# ---------------------------------------------------------------------------
# Rail responses
# ---------------------------------------------------------------------------

# Return codes the banking rail sends back on a failed transfer, mapped to
# whether the payout may be retried. Codes are the rail's, not ours; the
# mapping is what our operations team agreed with them.
_RAIL_RETRYABLE = {
    "AC01": False,  # incorrect account number
    "AC04": False,  # closed account
    "AC06": False,  # blocked account
    "AM04": True,   # insufficient funds at the rail
    "AG01": False,  # transaction forbidden
    "MD07": False,  # end customer deceased
    "TM01": True,   # cut-off time missed
    "SL01": True,   # specific service offered by debtor agent
}


def is_retryable_rail_code(code: str) -> bool:
    """Whether a rail return code allows a retry. Unknown codes do not."""
    return _RAIL_RETRYABLE.get(code.upper(), False)


def rail_code_is_known(code: str) -> bool:
    return code.upper() in _RAIL_RETRYABLE


def handle_rail_rejection(payout_id: str, code: str) -> Payout:
    """Record a rail rejection against a submitted payout.

    The payout moves to rejected regardless of retryability; a retry creates
    a new payout rather than resurrecting this one, so the state machine
    stays acyclic and every attempt has its own audit trail.
    """
    payout = get_payout(payout_id)
    payout.rejection_reason = f"rail:{code.upper()}"
    payout.transition(PayoutState.REJECTED, f"rail rejected {code.upper()}")
    return payout


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_against_statement(bank_rows: list[tuple[str, Decimal]]) -> dict:
    """Match bank statement rows against submitted payouts.

    Rows arrive as (payout_reference, amount). A row that matches a known
    submitted payout of the same amount is a clean match; anything else is
    surfaced for an operator rather than guessed at.
    """
    matched: list[str] = []
    amount_mismatch: list[str] = []
    unknown: list[str] = []

    for reference, amount in bank_rows:
        payout = _PAYOUTS.get(reference)
        if payout is None:
            unknown.append(reference)
            continue
        expected = quantize2(payout.amount + payout.fee)
        if quantize2(to_decimal(amount)) == expected:
            matched.append(reference)
        else:
            amount_mismatch.append(reference)

    return {
        "matched": matched,
        "amount_mismatch": amount_mismatch,
        "unknown": unknown,
        "clean": not amount_mismatch and not unknown,
    }


def unsettled_older_than(hours: float) -> list[Payout]:
    """Submitted payouts that have not settled within a time budget.

    The ops runbook chases these with the rail. Uses the payout's last update
    time, which for a submitted payout is its submission moment.
    """
    now = utc_now()
    stale: list[Payout] = []
    for payout in _PAYOUTS.values():
        if payout.state != PayoutState.SUBMITTED:
            continue
        elapsed = (now - payout.updated_at).total_seconds() / 3600
        if elapsed > hours:
            stale.append(payout)
    return stale


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def state_counts() -> dict[str, int]:
    """How many payouts sit in each state, for the ops dashboard."""
    counts: dict[str, int] = {}
    for payout in _PAYOUTS.values():
        counts[payout.state.value] = counts.get(payout.state.value, 0) + 1
    return counts


def rejection_reasons() -> dict[str, int]:
    """Frequency of each rejection reason among rejected payouts."""
    counts: dict[str, int] = {}
    for payout in _PAYOUTS.values():
        if payout.state != PayoutState.REJECTED or not payout.rejection_reason:
            continue
        counts[payout.rejection_reason] = counts.get(payout.rejection_reason, 0) + 1
    return counts


def fee_revenue(currency: Optional[str] = None) -> Decimal:
    """Total fees collected on submitted or settled payouts."""
    total = Decimal("0")
    for payout in _PAYOUTS.values():
        if payout.state not in (PayoutState.SUBMITTED, PayoutState.SETTLED):
            continue
        if currency and payout.currency.upper() != currency.upper():
            continue
        total += payout.fee
    return quantize2(total)


def payout_history(user_id: str) -> list[dict]:
    """Compact history of a user's payouts for the account screen."""
    rows: list[dict] = []
    for payout in _PAYOUTS.values():
        if payout.user_id != user_id:
            continue
        rows.append({
            "payout_id": payout.payout_id,
            "amount": str(payout.amount),
            "fee": str(payout.fee),
            "currency": payout.currency,
            "state": payout.state.value,
            "destination": f"****{payout.iban_last4}",
            "transitions": len(payout.state_log),
        })
    return sorted(rows, key=lambda r: r["payout_id"])


def state_machine_description() -> dict:
    """Machine-readable description of the payout state machine.

    Used by the docs generator and by the ops console to render the allowed
    transitions without duplicating the table.
    """
    return {
        "states": [s.value for s in PayoutState],
        "terminal": sorted(s.value for s in _TERMINAL),
        "transitions": {
            state.value: sorted(t.value for t in targets)
            for state, targets in _TRANSITIONS.items()
        },
    }


# ---------------------------------------------------------------------------
# Cut-off windows
# ---------------------------------------------------------------------------

# Hour (UTC) after which a payout submitted today settles on the next
# business day instead. Mirrors the correspondent banks' cut-offs; the ETA
# display consults these so a late-afternoon payout does not promise today.
_CUT_OFF_HOURS = {
    "EUR": 16,
    "GBP": 15,
    "USD": 21,
    "CHF": 15,
    "SGD": 10,
    "THB": 9,
    "JPY": 6,
}


def cut_off_hour(currency: str) -> int:
    """UTC cut-off hour for a currency, defaulting to end of day."""
    return _CUT_OFF_HOURS.get(currency.upper(), 23)


def missed_cut_off(currency: str, at: Optional[datetime] = None) -> bool:
    """Whether a submission at this moment misses today's cut-off."""
    at = at or utc_now()
    return at.hour >= cut_off_hour(currency)


def effective_submission_day(currency: str, at: Optional[datetime] = None) -> datetime:
    """The day a submission is treated as sent, honouring cut-offs.

    A submission past the cut-off, or on a non-settlement day, rolls to the
    next settlement day. This is what the ETA display quotes from.
    """
    at = at or utc_now()
    day = at
    if missed_cut_off(currency, at):
        day = add_business_days(day, 1)
    while not is_settlement_day(day):
        day = day + timedelta(days=1)
    return day


def quote_with_cut_off(user_id: str, amount, currency: str, destination: str = "") -> dict:
    """A quote that accounts for today's cut-off in its settlement estimate.

    Same fee arithmetic as quote_payout; only the timing differs, because it
    starts counting from the effective submission day rather than from now.
    """
    quote = quote_payout(user_id, amount, currency, destination)
    submission_day = effective_submission_day(currency)
    quote["effective_submission"] = submission_day.date().isoformat()
    quote["missed_cut_off"] = missed_cut_off(currency)
    return quote
