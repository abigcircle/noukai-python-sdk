"""httpx-based HTTP transport. Owns: auth headers, retries, error mapping,
log hook invocation. Not aware of any specific endpoint.

Both ``AsyncTransport`` and ``SyncTransport`` share helpers from
``_transport_shared``. The async transport uses ``httpx.AsyncClient``
and ``asyncio.sleep``; the sync transport uses ``httpx.Client`` and
``time.sleep``. Neither bridges the other via ``asyncio.run()``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Iterator
from typing import Any

import httpx
from pydantic import BaseModel

from ._constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    HEADER_API_VERSION,
    HEADER_REQUEST_ID,
    HEADER_USER_AGENT,
)
from ._errors import APIConnectionError, APITimeoutError
from ._transport_shared import (
    _RETRYABLE_STATUS,
    Response,
    _backoff_seconds,
    _default_headers,
    _map_status_to_exception,
    _parse_error_body,
    _safe_json,
    _safe_parse_json,
)

# Re-export Response so existing code importing it from _transport still works.
__all__ = ["AsyncTransport", "SyncTransport", "Response"]


# ---------------------------------------------------------------------------
# Reserved headers -- never overridable via per-request ``extra_headers``
# ---------------------------------------------------------------------------
#
# Headers managed by the transport itself. A caller passing one of these via
# ``extra_headers`` would silently overwrite auth, version pinning, or
# request-id provenance -- :func:`_apply_extra_headers` strips them instead.
# The replay subsystem is the main caller of ``extra_headers`` (for
# ``X-Session-Id`` / ``X-Noukai-Replay``); a hardened allowlist here means a
# misconfigured ``trace_scope`` cannot rotate the bearer token by accident.
#
# Compared case-insensitively.
_RESERVED_HEADERS_LOWER = frozenset(
    {
        "authorization",
        HEADER_API_VERSION.lower(),
        HEADER_USER_AGENT.lower(),
        HEADER_REQUEST_ID.lower(),
        "content-type",
        "cookie",
    }
)


def _apply_extra_headers(
    target: dict[str, str],
    extra_headers: dict[str, str] | None,
) -> None:
    """Merge ``extra_headers`` into ``target``, silently dropping reserved keys.

    Exported as a module-private helper for unit tests; not part of the
    public API.
    """
    if not extra_headers:
        return
    for k, v in extra_headers.items():
        if k.lower() in _RESERVED_HEADERS_LOWER:
            continue
        target[k] = v


class AsyncTransport:
    """Async HTTP transport wrapping httpx. Single instance per AsyncNoukai client."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        log_handler: Callable[[dict[str, Any]], None] | None = None,
        log_payloads: bool = False,
        default_session_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout = timeout
        self._max_retries = max_retries
        self._log_handler = log_handler
        self._log_payloads = log_payloads
        self._default_session_id = default_session_id
        # Phase 3 lesson: store headers separately and pass explicitly on every
        # request so that swapping _httpx_client in tests does not lose them.
        self._headers = _default_headers(api_key)
        self._httpx_client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers=self._headers,
        )

    def _log(self, event: dict[str, Any]) -> None:
        if self._log_handler is not None:
            self._log_handler(event)

    def _prepare_body(
        self, json: BaseModel | dict[str, Any] | None
    ) -> dict[str, Any] | list[Any] | None:
        if isinstance(json, BaseModel):
            return json.model_dump(by_alias=True, exclude_none=True)
        return json

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: BaseModel | dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        """Send a non-streaming HTTP request.

        Retry policy: 5xx (and 408) are retried only when ``idempotent`` is
        True. ``idempotent`` defaults to True for GET/HEAD/OPTIONS/PUT/DELETE
        and False for POST/PATCH. Callers that know a POST is safe to retry
        (e.g. queue-backed ``/jobs`` submission with server-side idempotency
        keys) may pass ``idempotent=True`` explicitly. Connection errors
        before bytes are sent are always safe to retry once.

        Args:
            extra_headers: Optional per-request headers merged on top of
                ``self._headers``. Built as a new dict each call — does not
                mutate the shared ``_headers``.
        """
        body = self._prepare_body(json)
        effective_timeout = timeout if timeout is not None else self._timeout
        if idempotent is None:
            idempotent = method.upper() not in {"POST", "PATCH"}

        # Build per-request headers (never mutate self._headers).
        request_headers = dict(self._headers)
        _apply_extra_headers(request_headers, extra_headers)

        for attempt in range(self._max_retries + 1):
            self._log(
                {
                    "phase": "request",
                    "method": method,
                    "path": path,
                    "attempt": attempt,
                    **({"request_body": body} if self._log_payloads else {}),
                }
            )

            try:
                resp = await self._httpx_client.request(
                    method,
                    path.lstrip("/"),
                    json=body,
                    params=params,
                    timeout=effective_timeout,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                raise APITimeoutError(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise APIConnectionError(str(exc)) from exc

            request_id = resp.headers.get(HEADER_REQUEST_ID)

            self._log(
                {
                    "phase": "response",
                    "method": method,
                    "path": path,
                    "status_code": resp.status_code,
                    "request_id": request_id,
                    "attempt": attempt,
                    **({"response_body": _safe_json(resp)} if self._log_payloads else {}),
                }
            )

            if 200 <= resp.status_code < 300:
                return Response(
                    status_code=resp.status_code,
                    body=_safe_json(resp),
                    request_id=request_id,
                    headers=dict(resp.headers),
                )

            # Retryable status — sleep then loop (only for idempotent methods)
            if idempotent and resp.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                await asyncio.sleep(_backoff_seconds(attempt))
                continue

            # Non-retryable or retries exhausted — raise typed exception
            body_data = _safe_json(resp)
            code, message = _parse_error_body(body_data)
            retry_after: float | None = None
            if resp.status_code == 429:
                raw_ra = resp.headers.get("Retry-After", "")
                try:
                    retry_after = float(raw_ra) if raw_ra else None
                except ValueError:
                    retry_after = None
            www_authenticate: str | None = None
            if resp.status_code == 401:
                www_authenticate = resp.headers.get("WWW-Authenticate")

            raise _map_status_to_exception(
                resp.status_code,
                code,
                message,
                request_id=request_id,
                response_body=body_data,
                retry_after=retry_after,
                www_authenticate=www_authenticate,
            )

        # Defensive: loop body always returns or raises before exhausting
        raise APIConnectionError("Unknown transport failure")  # pragma: no cover

    async def stream(
        self,
        method: str,
        path: str,
        *,
        json: BaseModel | dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream raw bytes from a streaming endpoint.

        SSE parsing (event splitting, JSON decoding) is handled upstream.
        Streaming requests are not retried on 5xx.

        Args:
            extra_headers: Optional per-request headers merged on top of
                ``self._headers``. Built as a new dict — does not mutate
                the shared ``_headers``.
        """
        body = self._prepare_body(json)
        effective_timeout = timeout if timeout is not None else self._timeout

        # Build per-request headers (never mutate self._headers).
        request_headers = dict(self._headers)
        _apply_extra_headers(request_headers, extra_headers)

        self._log(
            {
                "phase": "request",
                "method": method,
                "path": path,
                "attempt": 0,
                "streaming": True,
            }
        )

        async with self._httpx_client.stream(
            method,
            path.lstrip("/"),
            json=body,
            params=params,
            timeout=effective_timeout,
            headers=request_headers,
        ) as resp:
            request_id = resp.headers.get(HEADER_REQUEST_ID)
            self._log(
                {
                    "phase": "response",
                    "method": method,
                    "path": path,
                    "status_code": resp.status_code,
                    "request_id": request_id,
                    "attempt": 0,
                    "streaming": True,
                }
            )
            if resp.status_code >= 400:
                raw = await resp.aread()
                body_data = _safe_parse_json(raw)
                code, message = _parse_error_body(body_data)
                www_authenticate: str | None = None
                if resp.status_code == 401:
                    www_authenticate = resp.headers.get("WWW-Authenticate")
                raise _map_status_to_exception(
                    resp.status_code,
                    code,
                    message,
                    request_id=request_id,
                    response_body=body_data,
                    www_authenticate=www_authenticate,
                )
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool."""
        await self._httpx_client.aclose()


class SyncTransport:
    """Synchronous HTTP transport wrapping httpx.Client.

    Mirror of ``AsyncTransport`` for use by the sync ``Noukai`` client.
    Uses ``httpx.Client`` (blocking I/O) and ``time.sleep`` for retry backoff.
    Does NOT use ``asyncio.run()`` — it is safe to call in any context,
    including inside a running event loop (e.g. Jupyter notebooks).

    Phase 3 lesson applied: ``_headers`` is stored separately and passed
    explicitly on every ``request()`` / ``stream()`` call so that test code
    that swaps ``_httpx_client`` after construction does not lose the auth
    headers.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        log_handler: Callable[[dict[str, Any]], None] | None = None,
        log_payloads: bool = False,
        default_session_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout = timeout
        self._max_retries = max_retries
        self._log_handler = log_handler
        self._log_payloads = log_payloads
        self._default_session_id = default_session_id
        # Store headers separately (Phase 3 lesson) — passed explicitly on
        # every request so swapping _httpx_client in tests doesn't lose them.
        self._headers = _default_headers(api_key)
        self._httpx_client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers=self._headers,
        )

    def _log(self, event: dict[str, Any]) -> None:
        if self._log_handler is not None:
            self._log_handler(event)

    def _prepare_body(
        self, json: BaseModel | dict[str, Any] | None
    ) -> dict[str, Any] | list[Any] | None:
        if isinstance(json, BaseModel):
            return json.model_dump(by_alias=True, exclude_none=True)
        return json

    def request(
        self,
        method: str,
        path: str,
        *,
        json: BaseModel | dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        """Send a blocking HTTP request.

        Retry policy: 5xx (and 408) are retried only when ``idempotent`` is
        True. Defaults to True for GET/HEAD/OPTIONS/PUT/DELETE and False for
        POST/PATCH. See ``AsyncTransport.request`` for details.

        Args:
            extra_headers: Optional per-request headers merged on top of
                ``self._headers``. Built as a new dict — does not mutate
                the shared ``_headers``.
        """
        body = self._prepare_body(json)
        effective_timeout = timeout if timeout is not None else self._timeout
        if idempotent is None:
            idempotent = method.upper() not in {"POST", "PATCH"}

        # Build per-request headers (never mutate self._headers).
        request_headers = dict(self._headers)
        _apply_extra_headers(request_headers, extra_headers)

        for attempt in range(self._max_retries + 1):
            self._log(
                {
                    "phase": "request",
                    "method": method,
                    "path": path,
                    "attempt": attempt,
                    **({"request_body": body} if self._log_payloads else {}),
                }
            )

            try:
                resp = self._httpx_client.request(
                    method,
                    path.lstrip("/"),
                    json=body,
                    params=params,
                    timeout=effective_timeout,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                raise APITimeoutError(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise APIConnectionError(str(exc)) from exc

            request_id = resp.headers.get(HEADER_REQUEST_ID)

            self._log(
                {
                    "phase": "response",
                    "method": method,
                    "path": path,
                    "status_code": resp.status_code,
                    "request_id": request_id,
                    "attempt": attempt,
                    **({"response_body": _safe_json(resp)} if self._log_payloads else {}),
                }
            )

            if 200 <= resp.status_code < 300:
                return Response(
                    status_code=resp.status_code,
                    body=_safe_json(resp),
                    request_id=request_id,
                    headers=dict(resp.headers),
                )

            # Retryable status — sleep then loop (only for idempotent methods)
            if idempotent and resp.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                time.sleep(_backoff_seconds(attempt))
                continue

            # Non-retryable or retries exhausted — raise typed exception
            body_data = _safe_json(resp)
            code, message = _parse_error_body(body_data)
            retry_after: float | None = None
            if resp.status_code == 429:
                raw_ra = resp.headers.get("Retry-After", "")
                try:
                    retry_after = float(raw_ra) if raw_ra else None
                except ValueError:
                    retry_after = None
            www_authenticate: str | None = None
            if resp.status_code == 401:
                www_authenticate = resp.headers.get("WWW-Authenticate")

            raise _map_status_to_exception(
                resp.status_code,
                code,
                message,
                request_id=request_id,
                response_body=body_data,
                retry_after=retry_after,
                www_authenticate=www_authenticate,
            )

        # Defensive: loop body always returns or raises before exhausting
        raise APIConnectionError("Unknown transport failure")  # pragma: no cover

    def stream(
        self,
        method: str,
        path: str,
        *,
        json: BaseModel | dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Iterator[bytes]:
        """Sync generator streaming raw bytes from a streaming endpoint.

        Streaming requests are not retried on 5xx.

        Args:
            extra_headers: Optional per-request headers merged on top of
                ``self._headers``. Built as a new dict — does not mutate
                the shared ``_headers``.
        """
        body = self._prepare_body(json)
        effective_timeout = timeout if timeout is not None else self._timeout

        # Build per-request headers (never mutate self._headers).
        request_headers = dict(self._headers)
        _apply_extra_headers(request_headers, extra_headers)

        self._log(
            {
                "phase": "request",
                "method": method,
                "path": path,
                "attempt": 0,
                "streaming": True,
            }
        )

        with self._httpx_client.stream(
            method,
            path.lstrip("/"),
            json=body,
            params=params,
            timeout=effective_timeout,
            headers=request_headers,
        ) as resp:
            request_id = resp.headers.get(HEADER_REQUEST_ID)
            self._log(
                {
                    "phase": "response",
                    "method": method,
                    "path": path,
                    "status_code": resp.status_code,
                    "request_id": request_id,
                    "attempt": 0,
                    "streaming": True,
                }
            )
            if resp.status_code >= 400:
                raw = resp.read()
                body_data = _safe_parse_json(raw)
                code, message = _parse_error_body(body_data)
                www_authenticate: str | None = None
                if resp.status_code == 401:
                    www_authenticate = resp.headers.get("WWW-Authenticate")
                raise _map_status_to_exception(
                    resp.status_code,
                    code,
                    message,
                    request_id=request_id,
                    response_body=body_data,
                    www_authenticate=www_authenticate,
                )
            yield from resp.iter_bytes()

    def close(self) -> None:
        """Close the underlying httpx connection pool."""
        self._httpx_client.close()
