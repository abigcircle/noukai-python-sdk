"""Tool-call resume helpers for AsyncFlow.execute() and Flow.execute().

Four functions:

- ``_attach_resume``: closure-captures the original request params and binds an
  async ``_resume`` callable onto a ``PausedResult``. Used by async client.
- ``_auto_resume_loop``: async driver loop — calls handler, resumes, repeats.
- ``_attach_resume_sync``: sync version of ``_attach_resume``. Binds a sync
  ``_resume_sync`` callable. Used by sync client.
- ``_auto_resume_loop_sync``: sync driver loop.

Async functions use ``await``; sync functions use plain calls. Neither bridges
via ``asyncio.run()``.
"""

from __future__ import annotations

import inspect
import warnings
from typing import TYPE_CHECKING, Any

from ._constants import TOOL_CALL_MESSAGES_SOFT_LIMIT
from ._errors import ToolCallLimitError
from ._models.requests import ExecuteRequest
from ._models.responses import ExecuteResult, PausedResult


def _check_tool_messages_size(message_count: int) -> None:
    """Emit a one-time warning if tool_call_messages grows large.

    Server caps the list size and will reject with ``MESSAGES_TOO_LARGE``.
    Surfacing this client-side gives the caller a chance to compact the
    conversation (e.g. summarise older tool turns) before hitting that wall.
    """
    if message_count > TOOL_CALL_MESSAGES_SOFT_LIMIT:
        warnings.warn(
            f"tool_call_messages has grown to {message_count} entries "
            f"(soft limit {TOOL_CALL_MESSAGES_SOFT_LIMIT}). The server will "
            f"eventually reject this request with MESSAGES_TOO_LARGE. "
            f"Consider compacting tool-call history.",
            ResourceWarning,
            stacklevel=3,
        )


if TYPE_CHECKING:
    from ._flow import AsyncFlow, AsyncToolHandler, Flow, ToolHandler, VersionSpec


def _attach_resume(
    paused: PausedResult,
    flow: AsyncFlow,
    parameters: dict[str, Any] | None,
    block_overrides: dict[str, dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any | None,
    trace: bool,
    version: VersionSpec,
    timeout: float | None,
) -> PausedResult:
    """Bind a ``_resume`` callable onto *paused* and return it.

    The closure captures every parameter from the original ``execute()`` call
    that the server needs on a resume request, plus the transport reference
    through *flow*. This allows the caller to call ``paused.resume(tool_results=...)``
    without re-passing any of those arguments.

    If the resume response is itself a ``PausedResult``, it is recursively
    attached (manual-chaining support).
    """

    async def _resume(*, tool_results: list[dict[str, Any]]) -> ExecuteResult | PausedResult:
        # Append caller-supplied tool results to the existing conversation.
        new_messages = list(paused.tool_call_messages) + list(tool_results)
        _check_tool_messages_size(len(new_messages))
        req = ExecuteRequest(
            # message is intentionally None on resume per server contract.
            message=None,
            parameters=parameters or {},
            block_overrides=block_overrides,
            attachments=attachments,
            tools=tools,
            tool_choice=tool_choice,
            # Carry all resume-state fields from the paused result.
            execution_id=paused.execution_id,
            paused_at_step=paused.paused_at_step,
            iterations_used=paused.iterations_used,
            tool_call_messages=new_messages,
            accumulated_outputs=paused.accumulated_outputs,
            trace=trace,
        )
        url = f"{flow._versioned_path(version)}/execute"
        resp = await flow._transport.request("POST", url, json=req, timeout=timeout)
        body: dict[str, Any] = resp.body if isinstance(resp.body, dict) else {}

        if body.get("status") == "tool_calls_required":
            next_paused = PausedResult.model_validate(body)
            return _attach_resume(
                next_paused,
                flow,
                parameters,
                block_overrides,
                attachments,
                tools,
                tool_choice,
                trace,
                version,
                timeout,
            )

        return ExecuteResult.model_validate(body)

    paused._resume = _resume
    return paused


def _attach_resume_sync(
    paused: PausedResult,
    flow: Flow,
    parameters: dict[str, Any] | None,
    block_overrides: dict[str, dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any | None,
    trace: bool,
    version: VersionSpec,
    timeout: float | None,
) -> PausedResult:
    """Bind a sync ``_resume_sync`` callable onto *paused* and return it.

    Sync mirror of ``_attach_resume``. Raises ``NotImplementedError`` if the
    caller tries to use it as an awaitable — sync clients should use
    ``tool_handler=`` (auto-resume mode) rather than manual ``resume()``.
    """

    def _resume_sync(*, tool_results: list[dict[str, Any]]) -> ExecuteResult | PausedResult:
        new_messages = list(paused.tool_call_messages) + list(tool_results)
        _check_tool_messages_size(len(new_messages))
        req = ExecuteRequest(
            message=None,
            parameters=parameters or {},
            block_overrides=block_overrides,
            attachments=attachments,
            tools=tools,
            tool_choice=tool_choice,
            execution_id=paused.execution_id,
            paused_at_step=paused.paused_at_step,
            iterations_used=paused.iterations_used,
            tool_call_messages=new_messages,
            accumulated_outputs=paused.accumulated_outputs,
            trace=trace,
        )
        url = f"{flow._versioned_path(version)}/execute"
        resp = flow._transport.request("POST", url, json=req, timeout=timeout)
        body: dict[str, Any] = resp.body if isinstance(resp.body, dict) else {}

        if body.get("status") == "tool_calls_required":
            next_paused = PausedResult.model_validate(body)
            return _attach_resume_sync(
                next_paused,
                flow,
                parameters,
                block_overrides,
                attachments,
                tools,
                tool_choice,
                trace,
                version,
                timeout,
            )

        return ExecuteResult.model_validate(body)

    paused._resume_sync = _resume_sync
    return paused


async def _auto_resume_loop(
    paused: PausedResult,
    handler: ToolHandler | AsyncToolHandler,
    max_rounds: int,
) -> ExecuteResult:
    """Drive the tool-handler loop until the run reaches a terminal state.

    Calls *handler* with the current ``tool_calls``, passes the returned
    results to ``PausedResult.resume()``, and repeats until either an
    ``ExecuteResult`` is returned or *max_rounds* is exhausted.

    Supports both sync and async *handler* callables; the check is done at
    runtime via ``inspect.isawaitable``.

    Args:
        paused: Initial ``PausedResult`` with ``_resume`` already attached.
        handler: Sync or async callable ``(tool_calls) -> tool_results``.
        max_rounds: Maximum number of tool-handler invocations before raising.

    Returns:
        The terminal ``ExecuteResult`` once the run completes.

    Raises:
        ToolCallLimitError: *max_rounds* invocations without a terminal result.
    """
    rounds = 0
    current: ExecuteResult | PausedResult = paused

    while isinstance(current, PausedResult):
        if rounds >= max_rounds:
            raise ToolCallLimitError(
                f"Tool call loop exceeded max_tool_rounds={max_rounds}",
                code="TOOL_CALL_LIMIT_CLIENT",
                execution_id=current.execution_id,
            )
        result = handler(current.tool_calls)
        if inspect.isawaitable(result):
            result = await result
        current = await current.resume(tool_results=result)
        rounds += 1

    return current


def _auto_resume_loop_sync(
    paused: PausedResult,
    handler: ToolHandler,
    max_rounds: int,
) -> ExecuteResult:
    """Sync driver loop for tool-handler auto-resume.

    Mirror of :func:`_auto_resume_loop` without ``await``. Only accepts
    synchronous *handler* callables — async handlers are rejected by
    :class:`Flow.execute` before reaching this function.

    Args:
        paused: Initial ``PausedResult`` with ``_resume_sync`` already attached.
        handler: Sync callable ``(tool_calls) -> tool_results``.
        max_rounds: Maximum number of tool-handler invocations before raising.

    Returns:
        The terminal ``ExecuteResult`` once the run completes.

    Raises:
        ToolCallLimitError: *max_rounds* invocations without a terminal result.
    """
    rounds = 0
    current: ExecuteResult | PausedResult = paused

    while isinstance(current, PausedResult):
        if rounds >= max_rounds:
            raise ToolCallLimitError(
                f"Tool call loop exceeded max_tool_rounds={max_rounds}",
                code="TOOL_CALL_LIMIT_CLIENT",
                execution_id=current.execution_id,
            )
        result = handler(current.tool_calls)
        current = current.resume_sync(tool_results=result)
        rounds += 1

    return current
