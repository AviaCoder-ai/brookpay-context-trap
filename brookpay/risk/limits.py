"""Velocity limits on withdrawals.

Two independent caps: number of withdrawals per rolling hour and total EUR
volume per rolling day. Both are evaluated before approval and recorded
after approval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from brookpay.config.settings import get_settings
from brookpay.utils.money import to_decimal

_HOUR = 3600.0
_DAY = 86400.0


@dataclass
class _UserWindow:
    events: list[tuple[float, Decimal]] = field(default_factory=list)

    def prune(self, now: float) -> None:
        self.events = [(ts, amt) for ts, amt in self.events if now - ts < _DAY]

    def count_last_hour(self, now: float) -> int:
        return sum(1 for ts, _ in self.events if now - ts < _HOUR)

    def volume_last_day(self, now: float) -> Decimal:
        return sum((amt for ts, amt in self.events if now - ts < _DAY), Decimal("0"))


class VelocityTracker:
    def __init__(
        self,
        max_per_hour: int | None = None,
        max_daily_eur: Decimal | None = None,
    ) -> None:
        settings = get_settings()
        self.max_per_hour = (
            max_per_hour
            if max_per_hour is not None
            else settings.velocity_max_withdrawals_per_hour
        )
        self.max_daily_eur = (
            max_daily_eur
            if max_daily_eur is not None
            else settings.velocity_max_daily_eur
        )
        self._windows: dict[str, _UserWindow] = {}

    def check(self, user_id: str, amount_eur) -> tuple[bool, str]:
        """(ok, reason). Does not record; call record() after approval."""
        now = time.time()
        window = self._windows.setdefault(user_id, _UserWindow())
        window.prune(now)
        if window.count_last_hour(now) >= self.max_per_hour:
            return False, "velocity_hourly_count"
        projected = window.volume_last_day(now) + to_decimal(amount_eur)
        if projected > self.max_daily_eur:
            return False, "velocity_daily_volume"
        return True, ""

    def record(self, user_id: str, amount_eur) -> None:
        window = self._windows.setdefault(user_id, _UserWindow())
        window.events.append((time.time(), to_decimal(amount_eur)))

    def count(self, user_id: str) -> int:
        """Withdrawals recorded for this user in the trailing hour.

        Read-only: it prunes expired events as a side effect of asking, which
        is safe because pruning only drops events that no window can still
        see. Callers use this for annotation, never for a decision; the
        decision path goes through check().
        """
        now = time.time()
        window = self._windows.setdefault(user_id, _UserWindow())
        window.prune(now)
        return window.count_last_hour(now)

    def volume(self, user_id: str) -> Decimal:
        """EUR volume recorded for this user in the trailing day."""
        now = time.time()
        window = self._windows.setdefault(user_id, _UserWindow())
        window.prune(now)
        return window.volume_last_day(now)

    def headroom(self, user_id: str) -> dict:
        """How much of each cap this user has left, for the support console.

        Purely informational: it reports the same windows check() consults,
        so an agent can explain a velocity denial without re-deriving it.
        """
        now = time.time()
        window = self._windows.setdefault(user_id, _UserWindow())
        window.prune(now)
        used_count = window.count_last_hour(now)
        used_volume = window.volume_last_day(now)
        return {
            "hourly_used": used_count,
            "hourly_limit": self.max_per_hour,
            "hourly_remaining": max(0, self.max_per_hour - used_count),
            "daily_used_eur": str(used_volume),
            "daily_limit_eur": str(self.max_daily_eur),
            "daily_remaining_eur": str(max(Decimal("0"), self.max_daily_eur - used_volume)),
        }

    def tracked_users(self) -> list[str]:
        """User ids with at least one event in a live window."""
        now = time.time()
        live: list[str] = []
        for user_id, window in self._windows.items():
            window.prune(now)
            if window.events:
                live.append(user_id)
        return sorted(live)

    def reset(self, user_id: str | None = None) -> None:
        if user_id is None:
            self._windows.clear()
        else:
            self._windows.pop(user_id, None)


# ---------------------------------------------------------------------------
# Tier-based caps
# ---------------------------------------------------------------------------

# Daily EUR caps per KYC tier. A tier's cap is a ceiling, not an allowance:
# the tracker's own configured cap still applies and the tighter of the two
# wins, so lowering the global cap during an incident takes effect for every
# tier without touching this table.
_TIER_DAILY_CAPS = {
    0: Decimal("0"),        # prospect: cannot hold funds, cannot withdraw
    1: Decimal("1000"),     # simplified due diligence
    2: Decimal("15000"),    # standard due diligence
    3: Decimal("100000"),   # enhanced due diligence
}

# Withdrawals per hour per tier. Same "tighter wins" rule applies.
_TIER_HOURLY_COUNTS = {
    0: 0,
    1: 3,
    2: 10,
    3: 25,
}


def tier_daily_cap(kyc_level: int) -> Decimal:
    """Daily EUR cap for a KYC tier, defaulting to the most restrictive."""
    return _TIER_DAILY_CAPS.get(kyc_level, _TIER_DAILY_CAPS[0])


def tier_hourly_count(kyc_level: int) -> int:
    """Hourly withdrawal count cap for a KYC tier."""
    return _TIER_HOURLY_COUNTS.get(kyc_level, _TIER_HOURLY_COUNTS[0])


def effective_daily_cap(kyc_level: int, configured: Decimal) -> Decimal:
    """The tighter of the tier cap and the configured cap."""
    return min(tier_daily_cap(kyc_level), to_decimal(configured))


def effective_hourly_count(kyc_level: int, configured: int) -> int:
    """The tighter of the tier count cap and the configured one."""
    return min(tier_hourly_count(kyc_level), configured)


def tier_allows_withdrawals(kyc_level: int) -> bool:
    """Whether a tier may withdraw at all."""
    return tier_daily_cap(kyc_level) > 0 and tier_hourly_count(kyc_level) > 0


# ---------------------------------------------------------------------------
# Cooling-off
# ---------------------------------------------------------------------------

# Seconds after a sensitive account change (new destination, tier upgrade)
# during which withdrawals are held. Enforced by the caller, which knows when
# the change happened; the tracker has no notion of account events.
COOLING_OFF_SECONDS = 24 * 3600


def in_cooling_off(seconds_since_change: float) -> bool:
    """Whether a sensitive change is still within its cooling-off window."""
    return seconds_since_change < COOLING_OFF_SECONDS


def cooling_off_remaining(seconds_since_change: float) -> float:
    """Seconds left in the cooling-off window, zero when it has elapsed."""
    return max(0.0, COOLING_OFF_SECONDS - seconds_since_change)


# ---------------------------------------------------------------------------
# Window introspection
# ---------------------------------------------------------------------------

def window_seconds() -> dict[str, float]:
    """The rolling window sizes this module enforces, for the docs."""
    return {"hourly": _HOUR, "daily": _DAY}


def describe_limits(tracker: "VelocityTracker") -> dict:
    """Describe a tracker's configuration for the diagnostics endpoint."""
    return {
        "max_per_hour": tracker.max_per_hour,
        "max_daily_eur": str(tracker.max_daily_eur),
        "windows": window_seconds(),
        "tracked_users": len(tracker.tracked_users()),
    }


def would_breach(tracker: "VelocityTracker", user_id: str, amount_eur) -> dict:
    """Explain whether an amount would breach a cap, without recording it.

    Mirrors check() but returns the arithmetic rather than a verdict, so the
    support console can show the customer exactly which cap is in the way and
    by how much.
    """
    ok, reason = tracker.check(user_id, amount_eur)
    headroom = tracker.headroom(user_id)
    return {
        "would_allow": ok,
        "reason": reason,
        "requested_eur": str(to_decimal(amount_eur)),
        "headroom": headroom,
    }


# ---------------------------------------------------------------------------
# Channel-specific caps
# ---------------------------------------------------------------------------

# Per-channel multipliers applied to the daily cap. A withdrawal initiated
# from a freshly installed mobile app is treated more conservatively than one
# from a long-lived API integration, because the account takeover risk
# profile differs. Multipliers never exceed 1.0: a channel can only tighten.
_CHANNEL_MULTIPLIERS = {
    "api": Decimal("1.0"),
    "web": Decimal("1.0"),
    "mobile": Decimal("0.8"),
    "support": Decimal("0.5"),
}


def channel_multiplier(channel: str) -> Decimal:
    """Cap multiplier for an initiation channel, defaulting to the tightest."""
    return _CHANNEL_MULTIPLIERS.get(channel, Decimal("0.5"))


def channel_adjusted_cap(daily_cap: Decimal, channel: str) -> Decimal:
    """Apply a channel multiplier to a daily cap."""
    return to_decimal(daily_cap) * channel_multiplier(channel)


def known_channels() -> tuple[str, ...]:
    return tuple(sorted(_CHANNEL_MULTIPLIERS))


# ---------------------------------------------------------------------------
# Step-up thresholds
# ---------------------------------------------------------------------------

# EUR amount above which an extra authentication factor is demanded, per
# tier. Below the threshold the session's existing authentication stands.
_STEP_UP_THRESHOLDS = {
    1: Decimal("250"),
    2: Decimal("2500"),
    3: Decimal("20000"),
}


def step_up_threshold(kyc_level: int) -> Decimal:
    """EUR amount above which step-up authentication is required."""
    return _STEP_UP_THRESHOLDS.get(kyc_level, Decimal("0"))


def requires_step_up(kyc_level: int, amount_eur) -> bool:
    """Whether an amount demands an extra authentication factor.

    A tier with no configured threshold requires step-up for any amount,
    which is the conservative reading of an unknown tier.
    """
    threshold = step_up_threshold(kyc_level)
    if threshold <= 0:
        return True
    return to_decimal(amount_eur) > threshold


# ---------------------------------------------------------------------------
# Aggregate exposure
# ---------------------------------------------------------------------------

def aggregate_volume(tracker: "VelocityTracker") -> Decimal:
    """Total EUR volume across every tracked user's daily window.

    Used by the treasury view to see how much is flowing out right now
    without querying the ledger. Purely observational.
    """
    total = Decimal("0")
    for user_id in tracker.tracked_users():
        total += tracker.volume(user_id)
    return total


def busiest_users(tracker: "VelocityTracker", top: int = 5) -> list[tuple[str, Decimal]]:
    """Tracked users ranked by daily EUR volume, highest first."""
    rows = [(uid, tracker.volume(uid)) for uid in tracker.tracked_users()]
    return sorted(rows, key=lambda kv: kv[1], reverse=True)[:top]


def utilisation(tracker: "VelocityTracker", user_id: str) -> float:
    """Fraction of a user's daily cap currently consumed, clamped to [0, 1]."""
    if tracker.max_daily_eur <= 0:
        return 1.0
    used = tracker.volume(user_id)
    ratio = float(used / tracker.max_daily_eur)
    return max(0.0, min(1.0, ratio))


def near_cap(tracker: "VelocityTracker", user_id: str, warn_at: float = 0.8) -> bool:
    """Whether a user is close enough to their cap to warrant a warning."""
    return utilisation(tracker, user_id) >= warn_at


# ---------------------------------------------------------------------------
# Configuration sanity
# ---------------------------------------------------------------------------

def validate_configuration(max_per_hour: int, max_daily_eur: Decimal) -> list[str]:
    """Check a velocity configuration, empty list when it is coherent.

    Catches the configurations that would make the tracker behave in a way an
    operator would not expect: non-positive caps (which block everything) and
    an hourly count so high that the daily volume cap is the only real limit.
    """
    problems: list[str] = []
    if max_per_hour <= 0:
        problems.append("max_per_hour must be positive or nothing can withdraw")
    if to_decimal(max_daily_eur) <= 0:
        problems.append("max_daily_eur must be positive or nothing can withdraw")
    if max_per_hour > 1000:
        problems.append("max_per_hour is so high the hourly cap is inert")
    return problems


def configuration_is_sane(max_per_hour: int, max_daily_eur: Decimal) -> bool:
    return not validate_configuration(max_per_hour, max_daily_eur)


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------

@dataclass
class ShadowOutcome:
    """What the tracker would have decided, recorded without enforcing it."""

    user_id: str
    would_allow: bool
    reason: str
    amount_eur: str


class ShadowRecorder:
    """Collects what velocity would have done, without denying anything.

    Used when a new cap is being trialled in production: the engine runs in
    shadow mode (see the wiring feature flags), decisions are recorded here,
    and the numbers are reviewed before the cap is enforced for real. Nothing
    in this class can deny a withdrawal; that is the entire point.
    """

    def __init__(self) -> None:
        self._outcomes: list[ShadowOutcome] = []

    def observe(self, tracker: "VelocityTracker", user_id: str, amount_eur) -> ShadowOutcome:
        ok, reason = tracker.check(user_id, amount_eur)
        outcome = ShadowOutcome(
            user_id=user_id,
            would_allow=ok,
            reason=reason,
            amount_eur=str(to_decimal(amount_eur)),
        )
        self._outcomes.append(outcome)
        return outcome

    def would_deny_count(self) -> int:
        return sum(1 for o in self._outcomes if not o.would_allow)

    def deny_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return self.would_deny_count() / len(self._outcomes)

    def by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self._outcomes:
            if outcome.would_allow:
                continue
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return counts

    def affected_users(self) -> list[str]:
        """Users who would have been denied, for the pre-launch review."""
        return sorted({o.user_id for o in self._outcomes if not o.would_allow})

    def summary(self) -> dict:
        return {
            "observed": len(self._outcomes),
            "would_deny": self.would_deny_count(),
            "deny_rate": round(self.deny_rate(), 4),
            "by_reason": self.by_reason(),
            "affected_users": len(self.affected_users()),
        }


# ---------------------------------------------------------------------------
# Cap change impact
# ---------------------------------------------------------------------------

def impact_of_cap_change(
    tracker: "VelocityTracker",
    new_daily_eur: Decimal,
) -> dict:
    """Which tracked users would breach a proposed new daily cap.

    Run before tightening a cap so the change can be sized: it compares each
    tracked user's current daily volume against the proposed cap. Read-only;
    it mutates neither the tracker nor its windows beyond pruning.
    """
    proposed = to_decimal(new_daily_eur)
    breaching: list[str] = []
    for user_id in tracker.tracked_users():
        if tracker.volume(user_id) > proposed:
            breaching.append(user_id)
    tracked = tracker.tracked_users()
    return {
        "proposed_daily_eur": str(proposed),
        "current_daily_eur": str(tracker.max_daily_eur),
        "tracked_users": len(tracked),
        "would_breach": sorted(breaching),
        "breach_ratio": round(len(breaching) / len(tracked), 4) if tracked else 0.0,
    }
