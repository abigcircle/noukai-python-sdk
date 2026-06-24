"""Trace scope: decorator + context manager + accessor.

The scope establishes which mode the SDK runs in (normal / capture / replay)
for all `Flow.execute` and `Flow.steps`/`Flow.events` calls made inside it.

Plumbing rules (see design 20260605-SDK-replay-decorator):
- A ContextVar holds the current ScopeState (or None for normal mode).
- `@trace` wraps both sync and coroutine functions.
- `trace_scope()` is an async context manager (the underlying primitive).
- `trace_scope_sync()` is the sync mirror.
- `current_session_id()` reads the ContextVar; returns None outside any scope.
"""

from __future__ import annotations

import functools
import inspect
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from typing import Any, TypeVar, overload

from ._constants import REPLAY_ENABLED_ENV_VAR
from .replay._state import ScopeMode, ScopeState

# Module-level contextvar. None when no scope is active.
_scope_var: ContextVar[ScopeState | None] = ContextVar("noukai_scope", default=None)

T = TypeVar("T")


def _replay_env_enabled() -> bool:
    """Return True if NOUKAI_REPLAY_ENABLED is set to a truthy value."""
    return os.environ.get(REPLAY_ENABLED_ENV_VAR, "").lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


def current_session_id() -> str | None:
    """Return the session_id of the current trace scope, or None.

    Safe to call anywhere — outside a scope it returns None.
    """
    scope = _scope_var.get()
    return scope.session_id if scope else None


def _current_scope() -> ScopeState | None:
    """Internal helper for transport/Flow code to consult the contextvar."""
    return _scope_var.get()


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


@overload
def trace(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]: ...


@overload
def trace(fn: Callable[..., T]) -> Callable[..., T]: ...


def trace(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: open a trace scope for the decorated function.

    Works on both sync and async functions. Inside the wrapped function:
    - If no `X-Noukai-Replay` header is detected → CAPTURE mode (new session_id
      generated on first execute/steps call).
    - If `X-Noukai-Replay` header is detected AND `NOUKAI_REPLAY_ENABLED=true`
      env var is set → REPLAY mode (fetches session, serves recorded outputs).
    - Otherwise → NORMAL mode (no overhead).

    The header is detected by the framework adapter middleware; pure-function
    use (no framework) is always CAPTURE mode unless the explicit context
    manager forms (`trace_scope` / `trace_scope_sync`) are used with
    `replay_session_id=...`.

    The decorator does not inspect the wrapped function's arguments — header
    detection is framework-adapter-driven. Plain-Python users without a
    framework can call `trace_scope(replay_session_id=...)` directly.
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            async with trace_scope():
                return await fn(*args, **kwargs)
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with trace_scope_sync():
            return fn(*args, **kwargs)
    return sync_wrapper


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def trace_scope(
    *,
    replay_session_id: str | None = None,
    capture: bool = True,
    transport: Any | None = None,
) -> AsyncIterator[ScopeState]:
    """Async context manager establishing a trace scope.

    Args:
        replay_session_id: When set AND NOUKAI_REPLAY_ENABLED=true, opens a
            REPLAY scope serving recorded outputs for the given session.
            When unset OR env var is unset, opens a CAPTURE scope (or NORMAL
            if `capture=False`).
        capture: When False AND `replay_session_id` is None, scope is NORMAL
            mode — no session_id is generated. Useful for opting out at a
            sub-scope level. Default True.
        transport: Transport instance required for REPLAY mode session fetch.
            Pass `client._transport`. Ignored in CAPTURE/NORMAL mode.

    Yields:
        The ScopeState for inspection (mostly: scope.session_id).

    Raises:
        ReplayForbiddenError: backend 403 on session fetch.
        ReplaySessionNotFoundError: backend 404.
        ReplayInvalidSessionError: backend 400.
        ReplayNoSnapshotsError: session found but snapshots_available=false.
    """
    # Determine mode and session_id before entering the scope.
    mode: ScopeMode
    sid: str | None

    if replay_session_id and _replay_env_enabled():
        mode = ScopeMode.REPLAY
        sid = replay_session_id
    elif capture:
        mode = ScopeMode.CAPTURE
        sid = str(uuid.uuid4())
    else:
        mode = ScopeMode.NORMAL
        sid = None

    scope = ScopeState(mode=mode, session_id=sid)

    _emit_log(transport, {"phase": "scope_open", "mode": mode.value, "session_id": sid})

    # In REPLAY mode, pre-fetch the session before the body runs.
    if mode is ScopeMode.REPLAY:
        # Lazy import to avoid circular: fetcher needs Transport types.
        # fetcher.py is implemented in Phase 6; import-not-found is expected until then.
        from .replay.fetcher import fetch_session_async
        assert replay_session_id is not None  # guaranteed by mode == REPLAY branch
        scope.fetched_session = await fetch_session_async(
            transport=transport, session_id=replay_session_id
        )
        _validate_snapshots_available(scope)

    token = _scope_var.set(scope)
    try:
        yield scope
        if mode is ScopeMode.REPLAY:
            _check_leftovers(scope)
    finally:
        _scope_var.reset(token)
        _emit_log(transport, {"phase": "scope_close", "mode": mode.value, "session_id": sid})


@contextmanager
def trace_scope_sync(
    *,
    replay_session_id: str | None = None,
    capture: bool = True,
    transport: Any | None = None,
) -> Iterator[ScopeState]:
    """Sync mirror of :func:`trace_scope`. Same semantics, blocking I/O for
    session fetch in REPLAY mode (uses SyncTransport)."""
    mode: ScopeMode
    sid: str | None

    if replay_session_id and _replay_env_enabled():
        mode = ScopeMode.REPLAY
        sid = replay_session_id
    elif capture:
        mode = ScopeMode.CAPTURE
        sid = str(uuid.uuid4())
    else:
        mode = ScopeMode.NORMAL
        sid = None

    scope = ScopeState(mode=mode, session_id=sid)

    _emit_log(transport, {"phase": "scope_open", "mode": mode.value, "session_id": sid})

    if mode is ScopeMode.REPLAY:
        from .replay.fetcher import fetch_session_sync
        assert replay_session_id is not None  # guaranteed by mode == REPLAY branch
        scope.fetched_session = fetch_session_sync(
            transport=transport, session_id=replay_session_id
        )
        _validate_snapshots_available(scope)

    token = _scope_var.set(scope)
    try:
        yield scope
        if mode is ScopeMode.REPLAY:
            _check_leftovers(scope)
    finally:
        _scope_var.reset(token)
        _emit_log(transport, {"phase": "scope_close", "mode": mode.value, "session_id": sid})


# ---------------------------------------------------------------------------
# Log-emission helper
# ---------------------------------------------------------------------------


def _emit_log(transport: Any, event: dict[str, Any]) -> None:
    """Emit a log event via the transport's log_handler, if any.

    Silently swallows any exception from the handler so that logging never
    interrupts scope entry/exit.
    """
    if transport is None:
        return
    handler = getattr(transport, "_log_handler", None)
    if handler is not None:
        with suppress(Exception):  # noqa: BLE001
            handler(event)


# ---------------------------------------------------------------------------
# Internal helpers used by Phase 6 (and by Phase 4's scope-entry validation)
# ---------------------------------------------------------------------------


def _validate_snapshots_available(scope: ScopeState) -> None:
    """Raise ReplayNoSnapshotsError if any execution has snapshots_available=False."""
    from ._errors import ReplayNoSnapshotsError
    assert scope.fetched_session is not None
    for ex in scope.fetched_session.executions:
        if not ex.snapshots_available:
            raise ReplayNoSnapshotsError(
                f"Session {scope.session_id!r} has execution {ex.execution_id!r} "
                f"with snapshots_available=false (trace_capture_mode was "
                f"{ex.trace_capture_mode}). Cannot replay. Set trace_capture_mode "
                f"to 'full' or 'redacted' on the flow/org and re-record."
            )


def _check_leftovers(scope: ScopeState) -> None:
    """Raise ReplayLeftoverError if any executions were not consumed."""
    from ._errors import ReplayLeftoverError
    assert scope.fetched_session is not None
    all_ids = {ex.execution_id for ex in scope.fetched_session.executions}
    leftover = all_ids - scope.consumed_execution_ids
    if leftover:
        raise ReplayLeftoverError(
            f"Session {scope.session_id!r} has {len(leftover)} unconsumed "
            f"executions at scope exit: {sorted(leftover)}. Either the user "
            f"code stopped early, or recorded executions exist that user code "
            f"no longer reaches. Re-record after fixing."
        )


__all__ = [
    "trace",
    "trace_scope",
    "trace_scope_sync",
    "current_session_id",
    "_current_scope",
    "_scope_var",
]
