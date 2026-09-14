"""Keyless relay flow entrypoint (design 20260903-SDK-agent-relay, PR-2).

:class:`RelayFlow` / :class:`AsyncRelayFlow` run the **same** yield/resume
tool-calling loop as ``flow.execute()`` — but over a keyless
:class:`RelayExecuteTransport` pointed at a relay URL (no ``nk_`` key, no
``/seq`` path). This is the browser / server-to-server / CLI agent entrypoint:
the loop drives the round-trips and executes tools locally; a keyholder relay
forwards each request to the flow's ``/execute``.

Both fresh-call modes are supported — a single ``message`` (Nana style) and a
structured ``messages`` list (Pack Maker / agent-block style).

The client-side round limit is the SDK's ``DEFAULT_MAX_TOOL_ROUNDS`` (10),
reconciling the two historical limits (SDK 10 vs ``@noukai/agent`` 12) onto one
value — see CHANGELOG.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ._constants import DEFAULT_MAX_TOOL_ROUNDS
from ._models.requests import ChatMessage, ExecuteRequest
from ._models.responses import ExecuteResult, PausedResult
from ._tool_calls import (
    RelayExecuteTransport,
    RelaySyncExecuteTransport,
    check_messages_payload_size,
    drive_execute,
    drive_execute_sync,
    validate_fresh_call,
)

ToolHandler = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
AsyncToolHandler = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]
ToolChoice = Literal["auto", "none", "required"] | dict[str, Any]

MessagesArg = list[ChatMessage | dict[str, Any]] | None


def _build_request(
    message: str | None,
    messages: MessagesArg,
    parameters: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: ToolChoice | None,
    trace: bool,
) -> ExecuteRequest:
    validate_fresh_call(message, messages)
    check_messages_payload_size(messages)
    return ExecuteRequest(
        message=message,
        messages=[ChatMessage.model_validate(m) for m in messages] if messages else None,
        parameters=parameters or {},
        tools=tools,
        tool_choice=tool_choice,
        trace=trace,
    )


class RelayFlow:
    """Synchronous keyless relay flow. ``.execute(...)`` runs the shared loop
    over a :class:`RelaySyncExecuteTransport` pointed at *url*.

    Args:
        url: The relay endpoint (POSTs go here keyless).
        client: Optional injected ``httpx.Client`` (custom config / tests).
        timeout: Per-request timeout (seconds).
    """

    def __init__(self, url: str, *, client: Any = None, timeout: float | None = None) -> None:
        self._url = url
        self._client = client
        self._timeout = timeout

    def execute(
        self,
        message: str | None = None,
        *,
        messages: MessagesArg = None,
        parameters: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
    ) -> ExecuteResult | PausedResult:
        """Run the loop over the relay. Returns ``ExecuteResult`` on completion,
        or ``PausedResult`` when ``tool_handler`` is omitted and the flow paused
        for tools (drive it with ``.resume_sync(tool_results=...)``)."""
        if tool_handler is not None and inspect.iscoroutinefunction(tool_handler):
            raise TypeError("Sync RelayFlow cannot use async tool_handler. Use AsyncRelayFlow.")
        req = _build_request(message, messages, parameters, tools, tool_choice, trace)
        transport = RelaySyncExecuteTransport(self._url, client=self._client, timeout=self._timeout)
        return drive_execute_sync(
            transport,
            req,
            parameters=parameters,
            block_overrides=None,
            attachments=None,
            tools=tools,
            tool_choice=tool_choice,
            trace=trace,
            tool_handler=tool_handler,
            max_rounds=max_tool_rounds if max_tool_rounds is not None else DEFAULT_MAX_TOOL_ROUNDS,
        )


class AsyncRelayFlow:
    """Async keyless relay flow. Mirror of :class:`RelayFlow`; accepts both sync
    and async ``tool_handler`` callables."""

    def __init__(self, url: str, *, client: Any = None, timeout: float | None = None) -> None:
        self._url = url
        self._client = client
        self._timeout = timeout

    async def execute(
        self,
        message: str | None = None,
        *,
        messages: MessagesArg = None,
        parameters: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        tool_handler: ToolHandler | AsyncToolHandler | None = None,
        max_tool_rounds: int | None = None,
        trace: bool = False,
    ) -> ExecuteResult | PausedResult:
        """Run the loop over the relay. Returns ``ExecuteResult`` on completion,
        or ``PausedResult`` when ``tool_handler`` is omitted (drive it with
        ``await paused.resume(tool_results=...)``)."""
        req = _build_request(message, messages, parameters, tools, tool_choice, trace)
        transport = RelayExecuteTransport(self._url, client=self._client, timeout=self._timeout)
        return await drive_execute(
            transport,
            req,
            parameters=parameters,
            block_overrides=None,
            attachments=None,
            tools=tools,
            tool_choice=tool_choice,
            trace=trace,
            tool_handler=tool_handler,
            max_rounds=max_tool_rounds if max_tool_rounds is not None else DEFAULT_MAX_TOOL_ROUNDS,
        )
