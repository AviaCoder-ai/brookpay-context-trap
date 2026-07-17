"""Signup and wallet provisioning flow.

Onboarding turns a validated signup into a provisioned wallet. The pivotal
decision, "does this user already have a wallet", is answered by the balance
read path: its documented contract is that an unknown user yields None,
while any existing wallet (even an empty one) yields a snapshot. The flow
leans on that None-vs-snapshot distinction rather than on a separate
"account exists" query, so provisioning is decided by the same read the rest
of the system uses.

Everything before that decision is pure validation and checklist assembly;
everything after it either provisions a new wallet or reports the existing
one. The KYC helpers here are catalogue lookups against the constants
tables, not the verification vendor integration, which lives elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from brookpay.config.constants import (
    DEFAULT_CURRENCY,
    EVENT_ONBOARDING_STARTED,
    KYC_LEVEL_PROSPECT,
    KYC_LEVEL_SIMPLIFIED,
    KYC_LEVEL_STANDARD,
    KYC_REQUIRED_DOCS,
    STATUS_ACTIVE,
)
from brookpay.core import audit
from brookpay.core.errors import InvalidOperation
from brookpay.models.account import Account
from brookpay.services.account_service import get_user_balance
from brookpay.store.repository import accounts
from brookpay.utils.timeutils import utc_now
from brookpay.utils.validation import (
    is_supported_currency,
    is_valid_email,
    is_valid_user_id,
)


# ---------------------------------------------------------------------------
# Onboarding plan value object
# ---------------------------------------------------------------------------

@dataclass
class OnboardingPlan:
    user_id: str
    needs_wallet: bool
    existing_status: str | None = None
    checks: list[str] = field(default_factory=list)

    @property
    def is_new(self) -> bool:
        return self.needs_wallet

    def add_check(self, label: str) -> None:
        self.checks.append(label)

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "needs_wallet": self.needs_wallet,
            "existing_status": self.existing_status,
            "checks": list(self.checks),
        }


# ---------------------------------------------------------------------------
# Signup form validation
# ---------------------------------------------------------------------------

@dataclass
class SignupForm:
    user_id: str
    email: str
    country: str = ""
    referral_code: str = ""


def validate_signup(form: SignupForm) -> list[str]:
    """Return a list of problems with a signup form, empty when clean.

    Structural validation only: user id shape, email shape, country present.
    Uniqueness and deliverability are checked downstream; this is the cheap
    front-door pass that rejects malformed input before any work.
    """
    problems: list[str] = []
    if not is_valid_user_id(form.user_id):
        problems.append("invalid user id")
    if not is_valid_email(form.email):
        problems.append("invalid email")
    if not form.country:
        problems.append("country required")
    return problems


def signup_is_valid(form: SignupForm) -> bool:
    return not validate_signup(form)


# ---------------------------------------------------------------------------
# KYC checklist assembly
# ---------------------------------------------------------------------------

def required_documents(level: int) -> tuple[str, ...]:
    """Documents required to reach a KYC level, from the constants table."""
    return KYC_REQUIRED_DOCS.get(level, ())


def missing_documents(level: int, provided: set[str]) -> list[str]:
    """Documents still needed to reach a level, given what is on file."""
    return [doc for doc in required_documents(level) if doc not in provided]


def can_reach_level(level: int, provided: set[str]) -> bool:
    """Whether the provided document set satisfies a level's requirements."""
    return not missing_documents(level, provided)


def highest_reachable_level(provided: set[str]) -> int:
    """The highest KYC level the provided documents satisfy.

    Walks the levels in ascending order and stops at the first unmet one, so
    a gap at a lower level caps the reachable level even if higher-level docs
    happen to be present.
    """
    reachable = KYC_LEVEL_PROSPECT
    for level in (KYC_LEVEL_SIMPLIFIED, KYC_LEVEL_STANDARD):
        if can_reach_level(level, provided):
            reachable = level
        else:
            break
    return reachable


def onboarding_checklist(level: int, provided: set[str]) -> list[dict]:
    """A rendered checklist of documents for the onboarding UI."""
    checklist = []
    for doc in required_documents(level):
        checklist.append({"document": doc, "provided": doc in provided})
    return checklist


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

# Country codes BrookPay cannot onboard for regulatory reasons. Tiny and
# explicit; the authoritative list is maintained by compliance and synced
# into the onboarding service configuration, this is the vendored snapshot.
_BLOCKED_COUNTRIES = frozenset({"XX", "ZZ"})

# Minimum age, in years, to hold a wallet. Enforced from the verified date
# of birth once identity is checked; the signup only records the claim.
MIN_AGE_YEARS = 18


def country_is_serviceable(country: str) -> bool:
    """Whether onboarding is permitted for a country code."""
    return bool(country) and country.upper() not in _BLOCKED_COUNTRIES


def eligibility_problems(form: SignupForm) -> list[str]:
    """Eligibility issues beyond form validity: geography, mainly.

    Distinct from validate_signup, which is purely structural. A signup can
    be well-formed yet ineligible (blocked country), and the two are
    reported separately so the UI can message them differently.
    """
    problems: list[str] = []
    if not country_is_serviceable(form.country):
        problems.append(f"country '{form.country}' is not serviceable")
    return problems


def is_eligible(form: SignupForm) -> bool:
    return signup_is_valid(form) and not eligibility_problems(form)


# ---------------------------------------------------------------------------
# Lightweight pre-screening
# ---------------------------------------------------------------------------

# Substrings that, if present in a referral code, mark it as a test code and
# exclude it from referral rewards. Not fraud screening, just hygiene.
_TEST_REFERRAL_MARKERS = ("TEST", "DEMO", "QA")


def referral_is_rewardable(referral_code: str) -> bool:
    """Whether a referral code should earn a reward.

    Empty codes are fine (no referral); test-marked codes are accepted for
    signup but excluded from rewards so internal testing does not accrue
    referral bonuses.
    """
    if not referral_code:
        return True
    upper = referral_code.upper()
    return not any(marker in upper for marker in _TEST_REFERRAL_MARKERS)


def normalise_referral(referral_code: str) -> str:
    """Canonical form of a referral code for storage and comparison."""
    return "".join(referral_code.split()).upper()


def prescreen_signup(form: SignupForm) -> dict:
    """Assemble a compact pre-screen result for a signup form.

    Combines structural validity, eligibility and referral hygiene into one
    record the onboarding UI reads. Performs no account work and reads no
    account state; it is safe to call before anything is provisioned.
    """
    structural = validate_signup(form)
    eligibility = eligibility_problems(form)
    return {
        "user_id": form.user_id,
        "structural_problems": structural,
        "eligibility_problems": eligibility,
        "referral_rewardable": referral_is_rewardable(form.referral_code),
        "ok": not structural and not eligibility,
    }


# ---------------------------------------------------------------------------
# Wallet decision (depends on the None-vs-snapshot balance contract)
# ---------------------------------------------------------------------------

def evaluate_new_user(user_id: str) -> OnboardingPlan:
    """Decide whether a signup needs wallet provisioning.

    The balance lookup contract is the source of truth here: a None result
    means the user has no wallet yet, anything else means a wallet already
    exists (possibly empty) and provisioning must be skipped. The existing
    account's status is read off the returned snapshot, so this decision
    depends on the read returning either None or a structured snapshot, not
    a bare number: a bare zero would read as an existing wallet and silently
    suppress provisioning for a brand new user.
    """
    checks: list[str] = []
    if not is_valid_user_id(user_id):
        raise ValueError(f"invalid user id '{user_id}'")
    checks.append("user_id_format:ok")

    snapshot = get_user_balance(user_id)
    if snapshot is None:
        checks.append("wallet:absent")
        audit.record(EVENT_ONBOARDING_STARTED, user_id=user_id, path="new_wallet")
        return OnboardingPlan(user_id=user_id, needs_wallet=True, checks=checks)

    checks.append("wallet:present")
    return OnboardingPlan(
        user_id=user_id,
        needs_wallet=False,
        existing_status=snapshot["status"],
        checks=checks,
    )


def provision_wallet(user_id: str, currency: str = DEFAULT_CURRENCY) -> Account:
    """Create an empty active wallet for a brand new user."""
    plan = evaluate_new_user(user_id)
    if not plan.needs_wallet:
        raise InvalidOperation(f"user '{user_id}' already has a wallet")
    if not is_supported_currency(currency):
        raise InvalidOperation(f"unsupported wallet currency '{currency}'")
    now = utc_now()
    account = Account(
        user_id=user_id,
        raw_balance=Decimal("0.00"),
        currency=currency,
        status=STATUS_ACTIVE,
        opened_at=now,
        updated_at=now,
    )
    return accounts.add(account)


def onboard(form: SignupForm, currency: str = DEFAULT_CURRENCY) -> dict:
    """End-to-end onboarding for a validated signup form.

    Validates the form, evaluates whether a wallet is needed, and provisions
    one when it is. Returns a compact result describing what happened. Any
    validation failure short-circuits before the wallet decision.
    """
    problems = validate_signup(form)
    if problems:
        return {"ok": False, "problems": problems}

    plan = evaluate_new_user(form.user_id)
    if not plan.needs_wallet:
        return {
            "ok": True,
            "provisioned": False,
            "existing_status": plan.existing_status,
        }

    account = provision_wallet(form.user_id, currency)
    return {
        "ok": True,
        "provisioned": True,
        "currency": account.currency,
        "status": account.status,
    }


# ---------------------------------------------------------------------------
# Onboarding funnel metrics
# ---------------------------------------------------------------------------

STAGE_SIGNUP = "signup"
STAGE_VALIDATED = "validated"
STAGE_WALLET = "wallet_provisioned"
STAGE_FUNDED = "first_funded"

FUNNEL_STAGES = (STAGE_SIGNUP, STAGE_VALIDATED, STAGE_WALLET, STAGE_FUNDED)


def funnel_conversion(counts: dict[str, int]) -> dict[str, float]:
    """Stage-to-stage conversion rates from raw stage counts.

    Each rate is relative to the immediately preceding stage; a missing or
    zero preceding stage yields a zero rate rather than a division error.
    """
    rates: dict[str, float] = {}
    for i in range(1, len(FUNNEL_STAGES)):
        prev, cur = FUNNEL_STAGES[i - 1], FUNNEL_STAGES[i]
        base = counts.get(prev, 0)
        rates[f"{prev}->{cur}"] = (counts.get(cur, 0) / base) if base else 0.0
    return rates


def overall_conversion(counts: dict[str, int]) -> float:
    """Signup-to-funded conversion, the headline onboarding number."""
    base = counts.get(STAGE_SIGNUP, 0)
    return (counts.get(STAGE_FUNDED, 0) / base) if base else 0.0


# ---------------------------------------------------------------------------
# Welcome and reminders
# ---------------------------------------------------------------------------

# Days after signup that each onboarding reminder fires for users who have
# not completed provisioning.
REMINDER_SCHEDULE_DAYS = (1, 3, 7)


def due_reminders(days_since_signup: int, completed: bool) -> list[int]:
    """Reminder day-markers due by now for an incomplete onboarding."""
    if completed:
        return []
    return [d for d in REMINDER_SCHEDULE_DAYS if d <= days_since_signup]


def welcome_payload(user_id: str, currency: str) -> dict:
    """Content payload for the welcome message after provisioning."""
    return {
        "user_id": user_id,
        "currency": currency,
        "next_steps": [
            "verify_identity",
            "add_funds",
            "explore_dashboard",
        ],
    }


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------

def signup_cohort(year: int, month: int) -> str:
    """Monthly cohort key a signup belongs to, "YYYY-MM"."""
    return f"{year:04d}-{month:02d}"


def cohort_retention(cohort_counts: dict[str, int], active_counts: dict[str, int]) -> dict[str, float]:
    """Retention ratio per cohort from signup and still-active counts.

    Each cohort's retention is active-over-signed; a cohort with no signups
    yields zero rather than a division error. Pure arithmetic over the two
    count maps, intended for the growth dashboard.
    """
    retention: dict[str, float] = {}
    for cohort, signed in cohort_counts.items():
        active = active_counts.get(cohort, 0)
        retention[cohort] = (active / signed) if signed else 0.0
    return retention


def rank_cohorts(retention: dict[str, float], top: int = 5) -> list[tuple[str, float]]:
    """Cohorts ranked by retention, best first."""
    ordered = sorted(retention.items(), key=lambda kv: kv[1], reverse=True)
    return ordered[:top]


# ---------------------------------------------------------------------------
# Drip campaign
# ---------------------------------------------------------------------------

@dataclass
class DripStep:
    day: int
    template: str


# The onboarding drip: which message goes out on which day-after-signup for
# users who have not yet funded their wallet.
DRIP_CAMPAIGN = (
    DripStep(day=0, template="welcome"),
    DripStep(day=1, template="verify_identity_nudge"),
    DripStep(day=3, template="add_funds_nudge"),
    DripStep(day=7, template="feature_tour"),
    DripStep(day=14, template="final_nudge"),
)


def drip_due(days_since_signup: int, funded: bool) -> list[str]:
    """Templates due by now for an unfunded onboarding.

    A funded user exits the drip entirely. Otherwise every step whose day
    marker has passed is due; the caller is responsible for not resending a
    template already delivered.
    """
    if funded:
        return []
    return [step.template for step in DRIP_CAMPAIGN if step.day <= days_since_signup]


def next_drip_step(days_since_signup: int) -> DripStep | None:
    """The next drip step strictly after the current day, if any."""
    for step in DRIP_CAMPAIGN:
        if step.day > days_since_signup:
            return step
    return None


# ---------------------------------------------------------------------------
# Re-engagement
# ---------------------------------------------------------------------------

def is_dormant_onboarding(days_since_signup: int, funded: bool) -> bool:
    """Whether an unfunded signup has gone cold enough to re-engage."""
    return not funded and days_since_signup > DRIP_CAMPAIGN[-1].day


def reengagement_segment(days_since_signup: int) -> str:
    """Bucket a cold onboarding for the re-engagement campaign."""
    if days_since_signup <= 30:
        return "recent"
    if days_since_signup <= 90:
        return "lapsing"
    return "lapsed"


# ---------------------------------------------------------------------------
# Onboarding timeline
# ---------------------------------------------------------------------------

@dataclass
class TimelineEvent:
    stage: str
    at: str
    detail: str = ""


def build_timeline(events: list[TimelineEvent]) -> list[dict]:
    """Order onboarding events chronologically for the account timeline.

    Sorts by the ISO timestamp string, which sorts correctly for the
    fixed-width UTC format the audit trail emits. Purely presentational.
    """
    ordered = sorted(events, key=lambda e: e.at)
    return [{"stage": e.stage, "at": e.at, "detail": e.detail} for e in ordered]


def stage_durations(events: list[TimelineEvent]) -> dict[str, str]:
    """Human labels for the gap between consecutive onboarding stages.

    Given ordered events, reports the transition names; the actual duration
    computation is left to the caller, which has the parsed timestamps. This
    keeps the helper free of datetime parsing and safe to call on raw rows.
    """
    ordered = sorted(events, key=lambda e: e.at)
    transitions: dict[str, str] = {}
    for i in range(1, len(ordered)):
        key = f"{ordered[i - 1].stage}->{ordered[i].stage}"
        transitions[key] = ordered[i].at
    return transitions


# ---------------------------------------------------------------------------
# SLA tracking
# ---------------------------------------------------------------------------

# Target hours from signup to wallet provisioning. Breaching this feeds the
# onboarding health dashboard, not any customer-facing behaviour.
PROVISIONING_SLA_HOURS = 24


def sla_status(hours_elapsed: float, provisioned: bool) -> str:
    """Classify an onboarding against the provisioning SLA.

    A provisioned onboarding is done regardless of elapsed time; an
    unprovisioned one is on-track until the SLA window, then breaching.
    """
    if provisioned:
        return "met"
    if hours_elapsed <= PROVISIONING_SLA_HOURS:
        return "on_track"
    return "breaching"


def sla_summary(rows: list[tuple[float, bool]]) -> dict:
    """Aggregate (hours_elapsed, provisioned) rows into SLA counts."""
    counts = {"met": 0, "on_track": 0, "breaching": 0}
    for hours_elapsed, provisioned in rows:
        counts[sla_status(hours_elapsed, provisioned)] += 1
    return counts
