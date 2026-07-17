"""In-process notification queue.

Production drains this queue into the email and push providers; locally the
queue is simply inspectable, which is all the jobs and tests need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from brookpay.utils.timeutils import utc_now


@dataclass
class Notification:
    user_id: str
    kind: str
    message: str
    created_at: datetime
    meta: dict[str, Any] = field(default_factory=dict)


_QUEUE: list[Notification] = []


def enqueue(user_id: str, kind: str, message: str, **meta: Any) -> Notification:
    note = Notification(
        user_id=user_id,
        kind=kind,
        message=message,
        created_at=utc_now(),
        meta=meta,
    )
    _QUEUE.append(note)
    return note


def pending(kind: Optional[str] = None) -> list[Notification]:
    if kind is None:
        return list(_QUEUE)
    return [n for n in _QUEUE if n.kind == kind]


def count(kind: Optional[str] = None) -> int:
    return len(pending(kind))


def drain(kind: Optional[str] = None) -> list[Notification]:
    """Pop and return matching notifications (delivery simulation)."""
    global _QUEUE
    taken = pending(kind)
    if kind is None:
        _QUEUE = []
    else:
        _QUEUE = [n for n in _QUEUE if n.kind != kind]
    return taken
