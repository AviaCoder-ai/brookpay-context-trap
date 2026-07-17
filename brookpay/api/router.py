"""Route table.

Framework agnostic mapping of HTTP routes to handler callables plus the
tiny matching logic the ASGI adapter needs. Path parameters use the
{name} placeholder convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from brookpay.api.handlers import (
    handle_balance_request,
    handle_onboarding_check,
)


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    handler: Callable[..., dict]

    def compiled(self) -> re.Pattern:
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.pattern)
        return re.compile(f"^{regex}$")


ROUTES: tuple[Route, ...] = (
    Route("GET", "/v1/users/{user_id}/balance", handle_balance_request),
    Route("POST", "/v1/onboarding/{user_id}/check", handle_onboarding_check),
)


def match(method: str, path: str) -> Optional[tuple[Route, dict]]:
    """Return (route, path_params) for the first matching route."""
    for route in ROUTES:
        if route.method != method.upper():
            continue
        m = route.compiled().match(path)
        if m:
            return route, m.groupdict()
    return None


def dispatch(method: str, path: str, **query) -> dict:
    """Resolve and invoke a handler, merging path and query parameters."""
    found = match(method, path)
    if found is None:
        return {
            "http_status": 404,
            "body": {"error": {"code": "no_route",
                               "message": f"{method} {path}"}},
            "headers": {},
        }
    route, params = found
    return route.handler(**params, **query)
