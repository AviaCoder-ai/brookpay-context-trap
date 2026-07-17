"""Append-only audit trail.

Every regulated action records an event here. The trail is queryable by
event name and time window; the compliance jobs build their regulatory
exports exclusively from this data.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from brookpay.utils.timeutils import iso, parse_iso, utc_now

_TRAIL: list[dict[str, Any]] = []


def record(event: str, **fields: Any) -> dict[str, Any]:
    """Append one event. Returns the stored entry."""
    entry: dict[str, Any] = {"event": event, "at": iso(utc_now())}
    entry.update(fields)
    _TRAIL.append(entry)
    return entry


def query(
    event: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Filter the trail. All parameters are optional."""
    out = []
    for entry in _TRAIL:
        if event is not None and entry.get("event") != event:
            continue
        at = parse_iso(entry["at"])
        if since is not None and at < since:
            continue
        if until is not None and at >= until:
            continue
        out.append(entry)
    return out


def count(event: Optional[str] = None) -> int:
    return len(query(event=event))


def export_jsonl(path: str) -> int:
    """Dump the whole trail as JSON lines. Returns the number of rows."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for entry in _TRAIL:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return len(_TRAIL)


def reset() -> None:
    """Test helper. Never used in production paths."""
    _TRAIL.clear()
