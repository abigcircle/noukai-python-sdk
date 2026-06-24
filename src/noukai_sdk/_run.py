"""Run proxy. Returned by `flow.run(execution_id)`. Factory for trace
operations on a single execution."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from typing import TYPE_CHECKING, Any, Literal

from ._models.events import StreamEvent
from ._models.trace import StepAttempts, StepTrace, Trace
from ._paths import run_path
from ._streaming import parse_sse_stream, parse_sse_stream_sync

if TYPE_CHECKING:
    from ._transport import AsyncTransport, SyncTransport

AttemptSpec = Literal["latest", "all"] | int


class Run:
    """Synchronous run proxy.

    Returned by ``Flow.run(execution_id)``. Binds a specific execution ID
    and exposes trace retrieval operations. No network call is made at
    construction time.
    """

    def __init__(
        self,
        transport: SyncTransport,
        org: str,
        project: str,
        slug: str,
        execution_id: str,
    ) -> None:
        self._transport = transport
        self._org = org
        self._project = project
        self._slug = slug
        self._execution_id = execution_id

    @property
    def execution_id(self) -> str:
        """The execution ID this proxy is bound to."""
        return self._execution_id

    @property
    def _base_path(self) -> str:
        return run_path(self._org, self._project, self._slug, self._execution_id)

    def trace(self, *, timeout: float | None = None) -> Trace:
        """Fetch the whole-run trace with the latest attempt per step.

        Wraps ``GET /seq/{org}/{project}/{slug}/runs/{execution_id}/trace``.

        Args:
            timeout: Per-request timeout override (seconds).

        Returns:
            A ``Trace`` containing a ``RunSummary`` and a list of
            ``StepTrace`` objects, one per step (latest attempt each).

        Raises:
            FlowNotFoundError: execution_id not found.
            APIConnectionError: network failure.
            APITimeoutError: request timed out.
        """
        resp = self._transport.request("GET", f"{self._base_path}/trace", timeout=timeout)
        return Trace.model_validate(resp.body)

    def step_trace(
        self,
        step_id: str,
        *,
        attempt: AttemptSpec = "latest",
        loop_index: int | None = None,
        timeout: float | None = None,
    ) -> StepTrace | StepAttempts:
        """Fetch per-step trace, optionally across multiple attempts.

        With ``attempt="latest"`` (default) returns one ``StepTrace``.
        With ``attempt="all"`` returns a ``StepAttempts`` collection.
        With an integer N returns the specific attempt.

        Args:
            step_id: The step identifier to fetch trace data for.
            attempt: ``"latest"`` (default) returns the most recent attempt.
                ``"all"`` returns all attempts as ``StepAttempts``.
                An integer N returns that specific attempt number.
            loop_index: Optional loop iteration index for steps inside loops.
            timeout: Per-request timeout override (seconds).

        Returns:
            ``StepTrace`` for a single attempt, or ``StepAttempts`` when
            ``attempt="all"``.

        Raises:
            FlowNotFoundError: execution_id or step_id not found.
            APIConnectionError: network failure.
            APITimeoutError: request timed out.
        """
        params: dict[str, Any] = {}
        if attempt != "latest":
            params["attempt"] = str(attempt)
        if loop_index is not None:
            params["loop_index"] = str(loop_index)
        url = f"{self._base_path}/steps/{step_id}/trace"
        resp = self._transport.request("GET", url, params=params or None, timeout=timeout)
        body = resp.body or {}
        if attempt == "all":
            return StepAttempts.model_validate(body)
        return StepTrace.model_validate(body)

    def live_trace(self) -> Generator[StreamEvent, None, None]:
        """Stream live trace events. Replays from DB then live-tails Redis.

        Wraps ``GET /seq/{org}/{project}/{slug}/runs/{execution_id}/trace/stream``.
        Iterator terminates when the run reaches a terminal state and the
        server closes the connection.

        Yields:
            ``StreamEvent`` union — typed SSE events replayed from DB then
            live-tailed from Redis until the run terminates.

        Raises:
            FlowNotFoundError: execution_id not found.
            APIConnectionError: network failure.
        """
        url = f"{self._base_path}/trace/stream"
        byte_stream = self._transport.stream("GET", url)
        yield from parse_sse_stream_sync(byte_stream)


class AsyncRun:
    """Async run proxy.

    Returned by ``AsyncFlow.run(execution_id)``. Binds a specific execution
    ID and exposes async trace retrieval operations. No network call is made
    at construction time.
    """

    def __init__(
        self,
        transport: AsyncTransport,
        org: str,
        project: str,
        slug: str,
        execution_id: str,
    ) -> None:
        self._transport = transport
        self._org = org
        self._project = project
        self._slug = slug
        self._execution_id = execution_id

    @property
    def execution_id(self) -> str:
        """The execution ID this proxy is bound to."""
        return self._execution_id

    @property
    def _base_path(self) -> str:
        return run_path(self._org, self._project, self._slug, self._execution_id)

    async def trace(self, *, timeout: float | None = None) -> Trace:
        """Fetch the whole-run trace with the latest attempt per step.

        Wraps ``GET /seq/{org}/{project}/{slug}/runs/{execution_id}/trace``.

        Args:
            timeout: Per-request timeout override (seconds).

        Returns:
            A ``Trace`` containing a ``RunSummary`` and a list of
            ``StepTrace`` objects, one per step (latest attempt each).

        Raises:
            FlowNotFoundError: execution_id not found.
            APIConnectionError: network failure.
            APITimeoutError: request timed out.
        """
        resp = await self._transport.request("GET", f"{self._base_path}/trace", timeout=timeout)
        return Trace.model_validate(resp.body)

    async def step_trace(
        self,
        step_id: str,
        *,
        attempt: AttemptSpec = "latest",
        loop_index: int | None = None,
        timeout: float | None = None,
    ) -> StepTrace | StepAttempts:
        """Fetch per-step trace, optionally across multiple attempts.

        Args:
            step_id: The step identifier to fetch trace data for.
            attempt: ``"latest"`` (default) returns the most recent attempt.
                ``"all"`` returns all attempts as ``StepAttempts``.
                An integer N returns that specific attempt number.
            loop_index: Optional loop iteration index for steps inside loops.
            timeout: Per-request timeout override (seconds).

        Returns:
            ``StepTrace`` for a single attempt, or ``StepAttempts`` when
            ``attempt="all"``.

        Raises:
            FlowNotFoundError: execution_id or step_id not found.
            APIConnectionError: network failure.
            APITimeoutError: request timed out.
        """
        params: dict[str, Any] = {}
        if attempt != "latest":
            params["attempt"] = str(attempt)
        if loop_index is not None:
            params["loop_index"] = str(loop_index)
        url = f"{self._base_path}/steps/{step_id}/trace"
        resp = await self._transport.request("GET", url, params=params or None, timeout=timeout)
        body = resp.body or {}
        if attempt == "all":
            return StepAttempts.model_validate(body)
        return StepTrace.model_validate(body)

    async def live_trace(self) -> AsyncGenerator[StreamEvent, None]:
        """Stream live trace events asynchronously.

        Replays from DB then live-tails Redis until the run terminates.
        The server closes the connection when the run reaches a terminal state.

        Yields:
            ``StreamEvent`` union — typed SSE events.

        Raises:
            FlowNotFoundError: execution_id not found.
            APIConnectionError: network failure.
        """
        url = f"{self._base_path}/trace/stream"
        byte_stream = self._transport.stream("GET", url)
        async for event in parse_sse_stream(byte_stream):
            yield event
