"""End-to-end smoke scenarios.

Exercises the customer facing flows (billing, statements, withdrawals, API,
onboarding, alerting, compliance) against the seeded fixture book and prints
a PASS/FAIL table. Exit code 0 only when every scenario passes.

Run from the repository root:

    python3 scripts/run_scenarios.py
"""

from __future__ import annotations

import sys
import traceback
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brookpay.app.wiring import build_application
from brookpay.billing.invoicing import build_invoice, charge_invoice
from brookpay.core.api import check_new_user, fetch_balance_snapshot
from brookpay.api.handlers import handle_balance_request
from brookpay.fx.engine import convert
from brookpay.jobs.compliance_export import export_balance_read_rows
from brookpay.jobs.low_balance_alerts import KIND_LOW_BALANCE, run_low_balance_scan
from brookpay.reporting.statements import build_statement_view
from brookpay.services import notifications


class ScenarioFailure(AssertionError):
    pass


def scenario_billing_charge(app) -> str:
    """An active customer is charged a standard plan invoice in EUR."""
    invoice = build_invoice("alice", [("plan_standard", 1)], currency="EUR")
    outcome = charge_invoice(invoice)
    if outcome.status != "charged":
        raise ScenarioFailure(
            f"expected status 'charged', got '{outcome.status}' "
            f"(reason: {outcome.reason})"
        )
    if outcome.currency != "EUR":
        raise ScenarioFailure(f"charged in '{outcome.currency}', expected EUR")
    return f"invoice {invoice.invoice_id} charged {outcome.charged_amount} EUR"


def scenario_statement_multicurrency(app) -> str:
    """A JPY account holder reads a statement displayed in EUR."""
    expected_amount = convert(Decimal("100000"), "JPY", "EUR")
    expected_line = f"{float(expected_amount):,.2f} EUR"
    view = build_statement_view("kenji", 2026, 6, display_currency="EUR")
    if view["balance_line"] != expected_line:
        raise ScenarioFailure(
            f"balance line is '{view['balance_line']}', expected "
            f"'{expected_line}' (currency conversion lost)"
        )
    return f"kenji statement shows {view['balance_line']}"


def scenario_withdrawal_frozen_guard(app) -> str:
    """A frozen account must never be allowed to withdraw."""
    decision = app.risk.review_withdrawal("freya", Decimal("100"), currency="USD")
    if decision.allowed:
        raise ScenarioFailure(
            "CRITICAL: withdrawal APPROVED for a frozen account, "
            "with no error and no denial recorded"
        )
    if decision.reason != "account_frozen":
        raise ScenarioFailure(
            f"denied for '{decision.reason}', expected 'account_frozen'"
        )
    return "frozen account denied (account_frozen)"


def scenario_api_balance_endpoint(app) -> str:
    """The balance endpoint returns a complete snapshot with cache headers."""
    response = handle_balance_request("alice", currency="USD")
    if response["http_status"] != 200:
        raise ScenarioFailure(f"HTTP {response['http_status']}, expected 200")
    body = response["body"]
    missing = {"amount", "currency", "status", "last_updated"} - set(body)
    if missing:
        raise ScenarioFailure(f"body misses keys: {sorted(missing)}")
    if body["currency"] != "USD":
        raise ScenarioFailure(f"currency '{body['currency']}', expected USD")
    if "Last-Modified" not in response["headers"]:
        raise ScenarioFailure("Last-Modified header missing")
    return f"200 OK, {body['amount']} {body['currency']}, headers set"


def scenario_onboarding_unknown_user(app) -> str:
    """A brand new user is detected as needing wallet provisioning."""
    plan = check_new_user("ghost")
    if not plan.needs_wallet:
        raise ScenarioFailure("unknown user not flagged for provisioning")
    return "unknown user 'ghost' flagged needs_wallet=True"


def scenario_low_balance_alerts(app) -> str:
    """Recently consulted low-balance accounts trigger an alert."""
    before = notifications.count(KIND_LOW_BALANCE)
    # Regular product traffic: two customers consult their balance.
    fetch_balance_snapshot("dora")
    fetch_balance_snapshot("alice")
    run_low_balance_scan()
    delta = notifications.count(KIND_LOW_BALANCE) - before
    if delta < 1:
        raise ScenarioFailure(
            "no low balance alert emitted (no balance snapshots available "
            "to the scan)"
        )
    return f"{delta} low balance alert(s) emitted"


def scenario_compliance_export(app) -> str:
    """Every balance consultation must appear in the regulatory export."""
    before = len(export_balance_read_rows())
    for user_id in ("alice", "kenji", "mira"):
        fetch_balance_snapshot(user_id)
    delta = len(export_balance_read_rows()) - before
    if delta != 3:
        raise ScenarioFailure(
            f"expected 3 new balance.read rows in the export, got {delta}"
        )
    return f"{delta} balance.read rows added to the export"


SCENARIOS = (
    ("billing_charge", scenario_billing_charge),
    ("statement_multicurrency", scenario_statement_multicurrency),
    ("withdrawal_frozen_guard", scenario_withdrawal_frozen_guard),
    ("api_balance_endpoint", scenario_api_balance_endpoint),
    ("onboarding_unknown_user", scenario_onboarding_unknown_user),
    ("low_balance_alerts", scenario_low_balance_alerts),
    ("compliance_export", scenario_compliance_export),
)


def main() -> int:
    app = build_application()
    results: list[tuple[str, bool, str]] = []

    for name, fn in SCENARIOS:
        try:
            detail = fn(app)
            results.append((name, True, detail))
        except ScenarioFailure as exc:
            results.append((name, False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            tb = traceback.extract_tb(exc.__traceback__)[-1]
            results.append((
                name,
                False,
                f"{type(exc).__name__}: {exc} "
                f"[{Path(tb.filename).name}:{tb.lineno}]",
            ))

    width = max(len(name) for name, _, _ in results)
    print()
    print(f"BrookPay end-to-end scenarios ({len(results)} total)")
    print("-" * 78)
    failures = 0
    for name, passed, detail in results:
        mark = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{name.ljust(width)}  {mark}  {detail}")
    print("-" * 78)
    if failures:
        print(f"{failures} scenario(s) FAILED, {len(results) - failures} passed.")
    else:
        print("All scenarios passed.")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
