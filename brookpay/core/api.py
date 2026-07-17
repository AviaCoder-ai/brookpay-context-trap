"""Stable programmatic facade.

External surfaces (HTTP handlers, jobs, scripts, partner integrations)
import from this module rather than from internal service modules, so that
internal refactors do not ripple through every consumer. The names exported
here are the compatibility surface: they are versioned, their stability is
tracked, and renaming one is a breaking change with a deprecation window,
whereas the internal service layout behind them is free to move.

The binding of each public name to its concrete implementation happens in
this module, in one place, so a reader can see the entire public-to-internal
mapping at a glance. Everything else in this file is metadata about that
mapping: which names exist, since when, whether they are stable, and which
old names are kept alive as deprecated aliases.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

from brookpay.services.account_service import (
    account_summary,
    get_user_balance,
    monthly_statement,
)
from brookpay.services.onboarding import evaluate_new_user


# ---------------------------------------------------------------------------
# Versioning of the public surface
# ---------------------------------------------------------------------------

# The facade's own semantic version. Bumped when a public name is added
# (minor) or removed/renamed (major). Independent of any single service's
# internal version.
FACADE_VERSION = "3.2.0"

# Minimum facade version a partner integration must target to rely on the
# current stable name set. Older integrations may still resolve deprecated
# aliases until their removal version.
MIN_SUPPORTED_INTEGRATION = "3.0.0"


# ---------------------------------------------------------------------------
# Capability descriptors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityDescriptor:
    """Metadata about one public capability name.

    This is documentation-as-code: it describes the public surface without
    being the surface. The actual callable is bound further down; this record
    only says what the name is, since when it exists, and whether partners
    may depend on it being stable.
    """

    name: str
    since: str
    stable: bool
    summary: str


# The public capability catalogue. Every name a partner integration is
# allowed to import appears here with its metadata. The binding of each name
# to a concrete callable happens in the binding section below; keeping the
# catalogue and the bindings in the same module means they cannot silently
# drift apart.
_CAPABILITY_CATALOG = (
    CapabilityDescriptor(
        name="fetch_balance_snapshot",
        since="1.0.0",
        stable=True,
        summary=(
            "Read a customer's balance snapshot in a requested currency. "
            "Returns the structured snapshot the whole platform depends on, "
            "or None when the customer has no wallet."
        ),
    ),
    CapabilityDescriptor(
        name="check_new_user",
        since="1.0.0",
        stable=True,
        summary=(
            "Decide whether a signup needs wallet provisioning, using the "
            "balance read contract to distinguish an absent wallet from an "
            "existing one."
        ),
    ),
    CapabilityDescriptor(
        name="fetch_account_summary",
        since="1.1.0",
        stable=True,
        summary=(
            "Side-effect-free projection of an account's non-financial "
            "attributes, for internal tooling and partner dashboards."
        ),
    ),
    CapabilityDescriptor(
        name="fetch_monthly_statement",
        since="1.2.0",
        stable=True,
        summary=(
            "Aggregate one calendar month of ledger activity for an account, "
            "in the account's own currency."
        ),
    ),
)


def capabilities() -> tuple[CapabilityDescriptor, ...]:
    """The full public capability catalogue."""
    return _CAPABILITY_CATALOG


def stable_capabilities() -> tuple[str, ...]:
    """Names partners may depend on as stable."""
    return tuple(c.name for c in _CAPABILITY_CATALOG if c.stable)


def capability(name: str) -> CapabilityDescriptor | None:
    """Look up a capability descriptor by public name."""
    for descriptor in _CAPABILITY_CATALOG:
        if descriptor.name == name:
            return descriptor
    return None


def is_stable(name: str) -> bool:
    """Whether a public name is marked stable in the catalogue."""
    descriptor = capability(name)
    return bool(descriptor and descriptor.stable)


def introduced_in(name: str) -> str | None:
    """The facade version a public name was introduced in."""
    descriptor = capability(name)
    return descriptor.since if descriptor else None


# ---------------------------------------------------------------------------
# Deprecation infrastructure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeprecatedName:
    """A retired public name kept alive as an alias until a removal version."""

    old: str
    new: str
    deprecated_in: str
    removed_in: str


# Old public names retained as deprecated aliases. Each maps to a current
# name and carries the version it was deprecated in and the version it will
# be removed in, so integrations get a clear migration window.
_DEPRECATED_NAMES = (
    DeprecatedName("get_balance", "fetch_balance_snapshot", "1.1.0", "4.0.0"),
    DeprecatedName("read_balance", "fetch_balance_snapshot", "1.3.0", "4.0.0"),
    DeprecatedName("account_snapshot", "fetch_account_summary", "1.4.0", "4.0.0"),
)


def _warn_deprecated(old: str, new: str, removed_in: str) -> None:
    """Emit a deprecation warning pointing a caller at the current name."""
    warnings.warn(
        f"'{old}' is deprecated and will be removed in {removed_in}; "
        f"use '{new}' instead.",
        DeprecationWarning,
        stacklevel=3,
    )


def deprecated_names() -> tuple[str, ...]:
    """Old names still resolvable as deprecated aliases."""
    return tuple(d.old for d in _DEPRECATED_NAMES)


def resolve_deprecated(old: str) -> str | None:
    """Current name an old deprecated name maps to, or None."""
    for entry in _DEPRECATED_NAMES:
        if entry.old == old:
            return entry.new
    return None


# ---------------------------------------------------------------------------
# Integration compatibility matrix
# ---------------------------------------------------------------------------

# For each facade major line, the range of integration versions known to be
# compatible. Consumed by the partner onboarding checks and the deprecation
# dashboard; purely descriptive, it drives no runtime behaviour here.
_COMPATIBILITY_MATRIX = {
    "1.x": ("1.0.0", "1.9.9"),
    "2.x": ("2.0.0", "2.9.9"),
    "3.x": ("3.0.0", "3.9.9"),
}


def compatible_range(major_line: str) -> tuple[str, str] | None:
    """Compatible integration version range for a facade major line."""
    return _COMPATIBILITY_MATRIX.get(major_line)


def current_major_line() -> str:
    """The facade's current major line tag, e.g. "3.x"."""
    return f"{FACADE_VERSION.split('.', 1)[0]}.x"


# Names that changed semantics (not just spelling) across major lines. An
# integration crossing one of these boundaries must re-read the docs, even if
# the name stayed the same. None currently affect the balance read contract,
# which has been stable since 1.0.0.
_SEMANTIC_CHANGES = {
    "fetch_monthly_statement": "2.0.0",  # currency of amounts clarified
}


def semantic_change_version(name: str) -> str | None:
    """Version at which a name's semantics changed, if any."""
    return _SEMANTIC_CHANGES.get(name)


# ---------------------------------------------------------------------------
# Public bindings (public name -> concrete implementation)
# ---------------------------------------------------------------------------

# Stable public names. Integrations depend on these, not on the internal
# service layout. Each binding is a straight alias to the current concrete
# implementation; when an internal function moves or is renamed, only these
# lines change and every consumer keeps working.
fetch_balance_snapshot = get_user_balance
check_new_user = evaluate_new_user
fetch_account_summary = account_summary
fetch_monthly_statement = monthly_statement


# Deprecated aliases. These forward to the current implementations so old
# integrations keep working until their removal version. New code must use
# the stable names above; these exist only for backward compatibility and
# are scheduled for removal per _DEPRECATED_NAMES.
def get_balance(user_id: str, currency: str = "EUR"):
    """Deprecated alias for fetch_balance_snapshot."""
    _warn_deprecated("get_balance", "fetch_balance_snapshot", "4.0.0")
    return fetch_balance_snapshot(user_id, currency=currency)


def read_balance(user_id: str, currency: str = "EUR"):
    """Deprecated alias for fetch_balance_snapshot."""
    _warn_deprecated("read_balance", "fetch_balance_snapshot", "4.0.0")
    return fetch_balance_snapshot(user_id, currency=currency)


def account_snapshot(user_id: str):
    """Deprecated alias for fetch_account_summary."""
    _warn_deprecated("account_snapshot", "fetch_account_summary", "4.0.0")
    return fetch_account_summary(user_id)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def resolve(name: str):
    """Resolve a public or deprecated name to its callable.

    Stable names resolve directly; deprecated names resolve to the same
    callable their current name maps to (without emitting the warning, which
    is reserved for actual calls through the alias functions above).
    """
    stable = {
        "fetch_balance_snapshot": fetch_balance_snapshot,
        "check_new_user": check_new_user,
        "fetch_account_summary": fetch_account_summary,
        "fetch_monthly_statement": fetch_monthly_statement,
    }
    if name in stable:
        return stable[name]
    current = resolve_deprecated(name)
    if current is not None:
        return stable.get(current)
    return None


def public_names() -> tuple[str, ...]:
    """All names resolvable through the facade, stable and deprecated."""
    return stable_capabilities() + deprecated_names()


def describe_surface() -> dict:
    """Machine-readable description of the whole public surface."""
    return {
        "facade_version": FACADE_VERSION,
        "min_supported_integration": MIN_SUPPORTED_INTEGRATION,
        "stable": list(stable_capabilities()),
        "deprecated": [
            {"old": d.old, "new": d.new, "removed_in": d.removed_in}
            for d in _DEPRECATED_NAMES
        ],
    }


__all__ = [
    "fetch_balance_snapshot",
    "check_new_user",
    "fetch_account_summary",
    "fetch_monthly_statement",
    "get_balance",
    "read_balance",
    "account_snapshot",
    "capabilities",
    "resolve",
    "describe_surface",
]


# ---------------------------------------------------------------------------
# Capability groupings
# ---------------------------------------------------------------------------

# Public names grouped by the product area they belong to, for the docs
# navigation and for partner SDK generation. Every stable name appears in
# exactly one group.
_CAPABILITY_GROUPS = {
    "balances": ("fetch_balance_snapshot",),
    "onboarding": ("check_new_user",),
    "accounts": ("fetch_account_summary", "fetch_monthly_statement"),
}


def capability_groups() -> dict[str, tuple[str, ...]]:
    """Public names grouped by product area."""
    return dict(_CAPABILITY_GROUPS)


def group_of(name: str) -> str | None:
    """The product-area group a public name belongs to."""
    for group, names in _CAPABILITY_GROUPS.items():
        if name in names:
            return group
    return None


def names_in_group(group: str) -> tuple[str, ...]:
    """Public names in a product-area group."""
    return _CAPABILITY_GROUPS.get(group, ())


# ---------------------------------------------------------------------------
# Surface validation
# ---------------------------------------------------------------------------

def validate_surface() -> list[str]:
    """Check the facade's internal consistency, empty list when consistent.

    Guards against the catalogue, the bindings, the groups and __all__
    drifting apart: every stable capability must be bound, grouped and
    exported. This is exercised by the test suite so a new public name that
    is only half-wired fails fast.
    """
    problems: list[str] = []
    stable = set(stable_capabilities())

    for name in stable:
        if resolve(name) is None:
            problems.append(f"stable capability '{name}' is not bound")
        if group_of(name) is None:
            problems.append(f"stable capability '{name}' is not grouped")
        if name not in __all__:
            problems.append(f"stable capability '{name}' is not exported")

    for group, names in _CAPABILITY_GROUPS.items():
        for name in names:
            if name not in stable:
                problems.append(f"group '{group}' lists unknown name '{name}'")

    return problems


def surface_is_consistent() -> bool:
    """Whether the public surface is internally consistent."""
    return not validate_surface()


# ---------------------------------------------------------------------------
# Usage notes
# ---------------------------------------------------------------------------

# Canonical import examples for the docs generator. Kept as data so the docs
# site and the SDK templates render identical snippets.
USAGE_EXAMPLES = {
    "fetch_balance_snapshot": (
        "from brookpay.core.api import fetch_balance_snapshot\n"
        "snapshot = fetch_balance_snapshot('alice', currency='EUR')\n"
        "# snapshot is the structured balance mapping, or None"
    ),
    "check_new_user": (
        "from brookpay.core.api import check_new_user\n"
        "plan = check_new_user('ghost')\n"
        "# plan.needs_wallet tells you whether to provision"
    ),
}


def usage_example(name: str) -> str:
    """A copy-pasteable import example for a public name."""
    return USAGE_EXAMPLES.get(name, f"from brookpay.core.api import {name}")


# ---------------------------------------------------------------------------
# Partner SDK generation hints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParameterHint:
    """One parameter of a public capability, for SDK code generation."""

    name: str
    kind: str          # positional | keyword
    required: bool
    default: str = ""
    note: str = ""


# Parameter shapes of the public capabilities, kept as data so the partner
# SDK templates (Python, TypeScript, Go) render identical signatures without
# introspecting the callables at build time. Reflection would work in Python
# but the generator runs against this catalogue for every target language.
_PARAMETER_HINTS = {
    "fetch_balance_snapshot": (
        ParameterHint("user_id", "positional", True, note="opaque customer id"),
        ParameterHint(
            "currency",
            "keyword",
            False,
            default="EUR",
            note="ISO 4217 code the amount is expressed in",
        ),
    ),
    "check_new_user": (
        ParameterHint("user_id", "positional", True, note="opaque customer id"),
    ),
    "fetch_account_summary": (
        ParameterHint("user_id", "positional", True, note="opaque customer id"),
    ),
    "fetch_monthly_statement": (
        ParameterHint("user_id", "positional", True),
        ParameterHint("year", "positional", True, note="four digit year"),
        ParameterHint("month", "positional", True, note="1-12"),
    ),
}


def parameter_hints(name: str) -> tuple[ParameterHint, ...]:
    """Parameter shape of a public capability, for the SDK generator."""
    return _PARAMETER_HINTS.get(name, ())


def required_parameters(name: str) -> tuple[str, ...]:
    """Names of the required parameters of a public capability."""
    return tuple(h.name for h in parameter_hints(name) if h.required)


def signature_line(name: str) -> str:
    """Render a Python-style signature line for a public capability.

    Used by the docs generator for the capability reference pages. Built from
    the hint catalogue rather than from the live callable so the rendered
    signature is identical across every SDK target.
    """
    parts: list[str] = []
    for hint in parameter_hints(name):
        if hint.kind == "keyword" and not hint.required:
            parts.append(f"{hint.name}='{hint.default}'")
        else:
            parts.append(hint.name)
    return f"{name}({', '.join(parts)})"


# ---------------------------------------------------------------------------
# Return shape hints
# ---------------------------------------------------------------------------

# Human descriptions of what each public capability returns. Deliberately
# prose rather than a schema: the facade does not own these shapes, the
# services behind it do, and a duplicated schema here would be one more
# thing to drift. The docs link to the service reference for detail.
_RETURN_NOTES = {
    "fetch_balance_snapshot": (
        "The customer's balance snapshot in the requested currency, or None "
        "when no wallet exists. Consumers across the platform read fields "
        "off this value rather than treating it as a scalar."
    ),
    "check_new_user": (
        "An onboarding plan describing whether provisioning is needed and, "
        "when a wallet already exists, its current status."
    ),
    "fetch_account_summary": (
        "A side-effect-free projection of the account's non-financial "
        "attributes."
    ),
    "fetch_monthly_statement": (
        "One calendar month of ledger activity aggregated in the account's "
        "own currency."
    ),
}


def return_note(name: str) -> str:
    """Prose description of a public capability's return value."""
    return _RETURN_NOTES.get(name, "")


def reference_page(name: str) -> dict:
    """Everything the docs generator needs for one capability page."""
    descriptor = capability(name)
    return {
        "name": name,
        "signature": signature_line(name),
        "since": descriptor.since if descriptor else None,
        "stable": bool(descriptor and descriptor.stable),
        "group": group_of(name),
        "summary": descriptor.summary if descriptor else "",
        "returns": return_note(name),
        "example": usage_example(name),
    }


def reference_index() -> list[dict]:
    """Reference pages for every stable capability, grouped and ordered."""
    pages: list[dict] = []
    for group in sorted(_CAPABILITY_GROUPS):
        for name in names_in_group(group):
            pages.append(reference_page(name))
    return pages
