"""Incoming HTTP response bodies (non-streaming). Mirror server response
models with snake_case access, camelCase wire."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from ._aliases import WIRE_CONFIG


class ExecuteResult(BaseModel):
    """Successful POST /execute response."""

    model_config = WIRE_CONFIG

    status: Literal["completed", "failed"]
    result: Any = None
    flow_id: str = Field(alias="flowId")
    block_count: int = Field(alias="blockCount")
    execution_id: str | None = Field(default=None, alias="executionId")

    # Non-wire: injected by Flow.execute after parsing. Excluded from
    # serialization because PrivateAttr is not part of model schema.
    _session_id: str | None = PrivateAttr(default=None)

    @property
    def output(self) -> Any:
        """Alias for .result. More natural read in user code."""
        return self.result

    @property
    def session_id(self) -> str | None:
        """The session_id this execution was tagged with (capture mode) or
        replayed from (replay mode). None when called outside a trace scope."""
        return self._session_id

    @property
    def requires_tool_calls(self) -> bool:
        return False


class PausedResult(BaseModel):
    """POST /execute response when blocked for tool calls."""

    model_config = WIRE_CONFIG

    status: Literal["tool_calls_required"] = "tool_calls_required"
    execution_id: str = Field(alias="executionId")
    paused_at_step: str = Field(alias="pausedAtStep")
    iterations_used: int = Field(alias="iterationsUsed")
    tool_call_messages: list[dict[str, Any]] = Field(alias="toolCallMessages")
    tool_calls: list[dict[str, Any]] = Field(alias="toolCalls")
    accumulated_outputs: dict[str, Any] = Field(default_factory=dict, alias="accumulatedOutputs")
    flow_id: str = Field(alias="flowId")
    block_count: int = Field(alias="blockCount")

    # Private: injected by _attach_resume / _attach_resume_sync; excluded
    # from serialization.
    _resume: Callable[..., Awaitable[ExecuteResult | PausedResult]] | None = PrivateAttr(
        default=None
    )
    _resume_sync: Callable[..., ExecuteResult | PausedResult] | None = PrivateAttr(default=None)

    # Non-wire: injected by Flow.execute after parsing (Phase 5).
    _session_id: str | None = PrivateAttr(default=None)

    @property
    def session_id(self) -> str | None:
        """The session_id active when this paused result was produced.
        None when called outside a trace scope."""
        return self._session_id

    @property
    def requires_tool_calls(self) -> bool:
        return True

    async def resume(self, *, tool_results: list[dict[str, Any]]) -> ExecuteResult | PausedResult:
        """Send tool results back to the server and continue execution.

        Async-only. For sync callers, use :meth:`resume_sync` (or pass a
        ``tool_handler`` to ``flow.execute()`` and let the SDK auto-loop).

        Args:
            tool_results: A list of ``{"role": "tool", "tool_call_id": ...,
                "content": ...}`` messages produced by the caller.

        Returns:
            ``ExecuteResult`` when the run completes, or another
            ``PausedResult`` if the model requests more tool calls.

        Raises:
            RuntimeError: If called on a manually constructed ``PausedResult``
                (i.e. not returned from ``flow.execute()``).
            TypeError: If this ``PausedResult`` came from the sync ``Noukai``
                client. Use :meth:`resume_sync` instead.
        """
        if self._resume is None:
            if self._resume_sync is not None:
                raise TypeError(
                    "PausedResult.resume() is async-only. This result came from "
                    "the sync Noukai client — call .resume_sync(tool_results=...) "
                    "instead, or pass tool_handler= to flow.execute() to let the "
                    "SDK auto-loop."
                )
            raise RuntimeError(
                "PausedResult.resume() is only usable on results returned by "
                "flow.execute(). Cannot resume a manually constructed PausedResult."
            )
        return await self._resume(tool_results=tool_results)

    def resume_sync(self, *, tool_results: list[dict[str, Any]]) -> ExecuteResult | PausedResult:
        """Synchronous mid-execution resume (sync client only).

        Mirror of :meth:`resume` for the sync ``Noukai`` client. Returns the
        next ``ExecuteResult`` or ``PausedResult`` directly (no ``await``).

        Raises:
            RuntimeError: If called on a manually constructed ``PausedResult``.
            TypeError: If this ``PausedResult`` came from ``AsyncNoukai``. Use
                :meth:`resume` and ``await`` it instead.
        """
        if self._resume_sync is None:
            if self._resume is not None:
                raise TypeError(
                    "PausedResult.resume_sync() is for the sync client. This "
                    "result came from AsyncNoukai — use `await paused.resume(...)`."
                )
            raise RuntimeError(
                "PausedResult.resume_sync() is only usable on results returned "
                "by flow.execute(). Cannot resume a manually constructed PausedResult."
            )
        return self._resume_sync(tool_results=tool_results)


class JobAccepted(BaseModel):
    """POST /jobs response (initial)."""

    model_config = WIRE_CONFIG

    execution_id: str = Field(alias="executionId")
    status: str
    flow_id: str = Field(alias="flowId")
    block_count: int = Field(alias="blockCount")

    # Non-wire: injected by Flow.execute_async after parsing (Phase 5).
    _session_id: str | None = PrivateAttr(default=None)

    @property
    def session_id(self) -> str | None:
        """The session_id active when this job was submitted.
        None when called outside a trace scope."""
        return self._session_id


class JobStatus(BaseModel):
    """GET /jobs/{id} response."""

    model_config = WIRE_CONFIG

    execution_id: str = Field(alias="executionId")
    status: Literal["pending", "running", "completed", "failed"]
    result: Any = None
    error: str | None = None
