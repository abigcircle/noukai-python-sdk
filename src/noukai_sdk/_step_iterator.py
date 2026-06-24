"""Step iterator: drives ``/seq/.../step`` end-to-end, carrying the cursor.

This is the single most complex piece of the SDK. Responsibilities:

- ``POST /step`` calls in a loop, threading ``executionId +
  accumulatedOutputs`` between calls.
- SSE parsing via :func:`_streaming.parse_sse_stream` (async) or
  :func:`_streaming.parse_sse_stream_sync` (sync).
- Tool-call handling in two modes:

  * **auto-resume** (when ``tool_handler`` is provided): the iterator
    invokes the handler, captures results, and re-issues ``/step`` with
    the tool result messages — the user never sees a
    :class:`ToolCallsRequired` event.
  * **manual** (no handler, async only): the iterator yields
    :class:`ToolCallsRequired` with a bound ``.resume(tool_results=...)``
    coroutine. Awaiting that coroutine mutates the iterator state; the next
    iteration re-issues ``/step`` with the supplied tool messages.
    Sync clients support auto-resume only (manual resume is not supported).

- Termination on ``flow_completed`` or ``step_error``.

Design notes:

Both iterators are **classes** rather than plain generators. The class form
lets the ``.resume()`` callable bound onto a yielded
:class:`ToolCallsRequired` event mutate iterator state directly
(``self._pending_tool_results``), which is much simpler than queue-based
shimming or generator ``.asend()`` gymnastics.

Cursor mutation lives in exactly one place: the body of :meth:`_drive` /
:meth:`_drive_sync`.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any

from ._constants import DEFAULT_MAX_TOOL_ROUNDS, HEADER_SESSION_ID
from ._errors import ToolCallLimitError
from ._models.events import (
    FlowCompleted,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepPaused,
    StreamEvent,
    ToolCallsRequired,
)
from ._models.requests import StepRequest
from ._streaming import parse_sse_stream, parse_sse_stream_sync
from ._tool_calls import _check_tool_messages_size

if TYPE_CHECKING:
    from ._flow import AsyncFlow, Flow, VersionSpec

ToolHandlerLike = Callable[
    [list[dict[str, Any]]],
    list[dict[str, Any]] | Awaitable[list[dict[str, Any]]],
]


class EventIterator:
    """Async iterator driving ``/seq/.../step`` end-to-end.

    Use via :meth:`AsyncFlow.events` (yields every event) or
    :meth:`AsyncFlow.steps` (yields only :class:`StepCompleted`).

    The iterator owns the cursor (``execution_id + accumulated_outputs +
    step_index``) and the pending tool-call resume state. Callers see a
    flat async iterator; the multi-call HTTP dance is hidden.
    """

    def __init__(
        self,
        flow: AsyncFlow,
        *,
        message: str | None,
        parameters: dict[str, Any] | None,
        block_overrides: dict[str, dict[str, Any]] | None,
        input_overrides: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None,
        run_remaining: bool,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any | None,
        tool_handler: ToolHandlerLike | None,
        max_tool_rounds: int | None,
        trace: bool,
        version: VersionSpec,
        yield_only_step_completed: bool,
        session_id: str | None = None,
    ) -> None:
        self._flow = flow
        self._message = message
        self._parameters = parameters
        self._block_overrides = block_overrides
        self._input_overrides = input_overrides
        self._attachments = attachments
        self._run_remaining = run_remaining
        self._tools = tools
        self._tool_choice = tool_choice
        self._tool_handler = tool_handler
        self._max_rounds = (
            max_tool_rounds if max_tool_rounds is not None else DEFAULT_MAX_TOOL_ROUNDS
        )
        self._trace = trace
        self._version = version
        self._yield_only_step_completed = yield_only_step_completed
        self._session_id = session_id

        # Cursor state — mutated only in _drive.
        self._execution_id: str | None = None
        self._accumulated_outputs: dict[str, Any] = {}
        self._step_index: int = 0
        self._tool_rounds: int = 0

        # Pending tool-call state. Either set by the auto-resume path (in
        # _drive) or by user-side .resume() callbacks attached to yielded
        # ToolCallsRequired events.
        self._pending_tool_messages: list[dict[str, Any]] | None = None
        self._pending_iterations_used: int = 0

        # The driver async generator. Lazily started.
        self._driver: AsyncIterator[StreamEvent] = self._drive()

    def __aiter__(self) -> EventIterator:
        return self

    async def __anext__(self) -> StreamEvent:
        return await self._driver.__anext__()

    # --- internals --------------------------------------------------------

    def _build_request(self) -> StepRequest:
        """Build the next ``/step`` request body from current cursor state."""
        # ``message`` is sent only on the very first call AND only when we
        # are not resuming with tool messages (server contract).
        is_first_call = (
            self._step_index == 0
            and self._execution_id is None
            and self._pending_tool_messages is None
        )
        return StepRequest(
            execution_id=self._execution_id,
            step_index=self._step_index,
            accumulated_outputs=dict(self._accumulated_outputs),
            message=self._message if is_first_call else None,
            parameters=(self._parameters or {}) if is_first_call else {},
            attachments=self._attachments if is_first_call else None,
            input_overrides=self._input_overrides or {},
            block_overrides=self._block_overrides,
            run_remaining=self._run_remaining,
            tools=self._tools,
            tool_choice=self._tool_choice,
            tool_call_messages=self._pending_tool_messages,
            iterations_used=self._pending_iterations_used,
            trace=self._trace,
        )

    def _attach_resume(self, event: ToolCallsRequired) -> None:
        """Bind a ``.resume(tool_results=...)`` coroutine onto *event*.

        Awaiting it stashes the next ``/step`` tool messages on this
        iterator. The driver loop, after yielding *event*, breaks out of
        the current SSE stream and the next ``__anext__`` re-enters
        :meth:`_drive`'s outer ``while`` with the pending state set.
        """
        flow_iter = self

        async def _resume(*, tool_results: list[dict[str, Any]]) -> None:
            new_messages = list(event.tool_call_messages) + list(tool_results)
            _check_tool_messages_size(len(new_messages))
            flow_iter._pending_tool_messages = new_messages
            flow_iter._pending_iterations_used = event.iterations_used

        # Attached as a public attribute on the Pydantic model. The model
        # uses default ConfigDict (no `frozen=True`) so we can set arbitrary
        # attrs via object.__setattr__.
        object.__setattr__(event, "resume", _resume)

    async def _drive(self) -> AsyncIterator[StreamEvent]:
        """Main driver: loop over ``/step`` calls until terminal state."""
        flow_complete = False

        # Resolve effective session id for X-Session-Id injection.
        # Precedence: per-call kwarg > client default > scope (capture mode).
        # Uses `is not None` so an explicit "" is not silently overridden.
        from ._trace_scope import _current_scope

        scope = _current_scope()
        effective_sid: str | None = None
        if self._session_id is not None:
            effective_sid = self._session_id
        elif self._flow._transport._default_session_id is not None:
            effective_sid = self._flow._transport._default_session_id
        elif scope is not None:
            effective_sid = scope.session_id

        extra_headers: dict[str, str] | None = None
        if effective_sid is not None:
            extra_headers = {HEADER_SESSION_ID: effective_sid}

        while not flow_complete:
            req = self._build_request()
            # Clear pending tool state once it's been baked into the request.
            self._pending_tool_messages = None
            self._pending_iterations_used = 0

            url = f"{self._flow._versioned_path(self._version)}/step"
            byte_stream = self._flow._transport.stream(
                "POST", url, json=req, extra_headers=extra_headers
            )

            # Track within this single SSE stream whether we should re-issue
            # /step (auto-resume after tool calls, or step protocol pause).
            reissue = False

            async for event in parse_sse_stream(byte_stream):
                if isinstance(event, RunStarted):
                    if event.execution_id:
                        self._execution_id = event.execution_id
                    if not self._yield_only_step_completed:
                        yield event

                elif isinstance(event, StepCompleted):
                    self._accumulated_outputs[event.step_id] = event.output
                    self._step_index += 1
                    yield event  # always surfaced (both modes)

                elif isinstance(event, ToolCallsRequired):
                    # Server-side cursor authority: trust the event's
                    # executionId/stepIndex even if we didn't see RunStarted.
                    self._execution_id = event.execution_id
                    self._step_index = event.step_index
                    self._accumulated_outputs.update(event.accumulated_outputs)

                    if self._tool_handler is not None:
                        # Auto-resume path. Enforce client-side round limit
                        # BEFORE invoking the handler.
                        self._tool_rounds += 1
                        if self._tool_rounds > self._max_rounds:
                            raise ToolCallLimitError(
                                f"Tool call loop exceeded max_tool_rounds={self._max_rounds}",
                                code="TOOL_CALL_LIMIT_CLIENT",
                                execution_id=event.execution_id,
                            )
                        result = self._tool_handler(event.tool_calls)
                        if inspect.isawaitable(result):
                            result = await result
                        # Stage the next /step's resume state.
                        new_messages = list(event.tool_call_messages) + list(result)
                        _check_tool_messages_size(len(new_messages))
                        self._pending_tool_messages = new_messages
                        self._pending_iterations_used = event.iterations_used
                        reissue = True
                        break  # exit inner SSE loop; outer while re-issues.

                    # Manual mode: hand off to caller. Attach .resume();
                    # after yield, the driver exits the current SSE stream
                    # and the next outer iteration re-issues /step using
                    # whatever the user staged via .resume(). If the user
                    # never resumes, _pending_tool_messages stays None and
                    # the next /step will repeat the same request — that's
                    # the user's responsibility to handle (or close the
                    # iterator).
                    self._attach_resume(event)
                    yield event
                    reissue = True
                    break

                elif isinstance(event, StepPaused):
                    # Step protocol pause — server signals "issue next /step".
                    if not self._yield_only_step_completed:
                        yield event
                    reissue = True
                    break

                elif isinstance(event, StepFailed):
                    yield event
                    flow_complete = True
                    break

                elif isinstance(event, FlowCompleted):
                    if not self._yield_only_step_completed:
                        yield event
                    flow_complete = True
                    break

                else:
                    # StepStarted, StepInput, StepOutput, etc.
                    if not self._yield_only_step_completed:
                        yield event

            if flow_complete:
                return

            if not reissue:
                # SSE stream ended without a terminal or pause signal.
                # Treat as completion to avoid an infinite loop.
                return


def make_events_iterator(
    flow: AsyncFlow,
    **kwargs: Any,
) -> EventIterator | AsyncIterator[StreamEvent]:
    """Construct an iterator yielding every typed event.

    In REPLAY mode, always dispatches to the matcher. The matcher applies
    the unified Q1 rule (also applied to execute()): an explicit
    ``session_id`` that differs from the scope's session triggers a one-shot
    fetch of that session; otherwise the scope cassette is used.

    Note: callers (Flow.events / Flow.steps) pre-compute ``effective_sid``,
    so ``kwargs["session_id"]`` already encodes the "explicit > default >
    scope" precedence. We recover the *user-explicit* sid here to drive the
    one-shot path correctly.
    """
    from ._trace_scope import _current_scope
    from .replay._state import ScopeMode

    scope = _current_scope()
    in_replay = scope is not None and scope.mode is ScopeMode.REPLAY
    if in_replay:
        assert scope is not None  # guaranteed by in_replay check above
        from .replay.matcher import match_events_async

        # Derive the user-explicit session_id (the one that should trigger a
        # one-shot fetch when it differs from scope.session_id). When the
        # caller pre-computed effective_sid == scope.session_id, that's the
        # "no explicit override" case → pass None.
        user_session_id = kwargs.get("session_id")
        explicit_sid = (
            user_session_id
            if (user_session_id is not None and user_session_id != scope.session_id)
            else None
        )
        return match_events_async(
            scope=scope,
            org=flow._org,
            project=flow._project,
            slug=flow._slug,
            message=kwargs.get("message"),
            transport=flow._transport if explicit_sid else None,
            explicit_session_id=explicit_sid,
        )

    kwargs.setdefault("yield_only_step_completed", False)
    return EventIterator(flow, **kwargs)


def make_steps_iterator(
    flow: AsyncFlow,
    **kwargs: Any,
) -> EventIterator | AsyncIterator[StreamEvent]:
    """Construct an iterator yielding only :class:`StepCompleted` events.

    In REPLAY mode, delegates to the matcher (which reconstructs full SSE);
    the StepCompleted filter is applied downstream via the iterator option.
    Unified Q1 rule applies — see :func:`make_events_iterator`.
    """
    from ._trace_scope import _current_scope
    from .replay._state import ScopeMode

    scope = _current_scope()
    in_replay = scope is not None and scope.mode is ScopeMode.REPLAY
    if in_replay:
        assert scope is not None  # guaranteed by in_replay check above
        from .replay.matcher import match_events_async

        user_session_id = kwargs.get("session_id")
        explicit_sid = (
            user_session_id
            if (user_session_id is not None and user_session_id != scope.session_id)
            else None
        )
        return match_events_async(
            scope=scope,
            org=flow._org,
            project=flow._project,
            slug=flow._slug,
            message=kwargs.get("message"),
            transport=flow._transport if explicit_sid else None,
            explicit_session_id=explicit_sid,
        )

    kwargs.setdefault("yield_only_step_completed", True)
    # steps() does not support run_remaining (per phase 2 interface).
    kwargs.setdefault("run_remaining", False)
    return EventIterator(flow, **kwargs)


# ---------------------------------------------------------------------------
# Sync step iterator
# ---------------------------------------------------------------------------

SyncToolHandler = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


class SyncEventIterator:
    """Sync iterator driving ``/seq/.../step`` end-to-end.

    Mirror of :class:`EventIterator` with sync semantics. Uses
    :func:`parse_sse_stream_sync` and a plain generator as the driver (no
    ``async``/``await``).

    Manual ``.resume()`` is NOT supported on the sync client — async tool
    handlers are rejected at construction time. Auto-resume (via
    ``tool_handler=``) is fully supported.

    Use via :meth:`Flow.events` (yields every event) or
    :meth:`Flow.steps` (yields only :class:`StepCompleted`).
    """

    def __init__(
        self,
        flow: Flow,
        *,
        message: str | None,
        parameters: dict[str, Any] | None,
        block_overrides: dict[str, dict[str, Any]] | None,
        input_overrides: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None,
        run_remaining: bool,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any | None,
        tool_handler: SyncToolHandler | None,
        max_tool_rounds: int | None,
        trace: bool,
        version: VersionSpec,
        yield_only_step_completed: bool,
        session_id: str | None = None,
    ) -> None:
        if tool_handler is not None and inspect.iscoroutinefunction(tool_handler):
            raise TypeError(
                "Sync client cannot use async tool_handler. Use AsyncNoukai for async handlers."
            )

        self._flow = flow
        self._message = message
        self._parameters = parameters
        self._block_overrides = block_overrides
        self._input_overrides = input_overrides
        self._attachments = attachments
        self._run_remaining = run_remaining
        self._tools = tools
        self._tool_choice = tool_choice
        self._tool_handler = tool_handler
        self._max_rounds = (
            max_tool_rounds if max_tool_rounds is not None else DEFAULT_MAX_TOOL_ROUNDS
        )
        self._trace = trace
        self._version = version
        self._yield_only_step_completed = yield_only_step_completed
        self._session_id = session_id

        # Cursor state — mutated only in _drive_sync.
        self._execution_id: str | None = None
        self._accumulated_outputs: dict[str, Any] = {}
        self._step_index: int = 0
        self._tool_rounds: int = 0

        # Pending tool-call state.
        self._pending_tool_messages: list[dict[str, Any]] | None = None
        self._pending_iterations_used: int = 0

        # The driver (plain generator). Lazily started.
        self._driver: Iterator[StreamEvent] = self._drive_sync()

    def __iter__(self) -> SyncEventIterator:
        return self

    def __next__(self) -> StreamEvent:
        return next(self._driver)

    # --- internals --------------------------------------------------------

    def _build_request(self) -> StepRequest:
        """Build the next ``/step`` request body from current cursor state."""
        is_first_call = (
            self._step_index == 0
            and self._execution_id is None
            and self._pending_tool_messages is None
        )
        return StepRequest(
            execution_id=self._execution_id,
            step_index=self._step_index,
            accumulated_outputs=dict(self._accumulated_outputs),
            message=self._message if is_first_call else None,
            parameters=(self._parameters or {}) if is_first_call else {},
            attachments=self._attachments if is_first_call else None,
            input_overrides=self._input_overrides or {},
            block_overrides=self._block_overrides,
            run_remaining=self._run_remaining,
            tools=self._tools,
            tool_choice=self._tool_choice,
            tool_call_messages=self._pending_tool_messages,
            iterations_used=self._pending_iterations_used,
            trace=self._trace,
        )

    def _drive_sync(self) -> Iterator[StreamEvent]:
        """Main driver: loop over ``/step`` calls until terminal state."""
        flow_complete = False

        # Resolve effective session id for X-Session-Id injection. Same
        # precedence as :class:`EventIterator._drive` — see comment there.
        from ._trace_scope import _current_scope

        scope = _current_scope()
        effective_sid: str | None = None
        if self._session_id is not None:
            effective_sid = self._session_id
        elif self._flow._transport._default_session_id is not None:
            effective_sid = self._flow._transport._default_session_id
        elif scope is not None:
            effective_sid = scope.session_id

        extra_headers: dict[str, str] | None = None
        if effective_sid is not None:
            extra_headers = {HEADER_SESSION_ID: effective_sid}

        while not flow_complete:
            req = self._build_request()
            # Clear pending tool state once baked into the request.
            self._pending_tool_messages = None
            self._pending_iterations_used = 0

            url = f"{self._flow._versioned_path(self._version)}/step"
            byte_stream = self._flow._transport.stream(
                "POST", url, json=req, extra_headers=extra_headers
            )

            reissue = False

            for event in parse_sse_stream_sync(byte_stream):
                if isinstance(event, RunStarted):
                    if event.execution_id:
                        self._execution_id = event.execution_id
                    if not self._yield_only_step_completed:
                        yield event

                elif isinstance(event, StepCompleted):
                    self._accumulated_outputs[event.step_id] = event.output
                    self._step_index += 1
                    yield event  # always surfaced

                elif isinstance(event, ToolCallsRequired):
                    self._execution_id = event.execution_id
                    self._step_index = event.step_index
                    self._accumulated_outputs.update(event.accumulated_outputs)

                    if self._tool_handler is not None:
                        self._tool_rounds += 1
                        if self._tool_rounds > self._max_rounds:
                            raise ToolCallLimitError(
                                f"Tool call loop exceeded max_tool_rounds={self._max_rounds}",
                                code="TOOL_CALL_LIMIT_CLIENT",
                                execution_id=event.execution_id,
                            )
                        result = self._tool_handler(event.tool_calls)
                        new_messages = list(event.tool_call_messages) + list(result)
                        _check_tool_messages_size(len(new_messages))
                        self._pending_tool_messages = new_messages
                        self._pending_iterations_used = event.iterations_used
                        reissue = True
                        break

                    # Manual resume is not supported on the sync client.
                    # Yield the event (so callers can at least see it) and stop.
                    yield event
                    return

                elif isinstance(event, StepPaused):
                    if not self._yield_only_step_completed:
                        yield event
                    reissue = True
                    break

                elif isinstance(event, StepFailed):
                    yield event
                    flow_complete = True
                    break

                elif isinstance(event, FlowCompleted):
                    if not self._yield_only_step_completed:
                        yield event
                    flow_complete = True
                    break

                else:
                    # StepStarted, StepInput, StepOutput, etc.
                    if not self._yield_only_step_completed:
                        yield event

            if flow_complete:
                return

            if not reissue:
                return


def make_sync_events_iterator(
    flow: Flow,
    **kwargs: Any,
) -> SyncEventIterator | Iterator[StreamEvent]:
    """Construct a sync iterator yielding every typed event.

    In REPLAY mode, always dispatches to the matcher's sync generator. See
    :func:`make_events_iterator` for the unified Q1 rule.
    """
    from ._trace_scope import _current_scope
    from .replay._state import ScopeMode

    scope = _current_scope()
    in_replay = scope is not None and scope.mode is ScopeMode.REPLAY
    if in_replay:
        assert scope is not None  # guaranteed by in_replay check above
        from .replay.matcher import match_events_sync

        user_session_id = kwargs.get("session_id")
        explicit_sid = (
            user_session_id
            if (user_session_id is not None and user_session_id != scope.session_id)
            else None
        )
        return match_events_sync(
            scope=scope,
            org=flow._org,
            project=flow._project,
            slug=flow._slug,
            message=kwargs.get("message"),
            transport=flow._transport if explicit_sid else None,
            explicit_session_id=explicit_sid,
        )

    kwargs.setdefault("yield_only_step_completed", False)
    return SyncEventIterator(flow, **kwargs)


def make_sync_steps_iterator(
    flow: Flow,
    **kwargs: Any,
) -> SyncEventIterator | Iterator[StreamEvent]:
    """Construct a sync iterator yielding only :class:`StepCompleted` events.

    In REPLAY mode, delegates to the matcher's sync generator. Unified
    Q1 rule applies — see :func:`make_events_iterator`.
    """
    from ._trace_scope import _current_scope
    from .replay._state import ScopeMode

    scope = _current_scope()
    in_replay = scope is not None and scope.mode is ScopeMode.REPLAY
    if in_replay:
        assert scope is not None  # guaranteed by in_replay check above
        from .replay.matcher import match_events_sync

        user_session_id = kwargs.get("session_id")
        explicit_sid = (
            user_session_id
            if (user_session_id is not None and user_session_id != scope.session_id)
            else None
        )
        return match_events_sync(
            scope=scope,
            org=flow._org,
            project=flow._project,
            slug=flow._slug,
            message=kwargs.get("message"),
            transport=flow._transport if explicit_sid else None,
            explicit_session_id=explicit_sid,
        )

    kwargs.setdefault("yield_only_step_completed", True)
    kwargs.setdefault("run_remaining", False)
    return SyncEventIterator(flow, **kwargs)
