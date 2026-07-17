"""HTTP handlers (framework agnostic).

Handlers resolve business capabilities through the service registry so the
API layer carries no hard imports of service internals; bindings live in the
application wiring. A handler's job is narrow: validate the request shape,
invoke the bound capability by its registry name, and marshal the result
into the transport envelope (status, body, headers). No handler reaches into
a service module directly, and no handler makes a business decision of its
own.

Keeping every handler in this shape means the route table (see router.py)
never imports a service, and a capability can be rebound in wiring without
touching a single handler.
"""

from __future__ import annotations

from typing import Any, Optional

from brookpay.api.schemas import bad_request, not_found, ok
from brookpay.config.constants import (
    BALANCE_CACHE_TTL_SECONDS,
    SERVICE_BALANCE_READ,
    SERVICE_ONBOARDING_CHECK,
)
from brookpay.core import registry
from brookpay.utils.timeutils import iso, utc_now
from brookpay.utils.validation import (
    is_supported_currency,
    is_valid_user_id,
    normalize_currency,
)


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------

def _query_param(query: dict, name: str, default: str = "") -> str:
    """Read a single query parameter, tolerant of list-valued inputs.

    WSGI-style query dicts sometimes map a key to a list of values; take the
    first and coerce to string, so handlers can treat parameters uniformly.
    """
    value = query.get(name, default)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value)


def _int_param(query: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer parameter, clamping out-of-range values."""
    raw = _query_param(query, name, str(default))
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return default
    return max(minimum, min(maximum, value))


def _bool_param(query: dict, name: str, default: bool = False) -> bool:
    raw = _query_param(query, name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


def parse_pagination(query: dict) -> tuple[int, int]:
    """(page, page_size) from the query, clamped to sane bounds."""
    page = _int_param(query, "page", 1, 1, 10_000)
    size = _int_param(query, "page_size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    return page, size


def paginate(items: list, page: int, page_size: int) -> dict:
    """Slice a list and describe the page for the response envelope."""
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    window = items[start:end]
    pages = (total + page_size - 1) // page_size if page_size else 1
    return {
        "items": window,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, pages),
        "has_next": end < total,
        "has_prev": start > 0,
    }


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

# Maps a domain error name to a transport response builder. Handlers catch
# nothing themselves for the happy path; this table is used by the router's
# error middleware to turn a raised domain error into a clean response.
_ERROR_RESPONSES = {
    "AccountNotFound": lambda msg: not_found("unknown_account", msg),
    "InvalidOperation": lambda msg: bad_request("invalid_operation", msg),
    "ValueError": lambda msg: bad_request("invalid_request", msg),
}


def map_error(error_name: str, message: str) -> dict:
    """Turn a domain error name into a transport response."""
    builder = _ERROR_RESPONSES.get(error_name)
    if builder is None:
        return bad_request("error", message)
    return builder(message)


def _validate_user_and_currency(user_id: str, currency: str) -> Optional[dict]:
    """Shared front-door validation. Returns an error response or None."""
    if not is_valid_user_id(user_id):
        return bad_request("invalid_user_id", f"malformed user id '{user_id}'")
    normalised = normalize_currency(currency)
    if not is_supported_currency(normalised):
        return bad_request("unsupported_currency", f"'{normalised}' not supported")
    return None


# ---------------------------------------------------------------------------
# Content negotiation
# ---------------------------------------------------------------------------

_SUPPORTED_MEDIA = ("application/json", "text/plain", "*/*")


def negotiate_media_type(accept_header: str) -> str:
    """Pick a response media type from an Accept header.

    Deliberately tiny: BrookPay's API speaks JSON, offers plain text for a
    couple of human-facing endpoints, and treats anything unrecognised as
    JSON. Quality values are ignored; the first supported match wins.
    """
    if not accept_header:
        return "application/json"
    offered = [part.split(";")[0].strip().lower() for part in accept_header.split(",")]
    for media in offered:
        if media in _SUPPORTED_MEDIA:
            return "application/json" if media == "*/*" else media
    return "application/json"


def wants_plain_text(accept_header: str) -> bool:
    return negotiate_media_type(accept_header) == "text/plain"


# ---------------------------------------------------------------------------
# Auth context
# ---------------------------------------------------------------------------

def parse_bearer(authorization: str) -> Optional[str]:
    """Extract a bearer token from an Authorization header.

    Shape check only; validation of the token is the auth middleware's job.
    Returns None when the header is absent or not a bearer scheme.
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def actor_from_context(context: Optional[dict]) -> str:
    """Best-effort actor id for logging, from the request context."""
    if not context:
        return "anonymous"
    return str(context.get("actor", "anonymous"))


# ---------------------------------------------------------------------------
# Idempotency and ETags
# ---------------------------------------------------------------------------

def idempotency_key(headers: Optional[dict]) -> Optional[str]:
    """Read the Idempotency-Key header, if the client sent one."""
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() == "idempotency-key":
            return str(value).strip() or None
    return None


def weak_etag(payload: Any) -> str:
    """A cheap weak ETag for a JSON-able body.

    Not cryptographic and not stable across Python versions; it exists only
    to let clients short-circuit unchanged reads within a process. A stable
    hash would need a canonical serialisation the read path does not provide.
    """
    digest = abs(hash(repr(payload))) & 0xFFFFFFFF
    return f'W/"{digest:08x}"'


def not_modified(etag: str) -> dict:
    """A 304 response carrying the matching ETag."""
    return {"http_status": 304, "body": None, "headers": {"ETag": etag}}


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------

def build_request_context(headers: Optional[dict]) -> dict:
    """Assemble the per-request context handlers and middleware share.

    Pure header parsing: it never touches business state. Bundles the actor
    (from a bearer token, if any), the idempotency key, and the negotiated
    media type so downstream code reads one dict instead of re-parsing
    headers repeatedly.
    """
    headers = headers or {}
    accept = ""
    authorization = ""
    for name, value in headers.items():
        lname = name.lower()
        if lname == "accept":
            accept = str(value)
        elif lname == "authorization":
            authorization = str(value)
    token = parse_bearer(authorization)
    return {
        "actor": "token" if token else "anonymous",
        "idempotency_key": idempotency_key(headers),
        "media_type": negotiate_media_type(accept),
        "authenticated": token is not None,
    }


def require_authenticated(context: dict) -> Optional[dict]:
    """Return a 401 response when the context is unauthenticated, else None."""
    if not context.get("authenticated"):
        response = bad_request("unauthenticated", "authentication required")
        response["http_status"] = 401
        return response
    return None


# ---------------------------------------------------------------------------
# Balance endpoint (resolves the balance capability through the registry)
# ---------------------------------------------------------------------------

def handle_balance_request(user_id: str, currency: str = "EUR") -> dict:
    """GET /v1/users/{user_id}/balance?currency=XXX

    Resolves the balance-read capability by its registry name and marshals
    the returned snapshot into the response. The handler depends on the
    snapshot being a mapping: it copies the snapshot into the body and lifts
    the "last_updated" field into the Last-Modified header. If the bound
    capability ever returns a bare scalar instead of the mapping, both the
    dict copy and the header lift fail here, which is why this endpoint is
    coupled to the snapshot's shape and not merely to its numeric value.
    """
    if not is_valid_user_id(user_id):
        return bad_request("invalid_user_id", f"malformed user id '{user_id}'")
    currency = normalize_currency(currency)
    if not is_supported_currency(currency):
        return bad_request("unsupported_currency", f"'{currency}' not supported")

    snapshot = registry.invoke(SERVICE_BALANCE_READ, user_id, currency=currency)
    if snapshot is None:
        return not_found("unknown_account", f"no wallet for '{user_id}'")

    body = dict(snapshot)
    body["retrieved_at"] = iso(utc_now())
    headers = {
        "Last-Modified": snapshot["last_updated"],
        "Cache-Control": f"private, max-age={BALANCE_CACHE_TTL_SECONDS}",
    }
    return ok(body, headers=headers)


def handle_onboarding_check(user_id: str) -> dict:
    """POST /v1/onboarding/check"""
    if not is_valid_user_id(user_id):
        return bad_request("invalid_user_id", f"malformed user id '{user_id}'")
    plan = registry.invoke(SERVICE_ONBOARDING_CHECK, user_id)
    return ok({
        "user_id": plan.user_id,
        "needs_wallet": plan.needs_wallet,
        "existing_status": plan.existing_status,
        "checks": plan.checks,
    })


# ---------------------------------------------------------------------------
# Statement endpoint
# ---------------------------------------------------------------------------

def handle_statement_request(user_id: str, query: Optional[dict] = None) -> dict:
    """GET /v1/users/{user_id}/statement?year=&month=&currency=

    Delegates rendering to the reporting layer. Imported lazily so the API
    package does not depend on reporting at import time; the dependency is
    real but only needed when this route is actually hit.
    """
    query = query or {}
    if not is_valid_user_id(user_id):
        return bad_request("invalid_user_id", f"malformed user id '{user_id}'")

    year = _int_param(query, "year", utc_now().year, 2000, 2100)
    month = _int_param(query, "month", utc_now().month, 1, 12)
    currency = normalize_currency(_query_param(query, "currency", "EUR"))
    if not is_supported_currency(currency):
        return bad_request("unsupported_currency", f"'{currency}' not supported")

    from brookpay.reporting.statements import build_statement_view, statement_metadata

    view = build_statement_view(user_id, year, month, display_currency=currency)
    return ok({
        "meta": statement_metadata(view),
        "balance_line": view["balance_line"],
        "transaction_count": view["transaction_count"],
    })


# ---------------------------------------------------------------------------
# Diagnostics endpoints
# ---------------------------------------------------------------------------

def handle_health() -> dict:
    """GET /healthz - liveness, no dependencies touched."""
    return ok({"status": "ok"})


def handle_readiness() -> dict:
    """GET /readyz - readiness, reflects the wiring verification.

    Imported lazily to avoid a circular import at module load: wiring imports
    services which the registry binds, and this handler only needs wiring at
    request time.
    """
    from brookpay.app.wiring import readiness

    ready, detail = readiness()
    body = {"ready": ready, "services_missing": detail.get("services_missing", [])}
    if ready:
        return ok(body)
    response = ok(body)
    response["http_status"] = 503
    return response


def handle_service_catalog() -> dict:
    """GET /v1/_internal/services - names currently bound in the registry."""
    return ok({"services": sorted(registry.registered())})


# ---------------------------------------------------------------------------
# Envelope post-processing
# ---------------------------------------------------------------------------

def with_request_id(response: dict, request_id: str) -> dict:
    """Attach a request id to a response envelope's headers, in place."""
    headers = response.setdefault("headers", {})
    headers["X-Request-Id"] = request_id
    return response


def with_cors(response: dict, origin: str = "*") -> dict:
    """Attach permissive CORS headers for the public read endpoints."""
    headers = response.setdefault("headers", {})
    headers["Access-Control-Allow-Origin"] = origin
    headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def summarise_response(response: dict) -> dict:
    """Compact log line for a handled response (no body, just shape)."""
    body = response.get("body")
    return {
        "http_status": response.get("http_status"),
        "has_body": body is not None,
        "body_keys": sorted(body) if isinstance(body, dict) else None,
        "header_count": len(response.get("headers", {})),
    }


# ---------------------------------------------------------------------------
# Route descriptors
# ---------------------------------------------------------------------------

# Machine-readable description of every route, consumed by router.py to build
# the dispatch table and by the docs generator. Keeping it here, next to the
# handlers, means a new handler and its route are added in one place.
ROUTE_DESCRIPTORS = (
    {
        "method": "GET",
        "path": "/v1/users/{user_id}/balance",
        "handler": "handle_balance_request",
        "auth": True,
        "idempotent": True,
    },
    {
        "method": "POST",
        "path": "/v1/onboarding/check",
        "handler": "handle_onboarding_check",
        "auth": True,
        "idempotent": True,
    },
    {
        "method": "GET",
        "path": "/v1/users/{user_id}/statement",
        "handler": "handle_statement_request",
        "auth": True,
        "idempotent": True,
    },
    {
        "method": "GET",
        "path": "/healthz",
        "handler": "handle_health",
        "auth": False,
        "idempotent": True,
    },
    {
        "method": "GET",
        "path": "/readyz",
        "handler": "handle_readiness",
        "auth": False,
        "idempotent": True,
    },
    {
        "method": "GET",
        "path": "/v1/_internal/services",
        "handler": "handle_service_catalog",
        "auth": True,
        "idempotent": True,
    },
)


def routes_for_method(method: str) -> list[dict]:
    """All route descriptors for an HTTP method."""
    upper = method.upper()
    return [r for r in ROUTE_DESCRIPTORS if r["method"] == upper]


def public_routes() -> list[str]:
    """Paths that do not require authentication."""
    return [r["path"] for r in ROUTE_DESCRIPTORS if not r["auth"]]


def describe_api() -> dict:
    """Compact self-description for the docs endpoint."""
    return {
        "routes": [
            {"method": r["method"], "path": r["path"], "auth": r["auth"]}
            for r in ROUTE_DESCRIPTORS
        ],
        "media_types": list(_SUPPORTED_MEDIA),
        "max_page_size": MAX_PAGE_SIZE,
    }


# ---------------------------------------------------------------------------
# Options preflight
# ---------------------------------------------------------------------------

def handle_options(path: str) -> dict:
    """Answer a CORS preflight for a known path.

    Returns the allowed methods for the path so the browser can proceed.
    Unknown paths still get a permissive answer; the real method check
    happens when the actual request arrives.
    """
    methods = sorted({
        r["method"] for r in ROUTE_DESCRIPTORS if _path_matches(r["path"], path)
    }) or ["GET", "POST"]
    response = ok(None)
    response["headers"] = {
        "Allow": ", ".join(methods),
        "Access-Control-Allow-Methods": ", ".join(methods + ["OPTIONS"]),
    }
    return response


def _path_matches(template: str, actual: str) -> bool:
    """Match a "/a/{x}/b" template against a concrete path, segment-wise."""
    t_parts = [p for p in template.split("/") if p]
    a_parts = [p for p in actual.split("/") if p]
    if len(t_parts) != len(a_parts):
        return False
    for tp, ap in zip(t_parts, a_parts):
        if tp.startswith("{") and tp.endswith("}"):
            continue
        if tp != ap:
            return False
    return True


# ---------------------------------------------------------------------------
# Payout and dispute intake (thin front doors)
# ---------------------------------------------------------------------------

def handle_payout_quote(user_id: str, query: Optional[dict] = None) -> dict:
    """GET /v1/users/{user_id}/payouts/quote?amount=&currency=&destination=

    Returns an ETA and fee estimate for a payout without initiating it. The
    heavy lifting lives in the payouts service; this handler validates the
    request shape and delegates. Imported lazily to keep the API package
    free of a service import at load time.
    """
    query = query or {}
    err = _validate_user_and_currency(user_id, _query_param(query, "currency", "EUR"))
    if err is not None:
        return err
    amount = _query_param(query, "amount", "0")
    destination = _query_param(query, "destination", "")

    from brookpay.services import payouts

    try:
        quote = payouts.quote_payout(user_id, amount, _query_param(query, "currency", "EUR"), destination)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 400
        return map_error(type(exc).__name__, str(exc))
    return ok(quote)


def handle_dispute_intake(user_id: str, body: Optional[dict] = None) -> dict:
    """POST /v1/users/{user_id}/disputes

    Records the intake of a customer dispute. Only the request envelope is
    handled here; scheme-specific processing is downstream. The reason code
    is validated against the known dispute reasons in constants.
    """
    body = body or {}
    if not is_valid_user_id(user_id):
        return bad_request("invalid_user_id", f"malformed user id '{user_id}'")

    from brookpay.config.constants import DISPUTE_WINDOW_DAYS

    reason = str(body.get("reason", "")).strip()
    if reason not in DISPUTE_WINDOW_DAYS:
        return bad_request("unknown_reason", f"unrecognised dispute reason '{reason}'")

    return ok({
        "user_id": user_id,
        "reason": reason,
        "window_days": DISPUTE_WINDOW_DAYS[reason],
        "status": "received",
    })


# ---------------------------------------------------------------------------
# Method dispatch
# ---------------------------------------------------------------------------

_HANDLER_TABLE = {
    "handle_balance_request": handle_balance_request,
    "handle_onboarding_check": handle_onboarding_check,
    "handle_statement_request": handle_statement_request,
    "handle_payout_quote": handle_payout_quote,
    "handle_dispute_intake": handle_dispute_intake,
    "handle_health": handle_health,
    "handle_readiness": handle_readiness,
    "handle_service_catalog": handle_service_catalog,
}


def resolve_handler(name: str):
    """Look up a handler callable by the name used in ROUTE_DESCRIPTORS."""
    return _HANDLER_TABLE.get(name)


def handler_names() -> list[str]:
    return sorted(_HANDLER_TABLE)
