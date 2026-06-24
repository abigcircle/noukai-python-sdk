"""Fetches GET /seq/sessions/{session_id} and maps backend errors to typed
replay errors.

Used at trace_scope entry in REPLAY mode."""

from __future__ import annotations

from typing import Any

from .._errors import (
    NoukaiError,
    PermissionDeniedError,
    ReplayForbiddenError,
    ReplayInvalidSessionError,
    ReplaySessionExpiredError,
    ReplaySessionNotFoundError,
)
from .._models.session import SessionResponse
from .._paths import session_path


async def fetch_session_async(
    *,
    transport: Any,  # AsyncTransport at runtime; weakly typed to avoid circular import
    session_id: str,
) -> SessionResponse:
    """Fetch a session from the backend in async mode.

    Raises:
        ReplayInvalidSessionError: 400.
        ReplayForbiddenError: 403.
        ReplaySessionNotFoundError: 404.
        ReplaySessionExpiredError: 410.
        NoukaiError: other 5xx / network errors (re-raised as-is).
    """
    try:
        resp = await transport.request("GET", session_path(session_id))
    except PermissionDeniedError as e:
        raise ReplayForbiddenError(
            f"Replay forbidden for session {session_id!r}: {e.message}",
            status_code=403,
            response_body=e.response_body,
        ) from e
    except NoukaiError as e:
        raise _map_replay_error(e, session_id) from e

    return SessionResponse.model_validate(resp.body or {})


def fetch_session_sync(
    *,
    transport: Any,  # SyncTransport
    session_id: str,
) -> SessionResponse:
    """Sync mirror of :func:`fetch_session_async`."""
    try:
        resp = transport.request("GET", session_path(session_id))
    except PermissionDeniedError as e:
        raise ReplayForbiddenError(
            f"Replay forbidden for session {session_id!r}: {e.message}",
            status_code=403,
            response_body=e.response_body,
        ) from e
    except NoukaiError as e:
        raise _map_replay_error(e, session_id) from e

    return SessionResponse.model_validate(resp.body or {})


def _map_replay_error(e: NoukaiError, session_id: str) -> NoukaiError:
    """Convert a transport-level error to a typed replay error if mappable."""
    status = e.status_code or 0
    if status == 400:
        return ReplayInvalidSessionError(
            f"Invalid session id {session_id!r}: {e.message}",
            status_code=400,
            response_body=e.response_body,
        )
    if status == 404:
        return ReplaySessionNotFoundError(
            f"No session found for id {session_id!r}",
            status_code=404,
            response_body=e.response_body,
        )
    if status == 410:
        return ReplaySessionExpiredError(
            f"Session {session_id!r} has expired",
            status_code=410,
            response_body=e.response_body,
        )
    # 5xx / other — re-raise as-is so the caller sees the underlying transport error
    return e
