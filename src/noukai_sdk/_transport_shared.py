"""Shared pure helpers for AsyncTransport and SyncTransport.

Both transports use identical header construction, user-agent building,
error mapping, error body parsing, retry backoff, and the Response dataclass.
Extracting them here avoids duplication and ensures parity.
"""

from __future__ import annotations

import json as _json_module
import logging
import platform
import random
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from . import _version
from ._constants import (
    API_VERSION,
    HEADER_API_VERSION,
    HEADER_USER_AGENT,
)
from ._errors import (
    APIConnectionError,  # noqa: F401 — re-exported for transport modules
    APITimeoutError,  # noqa: F401 — re-exported for transport modules
    AuthenticationError,
    FlowExecutionError,
    FlowNotFoundError,
    InsufficientCreditsError,
    NoukaiError,
    PermissionDeniedError,
    RateLimitError,
)

_logger = logging.getLogger("noukai_sdk.transport")


@dataclass(frozen=True)
class Response:
    status_code: int
    body: dict[str, Any] | list[Any] | None
    request_id: str | None
    headers: dict[str, str]


# 408 Request Timeout is conventionally retryable; 5xx server-side errors are.
# 429 is handled separately (Retry-After honored, not bulk-retried here).
_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 500, 502, 503, 504})


def _backoff_seconds(attempt: int, *, jitter: bool = True) -> float:
    """Exponential backoff with optional ±25% jitter.

    Base sequence:
        attempt=0 → 1.0 (first retry)
        attempt=1 → 4.0
        attempt=2 → 16.0

    Jitter mitigates thundering-herd when multiple clients retry in lockstep
    after a shared 503. Disabled in tests via ``jitter=False`` for determinism.
    """
    base = 1.0 if attempt == 0 else float(4**attempt)
    if not jitter:
        return base
    return base * random.uniform(0.75, 1.25)


def _build_user_agent() -> str:
    py = ".".join(map(str, sys.version_info[:3]))
    return (
        f"noukai-python/{_version.__version__} "
        f"(httpx/{httpx.__version__}; python/{py}; {platform.system()})"
    )


def _default_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        HEADER_API_VERSION: API_VERSION,
        HEADER_USER_AGENT: _build_user_agent(),
        "Accept": "application/json",
    }


def _parse_error_body(body: Any) -> tuple[str | None, str]:
    """Extract (code, message) from a FastAPI-shaped error body.

    Server convention: {"detail": {"code": "...", "message": "..."}}
    Fallback: {"detail": "string"}.
    """
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("code"), detail.get("message", "")
        if isinstance(detail, str):
            return None, detail
    return None, str(body)


def _map_status_to_exception(
    status: int,
    code: str | None,
    message: str,
    *,
    request_id: str | None,
    response_body: Any,
    retry_after: float | None = None,
    www_authenticate: str | None = None,
) -> NoukaiError:
    # message is passed as first positional arg; do NOT include it in kwargs
    kwargs: dict[str, Any] = dict(
        status_code=status,
        code=code,
        request_id=request_id,
        response_body=response_body,
    )
    if status == 401:
        return AuthenticationError(message, **kwargs, www_authenticate=www_authenticate)
    if status == 402:
        return InsufficientCreditsError(message, **kwargs)
    if status == 403:
        return PermissionDeniedError(message, **kwargs)
    if status == 404:
        return FlowNotFoundError(message, **kwargs)
    if status == 429:
        return RateLimitError(message, **kwargs, retry_after=retry_after)
    return FlowExecutionError(message, **kwargs)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception as exc:
        _logger.debug("[0x000995] Failed to decode JSON response body: %s", exc)
        return None


def _safe_parse_json(raw: bytes) -> Any:
    try:
        return _json_module.loads(raw)
    except Exception as exc:
        _logger.debug("[0x000994] Failed to parse JSON bytes: %s", exc)
        return None
