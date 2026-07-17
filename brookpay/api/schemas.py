"""Response envelopes for the HTTP layer.

Handlers stay framework agnostic and return plain dicts shaped by these
helpers; the ASGI adapter serializes them.
"""

from __future__ import annotations

from typing import Any, Optional


def ok(body: Any, headers: Optional[dict] = None, http_status: int = 200) -> dict:
    return {
        "http_status": http_status,
        "body": body,
        "headers": headers or {},
    }


def error(code: str, message: str, http_status: int) -> dict:
    return {
        "http_status": http_status,
        "body": {"error": {"code": code, "message": message}},
        "headers": {},
    }


def not_found(code: str, message: str) -> dict:
    return error(code, message, 404)


def bad_request(code: str, message: str) -> dict:
    return error(code, message, 400)
