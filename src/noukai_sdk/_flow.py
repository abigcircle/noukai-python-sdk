"""Bound flow proxy. Returned by `client.flow(slug)`. Factory for execution
and trace operations on one specific flow."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any, Literal, cast

from ._constants import DEFAULT_MAX_TOOL_ROUNDS, HEADER_SESSION_ID
from ._jobs import AsyncJob, Job
from ._models.events import StepCompleted, StreamEvent
from ._models.requests import ExecuteRequest
from ._models.responses import ExecuteResult, JobAccepted, PausedResult
from ._paths import flow_base, flow_execute_path, flow_jobs_submit_path
from ._run import AsyncRun, Run
from ._step_iterator import (
    make_events_iterator,
    make_steps_iterator,
    make_sync_events_iterator,
    make_sync_steps_iterator,
)
from ._tool_calls import (
    _attach_resume,
    _attach_resume_sync,
    _auto_resume_loop,
    _auto_resume_loop_sync,
)
from ._trace_scope import _current_scope
from .replay._state import ScopeMode

if TYPE_CHECKING:
    from ._transport import AsyncTransport, SyncTransport
    from .replay._state import ScopeState

# Tool-call handler signatures
ToolHandler = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
AsyncToolHandler = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]

# Tool choice. Matches OpenAI's tool_choice envelope.
#   "auto" | "none" | "required" | {"type": "function", "function": {"name": "..."}}
# Typed loosely on the dict side because callers may include provider-specific
# extra fields; the SDK forwards the value to the server unchanged.
ToolChoice = Literal["auto", "none", "required"] | dict[str, Any]

# Flow version selector
VersionSpec = str | int  # "draft" | "production" | <int>


class Flow:
    """Synchronous bound flow proxy.

    Returned by ``Noukai.flow(slug)``. Binds a specific org/project/slug
    triple and exposes execution and trace operations. No network call is
    made at construction time.

    Note on manual tool-call resume: ``Flow.execute()`` supports
    ``tool_handler=`` (auto-resume mode) for synchronous tool handlers.
    Manual mid-execution resume via ``PausedResult.resume()`` is only
    available on ``AsyncFlow``; sync users should use ``tool_handler=``
    for automatic looping.
    """

    def __init__(
        self,
        transport: SyncTransport,
        org: str,
        project: str,
        slug: str,
    ) -> None:
        self._transport = transport
        self._org = org
        self._project = project
        self._slug = slug

    @property
    def org(self) -> str:
        """Organisation identifier extracted from the slug."""
        return self._org

    @property
    def project(self) -> str:
        """Project identifier extracted from the slug."""
        return self._project

    @property
    def slug(self) -> str:
        """Flow slug (short name within the project)."""
        return self._slug

    @property
    def _base_path(self) -> str:
        return flow_base(self._org, self._project, self._slug)

    def _versioned_path(self, version: VersionSpec) -> str:
        """Build the URL path segment for the given version.

        - ``"draft"``      → ``/seq/{org}/{project}/{slug}``
        - ``<int>``        → ``/seq/{org}/{project}/{slug}/v{N}``
        - ``"production"`` → raises ``NotImplementedError`` (server contract
          not yet finalised; reserved for a future SDK release).

        Delegates to :func:`_paths.flow_base` so the central audit file
        owns the wire shape.
        """
        return flow_base(self._org, self._project, self._slug, self._path_version(version))

    def _path_version(self, version: VersionSpec) -> str | int:
        """Normalize a ``VersionSpec`` into the int-or-"draft" form the
        ``_paths`` helpers expect, validating ``"production"`` up-front.

        Raises:
            NotImplementedError: ``version="production"`` is not yet supported.
            ValueError: any other unrecognised version.
        """
        if isinstance(version, int):
            return version
        if version == "draft":
            return "draft"
        if version == "production":
            raise NotImplementedError(
                'flow.execute(version="production") is not yet supported in the SDK. '
                'Pin to an integer version (e.g. version=3) or use the default "draft". '
                "A future release will land this once the server contract is finalized."
            )
        raise ValueError(f"Invalid version: {version!r}")

    def _resolve_session_id(
        self,
        session_id: str | None,
        scope: ScopeState | None,
    ) -> str | None:
        """Resolve the effective session id used for ``X-Session-Id`` injection.

        Precedence (highest first):

        1. per-call ``session_id`` kwarg
        2. transport ``_default_session_id`` (set on the client at construction)
        3. surrounding ``trace_scope`` contextvar

        Uses ``is not None`` chains so an explicit empty-string ``session_id``
        is not silently overridden by the next tier.
        """
        if session_id is not None:
            return session_id
        if self._transport._default_session_id is not None:
            return self._transport._default_session_id
        if scope is not None:
            return scope.session_id
        return None

    def execute(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        timeout: float | None = None,
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> ExecuteResult | PausedResult:
        """Synchronous in-process flow execution.

        If ``tool_handler`` is given AND the server returns a paused
        ``PausedResult``, the SDK loops: it invokes ``tool_handler`` with
        the requested tool calls, resumes the execution with the results,
        and continues until the run completes or ``max_tool_rounds`` is
        exhausted. With ``tool_handler=None``, the paused state is returned
        for the caller to drive manually (note: manual ``.resume()`` requires
        the async client — see ``AsyncFlow.execute``).

        Async tool handlers are rejected at call time with ``TypeError``
        rather than failing at ``await`` time.

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            attachments: Up to 10 media attachments (HTTPS URLs + MIME types).
            tools: Tool definitions in OpenAI format. Max 64.
            tool_choice: ``"auto" | "none" | "required" | {"type": "function",
                "function": {"name": ...}}``.
            tool_handler: Optional sync callable that takes a list of pending
                tool calls and returns a list of tool results. SDK loops
                until the run completes if provided. Async callables are
                rejected — use ``AsyncNoukai`` for async handlers.
            max_tool_rounds: Safety bound on the tool-handler loop.
                Defaults to ``DEFAULT_MAX_TOOL_ROUNDS`` (10). Raises
                ``ToolCallLimitError`` if exceeded.
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default) or a positive int published version
                number. ``"production"`` is reserved for a future release and
                raises ``NotImplementedError`` when passed.
            timeout: Per-request timeout override (seconds).
            session_id: Explicit session id override. Precedence (highest first):
                this kwarg > client-level default > active trace_scope contextvar
                > None. Silently ignored in Phase 2; wired in Phase 5 (capture)
                and Phase 6 (replay).

        Returns:
            ``ExecuteResult`` on completion, or ``PausedResult`` if
            ``tool_handler`` was omitted and the server paused for tools.

        Raises:
            TypeError: if ``tool_handler`` is an async function.
            NotImplementedError: if ``version="production"`` is passed.
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
            FlowExecutionError: server-side execution failure (5xx). Check
                ``e.code`` for specific reasons.
            ToolCallLimitError: ``max_tool_rounds`` exhausted.
        """
        if tool_handler is not None and inspect.iscoroutinefunction(tool_handler):
            raise TypeError(
                "Sync client cannot use async tool_handler. Use AsyncNoukai for async handlers."
            )

        scope = _current_scope()

        # Precedence: explicit kwarg > transport default > contextvar.
        # Use `is not None` chains so an explicit empty-string session_id
        # is not silently overridden by the next tier (consistent with the
        # Node SDK's ?? operator).
        effective_sid = self._resolve_session_id(session_id, scope)

        # REPLAY dispatch (unified Q1 rule — always go to matcher in replay;
        # the matcher handles the explicit-sid-differs case via one-shot fetch).
        if scope is not None and scope.mode is ScopeMode.REPLAY:
            from .replay.matcher import match_execute_sync
            explicit_sid = (
                session_id
                if (session_id is not None and session_id != scope.session_id)
                else None
            )
            return match_execute_sync(
                scope=scope,
                org=self._org,
                project=self._project,
                slug=self._slug,
                message=message,
                transport=self._transport if explicit_sid is not None else None,
                explicit_session_id=explicit_sid,
            )

        req = ExecuteRequest(
            message=message,
            parameters=parameters or {},
            block_overrides=block_overrides,
            attachments=attachments,
            tools=tools,
            tool_choice=tool_choice,
            trace=trace,
        )
        url = flow_execute_path(self._org, self._project, self._slug, self._path_version(version))

        # Inject X-Session-Id header when a session_id is active.
        extra_headers: dict[str, str] = {}
        if effective_sid is not None:
            extra_headers[HEADER_SESSION_ID] = effective_sid

        resp = self._transport.request(
            "POST", url, json=req, timeout=timeout, extra_headers=extra_headers or None
        )
        raw_body = resp.body or {}
        body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}

        if body.get("status") == "tool_calls_required":
            paused = PausedResult.model_validate(body)
            paused._session_id = effective_sid
            paused = _attach_resume_sync(
                paused,
                self,
                parameters,
                block_overrides,
                attachments,
                tools,
                tool_choice,
                trace,
                version,
                timeout,
            )
            if tool_handler is not None:
                return _auto_resume_loop_sync(
                    paused,
                    tool_handler,
                    max_tool_rounds if max_tool_rounds is not None else DEFAULT_MAX_TOOL_ROUNDS,
                )
            return paused

        result = ExecuteResult.model_validate(body)
        result._session_id = effective_sid
        return result

    def execute_async(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        timeout: float | None = None,
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> Job:
        """Submit an async (queue-backed) execution. Returns immediately.

        Note: the ``_async`` suffix refers to the SERVER-SIDE execution model
        (queue-backed), not the Python client semantics. This method is a
        regular blocking call that returns a ``Job`` handle.

        Use ``.wait(timeout=)`` on the returned ``Job`` to block until
        completion (polls under the hood), or ``.poll()`` to check status
        without waiting. Tool calls are NOT supported on this path
        (server-side limitation).

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default), ``"production"``, or a positive
                int published version number.
            timeout: Per-request timeout override for the submission call
                (seconds). Does not affect the async execution itself.

        Returns:
            A ``Job`` handle for polling or waiting on the execution result.

        Raises:
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
        """
        scope = _current_scope()
        effective_sid = self._resolve_session_id(session_id, scope)

        # R10: execute_async() / jobs are not supported in replay mode v1.
        if scope is not None and scope.mode is ScopeMode.REPLAY:
            from ._errors import ReplayMissError

            raise ReplayMissError(
                "Replay does not support execute_async() / jobs in v1. "
                "Use execute() or events()/steps() for replay-mode calls."
            )

        req = ExecuteRequest(
            message=message,
            parameters=parameters or {},
            block_overrides=block_overrides,
            trace=trace,
        )
        url = flow_jobs_submit_path(
            self._org, self._project, self._slug, self._path_version(version)
        )

        extra_headers: dict[str, str] = {}
        if effective_sid is not None:
            extra_headers[HEADER_SESSION_ID] = effective_sid

        resp = self._transport.request(
            "POST", url, json=req, timeout=timeout, extra_headers=extra_headers or None
        )
        accepted = JobAccepted.model_validate(resp.body)
        accepted._session_id = effective_sid
        return Job(
            transport=self._transport,
            org=self._org,
            project=self._project,
            slug=self._slug,
            execution_id=accepted.execution_id,
            flow_id=accepted.flow_id,
        )

    def steps(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        input_overrides: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> Iterator[StepCompleted]:
        """Iterate the flow step by step, yielding one ``StepCompleted`` per
        finished step.

        SDK owns the ``execution_id + accumulated_outputs`` cursor — caller
        just iterates. Tool-call pauses are handled the same way as
        ``execute(tool_handler=...)``.

        For full visibility into every event (including ``StepInput``,
        ``StepOutput``, ``StepPaused``), use ``flow.events(...)`` instead.

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            input_overrides: Low-level per-step input overrides.
            tools: Tool definitions in OpenAI format. Max 64.
            tool_choice: ``"auto" | "none" | "required" | {"type": "function",
                "function": {"name": ...}}``.
            tool_handler: Optional sync callable that handles tool calls
                transparently within the iteration loop.
            max_tool_rounds: Safety bound on the tool-handler loop.
                Defaults to ``DEFAULT_MAX_TOOL_ROUNDS`` (10).
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default), ``"production"``, or a positive
                int published version number.

        Yields:
            ``StepCompleted`` events, one per finished flow step.

        Raises:
            TypeError: if ``tool_handler`` is an async function.
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
            FlowExecutionError: server-side execution failure.
            ToolCallLimitError: ``max_tool_rounds`` exhausted.
        """
        # Pass the user-explicit session_id (may be None) — the iterator
        # resolves the effective sid against the scope + client default
        # at request-build time.
        return cast(
            "Iterator[StepCompleted]",
            make_sync_steps_iterator(
                self,
                message=message,
                parameters=parameters,
                block_overrides=block_overrides,
                input_overrides=input_overrides,
                attachments=attachments,
                tools=tools,
                tool_choice=tool_choice,
                tool_handler=tool_handler,
                max_tool_rounds=max_tool_rounds,
                trace=trace,
                version=version,
                session_id=session_id,
            ),
        )

    def events(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        input_overrides: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        run_remaining: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> Iterator[StreamEvent]:
        """Iterate every typed SSE event from the step-through stream.

        With ``run_remaining=True`` the server executes through to the
        end without pausing between steps; with the default ``False`` it
        pauses between steps (the SDK auto-advances the cursor between
        iterations).

        When ``tool_handler`` is given, ``ToolCallsRequired`` events are
        consumed internally and never surface to the caller. Without it,
        the iterator yields ``ToolCallsRequired`` and stops (manual resume
        requires the async client).

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            input_overrides: Low-level per-step input overrides.
            run_remaining: When True, the server runs all remaining steps
                without pausing. Default False (step-by-step).
            tools: Tool definitions in OpenAI format. Max 64.
            tool_choice: ``"auto" | "none" | "required" | {"type": "function",
                "function": {"name": ...}}``.
            tool_handler: Optional sync callable that handles tool calls;
                consumed events are not yielded to caller.
            max_tool_rounds: Safety bound on the tool-handler loop.
                Defaults to ``DEFAULT_MAX_TOOL_ROUNDS`` (10).
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default), ``"production"``, or a positive
                int published version number.

        Yields:
            ``StreamEvent`` union — all typed SSE events from the stream.

        Raises:
            TypeError: if ``tool_handler`` is an async function.
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
            FlowExecutionError: server-side execution failure.
            ToolCallLimitError: ``max_tool_rounds`` exhausted.
        """
        return make_sync_events_iterator(
            self,
            message=message,
            parameters=parameters,
            block_overrides=block_overrides,
            input_overrides=input_overrides,
            attachments=attachments,
            run_remaining=run_remaining,
            tools=tools,
            tool_choice=tool_choice,
            tool_handler=tool_handler,
            max_tool_rounds=max_tool_rounds,
            trace=trace,
            version=version,
            session_id=session_id,
        )

    def run(self, execution_id: str) -> Run:
        """Build a Run proxy for trace operations on an existing execution.

        Args:
            execution_id: The execution ID returned by a previous call to
                ``execute()`` or ``execute_async()``.

        Returns:
            A ``Run`` proxy for fetching trace data on the given execution.
        """
        return Run(self._transport, self._org, self._project, self._slug, execution_id)


class AsyncFlow:
    """Async bound flow proxy. Same surface as Flow with async methods.

    Returned by ``AsyncNoukai.flow(slug)``. Binds a specific org/project/slug
    triple and exposes async execution and trace operations.
    """

    def __init__(
        self,
        transport: AsyncTransport,
        org: str,
        project: str,
        slug: str,
    ) -> None:
        self._transport = transport
        self._org = org
        self._project = project
        self._slug = slug

    @property
    def org(self) -> str:
        """Organisation identifier."""
        return self._org

    @property
    def project(self) -> str:
        """Project identifier."""
        return self._project

    @property
    def slug(self) -> str:
        """Flow slug (short name within the project)."""
        return self._slug

    @property
    def _base_path(self) -> str:
        return flow_base(self._org, self._project, self._slug)

    def _versioned_path(self, version: VersionSpec) -> str:
        """Build the URL path segment for the given version.

        - ``"draft"``      → ``/seq/{org}/{project}/{slug}``
        - ``<int>``        → ``/seq/{org}/{project}/{slug}/v{N}``
        - ``"production"`` → raises ``NotImplementedError`` (server contract
          not yet finalised; reserved for a future SDK release).

        Delegates to :func:`_paths.flow_base` so the central audit file
        owns the wire shape.
        """
        return flow_base(self._org, self._project, self._slug, self._path_version(version))

    def _path_version(self, version: VersionSpec) -> str | int:
        """Normalize a ``VersionSpec`` into the int-or-"draft" form the
        ``_paths`` helpers expect, validating ``"production"`` up-front.
        """
        if isinstance(version, int):
            return version
        if version == "draft":
            return "draft"
        if version == "production":
            raise NotImplementedError(
                'flow.execute(version="production") is not yet supported in the SDK. '
                'Pin to an integer version (e.g. version=3) or use the default "draft". '
                "A future release will land this once the server contract is finalized."
            )
        raise ValueError(f"Invalid version: {version!r}")

    def _resolve_session_id(
        self,
        session_id: str | None,
        scope: ScopeState | None,
    ) -> str | None:
        """Resolve the effective session id used for ``X-Session-Id`` injection.

        Precedence (highest first):

        1. per-call ``session_id`` kwarg
        2. transport ``_default_session_id`` (set on the client at construction)
        3. surrounding ``trace_scope`` contextvar

        Uses ``is not None`` chains so an explicit empty-string ``session_id``
        is not silently overridden by the next tier.
        """
        if session_id is not None:
            return session_id
        if self._transport._default_session_id is not None:
            return self._transport._default_session_id
        if scope is not None:
            return scope.session_id
        return None

    async def execute(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | AsyncToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        timeout: float | None = None,
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> ExecuteResult | PausedResult:
        """Async in-process flow execution.

        Identical semantics to ``Flow.execute``; accepts both sync and async
        ``tool_handler`` callables.

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            attachments: Up to 10 media attachments (HTTPS URLs + MIME types).
            tools: Tool definitions in OpenAI format. Max 64.
            tool_choice: ``"auto" | "none" | "required" | {"type": "function",
                "function": {"name": ...}}``.
            tool_handler: Optional sync or async callable that handles tool
                calls. SDK loops until the run completes if provided.
            max_tool_rounds: Safety bound on the tool-handler loop.
                Defaults to ``DEFAULT_MAX_TOOL_ROUNDS`` (10).
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default) or a positive int published version
                number. ``"production"`` is reserved for a future release and
                raises ``NotImplementedError`` when passed.
            timeout: Per-request timeout override (seconds).

        Returns:
            ``ExecuteResult`` on completion, or ``PausedResult`` if
            ``tool_handler`` was omitted and the server paused for tools.

        Raises:
            NotImplementedError: if ``version="production"`` is passed.
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
            FlowExecutionError: server-side execution failure.
            ToolCallLimitError: ``max_tool_rounds`` exhausted.
        """
        scope = _current_scope()

        effective_sid = self._resolve_session_id(session_id, scope)

        # REPLAY dispatch (unified Q1 rule — always go to matcher in replay;
        # matcher handles explicit-sid-differs case via one-shot fetch).
        if scope is not None and scope.mode is ScopeMode.REPLAY:
            from .replay.matcher import match_execute_async
            explicit_sid = (
                session_id
                if (session_id is not None and session_id != scope.session_id)
                else None
            )
            return await match_execute_async(
                scope=scope,
                org=self._org,
                project=self._project,
                slug=self._slug,
                message=message,
                transport=self._transport if explicit_sid is not None else None,
                explicit_session_id=explicit_sid,
            )

        req = ExecuteRequest(
            message=message,
            parameters=parameters or {},
            block_overrides=block_overrides,
            attachments=attachments,
            tools=tools,
            tool_choice=tool_choice,
            trace=trace,
        )
        url = flow_execute_path(self._org, self._project, self._slug, self._path_version(version))

        # Inject X-Session-Id header when a session_id is active.
        extra_headers: dict[str, str] = {}
        if effective_sid is not None:
            extra_headers[HEADER_SESSION_ID] = effective_sid

        resp = await self._transport.request(
            "POST", url, json=req, timeout=timeout, extra_headers=extra_headers or None
        )
        raw_body = resp.body or {}
        body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}

        if body.get("status") == "tool_calls_required":
            paused = PausedResult.model_validate(body)
            paused._session_id = effective_sid
            paused = _attach_resume(
                paused,
                self,
                parameters,
                block_overrides,
                attachments,
                tools,
                tool_choice,
                trace,
                version,
                timeout,
            )
            if tool_handler is not None:
                return await _auto_resume_loop(
                    paused,
                    tool_handler,
                    max_tool_rounds if max_tool_rounds is not None else DEFAULT_MAX_TOOL_ROUNDS,
                )
            return paused

        result = ExecuteResult.model_validate(body)
        result._session_id = effective_sid
        return result

    async def execute_async(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        timeout: float | None = None,
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> AsyncJob:
        """Submit an async (queue-backed) execution. Awaits only the submission.

        The returned ``AsyncJob`` handle can be polled or awaited for
        completion separately. Tool calls are NOT supported on this path
        (server-side limitation).

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default), ``"production"``, or a positive
                int published version number.
            timeout: Per-request timeout override for the submission call
                (seconds).

        Returns:
            An ``AsyncJob`` handle for polling or waiting on the result.

        Raises:
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
        """
        scope = _current_scope()
        effective_sid = self._resolve_session_id(session_id, scope)

        # R10: execute_async() / jobs are not supported in replay mode v1.
        if scope is not None and scope.mode is ScopeMode.REPLAY:
            from ._errors import ReplayMissError

            raise ReplayMissError(
                "Replay does not support execute_async() / jobs in v1. "
                "Use execute() or events()/steps() for replay-mode calls."
            )

        req = ExecuteRequest(
            message=message,
            parameters=parameters or {},
            block_overrides=block_overrides,
            trace=trace,
        )
        url = flow_jobs_submit_path(
            self._org, self._project, self._slug, self._path_version(version)
        )

        extra_headers: dict[str, str] = {}
        if effective_sid is not None:
            extra_headers[HEADER_SESSION_ID] = effective_sid

        resp = await self._transport.request(
            "POST", url, json=req, timeout=timeout, extra_headers=extra_headers or None
        )
        accepted = JobAccepted.model_validate(resp.body)
        accepted._session_id = effective_sid
        return AsyncJob(
            transport=self._transport,
            org=self._org,
            project=self._project,
            slug=self._slug,
            execution_id=accepted.execution_id,
            flow_id=accepted.flow_id,
        )

    def steps(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        input_overrides: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | AsyncToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> AsyncIterator[StepCompleted]:
        """Async-iterate the flow step by step, yielding one ``StepCompleted``
        per finished step.

        The SDK owns the ``execution_id + accumulated_outputs`` cursor —
        caller just iterates. Tool-call pauses are handled the same way as
        :meth:`AsyncFlow.execute` (auto-resume when ``tool_handler`` is
        provided; otherwise a ``ToolCallsRequired`` event is surfaced via
        :meth:`events` only — ``steps()`` filters it out).

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            input_overrides: Low-level per-step input overrides.
            tools: Tool definitions in OpenAI format. Max 64.
            tool_choice: ``"auto" | "none" | "required" | {"type": "function",
                "function": {"name": ...}}``.
            tool_handler: Optional sync or async callable that handles tool
                calls transparently within the iteration loop.
            max_tool_rounds: Safety bound on the tool-handler loop.
                Defaults to ``DEFAULT_MAX_TOOL_ROUNDS`` (10).
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default), ``"production"``, or a positive
                int published version number.

        Returns:
            An async iterator yielding ``StepCompleted`` events.

        Raises:
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
            FlowExecutionError: server-side execution failure.
            ToolCallLimitError: ``max_tool_rounds`` exhausted.
        """
        # Pass the user-explicit session_id (may be None) — the iterator
        # resolves the effective sid against the scope + client default
        # at request-build time. The iterator filters to only StepCompleted
        # at runtime; the cast narrows the declared type for callers.
        return cast(
            "AsyncIterator[StepCompleted]",
            make_steps_iterator(
                self,
                message=message,
                parameters=parameters,
                block_overrides=block_overrides,
                input_overrides=input_overrides,
                attachments=attachments,
                tools=tools,
                tool_choice=tool_choice,
                tool_handler=tool_handler,
                max_tool_rounds=max_tool_rounds,
                trace=trace,
                version=version,
                session_id=session_id,
            ),
        )

    def events(
        self,
        message: str | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        block_overrides: dict[str, dict[str, Any]] | None = None,
        input_overrides: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        run_remaining: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | AsyncToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
        version: VersionSpec = "draft",
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
    ) -> AsyncIterator[StreamEvent]:
        """Async-iterate every typed SSE event from the step-through stream.

        With ``run_remaining=True`` the server executes through to the end
        without pausing between steps; with the default ``False`` it pauses
        between steps and the SDK auto-advances the cursor between
        iterations.

        When ``tool_handler`` is given, ``ToolCallsRequired`` events are
        consumed internally and never surface to the caller. Without it,
        the iterator yields ``ToolCallsRequired`` for the caller to
        ``await event.resume(tool_results=...)``.

        Args:
            message: User message. Required for fresh calls.
            parameters: Extra initial inputs alongside ``message``.
            block_overrides: Per-step config overrides
                ``{step_id: {field: value}}``.
            input_overrides: Low-level per-step input overrides.
            run_remaining: When True, the server runs all remaining steps
                without pausing. Default False (step-by-step).
            tools: Tool definitions in OpenAI format. Max 64.
            tool_choice: ``"auto" | "none" | "required" | {"type": "function",
                "function": {"name": ...}}``.
            tool_handler: Optional sync or async callable that handles tool
                calls; consumed events are not yielded to caller.
            max_tool_rounds: Safety bound on the tool-handler loop.
                Defaults to ``DEFAULT_MAX_TOOL_ROUNDS`` (10).
            trace: When True, capture full input/output snapshots in trace.
            version: ``"draft"`` (default), ``"production"``, or a positive
                int published version number.

        Returns:
            An async iterator yielding ``StreamEvent`` union events.

        Raises:
            FlowNotFoundError: slug not found.
            InsufficientCreditsError: organisation balance is insufficient.
            FlowExecutionError: server-side execution failure.
            ToolCallLimitError: ``max_tool_rounds`` exhausted.
        """
        return make_events_iterator(
            self,
            message=message,
            parameters=parameters,
            block_overrides=block_overrides,
            input_overrides=input_overrides,
            attachments=attachments,
            run_remaining=run_remaining,
            tools=tools,
            tool_choice=tool_choice,
            tool_handler=tool_handler,
            max_tool_rounds=max_tool_rounds,
            trace=trace,
            version=version,
            session_id=session_id,
        )

    def run(self, execution_id: str) -> AsyncRun:
        """Build an AsyncRun proxy for trace operations on an existing execution.

        Args:
            execution_id: The execution ID returned by a previous call to
                ``execute()`` or ``execute_async()``.

        Returns:
            An ``AsyncRun`` proxy for fetching trace data on the given execution.
        """
        return AsyncRun(self._transport, self._org, self._project, self._slug, execution_id)
