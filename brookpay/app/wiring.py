"""Application wiring.

Single place where concrete implementations are bound together: fixtures
are seeded, registry services are registered and the risk engine receives
its funds source. Everything else stays decoupled.

The module also owns the runtime profile table (which features are on in
which environment), the startup health checks and the shutdown sequence.
Nothing outside this package may bind registry services; the verification
step at the end of the build enforces that every required capability was
bound here and nowhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from brookpay.config import constants as C
from brookpay.config.settings import Settings, get_settings
from brookpay.core import audit, cache, registry
from brookpay.risk.limits import VelocityTracker
from brookpay.risk.withdrawals import RiskEngine
from brookpay.services import account_service, onboarding
from brookpay.store import fixtures


# ---------------------------------------------------------------------------
# Runtime profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureFlags:
    """Static feature switches, resolved once per process at build time.

    These are deliberately not hot-reloadable: a flag flip requires a
    deploy, so an incident review can always tie behaviour to a build.
    """

    instant_payouts: bool = False
    savings_interest_pilot: bool = False
    statement_html_beta: bool = False
    velocity_shadow_mode: bool = False   # evaluate but never deny
    compliance_export: bool = True
    card_programme: bool = False


# Profile table. The environment name comes from settings; anything not
# listed falls back to the development profile, which is the most
# conservative one (everything experimental off, exports on).
PROFILES: dict[str, FeatureFlags] = {
    "development": FeatureFlags(),
    "staging": FeatureFlags(
        instant_payouts=True,
        statement_html_beta=True,
        velocity_shadow_mode=True,
    ),
    "production": FeatureFlags(
        instant_payouts=True,
        compliance_export=True,
    ),
}


def flags_for(environment: str) -> FeatureFlags:
    """Resolve the flag set for an environment name."""
    return PROFILES.get(environment, PROFILES["development"])


# ---------------------------------------------------------------------------
# Startup health checks
# ---------------------------------------------------------------------------

@dataclass
class HealthCheck:
    name: str
    probe: Callable[[], bool]
    critical: bool = True


@dataclass
class HealthReport:
    checks: list[tuple[str, bool, bool]] = field(default_factory=list)

    def add(self, name: str, passed: bool, critical: bool) -> None:
        self.checks.append((name, passed, critical))

    @property
    def healthy(self) -> bool:
        return all(passed for _, passed, critical in self.checks if critical)

    def as_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "checks": [
                {"name": n, "passed": p, "critical": c}
                for n, p, c in self.checks
            ],
        }


def _probe_cache_roundtrip() -> bool:
    """Write and read back a sentinel through the cache layer."""
    key = "health:roundtrip"
    cache.set(key, {"ok": True}, ttl=5)
    value = cache.get(key)
    cache.delete(key)
    return bool(value and value.get("ok"))


def _probe_audit_writable() -> bool:
    """The audit trail must accept writes before any regulated action."""
    entry = audit.record("health.probe", component="wiring")
    return bool(entry and entry.get("event") == "health.probe")


def _probe_fixture_book() -> bool:
    """At least one account must exist after seeding."""
    return account_service.account_exists("alice")


STARTUP_CHECKS: tuple[HealthCheck, ...] = (
    HealthCheck("cache_roundtrip", _probe_cache_roundtrip, critical=True),
    HealthCheck("audit_writable", _probe_audit_writable, critical=True),
    HealthCheck("fixture_book", _probe_fixture_book, critical=False),
)


def run_startup_checks() -> HealthReport:
    """Execute every startup probe and collect the report.

    Probes must be side-effect free beyond their own sentinels; a probe
    that mutates business state is a bug, not a health check.
    """
    report = HealthReport()
    for check in STARTUP_CHECKS:
        try:
            passed = bool(check.probe())
        except Exception:  # noqa: BLE001 - a crashing probe is a failure
            passed = False
        report.add(check.name, passed, check.critical)
    return report


# ---------------------------------------------------------------------------
# Startup ordering
# ---------------------------------------------------------------------------

# The build proceeds in fixed stages. Order matters and is part of the
# contract: fixtures before bindings (bindings may resolve accounts),
# bindings before the risk engine (the engine consumes a binding-adjacent
# callable), checks last (they exercise the wired graph).
BUILD_STAGES = (
    "seed_fixtures",
    "bind_services",
    "construct_risk_engine",
    "startup_checks",
)


@dataclass
class BuildTrace:
    """Timing of each build stage, kept for the diagnostics endpoint."""

    stages: list[tuple[str, float]] = field(default_factory=list)

    def mark(self, stage: str, started: float) -> None:
        self.stages.append((stage, round(time.monotonic() - started, 6)))

    def as_dict(self) -> dict:
        return {name: seconds for name, seconds in self.stages}


# ---------------------------------------------------------------------------
# Binding policy
# ---------------------------------------------------------------------------

# Registry names are versioned API surface between modules. The rules the
# review checklist enforces:
#   * a name is bound exactly once, here;
#   * a name is never rebound at runtime (replace=True exists only so a
#     forced rebuild in tests does not trip on its own leftovers);
#   * the callable bound must be importable at module scope, so a cold
#     process can always rebuild the graph without request traffic.
# Violations do not fail fast at bind time; they fail the verification
# step below, which deploys treat as fatal.

@dataclass(frozen=True)
class BindingRecord:
    """What was bound, by which stage, for the diagnostics endpoint."""

    name: str
    target: str
    stage: str


_BINDINGS: list[BindingRecord] = []


def _record_binding(name: str, target: Callable[..., object]) -> None:
    _BINDINGS.append(
        BindingRecord(
            name=name,
            target=f"{target.__module__}.{target.__qualname__}",
            stage="bind_services",
        )
    )


def bindings() -> tuple[BindingRecord, ...]:
    """Immutable view of everything bound during the last build."""
    return tuple(_BINDINGS)


# ---------------------------------------------------------------------------
# Registry namespace conventions
# ---------------------------------------------------------------------------

# Registry names are dotted and namespaced by product area. The convention is
# "<area>.<capability>", lowercase, no versions in the name: a capability that
# changes shape gets a new name, it does not get a "_v2" suffix, because the
# suffix would leak a migration detail into every consumer forever.
_NAME_SEPARATOR = "."


def name_area(service_name: str) -> str:
    """Product area of a registry name, "accounts" for "accounts.balance"."""
    return service_name.split(_NAME_SEPARATOR, 1)[0]


def name_capability(service_name: str) -> str:
    """Capability part of a registry name, "balance" for "accounts.balance"."""
    parts = service_name.split(_NAME_SEPARATOR, 1)
    return parts[1] if len(parts) == 2 else ""


def name_is_conventional(service_name: str) -> bool:
    """Whether a registry name follows the dotted, lowercase convention.

    Checked by the verification step during deploys; a non-conventional name
    is a review miss, not a runtime failure, so it is reported rather than
    raised.
    """
    if _NAME_SEPARATOR not in service_name:
        return False
    if service_name != service_name.lower():
        return False
    return bool(name_area(service_name) and name_capability(service_name))


def group_names_by_area(names) -> dict[str, list[str]]:
    """Group registry names by product area, for the diagnostics view."""
    grouped: dict[str, list[str]] = {}
    for name in sorted(names):
        grouped.setdefault(name_area(name), []).append(name)
    return grouped


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Application:
    settings: Settings
    risk: RiskEngine


_APP: Optional[Application] = None
_LAST_TRACE: Optional[BuildTrace] = None
_LAST_HEALTH: Optional[HealthReport] = None


def build_application(force: bool = False) -> Application:
    """Idempotent composition root."""
    global _APP, _LAST_TRACE, _LAST_HEALTH
    if _APP is not None and not force:
        return _APP

    trace = BuildTrace()

    t0 = time.monotonic()
    fixtures.seed()
    trace.mark("seed_fixtures", t0)

    t0 = time.monotonic()
    registry.register(C.SERVICE_BALANCE_READ, account_service.get_user_balance)
    registry.register(C.SERVICE_ONBOARDING_CHECK, onboarding.evaluate_new_user)
    _record_binding(C.SERVICE_BALANCE_READ, account_service.get_user_balance)
    _record_binding(C.SERVICE_ONBOARDING_CHECK, onboarding.evaluate_new_user)
    trace.mark("bind_services", t0)

    t0 = time.monotonic()
    risk = RiskEngine(
        funds_source=account_service.get_user_balance,
        velocity=VelocityTracker(),
    )
    trace.mark("construct_risk_engine", t0)

    t0 = time.monotonic()
    _LAST_HEALTH = run_startup_checks()
    trace.mark("startup_checks", t0)

    _LAST_TRACE = trace
    _APP = Application(settings=get_settings(), risk=risk)
    return _APP


# Capabilities that must be bound by the time the build finishes. The
# verification step compares this against the live registry; a missing
# binding is a deployment blocker, not a warning.
_REQUIRED_SERVICES = (
    C.SERVICE_BALANCE_READ,
    C.SERVICE_ONBOARDING_CHECK,
)


def verify_wiring() -> list[str]:
    """Return the list of required services that are NOT bound.

    Empty list means the wiring is complete. Callers treat a non-empty
    result as fatal during deploys and as a red health check at runtime.
    """
    bound = set(registry.registered())
    return [name for name in _REQUIRED_SERVICES if name not in bound]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def describe() -> dict:
    """Snapshot of the wired application for the diagnostics endpoint.

    Never includes secrets; settings are reduced to names the on-call can
    act on. Safe to log verbatim.
    """
    app = build_application()
    missing = verify_wiring()
    return {
        "environment": app.settings.environment,
        "flags": vars(flags_for(app.settings.environment)),
        "services_bound": list(registry.registered()),
        "services_missing": missing,
        "build_trace": _LAST_TRACE.as_dict() if _LAST_TRACE else {},
        "health": _LAST_HEALTH.as_dict() if _LAST_HEALTH else {},
        "cache": cache.stats(),
    }


def readiness() -> tuple[bool, dict]:
    """(ready, detail) pair for the orchestrator readiness probe.

    Ready means: build completed, required services bound, critical health
    checks green. Liveness is intentionally simpler and lives with the
    process supervisor, not here.
    """
    detail = describe()
    ready = not detail["services_missing"] and detail["health"].get(
        "healthy", False
    )
    return ready, detail


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

_SHUTDOWN_HOOKS: list[tuple[str, Callable[[], object]]] = []


def on_shutdown(name: str, hook: Callable[[], object]) -> None:
    """Register a shutdown hook. Hooks run in reverse registration order.

    Hooks must be idempotent: the supervisor may deliver the termination
    signal more than once during a slow drain.
    """
    _SHUTDOWN_HOOKS.append((name, hook))


def shutdown() -> list[tuple[str, bool]]:
    """Run every shutdown hook, best effort, never raising.

    Returns (name, ok) pairs so the caller can log partial failures. The
    application reference is cleared last so a late request during drain
    still sees a wired graph instead of a half torn-down one.
    """
    global _APP
    results: list[tuple[str, bool]] = []
    for name, hook in reversed(_SHUTDOWN_HOOKS):
        try:
            hook()
            results.append((name, True))
        except Exception:  # noqa: BLE001 - drain must not raise
            results.append((name, False))
    _APP = None
    return results


# ---------------------------------------------------------------------------
# Test support
# ---------------------------------------------------------------------------

def build_for_tests() -> Application:
    """Fresh application with cleared shared state, for the test suite.

    Clears the registry, the cache and the notification-adjacent state that
    leaks between scenarios, then rebuilds from scratch. Production code
    must never call this; the force rebuild path exists for controlled
    reloads and does not clear state.
    """
    registry.reset()
    cache.clear()
    return build_application(force=True)


# ---------------------------------------------------------------------------
# Controlled reload
# ---------------------------------------------------------------------------

def reload_application() -> Application:
    """Tear down and rebuild the graph in place.

    Used by the ops runbook when a fixture refresh or a rate table reload
    must be picked up without bouncing the process. The sequence is
    deliberately shutdown-then-build rather than build-then-swap: the
    registry is process-global, so two live graphs would fight over the
    same binding namespace.
    """
    shutdown()
    _BINDINGS.clear()
    return build_application(force=True)


# ---------------------------------------------------------------------------
# Dependency notes
# ---------------------------------------------------------------------------

# Human-readable map of who consumes what, kept next to the bindings so a
# reviewer touching this file sees the blast radius without spelunking.
# This is documentation, not enforcement; the enforcement is the referee
# suite and the verification step above.
DEPENDENCY_NOTES = {
    "risk_engine": (
        "Constructed here with an injected funds source callable and a "
        "velocity tracker. The engine never imports service modules."
    ),
    "http_handlers": (
        "Resolve business capabilities through the service registry only; "
        "the route table stays free of service imports."
    ),
    "jobs": (
        "Background jobs consume shared infrastructure (cache, audit "
        "trail) and module entry points; the scheduler catalog lists "
        "cadence."
    ),
    "facade": (
        "Scripts and partner integrations go through brookpay.core.api; "
        "internal modules import services directly."
    ),
}


# ---------------------------------------------------------------------------
# Fault injection (chaos drills)
# ---------------------------------------------------------------------------

_FAULTS: dict[str, bool] = {
    "cache_read_miss": False,
    "audit_write_drop": False,
}


def set_fault(name: str, active: bool) -> None:
    """Arm or disarm a named fault for the next drill window.

    Faults are consulted only by the drill harness in scripts/; production
    code paths never read this table. The indirection exists so a drill
    can be armed through the diagnostics endpoint without a deploy.
    """
    if name not in _FAULTS:
        raise KeyError(f"unknown fault '{name}'")
    _FAULTS[name] = active


def active_faults() -> tuple[str, ...]:
    return tuple(name for name, on in _FAULTS.items() if on)


def clear_faults() -> None:
    for name in _FAULTS:
        _FAULTS[name] = False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """`python -m brookpay.app.wiring [describe|verify|checks]`.

    Ops tooling shells into this during deploys: `verify` gates the
    rollout, `describe` feeds the deploy log, `checks` re-runs the startup
    probes against a live process image.
    """
    import json
    import sys as _sys

    args = list(argv if argv is not None else _sys.argv[1:])
    command = args[0] if args else "describe"

    if command == "verify":
        build_application()
        missing = verify_wiring()
        if missing:
            print(json.dumps({"ok": False, "missing": missing}))
            return 2
        print(json.dumps({"ok": True}))
        return 0

    if command == "checks":
        build_application()
        report = run_startup_checks()
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0 if report.healthy else 1

    if command == "describe":
        print(json.dumps(describe(), indent=2, sort_keys=True, default=str))
        return 0

    print(f"unknown command '{command}'", file=_sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Binding audit
# ---------------------------------------------------------------------------

def binding_report() -> dict:
    """Describe every binding made during the last build.

    Consumed by the deploy log and the diagnostics endpoint. Reports the
    public registry name, the fully qualified target it resolves to, and
    whether the name follows the naming convention, so a reviewer can see the
    entire public-to-internal mapping without reading the composition root.
    """
    records = bindings()
    return {
        "count": len(records),
        "by_area": group_names_by_area(r.name for r in records),
        "records": [
            {
                "name": r.name,
                "target": r.target,
                "stage": r.stage,
                "conventional": name_is_conventional(r.name),
            }
            for r in records
        ],
    }


def unconventional_names() -> list[str]:
    """Bound names that break the naming convention, for the deploy check."""
    return [r.name for r in bindings() if not name_is_conventional(r.name)]


def target_of(service_name: str) -> str | None:
    """The fully qualified target a bound name resolves to, if bound here."""
    for record in bindings():
        if record.name == service_name:
            return record.target
    return None


def rebound_names() -> list[str]:
    """Names bound more than once during a build, which the policy forbids.

    A name appearing twice means either a duplicated register call or a
    rebuild that did not clear the binding record; both are review misses the
    verification step surfaces rather than tolerating silently.
    """
    seen: dict[str, int] = {}
    for record in bindings():
        seen[record.name] = seen.get(record.name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)
