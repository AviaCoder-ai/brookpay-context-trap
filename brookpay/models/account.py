"""Account aggregate.

The account is the only object allowed to mutate its own balance: every
credit and debit goes through the guarded methods below, which enforce the
lifecycle rules (a closed account takes no money, a dormant one takes no
debits) and keep the updated timestamp honest. Services orchestrate, the
aggregate protects its invariants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from brookpay.config.constants import (
    CREDITABLE_STATUSES,
    SPENDABLE_STATUSES,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_DORMANT,
    STATUS_FROZEN,
)
from brookpay.core.errors import InvalidOperation
from brookpay.utils.money import quantize2, to_decimal
from brookpay.utils.timeutils import utc_now


@dataclass
class Account:
    user_id: str
    raw_balance: Decimal
    currency: str
    status: str
    opened_at: datetime
    updated_at: datetime
    kyc_level: int = 1
    metadata: dict = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_frozen(self) -> bool:
        return self.status == STATUS_FROZEN

    def touch(self) -> None:
        self.updated_at = utc_now()

    def credit(self, amount) -> Decimal:
        amt = quantize2(to_decimal(amount))
        if amt <= 0:
            raise InvalidOperation("credit amount must be positive")
        if self.status == STATUS_CLOSED:
            raise InvalidOperation("cannot credit a closed account")
        self.raw_balance = quantize2(self.raw_balance + amt)
        self.touch()
        return self.raw_balance

    def debit(self, amount) -> Decimal:
        amt = quantize2(to_decimal(amount))
        if amt <= 0:
            raise InvalidOperation("debit amount must be positive")
        if self.status in (STATUS_CLOSED, STATUS_DORMANT):
            raise InvalidOperation(f"cannot debit a {self.status} account")
        if self.raw_balance < amt:
            raise InvalidOperation("insufficient funds")
        self.raw_balance = quantize2(self.raw_balance - amt)
        self.touch()
        return self.raw_balance

    def transition(self, new_status: str) -> None:
        if new_status == self.status:
            return
        if self.status == STATUS_CLOSED:
            raise InvalidOperation("closed accounts cannot change status")
        self.status = new_status
        self.touch()

    def transition_to(self, new_status: str, reason: str = "") -> None:
        """Transition with an audited reason recorded in metadata.

        The plain transition() stays the primitive; this variant is what the
        lifecycle service calls so that the reason travels with the account
        and shows up in support tooling without a separate lookup.
        """
        previous = self.status
        self.transition(new_status)
        if previous != self.status:
            history = self.metadata.setdefault("status_history", [])
            history.append({
                "from": previous,
                "to": new_status,
                "reason": reason,
                "at": utc_now().isoformat(),
            })

    @property
    def is_dormant(self) -> bool:
        return self.status == STATUS_DORMANT

    @property
    def is_closed(self) -> bool:
        return self.status == STATUS_CLOSED

    @property
    def can_spend(self) -> bool:
        """Whether this account may originate outbound money movements."""
        return self.status in SPENDABLE_STATUSES

    @property
    def can_receive(self) -> bool:
        """Whether this account may receive credits.

        Dormant and frozen accounts still receive: payroll keeps arriving at
        a frozen account, it simply cannot leave again. Only a closed account
        rejects credits outright.
        """
        return self.status in CREDITABLE_STATUSES

    @property
    def age_days(self) -> int:
        """Days since the account was opened."""
        return max(0, (utc_now() - self.opened_at).days)

    @property
    def is_empty(self) -> bool:
        return self.raw_balance == 0

    def can_debit(self, amount) -> bool:
        """Whether a debit of this size would succeed, without attempting it.

        Mirrors debit()'s guards exactly. Used by callers that want to branch
        rather than catch, and by the support console to explain a failure.
        """
        try:
            amt = quantize2(to_decimal(amount))
        except (ValueError, TypeError, ArithmeticError):
            return False
        if amt <= 0:
            return False
        if self.status in (STATUS_CLOSED, STATUS_DORMANT):
            return False
        return self.raw_balance >= amt

    def available_after(self, amount) -> Decimal:
        """What the balance would be after debiting an amount.

        Arithmetic only; it does not check whether the debit is permitted, so
        the result can be negative. Callers pair it with can_debit().
        """
        return quantize2(self.raw_balance - quantize2(to_decimal(amount)))

    def adjust(self, delta, reason: str = "") -> Decimal:
        """Apply a signed adjustment, routing through credit() or debit().

        Used by the reconciliation flow, which computes a signed delta and
        does not want to branch. A zero delta is a no-op rather than an
        error: reconciliation frequently finds nothing to correct.
        """
        amount = quantize2(to_decimal(delta))
        if amount == 0:
            return self.raw_balance
        if amount > 0:
            balance = self.credit(amount)
        else:
            balance = self.debit(-amount)
        if reason:
            adjustments = self.metadata.setdefault("adjustments", [])
            adjustments.append({"delta": str(amount), "reason": reason})
        return balance

    def upgrade_kyc(self, new_level: int) -> int:
        """Raise the KYC level. Downgrades are not permitted here.

        A downgrade is a compliance action with its own workflow, not a data
        edit; allowing it through the aggregate would let a caller silently
        widen or narrow limits without review.
        """
        if new_level < self.kyc_level:
            raise InvalidOperation(
                f"cannot downgrade KYC from {self.kyc_level} to {new_level}"
            )
        if new_level == self.kyc_level:
            return self.kyc_level
        self.kyc_level = new_level
        self.touch()
        return self.kyc_level

    def tag(self, key: str, value) -> None:
        """Attach an operational tag to the account metadata."""
        self.metadata[key] = value

    def untag(self, key: str) -> bool:
        """Remove an operational tag. True when the tag existed."""
        return self.metadata.pop(key, _MISSING) is not _MISSING

    def status_history(self) -> list[dict]:
        """Recorded status transitions, oldest first."""
        return list(self.metadata.get("status_history", []))

    def describe(self) -> dict:
        """Compact, side-effect-free projection for tooling and logs.

        Deliberately excludes metadata, which can hold arbitrary operational
        content and is not safe to log verbatim.
        """
        return {
            "user_id": self.user_id,
            "currency": self.currency,
            "status": self.status,
            "kyc_level": self.kyc_level,
            "balance": str(self.raw_balance),
            "age_days": self.age_days,
            "can_spend": self.can_spend,
            "can_receive": self.can_receive,
        }


# Sentinel for untag(), so that removing a tag whose value is legitimately
# None is distinguishable from removing a tag that was never set.
_MISSING = object()


def new_account(
    user_id: str,
    currency: str,
    kyc_level: int = 1,
    opening_balance: Decimal = Decimal("0.00"),
) -> Account:
    """Construct a fresh active account with coherent timestamps.

    The two timestamps start equal: an account that has never been touched
    was last updated when it was opened. Callers that need a backdated
    account build the dataclass directly; this helper is for the normal path.
    """
    now = utc_now()
    return Account(
        user_id=user_id,
        raw_balance=quantize2(to_decimal(opening_balance)),
        currency=currency,
        status=STATUS_ACTIVE,
        opened_at=now,
        updated_at=now,
        kyc_level=kyc_level,
    )


def total_balance(accounts_list: list[Account], currency: str) -> Decimal:
    """Sum the balances of accounts already denominated in one currency.

    Raises on a currency mismatch rather than converting: this helper exists
    for same-currency arithmetic, and silently converting here would hide an
    FX decision that belongs to the caller.
    """
    total = Decimal("0")
    for account in accounts_list:
        if account.currency != currency:
            raise InvalidOperation(
                f"account {account.user_id} is {account.currency}, "
                f"expected {currency}"
            )
        total += account.raw_balance
    return quantize2(total)


def group_by_status(accounts_list: list[Account]) -> dict[str, list[Account]]:
    """Partition accounts by lifecycle status."""
    grouped: dict[str, list[Account]] = {}
    for account in accounts_list:
        grouped.setdefault(account.status, []).append(account)
    return grouped


# ---------------------------------------------------------------------------
# Lifecycle rules
# ---------------------------------------------------------------------------

# Which statuses an account may move to from each status. Closed is terminal
# on purpose: reopening is a new account with a new opened_at, so that the
# age and history of a wallet always mean what they say.
_ALLOWED_TRANSITIONS = {
    STATUS_ACTIVE: (STATUS_FROZEN, STATUS_DORMANT, STATUS_CLOSED),
    STATUS_FROZEN: (STATUS_ACTIVE, STATUS_CLOSED),
    STATUS_DORMANT: (STATUS_ACTIVE, STATUS_CLOSED),
    STATUS_CLOSED: (),
}


def allowed_transitions(status: str) -> tuple[str, ...]:
    """Statuses reachable from a given status."""
    return _ALLOWED_TRANSITIONS.get(status, ())


def transition_is_allowed(current: str, target: str) -> bool:
    """Whether a lifecycle transition is permitted.

    A transition to the same status is always allowed and is a no-op, which
    keeps callers from having to special-case idempotent updates.
    """
    if current == target:
        return True
    return target in allowed_transitions(current)


def is_terminal_status(status: str) -> bool:
    """Whether a status admits no further transitions."""
    return not allowed_transitions(status)


def describe_lifecycle() -> dict:
    """Machine-readable lifecycle description for the docs and ops console."""
    return {
        "statuses": sorted(_ALLOWED_TRANSITIONS),
        "terminal": sorted(s for s in _ALLOWED_TRANSITIONS if is_terminal_status(s)),
        "transitions": {
            status: sorted(targets)
            for status, targets in _ALLOWED_TRANSITIONS.items()
        },
        "spendable": sorted(SPENDABLE_STATUSES),
        "creditable": sorted(CREDITABLE_STATUSES),
    }


# ---------------------------------------------------------------------------
# Dormancy policy
# ---------------------------------------------------------------------------

# Days without a balance-changing movement after which an active account is
# considered dormant. The transition itself is the account service's call;
# this is the rule it consults.
DORMANCY_AFTER_DAYS = 365

# Days after dormancy at which the customer is warned before any further
# lifecycle action.
DORMANCY_WARNING_DAYS = 335


def is_dormancy_candidate(days_since_movement: int, status: str) -> bool:
    """Whether an account has gone quiet long enough to be marked dormant."""
    if status != STATUS_ACTIVE:
        return False
    return days_since_movement >= DORMANCY_AFTER_DAYS


def dormancy_warning_due(days_since_movement: int, status: str) -> bool:
    """Whether the pre-dormancy warning should be sent."""
    if status != STATUS_ACTIVE:
        return False
    return DORMANCY_WARNING_DAYS <= days_since_movement < DORMANCY_AFTER_DAYS


def days_until_dormancy(days_since_movement: int) -> int:
    """Days left before an account would become a dormancy candidate."""
    return max(0, DORMANCY_AFTER_DAYS - days_since_movement)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_account(account: Account) -> list[str]:
    """Structural problems with an account, empty list when it is coherent.

    Invariant checks the aggregate cannot enforce at construction time
    because the dataclass can be built directly (fixtures, migrations). Run
    by the store's consistency check rather than on every access.
    """
    problems: list[str] = []
    if not account.user_id:
        problems.append("user_id is empty")
    if account.raw_balance < 0:
        problems.append("negative balance")
    if account.status not in _ALLOWED_TRANSITIONS:
        problems.append(f"unknown status '{account.status}'")
    if account.updated_at < account.opened_at:
        problems.append("updated_at precedes opened_at")
    if account.kyc_level < 0:
        problems.append("negative kyc_level")
    if account.status == STATUS_CLOSED and account.raw_balance > 0:
        problems.append("closed account still holds funds")
    return problems


def account_is_coherent(account: Account) -> bool:
    return not validate_account(account)


# ---------------------------------------------------------------------------
# Interest eligibility
# ---------------------------------------------------------------------------

# Minimum balance, per currency, below which the savings pilot pays nothing.
# Vendored from the product spec; the accrual maths lives in the account
# service, this table only says who qualifies.
_INTEREST_MINIMUMS = {
    "EUR": Decimal("500"),
    "USD": Decimal("500"),
    "GBP": Decimal("400"),
    "CHF": Decimal("500"),
    "SGD": Decimal("700"),
    "THB": Decimal("18000"),
    "JPY": Decimal("80000"),
}


def interest_minimum(currency: str) -> Decimal:
    """Minimum qualifying balance for the savings pilot, in that currency."""
    return _INTEREST_MINIMUMS.get(currency.upper(), Decimal("0"))


def qualifies_for_interest(account: Account) -> bool:
    """Whether an account currently qualifies for interest accrual.

    Requires an active account holding at least the currency's minimum. A
    currency with no configured minimum qualifies at any balance, which is
    the permissive reading the product team asked for during the pilot.
    """
    if not account.is_active:
        return False
    return account.raw_balance >= interest_minimum(account.currency)


def shortfall_to_interest(account: Account) -> Decimal:
    """How much more the account needs to start earning interest.

    Zero when it already qualifies, so the UI can show a progress nudge
    without branching on eligibility first.
    """
    minimum = interest_minimum(account.currency)
    if account.raw_balance >= minimum:
        return Decimal("0.00")
    return quantize2(minimum - account.raw_balance)


# ---------------------------------------------------------------------------
# Tiering
# ---------------------------------------------------------------------------

# Balance thresholds, in EUR-equivalent, for the customer tier badge. The
# caller converts before consulting this table; the aggregate does no FX.
_TIER_THRESHOLDS = (
    ("platinum", Decimal("100000")),
    ("gold", Decimal("25000")),
    ("silver", Decimal("5000")),
    ("standard", Decimal("0")),
)


def tier_for_balance(balance_eur: Decimal) -> str:
    """Customer tier badge for an EUR-equivalent balance."""
    amount = to_decimal(balance_eur)
    for name, threshold in _TIER_THRESHOLDS:
        if amount >= threshold:
            return name
    return "standard"


def next_tier(balance_eur: Decimal) -> tuple[str, Decimal] | None:
    """The next tier up and how much more is needed to reach it.

    None when the account is already at the top tier, so the caller can hide
    the progress indicator rather than showing a satisfied one forever.
    """
    amount = to_decimal(balance_eur)
    ascending = sorted(_TIER_THRESHOLDS, key=lambda kv: kv[1])
    for name, threshold in ascending:
        if amount < threshold:
            return name, quantize2(threshold - amount)
    return None
