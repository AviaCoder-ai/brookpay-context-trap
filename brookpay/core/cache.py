"""Process-local TTL cache.

In production this module fronts Redis; the in-memory implementation keeps
the exact same interface so the rest of the codebase does not care. Keys are
plain strings, values are JSON-shaped Python objects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional


@dataclass
class _Entry:
    value: Any
    expires_at: Optional[float]  # epoch seconds, None means no expiry

    def is_live(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._hits = 0
        self._misses = 0

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        expires_at = time.time() + ttl if ttl else None
        self._data[key] = _Entry(value=value, expires_at=expires_at)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._data.get(key)
        now = time.time()
        if entry is None or not entry.is_live(now):
            if entry is not None:
                self._data.pop(key, None)
            self._misses += 1
            return default
        self._hits += 1
        return entry.value

    def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    def scan(self, prefix: str) -> Iterator[tuple[str, Any]]:
        """Yield (key, value) pairs for live entries under a key prefix."""
        now = time.time()
        for key in list(self._data.keys()):
            if not key.startswith(prefix):
                continue
            entry = self._data.get(key)
            if entry is None:
                continue
            if not entry.is_live(now):
                self._data.pop(key, None)
                continue
            yield key, entry.value

    def purge_expired(self) -> int:
        now = time.time()
        dead = [k for k, e in self._data.items() if not e.is_live(now)]
        for k in dead:
            self._data.pop(k, None)
        return len(dead)

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> dict:
        return {
            "entries": len(self._data),
            "hits": self._hits,
            "misses": self._misses,
        }


_CACHE = MemoryCache()


def set(key: str, value: Any, ttl: Optional[int] = None) -> None:  # noqa: A001
    _CACHE.set(key, value, ttl=ttl)


def get(key: str, default: Any = None) -> Any:
    return _CACHE.get(key, default=default)


def delete(key: str) -> bool:
    return _CACHE.delete(key)


def scan(prefix: str) -> Iterator[tuple[str, Any]]:
    return _CACHE.scan(prefix)


def purge_expired() -> int:
    return _CACHE.purge_expired()


def clear() -> None:
    _CACHE.clear()


def stats() -> dict:
    return _CACHE.stats()
