"""FastAPI / Starlette middleware for the @noukai.trace decorator.

Imports Starlette lazily so the SDK does not require Starlette/FastAPI as a
hard dependency. Raises ImportError if Starlette is missing.

Design decision: direct ASGI middleware (NOT BaseHTTPMiddleware).
BaseHTTPMiddleware buffers the full response body, which breaks streaming
responses (SSE, large downloads). Direct ASGI middleware lets us intercept
the ``http.response.start`` ASGI message to inject response headers without
touching the body at all. Slightly more boilerplate, but correctness-preserving.

Usage:
    from noukai_sdk import AsyncNoukai
    from noukai_sdk.adapters.fastapi import NoukaiTraceMiddleware

    noukai = AsyncNoukai(api_key=..., env="dev")
    app.add_middleware(NoukaiTraceMiddleware, client=noukai)

CORS / preflight handling is out of scope — configure CORS in your own app.
Async Flask (Quart) users should use this adapter, which works for any ASGI app.
Multiple X-Noukai-Replay headers: the first value is used; extras are ignored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._constants import (
    HEADER_REPLAY,
    HEADER_RESPONSE_SESSION,
)
from .._errors import (
    ReplayError,
    ReplayForbiddenError,
    ReplayInvalidSessionError,
    ReplayNoSnapshotsError,
    ReplaySessionExpiredError,
    ReplaySessionNotFoundError,
)

if TYPE_CHECKING:
    from .._client import AsyncNoukai


class NoukaiTraceMiddleware:
    """Direct ASGI middleware — used as ``app.add_middleware(NoukaiTraceMiddleware, client=...)``.

    Starlette instantiates the middleware with the downstream ``app`` plus any
    extra kwargs passed at registration time. The ``client`` kwarg is required.

    Why not BaseHTTPMiddleware?
    BaseHTTPMiddleware buffers the entire response body before the middleware
    callback can run, which destroys streaming. Direct ASGI middleware intercepts
    the ``http.response.start`` message to inject headers before the body is sent.

    Why pass ``client`` not ``transport`` directly?
    User code constructs ``AsyncNoukai`` once; passing the client lets the
    middleware read ``client._transport`` and the log_handler (used for
    scope_open/scope_close events from Phase 5).
    """

    def __init__(self, app: Any, *, client: AsyncNoukai) -> None:
        self.app = app
        self.client = client

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # Only act on HTTP requests; pass WebSocket / lifespan through unchanged.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Starlette's Request wraps the scope dict — lazy import keeps starlette
        # out of the hard-dependency list.
        try:
            from starlette.requests import Request
            from starlette.responses import JSONResponse
        except ImportError as exc:
            raise ImportError(
                "NoukaiTraceMiddleware requires Starlette/FastAPI. "
                "Install with `pip install starlette` or `pip install fastapi`."
            ) from exc

        from .._trace_scope import trace_scope

        request = Request(scope, receive=receive)
        # Header name lookup is case-insensitive in Starlette's Headers.
        replay_sid: str | None = request.headers.get(HEADER_REPLAY)

        # Mutable cell so send_wrapper can read the session_id set during scope entry.
        captured_session_id: dict[str, str | None] = {"sid": None}

        async def send_wrapper(message: dict[str, Any]) -> None:
            """Intercept the ASGI response-start message to inject the session header."""
            if message["type"] == "http.response.start" and captured_session_id["sid"]:
                headers = list(message.get("headers", []))
                headers.append(
                    (
                        HEADER_RESPONSE_SESSION.lower().encode(),
                        captured_session_id["sid"].encode(),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            async with trace_scope(
                replay_session_id=replay_sid,
                transport=self.client._transport,
            ) as scope_state:
                captured_session_id["sid"] = scope_state.session_id
                await self.app(scope, receive, send_wrapper)

        except ReplayForbiddenError as exc:
            response = JSONResponse(
                {"error": "replay_forbidden", "message": exc.message},
                status_code=403,
            )
            await response(scope, receive, send)

        except ReplaySessionNotFoundError as exc:
            response = JSONResponse(
                {"error": "replay_session_not_found", "message": exc.message},
                status_code=404,
            )
            await response(scope, receive, send)

        except ReplaySessionExpiredError as exc:
            response = JSONResponse(
                {"error": "replay_session_expired", "message": exc.message},
                status_code=410,
            )
            await response(scope, receive, send)

        except ReplayInvalidSessionError as exc:
            response = JSONResponse(
                {"error": "replay_invalid_session", "message": exc.message},
                status_code=400,
            )
            await response(scope, receive, send)

        except ReplayNoSnapshotsError as exc:
            response = JSONResponse(
                {"error": "replay_no_snapshots", "message": exc.message},
                status_code=409,
            )
            await response(scope, receive, send)

        except ReplayError as exc:
            # Catch-all for ReplayMissError / ReplayLeftoverError surfaced
            # during the handler (not at scope entry — those are specific above).
            response = JSONResponse(
                {"error": "replay_error", "message": exc.message},
                status_code=500,
            )
            await response(scope, receive, send)
