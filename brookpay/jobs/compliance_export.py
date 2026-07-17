"""Regulatory export of balance read events.

The regulator requires a daily file listing every balance consultation
(who, when, in which currency, account status at the time). The export is
built exclusively from the audit trail; if an event was not audited, it does
not exist for compliance purposes.

That "exclusively from the audit trail" is the load-bearing property. This
job never reconstructs consultations from anywhere else: no request logs, no
cache, no re-derivation. Its only input is the set of `balance.read` audit
events the balance read path emits as a side effect. If that side effect
ever stops, this export is silently empty, and an empty regulatory file
looks exactly like a day on which nobody checked a balance.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from brookpay.config.constants import EVENT_BALANCE_READ
from brookpay.config.settings import get_settings
from brookpay.core import audit

_COLUMNS = ("at", "user_id", "currency", "status")


# ---------------------------------------------------------------------------
# Column schema
# ---------------------------------------------------------------------------

def columns() -> tuple[str, ...]:
    """The regulatory file's column order, as a stable contract."""
    return _COLUMNS


def _row_from_entry(entry: dict) -> dict:
    """Project one audit entry into the export's column schema."""
    return {
        "at": entry.get("at", ""),
        "user_id": entry.get("user_id", ""),
        "currency": entry.get("currency", ""),
        "status": entry.get("status", ""),
    }


def _validate_row(row: dict) -> bool:
    """A row is exportable only if the mandatory fields are present."""
    return bool(row.get("at")) and bool(row.get("user_id"))


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

_FILE_PREFIX = "balance_reads"


def export_filename(day_label: str) -> str:
    """Canonical export filename for a day label, "balance_reads_YYYYMMDD.csv"."""
    return f"{_FILE_PREFIX}_{day_label}.csv"


def today_label() -> str:
    """Day label for the current UTC day, "YYYYMMDD"."""
    return datetime.utcnow().strftime("%Y%m%d")


def parse_label(filename: str) -> Optional[str]:
    """Recover the day label from an export filename, or None."""
    stem = Path(filename).stem
    if not stem.startswith(f"{_FILE_PREFIX}_"):
        return None
    return stem[len(_FILE_PREFIX) + 1:] or None


# ---------------------------------------------------------------------------
# Regulator profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegulatorProfile:
    """How one regulator wants the balance-read export delivered."""

    code: str
    delimiter: str
    include_header: bool
    date_format: str
    timezone: str


# Vendored profiles. Delimiters and date formats differ per regulator; the
# column set is the same everywhere and defined once by _COLUMNS. Refresh
# these only against the regulator's published spec.
_REGULATOR_PROFILES = {
    "default": RegulatorProfile("default", ",", True, "%Y-%m-%dT%H:%M:%S", "UTC"),
    "eu_nca": RegulatorProfile("eu_nca", ",", True, "%Y-%m-%dT%H:%M:%S", "UTC"),
    "uk_fca": RegulatorProfile("uk_fca", ",", True, "%d/%m/%Y %H:%M:%S", "UTC"),
    "sg_mas": RegulatorProfile("sg_mas", "|", True, "%Y-%m-%d %H:%M:%S", "UTC"),
}


def profile_for(regulator: str) -> RegulatorProfile:
    """Resolve a regulator profile, defaulting to the generic one."""
    return _REGULATOR_PROFILES.get(regulator, _REGULATOR_PROFILES["default"])


def supported_regulators() -> tuple[str, ...]:
    return tuple(sorted(_REGULATOR_PROFILES))


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

# The export schema version. Bumped when the column set or semantics change;
# recorded in the submission manifest so the regulator knows how to parse a
# historical file. The column set below must match _COLUMNS.
SCHEMA_VERSION = "2.1"

_SCHEMA_HISTORY = {
    "1.0": ("at", "user_id"),
    "2.0": ("at", "user_id", "currency"),
    "2.1": ("at", "user_id", "currency", "status"),
}


def schema_columns(version: str) -> tuple[str, ...]:
    """Columns for a historical schema version, for reparsing old files."""
    return _SCHEMA_HISTORY.get(version, _COLUMNS)


def schema_is_current(version: str) -> bool:
    return version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Submission windows
# ---------------------------------------------------------------------------

# The export covers a full UTC day and must be submitted by this hour of the
# following day. Consumed by the submission scheduler and the lateness check.
SUBMISSION_DEADLINE_HOUR = 12


def is_submission_late(submitted_hour: int, submitted_day_offset: int) -> bool:
    """Whether a submission missed its deadline.

    An export for day D is due by SUBMISSION_DEADLINE_HOUR on day D+1. A
    submission on D+1 before the hour is on time; anything later, or on a
    later day, is late.
    """
    if submitted_day_offset < 1:
        return False
    if submitted_day_offset > 1:
        return True
    return submitted_hour >= SUBMISSION_DEADLINE_HOUR


# ---------------------------------------------------------------------------
# PII handling policy
# ---------------------------------------------------------------------------

# Columns that carry customer identifiers. The export keeps them in clear
# because the regulator requires attributable records; this list exists so
# any *other* consumer of these rows knows which fields are sensitive.
_PII_COLUMNS = ("user_id",)


def pii_columns() -> tuple[str, ...]:
    return _PII_COLUMNS


def redact_for_internal(row: dict) -> dict:
    """Return a copy of a row with PII columns masked, for internal previews.

    Used when the export content is shown on an internal dashboard rather
    than submitted; the submitted file itself is never redacted, since the
    regulator needs attributable records.
    """
    redacted = dict(row)
    for col in _PII_COLUMNS:
        value = str(redacted.get(col, ""))
        if len(value) > 2:
            redacted[col] = f"{value[0]}***{value[-1]}"
    return redacted


# ---------------------------------------------------------------------------
# Regulator-specific row formatting
# ---------------------------------------------------------------------------

def format_row_for(row: dict, profile: RegulatorProfile) -> dict:
    """Apply a regulator profile's formatting to a normalized row.

    Only presentational transforms happen here (date format mainly); the set
    of columns and their meaning are fixed. The timestamp is reformatted from
    the canonical ISO form the audit trail stores into the profile's format
    when the two differ.
    """
    formatted = dict(row)
    at = row.get("at", "")
    if at and profile.date_format != "%Y-%m-%dT%H:%M:%S":
        try:
            parsed = datetime.strptime(at, "%Y-%m-%dT%H:%M:%S")
            formatted["at"] = parsed.strftime(profile.date_format)
        except ValueError:
            formatted["at"] = at
    return formatted


def serialise_rows(rows: list[dict], profile: RegulatorProfile) -> str:
    """Serialise rows to a delimited string using a regulator profile.

    Mirrors what write_daily_export writes to disk, but returns a string for
    the preview endpoint and for checksum computation over the exact bytes a
    regulator will receive.
    """
    lines: list[str] = []
    if profile.include_header:
        lines.append(profile.delimiter.join(_COLUMNS))
    for row in rows:
        formatted = format_row_for(row, profile)
        lines.append(profile.delimiter.join(str(formatted.get(col, "")) for col in _COLUMNS))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Export construction (built exclusively from the audit trail)
# ---------------------------------------------------------------------------

def export_balance_read_rows(since: Optional[datetime] = None) -> list[dict]:
    """Normalized rows for the regulatory file, oldest first.

    The rows come exclusively from `balance.read` audit events queried from
    the trail. There is no fallback source: if the balance read path stopped
    recording these events (for instance because the audit side effect was
    removed alongside a balance refactor), this returns an empty list and the
    regulatory file is empty, with nothing anywhere raising to indicate that
    consultations are happening but not being recorded.
    """
    rows = []
    for entry in audit.query(event=EVENT_BALANCE_READ, since=since):
        rows.append({
            "at": entry.get("at", ""),
            "user_id": entry.get("user_id", ""),
            "currency": entry.get("currency", ""),
            "status": entry.get("status", ""),
        })
    return rows


def count_balance_reads(since: Optional[datetime] = None) -> int:
    """How many balance.read events are in scope for the export.

    A thin count over the same audit query the export uses, so the number
    shown on the compliance dashboard matches the file that would be written.
    A zero here is the same silent-blindness signal described above.
    """
    return len(export_balance_read_rows(since=since))


def write_daily_export(
    rows: list[dict],
    day_label: Optional[str] = None,
    out_dir: Optional[str] = None,
) -> Path:
    """Write the CSV under var/exports/. Returns the file path."""
    settings = get_settings()
    directory = Path(out_dir or settings.exports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    label = day_label or today_label()
    path = directory / export_filename(label)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# ---------------------------------------------------------------------------
# Row filtering and grouping
# ---------------------------------------------------------------------------

def filter_by_status(rows: list[dict], status: str) -> list[dict]:
    """Rows whose recorded account status matches."""
    return [r for r in rows if r.get("status") == status]


def group_by_currency(rows: list[dict]) -> dict[str, int]:
    """Count of balance reads per currency, for the summary panel."""
    counts: dict[str, int] = {}
    for row in rows:
        cur = row.get("currency", "")
        counts[cur] = counts.get(cur, 0) + 1
    return counts


def group_by_user(rows: list[dict]) -> dict[str, int]:
    """Count of balance reads per user."""
    counts: dict[str, int] = {}
    for row in rows:
        uid = row.get("user_id", "")
        counts[uid] = counts.get(uid, 0) + 1
    return counts


def distinct_users(rows: list[dict]) -> int:
    return len({r.get("user_id", "") for r in rows if r.get("user_id")})


# ---------------------------------------------------------------------------
# Manifest and integrity
# ---------------------------------------------------------------------------

@dataclass
class ExportManifest:
    day_label: str
    row_count: int
    distinct_users: int
    sha256: str


def content_digest(rows: list[dict]) -> str:
    """Deterministic SHA-256 over the export's canonical row content.

    Computed from a stable serialisation of the rows so the same rows always
    yield the same digest, letting the regulator confirm the file was not
    altered after submission.
    """
    hasher = hashlib.sha256()
    for row in rows:
        canonical = "|".join(str(row.get(col, "")) for col in _COLUMNS)
        hasher.update(canonical.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def build_manifest(rows: list[dict], day_label: Optional[str] = None) -> ExportManifest:
    """Assemble the manifest that accompanies a submitted export."""
    return ExportManifest(
        day_label=day_label or today_label(),
        row_count=len(rows),
        distinct_users=distinct_users(rows),
        sha256=content_digest(rows),
    )


def verify_manifest(rows: list[dict], manifest: ExportManifest) -> bool:
    """Recompute the digest and confirm it matches the manifest."""
    return content_digest(rows) == manifest.sha256


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

# How many days of exports to keep on local disk before archival takes over.
LOCAL_RETENTION_DAYS = 90


def is_expired(day_label: str, today: Optional[str] = None) -> bool:
    """Whether an export day label is past the local retention window.

    Compares the YYYYMMDD labels lexicographically, which orders correctly
    for that fixed format, after computing the cutoff label. A malformed
    label is treated as not expired, so nothing is deleted on bad input.
    """
    today = today or today_label()
    try:
        today_dt = datetime.strptime(today, "%Y%m%d")
        label_dt = datetime.strptime(day_label, "%Y%m%d")
    except ValueError:
        return False
    return (today_dt - label_dt).days > LOCAL_RETENTION_DAYS


def expired_labels(labels: list[str], today: Optional[str] = None) -> list[str]:
    """Subset of labels past the retention window, safe to archive/remove."""
    return [label for label in labels if is_expired(label, today)]


# ---------------------------------------------------------------------------
# Run entry point
# ---------------------------------------------------------------------------

def run_export(since: Optional[datetime] = None) -> tuple[int, Path]:
    """Build and write today's file. Returns (row_count, path)."""
    rows = export_balance_read_rows(since=since)
    path = write_daily_export(rows)
    return len(rows), path


def run_export_with_manifest(since: Optional[datetime] = None) -> dict:
    """Build the file and its manifest in one pass, for the submission job."""
    rows = export_balance_read_rows(since=since)
    path = write_daily_export(rows)
    manifest = build_manifest(rows)
    return {
        "path": str(path),
        "row_count": manifest.row_count,
        "distinct_users": manifest.distinct_users,
        "sha256": manifest.sha256,
    }


def scheduler_entry() -> dict:
    """Describe this job for the scheduler catalog."""
    return {
        "name": "compliance_export",
        "interval_seconds": 86400,
        "entrypoint": "brookpay.jobs.compliance_export:run_export",
        "audit_driven": True,
    }


# ---------------------------------------------------------------------------
# Submission tracking
# ---------------------------------------------------------------------------

@dataclass
class SubmissionRecord:
    day_label: str
    regulator: str
    row_count: int
    sha256: str
    status: str  # prepared | submitted | acknowledged | rejected


class SubmissionLog:
    """In-memory log of export submissions, for the compliance console.

    The durable record lives in the regulatory portal and the audit trail;
    this is a convenience view of recent submissions and their acknowledgement
    status so an officer can see at a glance what has and has not been filed.
    """

    def __init__(self) -> None:
        self._records: list[SubmissionRecord] = []

    def prepare(self, manifest: ExportManifest, regulator: str = "default") -> SubmissionRecord:
        record = SubmissionRecord(
            day_label=manifest.day_label,
            regulator=regulator,
            row_count=manifest.row_count,
            sha256=manifest.sha256,
            status="prepared",
        )
        self._records.append(record)
        return record

    def mark(self, day_label: str, regulator: str, status: str) -> bool:
        for record in self._records:
            if record.day_label == day_label and record.regulator == regulator:
                record.status = status
                return True
        return False

    def outstanding(self) -> list[SubmissionRecord]:
        """Submissions not yet acknowledged by the regulator."""
        return [r for r in self._records if r.status not in ("acknowledged",)]

    def summary(self) -> dict:
        by_status: dict[str, int] = {}
        for record in self._records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
        return {"total": len(self._records), "by_status": by_status}


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_counts(export_row_count: int, audit_event_count: int) -> dict:
    """Compare the export row count against the raw audit event count.

    They should be equal: the export is a straight projection of the audit
    events. A mismatch means rows were dropped in projection (a bug) or the
    audit query and the export query diverged. A zero on both sides is the
    silent-blindness case, flagged separately so it is not mistaken for a
    clean reconciliation.
    """
    delta = export_row_count - audit_event_count
    return {
        "export_rows": export_row_count,
        "audit_events": audit_event_count,
        "delta": delta,
        "balanced": delta == 0,
        "both_empty": export_row_count == 0 and audit_event_count == 0,
    }


def self_check(since: Optional[datetime] = None) -> dict:
    """End-to-end sanity check of the export against the audit trail.

    Builds the rows, counts the underlying events, and reconciles. Used by
    the compliance dashboard's health widget; a not-balanced or both-empty
    result is surfaced to an officer rather than silently tolerated.
    """
    rows = export_balance_read_rows(since=since)
    event_count = audit.count(EVENT_BALANCE_READ)
    reconciliation = reconcile_counts(len(rows), event_count)
    return {
        "rows": len(rows),
        "distinct_users": distinct_users(rows),
        "reconciliation": reconciliation,
        "schema_version": SCHEMA_VERSION,
    }
