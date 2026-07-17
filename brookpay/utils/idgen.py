"""Identifier generation for domain objects."""

from __future__ import annotations

import itertools
import uuid

_SEQ = itertools.count(1)


def new_id(prefix: str) -> str:
    """Opaque unique id, e.g. inv_9f2c41ab03de."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sequential_id(prefix: str) -> str:
    """Monotonic id for in-process ordering needs (tests, fixtures)."""
    return f"{prefix}-{next(_SEQ):06d}"
