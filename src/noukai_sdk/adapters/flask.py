"""Flask before_request / after_request adapter for the @noukai.trace decorator.

Imports Flask lazily so the SDK does not require Flask as a hard dependency.
Raises ImportError if Flask is missing.

Flask is synchronous; this adapter uses ``trace_scope_sync``. Async Flask
(Quart) users should use the FastAPI / Starlette adapter instead, which works
for any ASGI app.

Design decisions:
- Scope is opened in ``before_request``, closed in ``teardown_request``.
  ``after_request`` sets the response header while the response object is still
  available; ``teardown_request`` closes the context manager after the response
  has been committed. This separation means ``ReplayLeftoverError`` raised at
  scope exit (``teardown_request``) cannot change the HTTP response status code.
  Acceptable for v1 — the SDK's log_handler surfaces the error, and leftover
  detection is a developer-tools concern.
- Header value is read via ``request.headers.get(HEADER_REPLAY)`` — Flask's
  Headers object already handles case-insensitive lookup.
- ``g._noukai_scope_cm`` / ``g._noukai_scope`` are private convention names.
  If scope open fails (returns an error response early), these are not set
  and ``after_request`` / ``teardown_request`` guards against that.
- Multiple X-Noukai-Replay headers: Flask reads the first; extras ignored.

Usage:
    from noukai_sdk import Noukai
    from noukai_sdk.adapters.flask import init_noukai_trace

    noukai = Noukai(api_key=..., env="dev")
    init_noukai_trace(app, client=noukai)
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
    from .._client import Noukai


def init_noukai_trace(app: Any, *, client: Noukai) -> None:
    """Register before_request / after_request / teardown_request hooks on a Flask app.

    Args:
        app: A Flask application instance.
        client: A ``Noukai`` (sync) client. The middleware reads
            ``client._transport`` to open the trace scope.

    Raises:
        ImportError: if Flask is not installed.
    """
    try:
        from flask import g, jsonify, request
    except ImportError as exc:
        raise ImportError(
            "init_noukai_trace requires Flask. Install with `pip install flask`."
        ) from exc

    from .._trace_scope import trace_scope_sync

    @app.before_request  # type: ignore[untyped-decorator]
    def _open_scope() -> Any:
        """Open a trace scope for this request. Returns an error response on failure."""
        replay_sid: str | None = request.headers.get(HEADER_REPLAY)
        try:
            cm = trace_scope_sync(
                replay_session_id=replay_sid,
                transport=client._transport,
            )
            scope = cm.__enter__()
            g._noukai_scope_cm = cm
            g._noukai_scope = scope
        except ReplayForbiddenError as exc:
            return jsonify({"error": "replay_forbidden", "message": exc.message}), 403
        except ReplaySessionNotFoundError as exc:
            return jsonify({"error": "replay_session_not_found", "message": exc.message}), 404
        except ReplaySessionExpiredError as exc:
            return jsonify({"error": "replay_session_expired", "message": exc.message}), 410
        except ReplayInvalidSessionError as exc:
            return jsonify({"error": "replay_invalid_session", "message": exc.message}), 400
        except ReplayNoSnapshotsError as exc:
            return jsonify({"error": "replay_no_snapshots", "message": exc.message}), 409
        return None

    @app.after_request  # type: ignore[untyped-decorator]
    def _set_session_header(response: Any) -> Any:
        """Inject X-Noukai-Session header on the outgoing response in capture mode."""
        scope = getattr(g, "_noukai_scope", None)
        if scope is not None and scope.session_id:
            response.headers[HEADER_RESPONSE_SESSION] = scope.session_id
        return response

    @app.teardown_request  # type: ignore[untyped-decorator]
    def _close_scope(exc: BaseException | None) -> None:
        """Close the trace context manager after the response is committed.

        If ``ReplayLeftoverError`` is raised here (scope exit detects unconsumed
        executions), the response has already been sent so the status code cannot
        be changed. The error propagates to Flask's error handler / log; the
        SDK's log_handler also receives the scope_close event.
        """
        cm = getattr(g, "_noukai_scope_cm", None)
        if cm is None:
            return
        # Propagate exception type/value into the CM so it can react properly.
        exc_type = type(exc) if exc is not None else None
        try:
            cm.__exit__(exc_type, exc, None)
        except ReplayError:
            # Re-raise so Flask's error handler / logging pipeline sees it.
            raise
