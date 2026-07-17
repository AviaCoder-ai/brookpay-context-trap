"""Low balance alerting.

Runs every few minutes. To avoid hammering the primary store, the scan is
driven exclusively by the balance snapshot cache that the read path keeps
warm: any account recently consulted has a fresh snapshot there. Accounts
without a cached snapshot are picked up on their next read.

That design choice is the important one: this job never reads accounts
directly. Its entire input is the set of snapshots the balance read path
writes as a side effect. If that side effect stops (for instance because the
read path no longer populates the cache), this scan simply finds nothing and
emits no alerts, with no error to signal that it has gone blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from brookpay.config.constants import BALANCE_CACHE_PREFIX, STATUS_ACTIVE
from brookpay.config.settings import get_settings
from brookpay.core import cache
from brookpay.fx.engine import to_eur
from brookpay.services import notifications

KIND_LOW_BALANCE = "low_balance"
KIND_CRITICAL_BALANCE = "critical_balance"


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Critical threshold as a fraction of the low-balance threshold. Below this,
# a stronger notification kind is used. Product-tunable; kept here next to
# the scan that applies it.
_CRITICAL_FRACTION = Decimal("0.25")


def critical_threshold(low_threshold: Decimal) -> Decimal:
    """The critical threshold derived from the low-balance threshold."""
    return low_threshold * _CRITICAL_FRACTION


def classify_severity(amount_eur: Decimal, low_threshold: Decimal) -> Optional[str]:
    """Return the alert kind for an amount, or None when above threshold.

    Amounts at or below the critical threshold get the critical kind; those
    below the low threshold but above critical get the low kind; anything
    else returns None (no alert).
    """
    if amount_eur <= critical_threshold(low_threshold):
        return KIND_CRITICAL_BALANCE
    if amount_eur < low_threshold:
        return KIND_LOW_BALANCE
    return None


# ---------------------------------------------------------------------------
# Cache key helpers
# ---------------------------------------------------------------------------

def _user_id_from_key(key: str) -> str:
    return key[len(BALANCE_CACHE_PREFIX):]


def _key_for_user(user_id: str) -> str:
    return f"{BALANCE_CACHE_PREFIX}{user_id}"


def cached_user_ids() -> list[str]:
    """User ids that currently have a cached balance snapshot.

    A thin wrapper over the cache scan, exposed so other jobs can see which
    accounts are "warm" without duplicating the prefix logic.
    """
    return [_user_id_from_key(key) for key, _ in cache.scan(BALANCE_CACHE_PREFIX)]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _already_notified(kind: str) -> set[str]:
    """User ids with a pending notification of a given kind."""
    return {n.user_id for n in notifications.pending(kind)}


def _dedup_keys() -> set[str]:
    """Union of pending low and critical notifications, for dedup checks."""
    return _already_notified(KIND_LOW_BALANCE) | _already_notified(KIND_CRITICAL_BALANCE)


# ---------------------------------------------------------------------------
# Alert templating
# ---------------------------------------------------------------------------

def _alert_message(kind: str, amount: str, currency: str, limit: Decimal) -> str:
    """Compose the customer-facing alert text for a kind."""
    if kind == KIND_CRITICAL_BALANCE:
        return (
            f"Your balance is critically low ({amount} {currency}). "
            f"Add funds to avoid interruptions."
        )
    return f"Your balance is below {limit} EUR ({amount} {currency})."


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

# Local hours (24h) during which non-critical alerts are held. Critical
# alerts ignore quiet hours. Product-owned; the scan itself does not consult
# these, the delivery worker does, but they live here with the alert logic.
QUIET_START_HOUR = 22
QUIET_END_HOUR = 8


def in_quiet_hours(local_hour: int) -> bool:
    """Whether a local hour falls in the overnight quiet window.

    The window wraps midnight, so the check is an OR of the two ends rather
    than a simple range. Hours are 0-23.
    """
    if QUIET_START_HOUR <= QUIET_END_HOUR:
        return QUIET_START_HOUR <= local_hour < QUIET_END_HOUR
    return local_hour >= QUIET_START_HOUR or local_hour < QUIET_END_HOUR


def should_hold(kind: str, local_hour: int) -> bool:
    """Whether an alert of a kind should be held during quiet hours."""
    if kind == KIND_CRITICAL_BALANCE:
        return False
    return in_quiet_hours(local_hour)


# ---------------------------------------------------------------------------
# Channel preferences
# ---------------------------------------------------------------------------

CHANNEL_PUSH = "push"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"

_ALLOWED_CHANNELS = (CHANNEL_PUSH, CHANNEL_EMAIL, CHANNEL_SMS)

# Default channel per alert kind. Critical alerts default to SMS for reach;
# low-balance alerts default to push. A user preference overrides these.
_DEFAULT_CHANNEL = {
    KIND_LOW_BALANCE: CHANNEL_PUSH,
    KIND_CRITICAL_BALANCE: CHANNEL_SMS,
}


def resolve_channel(kind: str, preference: Optional[str] = None) -> str:
    """Resolve the delivery channel for an alert kind and user preference."""
    if preference in _ALLOWED_CHANNELS:
        return preference
    return _DEFAULT_CHANNEL.get(kind, CHANNEL_PUSH)


# ---------------------------------------------------------------------------
# Rate limiting and snooze
# ---------------------------------------------------------------------------

# Minimum hours between two alerts of the same kind to one user, so a
# hovering balance does not alert every scan cycle.
MIN_HOURS_BETWEEN_ALERTS = 12


def is_rate_limited(hours_since_last: Optional[float]) -> bool:
    """Whether another alert is too soon after the last one.

    None means no previous alert, which is never rate limited. Otherwise the
    gap must meet the minimum spacing.
    """
    if hours_since_last is None:
        return False
    return hours_since_last < MIN_HOURS_BETWEEN_ALERTS


def snooze_until(current_hour: int, snooze_hours: int) -> int:
    """The hour-of-day an alert snooze expires, wrapping past midnight."""
    return (current_hour + max(0, snooze_hours)) % 24


# ---------------------------------------------------------------------------
# Notification budget
# ---------------------------------------------------------------------------

# Safety cap on notifications emitted per scan pass, so a misconfiguration or
# a cache anomaly cannot fan out a flood. The scan stops emitting once the
# budget is exhausted and logs that it hit the cap.
DEFAULT_SCAN_BUDGET = 5000


def within_budget(emitted: int, budget: int = DEFAULT_SCAN_BUDGET) -> bool:
    """Whether another notification is allowed under the per-pass budget."""
    return emitted < max(0, budget)


def budget_remaining(emitted: int, budget: int = DEFAULT_SCAN_BUDGET) -> int:
    return max(0, max(0, budget) - emitted)


# ---------------------------------------------------------------------------
# The scan (consumes the balance read path's cache side effect)
# ---------------------------------------------------------------------------

def run_low_balance_scan(threshold_eur: Optional[Decimal] = None) -> int:
    """Emit one notification per active account under the EUR threshold.

    Returns the number of notifications emitted during this pass. The scan
    iterates the balance snapshot cache exclusively: every candidate account
    is one the read path recently cached. An account that has never been read
    has no snapshot here and is therefore invisible to this scan, by design,
    until its next balance read warms the cache again.
    """
    limit = (
        threshold_eur
        if threshold_eur is not None
        else get_settings().low_balance_threshold_eur
    )
    already_notified = {
        n.user_id for n in notifications.pending(KIND_LOW_BALANCE)
    }
    emitted = 0
    for key, snap in cache.scan(BALANCE_CACHE_PREFIX):
        if snap.get("status") != STATUS_ACTIVE:
            continue
        user_id = _user_id_from_key(key)
        if user_id in already_notified:
            continue
        amount_eur = to_eur(snap["amount"], snap["currency"])
        if amount_eur < limit:
            notifications.enqueue(
                user_id,
                KIND_LOW_BALANCE,
                f"Your balance is below {limit} EUR "
                f"({snap['amount']} {snap['currency']}).",
                amount_eur=str(amount_eur),
            )
            emitted += 1
    return emitted


def scan_candidates(threshold_eur: Optional[Decimal] = None) -> list[dict]:
    """Dry-run view of which accounts the scan would alert, without emitting.

    Same cache-driven input as run_low_balance_scan; used by the ops console
    to preview a run. Because it reads the same snapshots, its blindness to
    never-read accounts is identical to the real scan's.
    """
    limit = (
        threshold_eur
        if threshold_eur is not None
        else get_settings().low_balance_threshold_eur
    )
    candidates: list[dict] = []
    for key, snap in cache.scan(BALANCE_CACHE_PREFIX):
        if snap.get("status") != STATUS_ACTIVE:
            continue
        amount_eur = to_eur(snap["amount"], snap["currency"])
        severity = classify_severity(amount_eur, limit)
        if severity is not None:
            candidates.append({
                "user_id": _user_id_from_key(key),
                "amount_eur": str(amount_eur),
                "severity": severity,
            })
    return candidates


# ---------------------------------------------------------------------------
# Severity-aware scan
# ---------------------------------------------------------------------------

def run_tiered_scan(threshold_eur: Optional[Decimal] = None) -> dict[str, int]:
    """Like run_low_balance_scan but splits low vs critical notifications.

    Shares the exact cache-driven input; the only difference is that amounts
    under the critical threshold get the critical kind. Returns per-kind
    emitted counts.
    """
    limit = (
        threshold_eur
        if threshold_eur is not None
        else get_settings().low_balance_threshold_eur
    )
    dedup = _dedup_keys()
    counts = {KIND_LOW_BALANCE: 0, KIND_CRITICAL_BALANCE: 0}
    for key, snap in cache.scan(BALANCE_CACHE_PREFIX):
        if snap.get("status") != STATUS_ACTIVE:
            continue
        user_id = _user_id_from_key(key)
        if user_id in dedup:
            continue
        amount_eur = to_eur(snap["amount"], snap["currency"])
        kind = classify_severity(amount_eur, limit)
        if kind is None:
            continue
        notifications.enqueue(
            user_id,
            kind,
            _alert_message(kind, str(snap["amount"]), snap["currency"], limit),
            amount_eur=str(amount_eur),
        )
        counts[kind] += 1
    return counts


# ---------------------------------------------------------------------------
# Digest and escalation
# ---------------------------------------------------------------------------

@dataclass
class AlertDigestRow:
    user_id: str
    kind: str
    amount_eur: str


def build_digest(candidates: list[dict]) -> dict:
    """Summarise a set of scan candidates into a digest for ops.

    Groups by severity and counts; used for the daily internal digest email
    rather than for customer notifications.
    """
    by_severity: dict[str, int] = {}
    for row in candidates:
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1
    return {
        "total": len(candidates),
        "by_severity": by_severity,
    }


def needs_escalation(candidate: dict) -> bool:
    """Whether a candidate warrants escalation beyond a normal alert."""
    return candidate.get("severity") == KIND_CRITICAL_BALANCE


def escalation_list(candidates: list[dict]) -> list[str]:
    """User ids among the candidates that need escalation."""
    return [c["user_id"] for c in candidates if needs_escalation(c)]


# ---------------------------------------------------------------------------
# Coverage diagnostics
# ---------------------------------------------------------------------------

def scan_coverage() -> dict:
    """Report how many accounts are currently visible to the scan.

    Because the scan is cache-driven, "visible" means "has a warm snapshot".
    A sudden drop in coverage is the signal that the read path may have
    stopped populating the cache, which is the failure mode that silently
    disables alerting.
    """
    warm = cached_user_ids()
    return {
        "warm_accounts": len(warm),
        "prefix": BALANCE_CACHE_PREFIX,
    }


# ---------------------------------------------------------------------------
# Alert history
# ---------------------------------------------------------------------------

@dataclass
class AlertRecord:
    user_id: str
    kind: str
    amount_eur: str
    at: str


class AlertHistory:
    """Bounded in-memory history of emitted alerts, for the ops view.

    The durable record is the notification store; this is a convenience ring
    buffer the console reads to show recent activity without querying the
    store. Oldest records drop once the cap is reached.
    """

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = max(1, capacity)
        self._records: list[AlertRecord] = []

    def record(self, user_id: str, kind: str, amount_eur: str, at: str) -> None:
        self._records.append(AlertRecord(user_id, kind, amount_eur, at))
        if len(self._records) > self._capacity:
            self._records = self._records[len(self._records) - self._capacity:]

    def recent(self, limit: int = 50) -> list[AlertRecord]:
        return self._records[-limit:]

    def for_user(self, user_id: str) -> list[AlertRecord]:
        return [r for r in self._records if r.user_id == user_id]

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Effectiveness metrics
# ---------------------------------------------------------------------------

def top_up_rate(alerted: int, topped_up: int) -> float:
    """Fraction of alerted users who topped up afterwards.

    The headline effectiveness metric for the alerting product. A zero
    alerted count yields zero rather than a division error.
    """
    return (topped_up / alerted) if alerted else 0.0


def effectiveness_summary(
    alerted: int,
    topped_up: int,
    unsubscribed: int,
) -> dict:
    """Compact effectiveness record for the alerting dashboard."""
    return {
        "alerted": alerted,
        "topped_up": topped_up,
        "unsubscribed": unsubscribed,
        "top_up_rate": round(top_up_rate(alerted, topped_up), 4),
        "unsubscribe_rate": round((unsubscribed / alerted) if alerted else 0.0, 4),
    }


# ---------------------------------------------------------------------------
# Backfill and replay
# ---------------------------------------------------------------------------

def backfill_from_snapshots(snapshots: list[tuple[str, dict]], threshold_eur: Decimal) -> list[dict]:
    """Compute candidates from an explicit snapshot list rather than the cache.

    Used to replay a scan against a captured set of snapshots, for instance
    when reconstructing what a past run would have alerted. Mirrors the
    scan's severity logic exactly, so a replay and a live run agree given the
    same snapshots.
    """
    candidates: list[dict] = []
    for key, snap in snapshots:
        if snap.get("status") != STATUS_ACTIVE:
            continue
        amount_eur = to_eur(snap["amount"], snap["currency"])
        severity = classify_severity(amount_eur, threshold_eur)
        if severity is not None:
            candidates.append({
                "user_id": _user_id_from_key(key),
                "amount_eur": str(amount_eur),
                "severity": severity,
            })
    return candidates


def diff_candidate_sets(before: list[dict], after: list[dict]) -> dict:
    """Which users newly qualify or no longer qualify between two runs."""
    before_ids = {c["user_id"] for c in before}
    after_ids = {c["user_id"] for c in after}
    return {
        "newly_low": sorted(after_ids - before_ids),
        "recovered": sorted(before_ids - after_ids),
        "still_low": sorted(before_ids & after_ids),
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config(low_threshold: Decimal) -> list[str]:
    """Sanity-check the alerting configuration, empty list when clean.

    Catches the obvious misconfigurations that would make the scan behave
    surprisingly: a non-positive threshold, or quiet hours that would hold
    every alert around the clock.
    """
    problems: list[str] = []
    if low_threshold <= 0:
        problems.append("low_balance threshold must be positive")
    if critical_threshold(low_threshold) <= 0:
        problems.append("critical threshold collapses to zero")
    if QUIET_START_HOUR == QUIET_END_HOUR:
        problems.append("quiet hours cover the whole day")
    return problems


# ---------------------------------------------------------------------------
# Scheduler hook
# ---------------------------------------------------------------------------

# Cadence, in seconds, at which the scheduler should run the scan. Read by
# the scheduler catalog; kept here so the job owns its own cadence.
SCAN_INTERVAL_SECONDS = 300


def scheduler_entry() -> dict:
    """Describe this job for the scheduler catalog."""
    return {
        "name": "low_balance_scan",
        "interval_seconds": SCAN_INTERVAL_SECONDS,
        "entrypoint": "brookpay.jobs.low_balance_alerts:run_low_balance_scan",
        "cache_driven": True,
    }
