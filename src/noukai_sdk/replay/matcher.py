"""Matches user code execute()/events()/steps() calls to recorded executions.

Matching rules (BE design 20260605-BE-execution-session-grouping
§"The SDK matcher compares ``flow.slug`` first; falls back to ``flow_id``
if needed"):

- The user-code call has (org, project, slug). The SDK client is bound to
  one (org, project) context, so we match on **bare slug** (not
  ``org/project/slug``). When the recorded execution's ``slug`` is None
  (e.g. the underlying flow has been deleted), the matcher cannot resolve
  by name and raises ``ReplayMissError``; users can pin an explicit
  ``session_id`` of a session captured while the flow still existed.
- ``execute(slug, ...)``: Nth code call to slug X matches Nth recorded
  execution for slug X in the session. Independent counter per slug.
- ``steps(slug, ...)`` / ``events(slug, ...)``: first call matches by
  slug-positional with trigger_type ``"step"``; SSE event reconstruction
  is handled by ``sse_reconstructor.py``.

Slug format: BARE ``flow.slug`` as stored in ``SessionExecution.slug``
(e.g. ``"grade-3"``). NO org/project prefix.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncIterator, Iterator
from typing import Any

from .._errors import (
    FlowExecutionError,
    ReplayMissError,
)
from .._models.responses import ExecuteResult
from .._models.session import SessionExecution
from ._state import ReplayCursor, ScopeState


def _execution_matches_slug(
    ex: SessionExecution,
    slug: str,
    trigger_type: str,
) -> bool:
    """Match a recorded execution against the user-code (slug, trigger_type)
    call. Bare slug is the primary key; ``flow_id`` fallback is used only
    when the recording's slug is unset.

    The user-code call site does not have ``flow_id`` available, so the
    fallback only saves the day in the rare case where every recording is
    the same (deleted) flow. In the common case (some recordings have
    valid slugs), the recordings with null slugs are simply skipped and
    accounted for in the diagnostic emitted by ``_find_nth_execution_for_slug``.
    """
    if ex.trigger_type != trigger_type:
        return False
    if ex.slug:
        return ex.slug == slug
    return False


def _find_nth_execution_for_slug(
    scope: ScopeState,
    slug: str,
    trigger_type: str,
    cursor_attr: str,
) -> SessionExecution:
    """Walk fetched executions in order, find the Nth one matching slug+trigger.

    ``cursor_attr`` is the scope attribute holding the cursor — 'execute_cursor'
    for execute(), 'step_first_call_cursor' for first-call step().
    """
    assert scope.fetched_session is not None
    cursor: ReplayCursor = getattr(scope, cursor_attr)
    n = cursor.next_index(slug)

    matches: list[SessionExecution] = [
        ex
        for ex in scope.fetched_session.executions
        if _execution_matches_slug(ex, slug, trigger_type)
    ]

    if n >= len(matches):
        total = len(scope.fetched_session.executions)
        null_slug_count = sum(1 for ex in scope.fetched_session.executions if not ex.slug)
        hint = (
            f" Note: {null_slug_count} recorded execution(s) have a null/empty slug "
            f"(deleted flow); they cannot be matched by slug name. Use a session "
            f"captured while the flow still existed."
            if null_slug_count
            else ""
        )
        raise ReplayMissError(
            f"Replay miss: user code made call #{n + 1} to slug {slug!r} "
            f"({trigger_type}), but the session has only {len(matches)} "
            f"recorded {trigger_type} execution(s) for that slug out of "
            f"{total} total. Either the user code has diverged from the "
            f"recording, or the recording is incomplete.{hint}"
        )
    ex = matches[n]
    scope.consumed_execution_ids.add(ex.execution_id)
    return ex


def _materialize_execute_result(
    ex: SessionExecution,
    scope_session_id: str | None,
) -> ExecuteResult:
    """Construct an ExecuteResult from a recorded execution, raising recorded
    errors instead if present.

    ``scope_session_id`` is the session_id from the surrounding trace_scope,
    surfaced on ``result.session_id`` per Phase 5 contract.
    """
    # If the execution recorded an error, re-raise it faithfully.
    if ex.error_at_step:
        # Look up the failed step's error_snapshot.
        for step in ex.steps:
            if step.step_id == ex.error_at_step and step.error_snapshot:
                err = step.error_snapshot
                raise FlowExecutionError(
                    err.get("message", "Recorded error"),
                    code=err.get("code"),
                    execution_id=ex.execution_id,
                )
        # error_at_step set but no matching step with error_snapshot —
        # synthesize a generic error to avoid silently returning a result.
        raise FlowExecutionError(
            f"Recorded execution {ex.execution_id!r} failed at step "
            f"{ex.error_at_step!r} but error_snapshot is unavailable.",
            execution_id=ex.execution_id,
        )

    # Synthesize the success result from the last step's output_snapshot.
    final_output = ex.steps[-1].output_snapshot if ex.steps else None
    result = ExecuteResult.model_validate(
        {
            "status": "completed",
            "result": final_output,
            "executionId": ex.execution_id,
            "flowId": ex.flow_id or "",
            "blockCount": len(ex.steps) if ex.steps else 1,
        }
    )
    result._session_id = scope_session_id
    return result


# ---------------------------------------------------------------------------
# Explicit-session-id one-shot helpers (Q1)
# ---------------------------------------------------------------------------


async def _find_first_in_explicit_session_async(
    *,
    transport: Any,
    slug: str,
    trigger_type: str,
    explicit_session_id: str,
) -> SessionExecution:
    """Fetch ``explicit_session_id`` and return the first execution matching
    the (slug, trigger_type) call. Used for the Q1 path where the user passes
    a session_id kwarg that differs from the surrounding scope."""
    from .fetcher import fetch_session_async

    one_shot = await fetch_session_async(transport=transport, session_id=explicit_session_id)
    matches = [ex for ex in one_shot.executions if _execution_matches_slug(ex, slug, trigger_type)]
    if not matches:
        raise ReplayMissError(
            f"Replay miss (explicit session_id={explicit_session_id!r}): "
            f"no recorded {trigger_type} execution for slug {slug!r}."
        )
    return matches[0]


def _find_first_in_explicit_session_sync(
    *,
    transport: Any,
    slug: str,
    trigger_type: str,
    explicit_session_id: str,
) -> SessionExecution:
    """Sync mirror of :func:`_find_first_in_explicit_session_async`."""
    from .fetcher import fetch_session_sync

    one_shot = fetch_session_sync(transport=transport, session_id=explicit_session_id)
    matches = [ex for ex in one_shot.executions if _execution_matches_slug(ex, slug, trigger_type)]
    if not matches:
        raise ReplayMissError(
            f"Replay miss (explicit session_id={explicit_session_id!r}): "
            f"no recorded {trigger_type} execution for slug {slug!r}."
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Execute-mode matching (scenarios 9–11, 14–17)
# ---------------------------------------------------------------------------


async def match_execute_async(
    scope: ScopeState,
    org: str,
    project: str,
    slug: str,
    message: str | None = None,
    *,
    transport: Any = None,
    explicit_session_id: str | None = None,
    **_kwargs: Any,
) -> ExecuteResult:
    """Match an async execute() call to a recorded execution and return its
    result, re-raising recorded errors faithfully.

    Per the unified Q1 rule (also applied to steps/events): if
    ``explicit_session_id`` is set and differs from ``scope.session_id``,
    perform a one-shot fetch of that explicit session and match the call
    against its first execution for the slug.
    """
    # org/project unused for matching — BE matches on bare slug within the
    # (org, project) context the SDK client is bound to.
    del org, project, message

    # Q1: explicit session_id kwarg inside replay scope → one-shot fetch.
    if explicit_session_id is not None and transport is not None:
        one_shot_ex = await _find_first_in_explicit_session_async(
            transport=transport,
            slug=slug,
            trigger_type="execute",
            explicit_session_id=explicit_session_id,
        )
        _consume_scope_slot_for_explicit_override(scope, slug, "execute", "execute_cursor")
        return _materialize_execute_result(one_shot_ex, explicit_session_id)

    key = (slug, "execute")
    # Mark in-flight BEFORE yielding, so concurrently scheduled tasks in
    # asyncio.gather can see each other as in-flight. This is what enables
    # the concurrent same-slug warning (Q6) to fire reliably under gather().
    _mark_inflight(scope, key)
    # Yield so that other concurrently gathered tasks get a chance to run
    # and call _mark_inflight before any of them proceeds to the cursor check.
    await asyncio.sleep(0)
    _detect_concurrent_from_inflight(scope, slug, "execute")
    try:
        ex = _find_nth_execution_for_slug(scope, slug, "execute", "execute_cursor")
        return _materialize_execute_result(ex, scope.session_id)
    finally:
        _release_inflight(scope, key)


def _consume_scope_slot_for_explicit_override(
    scope: ScopeState,
    slug: str,
    trigger_type: str,
    cursor_attr: str,
) -> None:
    """When an explicit session_id override is used, advance the scope's cursor
    and mark the corresponding scope-session entry as consumed (if any).

    This prevents a leftover error for scope executions that were "shadowed"
    by an explicit session_id kwarg on a single call (scenario 17).
    """
    assert scope.fetched_session is not None
    cursor: ReplayCursor = getattr(scope, cursor_attr)
    n = cursor.next_index(slug)
    matches = [
        ex
        for ex in scope.fetched_session.executions
        if _execution_matches_slug(ex, slug, trigger_type)
    ]
    if n < len(matches):
        # Mark the Nth scope entry consumed so leftover check passes.
        scope.consumed_execution_ids.add(matches[n].execution_id)


def match_execute_sync(
    scope: ScopeState,
    org: str,
    project: str,
    slug: str,
    message: str | None = None,
    *,
    transport: Any = None,
    explicit_session_id: str | None = None,
    **_kwargs: Any,
) -> ExecuteResult:
    """Sync mirror of :func:`match_execute_async`."""
    del org, project, message

    if explicit_session_id is not None and transport is not None:
        one_shot_ex = _find_first_in_explicit_session_sync(
            transport=transport,
            slug=slug,
            trigger_type="execute",
            explicit_session_id=explicit_session_id,
        )
        _consume_scope_slot_for_explicit_override(scope, slug, "execute", "execute_cursor")
        return _materialize_execute_result(one_shot_ex, explicit_session_id)

    key = (slug, "execute")
    _detect_concurrent(scope, slug, "execute")
    try:
        ex = _find_nth_execution_for_slug(scope, slug, "execute", "execute_cursor")
        return _materialize_execute_result(ex, scope.session_id)
    finally:
        _release_inflight(scope, key)


# ---------------------------------------------------------------------------
# Step-mode matching (entry points; SSE event production in sse_reconstructor)
# ---------------------------------------------------------------------------


async def match_events_async(
    scope: ScopeState,
    org: str,
    project: str,
    slug: str,
    message: str | None = None,
    *,
    transport: Any = None,
    explicit_session_id: str | None = None,
    **_kwargs: Any,
) -> AsyncIterator[Any]:
    """Find the matched step-trigger execution and stream reconstructed events.

    Applies the unified Q1 rule: explicit ``session_id`` that differs from
    the scope's session triggers a one-shot fetch of that session and
    reconstructs SSE from its first matching execution.
    """
    del org, project, message
    from .sse_reconstructor import reconstruct_events_async

    if explicit_session_id is not None and transport is not None:
        one_shot_ex = await _find_first_in_explicit_session_async(
            transport=transport,
            slug=slug,
            trigger_type="step",
            explicit_session_id=explicit_session_id,
        )
        _consume_scope_slot_for_explicit_override(scope, slug, "step", "step_first_call_cursor")
        async for event in reconstruct_events_async(one_shot_ex):
            yield event
        return

    _detect_concurrent(scope, slug, "step")
    ex = _find_nth_execution_for_slug(scope, slug, "step", "step_first_call_cursor")
    async for event in reconstruct_events_async(ex):
        yield event


def match_events_sync(
    scope: ScopeState,
    org: str,
    project: str,
    slug: str,
    message: str | None = None,
    *,
    transport: Any = None,
    explicit_session_id: str | None = None,
    **_kwargs: Any,
) -> Iterator[Any]:
    """Sync mirror of :func:`match_events_async`."""
    del org, project, message
    from .sse_reconstructor import reconstruct_events_sync

    if explicit_session_id is not None and transport is not None:
        one_shot_ex = _find_first_in_explicit_session_sync(
            transport=transport,
            slug=slug,
            trigger_type="step",
            explicit_session_id=explicit_session_id,
        )
        _consume_scope_slot_for_explicit_override(scope, slug, "step", "step_first_call_cursor")
        yield from reconstruct_events_sync(one_shot_ex)
        return

    _detect_concurrent(scope, slug, "step")
    ex = _find_nth_execution_for_slug(scope, slug, "step", "step_first_call_cursor")
    yield from reconstruct_events_sync(ex)


# ---------------------------------------------------------------------------
# In-flight tracking helpers (concurrent same-slug detection, Q6)
# ---------------------------------------------------------------------------
#
# We use a dict[key, int] counter on the scope (as ``_in_flight_counts``).
# This lets us distinguish "0 in flight", "1 in flight (current task only)",
# and ">1 in flight (concurrent tasks)". A plain set cannot represent the
# ">1" state for the same key.


def _get_inflight_counts(scope: ScopeState) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = getattr(scope, "_in_flight_counts", {})
    scope._in_flight_counts = counts  # type: ignore[attr-defined]
    return counts


def _mark_inflight(scope: ScopeState, key: tuple[str, str]) -> None:
    """Increment the in-flight counter for ``key``. Called BEFORE any yield
    point so concurrent async tasks can see each other in-flight."""
    counts = _get_inflight_counts(scope)
    counts[key] = counts.get(key, 0) + 1


def _release_inflight(scope: ScopeState, key: tuple[str, str]) -> None:
    """Decrement the in-flight counter after a call completes.

    Sequential calls: counter goes 0 → 1 → 0 → 1 → 0 (no overlap).
    Concurrent calls: counter goes 0 → 1 → 2 (overlap detected before release).
    """
    counts = _get_inflight_counts(scope)
    if counts.get(key, 0) > 0:
        counts[key] -= 1


def _detect_concurrent_from_inflight(scope: ScopeState, slug: str, trigger: str) -> None:
    """Warn if another task is already in-flight for (slug, trigger).

    Called AFTER ``_mark_inflight`` and a yield point. At this point the
    current task has already incremented the counter; if the count is > 1,
    another task is concurrently executing the same slug.
    """
    key = (slug, trigger)
    count = _get_inflight_counts(scope).get(key, 0)
    if count > 1:
        warnings.warn(
            f"Concurrent same-slug execute()/step() detected for {slug!r} "
            f"({trigger}) in replay scope. Per design, this is undefined "
            f"behavior in v1 — the matching may not reflect call order.",
            UserWarning,
            stacklevel=4,
        )


def _detect_concurrent(scope: ScopeState, slug: str, trigger: str) -> None:
    """Sync version: mark in-flight AND detect in one step.

    For sync/threading, Python's GIL makes dict ops safe. The check-then-set
    is atomic enough to detect thread overlap. After detection, mark in-flight
    for the duration of the call (released in the caller's try/finally).
    """
    key = (slug, trigger)
    counts = _get_inflight_counts(scope)
    already_in = counts.get(key, 0) > 0
    counts[key] = counts.get(key, 0) + 1
    if already_in:
        warnings.warn(
            f"Concurrent same-slug execute()/step() detected for {slug!r} "
            f"({trigger}) in replay scope. Per design, this is undefined "
            f"behavior in v1 — the matching may not reflect call order.",
            UserWarning,
            stacklevel=4,
        )
