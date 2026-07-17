"""Minimal service registry (command bus style).

Modules that must stay decoupled at import time invoke each other through
named services. Bindings are declared once in the application wiring.
"""

from __future__ import annotations

from typing import Any, Callable

from brookpay.core.errors import ServiceNotRegistered

_SERVICES: dict[str, Callable[..., Any]] = {}


def register(name: str, fn: Callable[..., Any], replace: bool = True) -> None:
    if not replace and name in _SERVICES:
        raise ValueError(f"service '{name}' already registered")
    _SERVICES[name] = fn


def resolve(name: str) -> Callable[..., Any]:
    try:
        return _SERVICES[name]
    except KeyError:
        raise ServiceNotRegistered(name) from None


def invoke(name: str, *args: Any, **kwargs: Any) -> Any:
    return resolve(name)(*args, **kwargs)


def registered() -> tuple[str, ...]:
    return tuple(sorted(_SERVICES))


def reset() -> None:
    """Test helper."""
    _SERVICES.clear()
