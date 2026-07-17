"""Withdrawal risk review.

The engine is wired with a funds source callable at application startup and
never imports account internals directly; that keeps risk logic testable
against synthetic providers and free of circular imports. Everything the
engine needs about an account arrives through that one callable, as an
opaque balance snapshot whose shape is owned by the balance read path.

The review pipeline is intentionally linear and side-effect light: it
gathers a few signals, evaluates a fixed ladder of rules, and returns a
frozen `Decision`. Denials are audited here; approvals are audited
downstream by the ledger once the payout actually settles, so that a review
that approves but whose payout later fails does not leave a phantom
"approved" event with no matching money movement.

Historical note (PAY-1187, CHANGELOG 1.3.0): account freezing was added
after the first version of this engine shipped. The snapshot readers below
carry a backward-compatibility path for the pre-freeze snapshot shape; that
path is load-bearing and is covered by the end-to-end scenarios.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional

from brookpay.config.constants import (
    EVENT_WITHDRAWAL_DENIED,
    STATUS_ACTIVE,
    STATUS_DORMANT,
    STATUS_FROZEN,
)
from brookpay.core import audit
from brookpay.fx.engine import to_eur
from brookpay.risk.limits import VelocityTracker
from brookpay.utils.idgen import new_id
from brookpay.utils.money import to_decimal
from brookpay.utils.validation import is_supported_currency, is_valid_user_id


# ---------------------------------------------------------------------------
# Denial reason taxonomy
# ---------------------------------------------------------------------------

# Reason strings are API surface: they appear in the audit trail, in support
# macros (see constants.SUPPORT_MACRO_KEYS) and in partner webhooks. Never
# rename a reason without a deprecation window; add a new one instead.
REASON_INVALID_USER = "invalid_user_id"
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"
REASON_NON_POSITIVE = "non_positive_amount"
REASON_UNKNOWN_ACCOUNT = "unknown_account"
REASON_FROZEN = "account_frozen"
REASON_DORMANT = "account_dormant"
REASON_UNREADABLE = "unreadable_balance"
REASON_INSUFFICIENT = "insufficient_funds"
REASON_VELOCITY_HOURLY = "velocity_hourly_count"
REASON_VELOCITY_DAILY = "velocity_daily_volume"


class ReasonClass(Enum):
    """Coarse grouping used by dashboards and support routing."""

    INPUT = "input"
    LIFECYCLE = "lifecycle"
    FUNDS = "funds"
    VELOCITY = "velocity"
    UNKNOWN = "unknown"


# Which coarse class each reason rolls up to. A reason missing from this map
# is reported as UNKNOWN rather than crashing the dashboard.
REASON_CLASS = {
    REASON_INVALID_USER: ReasonClass.INPUT,
    REASON_UNSUPPORTED_CURRENCY: ReasonClass.INPUT,
    REASON_NON_POSITIVE: ReasonClass.INPUT,
    REASON_UNKNOWN_ACCOUNT: ReasonClass.LIFECYCLE,
    REASON_FROZEN: ReasonClass.LIFECYCLE,
    REASON_DORMANT: ReasonClass.LIFECYCLE,
    REASON_UNREADABLE: ReasonClass.FUNDS,
    REASON_INSUFFICIENT: ReasonClass.FUNDS,
    REASON_VELOCITY_HOURLY: ReasonClass.VELOCITY,
    REASON_VELOCITY_DAILY: ReasonClass.VELOCITY,
}


def classify_reason(reason: str) -> ReasonClass:
    """Map a denial reason to its coarse class, tolerant of unknown values."""
    return REASON_CLASS.get(reason, ReasonClass.UNKNOWN)


def is_terminal_reason(reason: str) -> bool:
    """Terminal reasons will not clear by retrying; the UI says so.

    Lifecycle denials (frozen, dormant, unknown) do not resolve on retry:
    the customer must act (contact support, reactivate) first. Funds and
    velocity denials are transient and worth retrying later.
    """
    return classify_reason(reason) is ReasonClass.LIFECYCLE


# ---------------------------------------------------------------------------
# Decision value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    review_id: str

    @classmethod
    def allow(cls, review_id: str) -> "Decision":
        return cls(allowed=True, reason="", review_id=review_id)

    @classmethod
    def deny(cls, reason: str, review_id: str) -> "Decision":
        return cls(allowed=False, reason=reason, review_id=review_id)

    @property
    def denied(self) -> bool:
        return not self.allowed

    @property
    def reason_class(self) -> ReasonClass:
        return classify_reason(self.reason)

    @property
    def retryable(self) -> bool:
        """True when retrying the same request could plausibly succeed."""
        if self.allowed:
            return False
        return not is_terminal_reason(self.reason)

    def as_dict(self) -> dict:
        """Serialisation for the audit sink and the partner webhook."""
        return {
            "review_id": self.review_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "reason_class": self.reason_class.value,
            "retryable": self.retryable,
        }


# ---------------------------------------------------------------------------
# Request envelope and signals
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WithdrawalRequest:
    """Normalised view of an inbound withdrawal request."""

    user_id: str
    amount: Decimal
    currency: str
    destination: str = ""
    channel: str = "api"

    @classmethod
    def build(cls, user_id, amount, currency: str, **extra) -> "WithdrawalRequest":
        return cls(
            user_id=user_id,
            amount=to_decimal(amount),
            currency=currency,
            destination=str(extra.get("destination", "")),
            channel=str(extra.get("channel", "api")),
        )


@dataclass
class RiskSignals:
    """Signals gathered before the rule ladder runs.

    Kept as a plain container so the ladder stays a pure function of the
    signals; that makes each rule unit-testable without a live engine.
    """

    amount_eur: Decimal = Decimal("0")
    velocity_count_hour: int = 0
    velocity_volume_day: Decimal = Decimal("0")
    destination_new: bool = False
    high_value: bool = False

    def flags(self) -> tuple[str, ...]:
        out = []
        if self.destination_new:
            out.append("new_destination")
        if self.high_value:
            out.append("high_value")
        return tuple(out)


# High-value threshold in EUR. Above this, reviews are annotated for the
# manual queue even when they are approved, so analysts can spot-check.
HIGH_VALUE_EUR = Decimal("10000")


def _looks_high_value(amount_eur: Decimal) -> bool:
    return amount_eur >= HIGH_VALUE_EUR


def _normalise_destination(destination: str) -> str:
    """Collapse a destination identifier to a comparable form.

    IBANs arrive with spaces and mixed case; card tokens arrive prefixed.
    This is a display/compare helper only, never used for validation.
    """
    cleaned = "".join(ch for ch in destination if not ch.isspace())
    return cleaned.upper()


# Destinations the product will never pay out to, regardless of balance.
# Kept tiny and literal on purpose: anything list-driven belongs in the
# sanctions screening service, not in the hot path of a withdrawal review.
_BLOCKED_DESTINATION_PREFIXES = ("TEST", "SANDBOX", "INTERNAL")


def _is_blocked_destination(destination: str) -> bool:
    """Cheap prefix guard against obviously non-payable destinations.

    This is not sanctions screening; it only catches internal and test
    identifiers that must never receive real money if they leak into a
    production request. Real screening happens upstream and asynchronously.
    """
    if not destination:
        return False
    normalised = _normalise_destination(destination)
    return any(normalised.startswith(p) for p in _BLOCKED_DESTINATION_PREFIXES)


def _destination_kind(destination: str) -> str:
    """Best-effort classification of a destination identifier for display."""
    normalised = _normalise_destination(destination)
    if not normalised:
        return "unknown"
    if normalised.startswith("CARD"):
        return "card_token"
    if len(normalised) >= 15 and normalised[:2].isalpha():
        return "iban"
    return "account_ref"


# ---------------------------------------------------------------------------
# Snapshot readers (backward-compatibility shim lives here)
# ---------------------------------------------------------------------------

def _snapshot_status(snapshot) -> str:
    """Extract the lifecycle status from a balance snapshot.

    Backward compatibility (see CHANGELOG 1.3.0 / PAY-1187): snapshots
    produced before 1.3.0 were bare numeric amounts. Account freezing did
    not exist at the time, so a bare number maps to an active account.

    This branch is the reason the frozen-account guard depends on the
    snapshot keeping its structured shape: if the funds source ever returns
    a bare number again, every account reads as active here and the freeze
    check below silently stops firing.
    """
    if isinstance(snapshot, Mapping):
        return snapshot.get("status", STATUS_ACTIVE)
    return STATUS_ACTIVE


def _snapshot_amount(snapshot) -> Optional[Decimal]:
    """Extract the available amount, tolerant of the pre-1.3.0 shape.

    A structured snapshot carries the amount under the "amount" key; a
    legacy bare snapshot is itself the amount. Anything unparneable yields
    None, which the ladder treats as an unreadable balance (a denial, never
    an approval).
    """
    if isinstance(snapshot, Mapping):
        raw = snapshot.get("amount")
    else:
        raw = snapshot
    try:
        return to_decimal(raw)
    except (ValueError, TypeError):
        return None


def _snapshot_is_legacy(snapshot) -> bool:
    """True when the snapshot is the pre-1.3.0 bare-number shape.

    Exposed for diagnostics: a spike here means some producer regressed to
    the legacy shape, which is exactly the PAY-1187 failure mode.
    """
    return not isinstance(snapshot, Mapping)


def _snapshot_currency(snapshot, fallback: str) -> str:
    """Currency of a structured snapshot, or the caller fallback."""
    if isinstance(snapshot, Mapping):
        return snapshot.get("currency", fallback)
    return fallback


# ---------------------------------------------------------------------------
# Rule ladder (pure functions of the gathered signals)
# ---------------------------------------------------------------------------

def _check_inputs(request: WithdrawalRequest) -> Optional[str]:
    """Input-shape rules. Returns a reason string or None if all pass."""
    if not is_valid_user_id(request.user_id):
        return REASON_INVALID_USER
    if not is_supported_currency(request.currency):
        return REASON_UNSUPPORTED_CURRENCY
    if request.amount <= 0:
        return REASON_NON_POSITIVE
    return None


def _check_lifecycle(status: str) -> Optional[str]:
    """Lifecycle rules keyed off the snapshot status.

    Order matters: frozen is checked before dormant, and any non-active
    status that is neither maps to a generic per-status reason. The frozen
    branch here is the one PAY-1187 is about.
    """
    if status == STATUS_FROZEN:
        return REASON_FROZEN
    if status == STATUS_DORMANT:
        return REASON_DORMANT
    if status != STATUS_ACTIVE:
        return f"account_{status}"
    return None


def _check_funds(available: Optional[Decimal], requested: Decimal) -> Optional[str]:
    """Funds rules. None here means the balance itself was unreadable."""
    if available is None:
        return REASON_UNREADABLE
    if available < requested:
        return REASON_INSUFFICIENT
    return None


# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------

class RiskEngine:
    """Approves or denies withdrawal requests.

    Denials are recorded in the audit trail; approvals are recorded further
    downstream by the ledger once the payout settles. The engine holds no
    account state of its own: the injected funds source is the only bridge
    to the account world, and the velocity tracker is the only mutable
    state, scoped per user.
    """

    def __init__(
        self,
        funds_source: Callable[..., object],
        velocity: Optional[VelocityTracker] = None,
    ) -> None:
        self._funds_source = funds_source
        self._velocity = velocity or VelocityTracker()
        self._reviewed = 0
        self._denied = 0

    # -- introspection ------------------------------------------------------

    @property
    def reviewed_count(self) -> int:
        return self._reviewed

    @property
    def denied_count(self) -> int:
        return self._denied

    def approval_rate(self) -> float:
        """Approvals over reviews since process start, for the dashboard."""
        if self._reviewed == 0:
            return 1.0
        return (self._reviewed - self._denied) / self._reviewed

    # -- signal gathering ---------------------------------------------------

    def _gather_signals(
        self,
        request: WithdrawalRequest,
        available: Optional[Decimal],
    ) -> RiskSignals:
        """Collect the annotations the manual queue cares about.

        Pure with respect to account state: everything here derives from the
        request and the already-read snapshot amount. Velocity figures are
        read (not recorded) so an eventual denial does not pollute the
        window with a request that never settled.
        """
        amount_eur = to_eur(request.amount, request.currency)
        return RiskSignals(
            amount_eur=amount_eur,
            destination_new=bool(request.destination)
            and self._velocity.count(request.user_id) == 0,
            high_value=_looks_high_value(amount_eur),
        )

    # -- main entry point ---------------------------------------------------

    def review_withdrawal(
        self,
        user_id: str,
        amount,
        currency: str = "EUR",
        **extra,
    ) -> Decision:
        """Run the full rule ladder and return a frozen Decision.

        The ladder is fixed and ordered: inputs, then lifecycle (which needs
        the snapshot status), then funds (which needs the snapshot amount),
        then velocity. The snapshot is read exactly once, here, through the
        injected funds source; every downstream rule consumes that single
        read via the snapshot readers above.
        """
        request = WithdrawalRequest.build(user_id, amount, currency, **extra)
        review_id = new_id("rvw")
        self._reviewed += 1

        input_reason = _check_inputs(request)
        if input_reason is not None:
            return self._deny(input_reason, request.user_id, review_id)

        account_state = self._funds_source(request.user_id, currency=request.currency)

        if account_state is None:
            return self._deny(REASON_UNKNOWN_ACCOUNT, request.user_id, review_id)

        status = _snapshot_status(account_state)
        lifecycle_reason = _check_lifecycle(status)
        if lifecycle_reason is not None:
            return self._deny(lifecycle_reason, request.user_id, review_id)

        available = _snapshot_amount(account_state)
        funds_reason = _check_funds(available, request.amount)
        if funds_reason is not None:
            return self._deny(funds_reason, request.user_id, review_id)

        amount_eur = to_eur(request.amount, request.currency)
        ok, velocity_reason = self._velocity.check(request.user_id, amount_eur)
        if not ok:
            return self._deny(velocity_reason, request.user_id, review_id)

        self._velocity.record(request.user_id, amount_eur)
        return Decision.allow(review_id)

    # -- batch and explain --------------------------------------------------

    def review_many(self, requests) -> list[Decision]:
        """Review an iterable of (user_id, amount, currency) tuples."""
        out: list[Decision] = []
        for user_id, amount, currency in requests:
            out.append(self.review_withdrawal(user_id, amount, currency))
        return out

    def dry_run(self, user_id: str, amount, currency: str = "EUR") -> dict:
        """Explain what would happen without recording velocity.

        Used by the support console to answer "why can't my customer
        withdraw" without mutating any window. Mirrors the ladder but stops
        at the first failing rule and never calls velocity.record.
        """
        request = WithdrawalRequest.build(user_id, amount, currency)
        trace: list[str] = []

        input_reason = _check_inputs(request)
        trace.append(f"inputs:{input_reason or 'ok'}")
        if input_reason is not None:
            return {"would_allow": False, "reason": input_reason, "trace": trace}

        account_state = self._funds_source(request.user_id, currency=request.currency)
        if account_state is None:
            trace.append("account:missing")
            return {
                "would_allow": False,
                "reason": REASON_UNKNOWN_ACCOUNT,
                "trace": trace,
            }

        status = _snapshot_status(account_state)
        trace.append(f"status:{status}")
        lifecycle_reason = _check_lifecycle(status)
        if lifecycle_reason is not None:
            return {
                "would_allow": False,
                "reason": lifecycle_reason,
                "trace": trace,
            }

        available = _snapshot_amount(account_state)
        trace.append(f"available:{'unreadable' if available is None else available}")
        funds_reason = _check_funds(available, request.amount)
        if funds_reason is not None:
            return {"would_allow": False, "reason": funds_reason, "trace": trace}

        trace.append("velocity:not-evaluated-in-dry-run")
        return {"would_allow": True, "reason": "", "trace": trace}

    # -- denial recording ---------------------------------------------------

    def _deny(self, reason: str, user_id: str, review_id: str) -> Decision:
        self._denied += 1
        audit.record(
            EVENT_WITHDRAWAL_DENIED,
            user_id=user_id,
            reason=reason,
            review_id=review_id,
        )
        return Decision.deny(reason, review_id)


# ---------------------------------------------------------------------------
# Manual review queue (analyst tooling)
# ---------------------------------------------------------------------------

@dataclass
class QueueItem:
    review_id: str
    user_id: str
    amount_eur: Decimal
    flags: tuple[str, ...]
    resolved: bool = False
    resolution: str = ""


class ReviewQueue:
    """In-memory queue of reviews an analyst should look at.

    Populated by high-value approvals and by any denial classified as
    lifecycle, which are the two cases the compliance analysts triage. This
    is tooling around the engine, not part of the decision path.
    """

    def __init__(self) -> None:
        self._items: list[QueueItem] = []

    def enqueue(
        self,
        decision: Decision,
        user_id: str,
        amount_eur: Decimal,
        flags: tuple[str, ...] = (),
    ) -> QueueItem:
        item = QueueItem(
            review_id=decision.review_id,
            user_id=user_id,
            amount_eur=amount_eur,
            flags=flags,
        )
        self._items.append(item)
        return item

    def pending(self) -> list[QueueItem]:
        return [i for i in self._items if not i.resolved]

    def resolve(self, review_id: str, resolution: str) -> bool:
        for item in self._items:
            if item.review_id == review_id and not item.resolved:
                item.resolved = True
                item.resolution = resolution
                return True
        return False

    def summary(self) -> dict:
        pending = self.pending()
        return {
            "total": len(self._items),
            "pending": len(pending),
            "pending_value_eur": str(sum((i.amount_eur for i in pending), Decimal("0"))),
        }


# ---------------------------------------------------------------------------
# Engine metrics
# ---------------------------------------------------------------------------

@dataclass
class EngineMetrics:
    """Rolling counters exposed on the diagnostics endpoint.

    Deliberately additive and cheap: every counter is a plain int keyed by a
    reason or reason class, so the metrics scrape never walks account state.
    The engine feeds these through `observe`; nothing here influences a
    decision, it only describes decisions already made.
    """

    reviews: int = 0
    approvals: int = 0
    denials: int = 0
    by_reason: dict = field(default_factory=dict)
    by_class: dict = field(default_factory=dict)

    def observe(self, decision: Decision) -> None:
        self.reviews += 1
        if decision.allowed:
            self.approvals += 1
            return
        self.denials += 1
        self.by_reason[decision.reason] = self.by_reason.get(decision.reason, 0) + 1
        cls = decision.reason_class.value
        self.by_class[cls] = self.by_class.get(cls, 0) + 1

    def approval_rate(self) -> float:
        if self.reviews == 0:
            return 1.0
        return self.approvals / self.reviews

    def top_reasons(self, limit: int = 5) -> list[tuple[str, int]]:
        ordered = sorted(self.by_reason.items(), key=lambda kv: kv[1], reverse=True)
        return ordered[:limit]

    def as_dict(self) -> dict:
        return {
            "reviews": self.reviews,
            "approvals": self.approvals,
            "denials": self.denials,
            "approval_rate": round(self.approval_rate(), 4),
            "by_reason": dict(self.by_reason),
            "by_class": dict(self.by_class),
        }


# ---------------------------------------------------------------------------
# Decision journal
# ---------------------------------------------------------------------------

@dataclass
class JournalEntry:
    review_id: str
    user_id: str
    allowed: bool
    reason: str
    amount_eur: str


class DecisionJournal:
    """Bounded ring buffer of recent decisions for the support console.

    Bounded because it lives in process memory and the console only ever
    shows the last N; the durable record is the audit trail. Oldest entries
    are dropped once the cap is reached.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = max(1, capacity)
        self._entries: list[JournalEntry] = []

    def append(self, decision: Decision, user_id: str, amount_eur: Decimal) -> None:
        self._entries.append(
            JournalEntry(
                review_id=decision.review_id,
                user_id=user_id,
                allowed=decision.allowed,
                reason=decision.reason,
                amount_eur=str(amount_eur),
            )
        )
        if len(self._entries) > self._capacity:
            overflow = len(self._entries) - self._capacity
            self._entries = self._entries[overflow:]

    def recent(self, limit: int = 50) -> list[JournalEntry]:
        return self._entries[-limit:]

    def for_user(self, user_id: str) -> list[JournalEntry]:
        return [e for e in self._entries if e.user_id == user_id]

    def denial_streak(self, user_id: str) -> int:
        """How many of this user's most recent decisions were denials.

        A long streak is a support signal (the customer is stuck), surfaced
        so an agent can proactively reach out. Purely observational.
        """
        streak = 0
        for entry in reversed(self.for_user(user_id)):
            if entry.allowed:
                break
            streak += 1
        return streak


def build_default_engine(funds_source: Callable[..., object]) -> RiskEngine:
    """Convenience constructor mirroring the wiring defaults.

    The composition root builds the engine explicitly; this helper exists
    for scripts and tests that want an engine with the standard velocity
    tracker without importing the tracker themselves.
    """
    return RiskEngine(funds_source=funds_source, velocity=VelocityTracker())
