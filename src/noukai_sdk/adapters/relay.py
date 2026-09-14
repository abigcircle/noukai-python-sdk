"""Flow relay adapters — serve a flow to a browser (design 20260903-SDK-agent-relay, PR-1).

A *relay* is a keyholder proxy. It holds the ``nk_`` bearer, receives a keyless
POST from a browser (or another server), bounds abuse, runs an app-supplied
``authorize`` hook, then forwards the request **verbatim** to the flow's
``/execute`` endpoint and relays the upstream ``(status, body)`` back unchanged.
The browser drives the tool-calling loop and executes tools; the relay never
interprets the business payload.

Two framework variants, mirroring ``adapters/fastapi.py`` + ``adapters/flask.py``:

  - :func:`mount_flow_relay` — FastAPI / Starlette (async client)
  - :func:`flow_relay_blueprint` — Flask (sync client)

Framework imports are lazy so the SDK does not hard-depend on Starlette/Flask.

Security invariants (design § "What must NOT leak into the SDK"):

  - **App authorization stays in the app** — the ``authorize`` hook only; a
    role check is never baked into the SDK. Raise from ``authorize`` to reject;
    the exception propagates to the framework unchanged (FastAPI/Flask map it).
  - **Bound values (256 KiB / 40) are adapter config**, never SDK-wide constants.
  - The relay **never logs** the key or the body. Note this invariant is
    honored by the relay code itself, not enforced against the client's log
    config: constructing the keyholder ``client`` with ``log_payloads=True``
    **will** send the forwarded request/response bodies to that client's log
    handler (the ``nk_`` key is still never logged). Keep ``log_payloads`` off
    on a relay client.
  - The relay **does not raise typed errors** in place of relaying status — a
    non-raising transport forward (``raise_for_status=False``) passes upstream
    4xx/5xx through verbatim.

"Verbatim" forwarding is a **JSON value round-trip** — the relay parses the body
(to bound the ``messages`` / ``toolCallMessages`` counts) and re-serializes it,
so JSON values and object key order are preserved but it is not a byte-identical
pipe (insignificant whitespace and duplicate keys are not preserved).

Exports are subpath-only::

    from noukai_sdk.adapters.relay import mount_flow_relay, flow_relay_blueprint
"""

from __future__ import annotations

import json as _json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .._paths import VersionSegment, flow_execute_path

if TYPE_CHECKING:
    from .._client import AsyncNoukai, Noukai

__all__ = [
    "RelayBounds",
    "mount_flow_relay",
    "flow_relay_blueprint",
    "DEFAULT_RELAY_PATH",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_MESSAGES",
]

DEFAULT_RELAY_PATH = "/agent/execute"
DEFAULT_MAX_BODY_BYTES = 262_144  # 256 KiB
DEFAULT_MAX_MESSAGES = 40


@dataclass(frozen=True)
class RelayBounds:
    """Abuse bounds enforced by the relay before forwarding.

    Adapter config — never SDK-wide constants (design non-goal). ``max_body_bytes``
    is checked on the raw request bytes *before* JSON parse; ``max_messages``
    caps each of the ``messages`` / ``toolCallMessages`` arrays independently.
    """

    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_messages: int = DEFAULT_MAX_MESSAGES


class _RelayRejection(Exception):
    """Internal: a bounds/parse rejection carrying the HTTP ``(status, detail)``
    the relay should return. Never carries the key or the raw body."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _normalize_version(version: str | int) -> VersionSegment:
    """Coerce the relay's ``version`` into the ``"draft" | int`` form the path
    helpers expect. ``"production"`` is rejected (mirrors ``Flow._path_version``)."""
    if isinstance(version, int):
        return version
    if version == "draft":
        return "draft"
    raise ValueError(f"flow relay version must be 'draft' or a positive int, got {version!r}")


def _bound_and_parse(raw: bytes, bounds: RelayBounds) -> dict[str, Any]:
    """Bound raw bytes *before* parse, parse JSON, then bound message counts.

    Raises :class:`_RelayRejection` with the ``(status, detail)`` to return on
    any violation. Order matters: the byte bound runs before we ever decode
    attacker-controlled bytes.
    """
    if len(raw) > bounds.max_body_bytes:
        raise _RelayRejection(413, "BODY_TOO_LARGE")
    try:
        payload = _json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError guards against deeply-nested JSON within the byte cap.
        raise _RelayRejection(400, "INVALID_JSON") from None
    if not isinstance(payload, dict):
        raise _RelayRejection(400, "INVALID_JSON")
    for key in ("messages", "toolCallMessages"):
        val = payload.get(key)
        if isinstance(val, list) and len(val) > bounds.max_messages:
            raise _RelayRejection(413, "TOO_MANY_MESSAGES")
    return payload


def _normalize_upstream(status: int, body: Any) -> tuple[int, dict[str, Any] | list[Any]]:
    """Map an upstream ``(status, body)`` into a JSON-serializable ``(status, body)``.

    A JSON object/array is relayed verbatim. Anything else (``None`` from a
    non-JSON upstream in the Python transport, or a bare string/number) becomes
    a generic ``{"detail": "UPSTREAM_NON_JSON"}`` at the same upstream status.
    """
    if isinstance(body, (dict, list)):
        return status, body
    return status, {"detail": "UPSTREAM_NON_JSON"}


async def _read_capped_async(request: Any, max_bytes: int) -> bytes:
    """Stream the request body, aborting the read the moment the running byte
    total exceeds ``max_bytes``. Prevents buffering an unbounded untrusted body
    into memory before the size bound is enforced (the Starlette ``request.body()``
    path buffers everything first)."""
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise _RelayRejection(413, "BODY_TOO_LARGE")
            chunks.append(chunk)
    except _RelayRejection:
        raise
    except Exception:
        # A client that disconnects or sends a malformed chunked body raises
        # mid-stream (e.g. Starlette ClientDisconnect). Return 400 like the TS
        # relay rather than letting it surface as an unhandled framework 500.
        raise _RelayRejection(400, "INVALID_JSON") from None
    return b"".join(chunks)


def _read_capped_sync(stream: Any, max_bytes: int) -> bytes:
    """Sync mirror of :func:`_read_capped_async` — reads a WSGI input stream in
    bounded chunks, aborting once the running total exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _RelayRejection(413, "BODY_TOO_LARGE")
            chunks.append(chunk)
    except _RelayRejection:
        raise
    except Exception:
        # Mirror the async reader: a mid-read stream error becomes a 400, not a
        # framework 500.
        raise _RelayRejection(400, "INVALID_JSON") from None
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# FastAPI / Starlette (async)
# ---------------------------------------------------------------------------


def mount_flow_relay(
    app: Any,
    *,
    client: AsyncNoukai,
    org: str,
    project: str,
    slug: str,
    path: str = DEFAULT_RELAY_PATH,
    authorize: Callable[[Any], Awaitable[None]],
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    version: str | int = "draft",
) -> None:
    """Mount a ``POST {path}`` flow relay on a FastAPI / Starlette ``app``.

    The route: reads the raw body → bounds it → ``await authorize(request)`` →
    forwards verbatim to ``/seq/{org}/{project}/{slug}/execute`` with the
    ``nk_`` bearer (``raise_for_status=False``) → relays the upstream
    ``(status, body)`` verbatim.

    Args:
        app: A FastAPI / Starlette application.
        client: An ``AsyncNoukai`` client — its transport holds the ``nk_``
            bearer that is injected on the forwarded call.
        org, project, slug: The single flow this relay is pinned to. The caller
            cannot redirect the relay to another flow.
        path: Route path to mount (default ``/agent/execute``).
        authorize: ``async (request) -> None`` awaited before forwarding. Raise
            to reject (e.g. ``raise HTTPException(status_code=403)``); the
            exception propagates to the framework unchanged. App authorization
            (e.g. nouko's maker-role) belongs here, never in the SDK. Returning
            (any value) is treated as ALLOW — you must raise to deny.
        max_body_bytes: Max raw request size, checked before parse (default
            256 KiB). Over → ``413 {"detail": "BODY_TOO_LARGE"}``.
        max_messages: Max entries in each of ``messages`` / ``toolCallMessages``
            (default 40). Over → ``413 {"detail": "TOO_MANY_MESSAGES"}``.
        version: ``"draft"`` (default) or a published int version.

    Raises:
        ImportError: if Starlette/FastAPI is not installed.
        ValueError: if ``version`` is not ``"draft"`` or an int.
    """
    try:
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as exc:
        raise ImportError(
            "mount_flow_relay requires Starlette/FastAPI. "
            "Install with `pip install starlette` or `pip install fastapi`."
        ) from exc

    from .._errors import APIConnectionError

    bounds = RelayBounds(max_body_bytes=max_body_bytes, max_messages=max_messages)
    seg = _normalize_version(version)

    async def _relay(request: Request) -> JSONResponse:
        try:
            raw = await _read_capped_async(request, bounds.max_body_bytes)
            payload = _bound_and_parse(raw, bounds)
        except _RelayRejection as rej:
            return JSONResponse({"detail": rej.detail}, status_code=rej.status_code)

        # App authorization stays in the app: raise to reject; propagate.
        await authorize(request)

        url = flow_execute_path(org, project, slug, seg)
        try:
            resp = await client._transport.request(
                "POST", url, json=payload, raise_for_status=False
            )
        except APIConnectionError:
            # No upstream status to relay (connection/timeout) — signal 502.
            return JSONResponse({"detail": "UPSTREAM_UNAVAILABLE"}, status_code=502)
        status, body = _normalize_upstream(resp.status_code, resp.body)
        return JSONResponse(body, status_code=status)

    app.add_route(path, _relay, methods=["POST"])


# ---------------------------------------------------------------------------
# Flask (sync)
# ---------------------------------------------------------------------------


def flow_relay_blueprint(
    *,
    client: Noukai,
    org: str,
    project: str,
    slug: str,
    path: str = DEFAULT_RELAY_PATH,
    authorize: Callable[[Any], None],
    bounds: RelayBounds | None = None,
    version: str | int = "draft",
) -> Any:
    """Return a Flask ``Blueprint`` exposing ``POST {path}`` as a flow relay.

    Register with ``app.register_blueprint(flow_relay_blueprint(...))``. Sync
    mirror of :func:`mount_flow_relay`.

    Args:
        client: A ``Noukai`` (sync) client — holds the ``nk_`` bearer injected
            on the forwarded call.
        org, project, slug: The single flow this relay is pinned to.
        path: Route path (default ``/agent/execute``).
        authorize: ``(request) -> None`` called before forwarding. Raise a
            werkzeug ``HTTPException`` (e.g. ``abort(403)``) to reject. Returning
            (any value) is treated as ALLOW — you must raise to deny.
        bounds: Abuse bounds (default :class:`RelayBounds` = 256 KiB / 40).
        version: ``"draft"`` (default) or a published int version.

    Raises:
        ImportError: if Flask is not installed.
        ValueError: if ``version`` is not ``"draft"`` or an int.
    """
    try:
        from flask import Blueprint, jsonify, request
    except ImportError as exc:
        raise ImportError(
            "flow_relay_blueprint requires Flask. Install with `pip install flask`."
        ) from exc

    from .._errors import APIConnectionError

    resolved_bounds = bounds if bounds is not None else RelayBounds()
    seg = _normalize_version(version)
    # Unique blueprint name so one app can mount more than one relay — Flask
    # raises on a duplicate blueprint name. Mirrors the FastAPI side, where
    # distinct route paths never collide.
    _safe = "".join(c if c.isalnum() else "_" for c in f"{org}_{project}_{slug}_{path}")
    blueprint = Blueprint(f"noukai_flow_relay_{_safe}", __name__)

    @blueprint.post(path)
    def _relay() -> Any:
        try:
            raw = _read_capped_sync(request.stream, resolved_bounds.max_body_bytes)
            payload = _bound_and_parse(raw, resolved_bounds)
        except _RelayRejection as rej:
            return jsonify({"detail": rej.detail}), rej.status_code

        # App authorization stays in the app: raise to reject; propagate.
        authorize(request)

        url = flow_execute_path(org, project, slug, seg)
        try:
            resp = client._transport.request("POST", url, json=payload, raise_for_status=False)
        except APIConnectionError:
            return jsonify({"detail": "UPSTREAM_UNAVAILABLE"}), 502
        status, body = _normalize_upstream(resp.status_code, resp.body)
        return jsonify(body), status

    return blueprint
