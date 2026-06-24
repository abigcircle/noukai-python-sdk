"""Public exception hierarchy. Two-tier: base + status-mapped subclasses.
Server's specific error codes land on `.code` for branching."""

from __future__ import annotations

from typing import Any


class NoukaiError(Exception):
    """Base exception for all SDK errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        execution_id: str | None = None,
        request_id: str | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.execution_id = execution_id
        self.request_id = request_id
        self.response_body = response_body

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"status_code={self.status_code}, code={self.code!r}, "
            f"message={self.message!r}, request_id={self.request_id!r})"
        )


class APIConnectionError(NoukaiError):
    """Network / DNS / TLS failure before any HTTP response was received."""


class APITimeoutError(APIConnectionError):
    """httpx-level timeout (connect, read, write, or pool)."""


class AuthenticationError(NoukaiError):
    """HTTP 401. Missing or invalid API key.

    Captures the ``WWW-Authenticate`` response header when present (often
    contains a more specific reason such as
    ``Bearer error="invalid_token", error_description="..."``).
    """

    def __init__(
        self,
        *args: Any,
        www_authenticate: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.www_authenticate = www_authenticate


class PermissionDeniedError(NoukaiError):
    """HTTP 403. Key is valid but lacks access to the requested resource."""


class FlowNotFoundError(NoukaiError):
    """HTTP 404 on a slug lookup."""


class InsufficientCreditsError(NoukaiError):
    """HTTP 402. Includes INSUFFICIENT_CREDITS and CREDITS_EXHAUSTED."""


class RateLimitError(NoukaiError):
    """HTTP 429. Includes Retry-After header value if present."""

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class FlowExecutionError(NoukaiError):
    """HTTP 5xx or domain-level execution failure.

    Catch this to handle any execution-time error; branch on `.code` for
    specific server error codes (TOOL_ITERATION_LIMIT, BYOK_KEY_REJECTED,
    INVALID_TREE, etc.).
    """


class ToolCallLimitError(NoukaiError):
    """Client-side: tool_handler exceeded max_tool_rounds without resolving."""


# ---------------------------------------------------------------------------
# Replay errors (see design 20260605-SDK-replay-decorator)
# ---------------------------------------------------------------------------


class ReplayError(NoukaiError):
    """Base for all replay-related SDK errors."""


class ReplayDisabledError(ReplayError):
    """Raised when X-Noukai-Replay header was passed but NOUKAI_REPLAY_ENABLED
    env var is not set. Replay is gated behind an env var for production safety;
    this error is raised only when the SDK is configured to surface the gate
    explicitly (default: silently fall through to normal mode — see Phase 4)."""


class ReplayInvalidSessionError(ReplayError):
    """Backend returned 400 — the session_id is malformed or invalid."""


class ReplayForbiddenError(ReplayError):
    """Backend returned 403 — the session belongs to a project the principal
    cannot access. Wraps the underlying PermissionDeniedError context."""


class ReplaySessionNotFoundError(ReplayError):
    """Backend returned 404 — no session with that id exists."""


class ReplaySessionExpiredError(ReplayError):
    """Backend returned 410 — the session has been pruned by retention.

    Note: BE design defers TTL/retention to existing flow_runs policy; this
    error is shipped now so the SDK can surface it cleanly when retention
    eventually lands. If the backend never returns 410, this class is unused
    but harmless.
    """


class ReplayNoSnapshotsError(ReplayError):
    """Session was found but snapshots_available=false on at least one
    execution (org/flow had trace_capture_mode=off when captured). Replay
    cannot proceed. Surfaced before the first execute() call inside the scope."""


class ReplayMissError(ReplayError):
    """User code made an execute()/step() call that has no matching recorded
    execution for the (slug, position) or (execution_id, step_index) key."""


class ReplayLeftoverError(ReplayError):
    """Scope exit detected recorded executions that were never consumed.
    Raised in strict mode (the only mode in v1)."""
