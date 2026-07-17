"""Job catalog.

The production scheduler (systemd timers) shells into `python -m` targets;
this catalog documents cadence and gives local tooling one place to run
everything in order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from brookpay.jobs.compliance_export import run_export
from brookpay.jobs.low_balance_alerts import run_low_balance_scan
from brookpay.services.account_service import (
    expire_stale_holds,
    reconciliation_report,
    snapshot_all_active,
)


@dataclass(frozen=True)
class Job:
    name: str
    cadence: str
    run: Callable[[], object]


CATALOG: tuple[Job, ...] = (
    Job("low_balance_alerts", "*/5 * * * *", run_low_balance_scan),
    Job("compliance_export", "10 0 * * *", run_export),
    Job("daily_snapshots", "30 0 * * *", snapshot_all_active),
    Job("expire_stale_holds", "0 * * * *", expire_stale_holds),
    Job("reconciliation", "45 0 * * *", reconciliation_report),
)


def run_all() -> dict[str, object]:
    """Run every job once, sequentially. Local tooling only."""
    results: dict[str, object] = {}
    for job in CATALOG:
        results[job.name] = job.run()
    return results
