"""Tool-call loop + execute-transport seam (design 20260903-SDK-agent-relay).

The yield/resume tool-calling loop is written **once** here and made
transport-pluggable via the :class:`ExecuteTransport` seam, so it runs in three
positions against one wire contract:

- **direct** — ``flow.execute()`` over a :class:`DirectExecuteTransport` (the
  key-holding SDK transport); today's behavior, unchanged.
- **relay** (keyless) — ``RelayFlow.execute()`` over a
  :class:`RelayExecuteTransport` that POSTs the raw payload to a relay URL.

``send(payload) -> (status, body)`` is the only thing that varies. The direct
transport raises typed exceptions on non-2xx (so ``send`` returns only on 2xx);
the relay transport returns the upstream status verbatim and the shared driver
maps a non-2xx to the same typed exception taxonomy.

Async functions use ``await``; sync functions use plain calls. Neither bridges
via ``asyncio.run()``.
"""

from __future__ import annotations

import inspect
import json as _json
import warnings
from typing import TYPE_CHECKING, Any, Protocol

from ._constants import DEFAULT_TIMEOUT_SECONDS, TOOL_CALL_MESSAGES_SOFT_LIMIT
from ._errors import ToolCallLimitError
from ._models.requests import ChatMessage, ExecuteRequest
from ._models.responses import ExecuteResult, PausedResult
from ._transport_shared import _map_status_to_exception, _parse_error_body

# 1 MB — mirrors the server's MAX_MESSAGES_PAYLOAD_BYTES (413 MESSAGES_TOO_LARGE).
MESSAGES_PAYLOAD_SOFT_LIMIT_BYTES = 1_000_000
# Roles the server accepts in messages[] (system/function → 400 MESSAGES_ROLE_INVALID).
_ALLOWED_MESSAGE_ROLES = frozenset({"user", "assistant", "tool"})


if TYPE_CHECKING:
    from ._flow import AsyncFlow, AsyncToolHandler, Flow, ToolHandler, VersionSpec


# ---------------------------------------------------------------------------
# Client-side validation of the server's fresh-call contract (surface early)
# ---------------------------------------------------------------------------


def _message_role(message: Any) -> str | None:
    if isinstance(message, ChatMessage):
        return message.role
    if isinstance(message, dict):
        role = message.get("role")
        return role if isinstance(role, str) else None
    return None


def validate_fresh_call(message: str | None, messages: list[Any] | None) -> None:
    """Validate a fresh execute call against the server contract, client-side.

    - Exactly-one guard: ``message`` and ``messages`` are mutually exclusive
      (``messages`` stands in for ``message`` server-side). Passing neither is
      allowed (a parameters-only flow) — unchanged from before ``messages``.
      An empty ``messages=[]`` counts as absent (matching the serializer, which
      omits a falsy ``messages`` from the wire), so ``message="hi", messages=[]``
      is not treated as "both provided".
    - Role guard: ``messages[]`` may only carry ``user`` / ``assistant`` /
      ``tool`` roles; a ``system`` or ``function`` turn would override the flow
      author's rendered system prompt and the server rejects it with 400
      ``MESSAGES_ROLE_INVALID``.

    Raises:
        ValueError: on either violation.
    """
    if message is not None and messages:
        raise ValueError(
            "execute(): provide either `message` or `messages`, not both "
            "(`messages` stands in for `message`)."
        )
    if messages is not None:
        for i, entry in enumerate(messages):
            role = _message_role(entry)
            if role is None:
                raise ValueError(
                    f"messages[{i}] is missing a string `role`. Each turn must carry a "
                    f"'user', 'assistant', or 'tool' role."
                )
            if role not in _ALLOWED_MESSAGE_ROLES:
                raise ValueError(
                    f"messages[{i}].role={role!r} is not allowed. The server accepts only "
                    f"'user', 'assistant', or 'tool' — a caller 'system'/'function' turn would "
                    f"override the flow author's system prompt (MESSAGES_ROLE_INVALID)."
                )


def _check_tool_messages_size(message_count: int) -> None:
    """Emit a one-time warning if tool_call_messages grows large (by count).

    Server caps the list size and will reject with ``MESSAGES_TOO_LARGE``.
    Surfacing this client-side gives the caller a chance to compact the
    conversation (e.g. summarise older tool turns) before hitting that wall.
    """
    if message_count > TOOL_CALL_MESSAGES_SOFT_LIMIT:
        # UserWarning (not ResourceWarning) so it is visible under the default
        # warning filter — parity with check_messages_payload_size. A
        # ResourceWarning would be suppressed and the advisory never surface
        # during auto-resume.
        warnings.warn(
            f"tool_call_messages has grown to {message_count} entries "
            f"(soft limit {TOOL_CALL_MESSAGES_SOFT_LIMIT}). The server will "
            f"eventually reject this request with MESSAGES_TOO_LARGE. "
            f"Consider compacting tool-call history.",
            UserWarning,
            stacklevel=3,
        )


def check_messages_payload_size(messages: list[Any] | None) -> None:
    """Warn (once) when a ``messages`` payload approaches the server's 1 MB cap.

    The server rejects ``messages[]`` over ``MESSAGES_PAYLOAD_SOFT_LIMIT_BYTES``
    with 413 ``MESSAGES_TOO_LARGE``; warning at ~90% lets the caller compact
    before the hard wall. Best-effort — a non-serialisable payload is skipped.
    """
    if not messages:
        return
    try:
        dumped = [
            m.model_dump(by_alias=True, exclude_none=True) if isinstance(m, ChatMessage) else m
            for m in messages
        ]
        size = len(_json.dumps(dumped).encode("utf-8"))
    except (TypeError, ValueError):
        return
    if size > MESSAGES_PAYLOAD_SOFT_LIMIT_BYTES * 9 // 10:
        # UserWarning (not ResourceWarning) so it is visible under the default
        # warning filter — parity with the TS peer's console.warn. Python's
        # default filter already dedups per call site (effectively "warn once").
        warnings.warn(
            f"messages payload is ~{size} bytes, approaching the server's "
            f"{MESSAGES_PAYLOAD_SOFT_LIMIT_BYTES}-byte cap (MESSAGES_TOO_LARGE). "
            f"Consider compacting the conversation.",
            UserWarning,
            stacklevel=3,
        )


# ---------------------------------------------------------------------------
# Execute-transport seam
# ---------------------------------------------------------------------------


class ExecuteTransport(Protocol):
    """The one step the loop routes through: send an execute/resume payload and
    return ``(http_status, body)``. Async variant."""

    async def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...


class SyncExecuteTransport(Protocol):
    """Sync mirror of :class:`ExecuteTransport`."""

    def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...


def _raise_for_execute_status(status: int, body: dict[str, Any]) -> None:
    """Map a non-2xx ``(status, body)`` to the SDK's typed exception taxonomy.

    The direct transport already raises on non-2xx (rich, with header details),
    so this is a no-op there; it is the error path for the relay transport,
    which returns the upstream status verbatim.

    Note — header-derived error metadata is unavailable through this seam.
    ``send(payload) -> (status, body)`` carries no HTTP headers, so the
    header-sourced fields (``retry_after``, ``www_authenticate``,
    ``request_id``) are passed as ``None`` here. Over a relay this means a 429
    yields ``RateLimitError.retry_after = None`` and a 401 yields
    ``AuthenticationError.www_authenticate = None``, unlike the direct-transport
    path which reads them off the response headers. This is intentional and
    design-consistent (the seam does not widen to carry headers).
    """
    if 200 <= status < 300:
        return
    code, message = _parse_error_body(body)
    raise _map_status_to_exception(status, code, message, request_id=None, response_body=body)


class DirectExecuteTransport:
    """Async ``ExecuteTransport`` wrapping a flow's key-holding transport.

    Preserves today's resume behavior byte-for-byte: builds the same
    ``{versioned_path}/execute`` URL and calls the underlying transport with the
    same timeout (which raises typed exceptions on non-2xx)."""

    def __init__(self, flow: AsyncFlow, version: VersionSpec, timeout: float | None) -> None:
        self._flow = flow
        self._version = version
        self._timeout = timeout

    async def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self._flow._versioned_path(self._version)}/execute"
        resp = await self._flow._transport.request("POST", url, json=payload, timeout=self._timeout)
        body = resp.body if isinstance(resp.body, dict) else {}
        return resp.status_code, body


class DirectSyncExecuteTransport:
    """Sync mirror of :class:`DirectExecuteTransport`."""

    def __init__(self, flow: Flow, version: VersionSpec, timeout: float | None) -> None:
        self._flow = flow
        self._version = version
        self._timeout = timeout

    def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self._flow._versioned_path(self._version)}/execute"
        resp = self._flow._transport.request("POST", url, json=payload, timeout=self._timeout)
        body = resp.body if isinstance(resp.body, dict) else {}
        return resp.status_code, body


class RelayExecuteTransport:
    """Async keyless ``ExecuteTransport``: POST the raw payload to a relay URL.

    No ``nk_`` key, no ``/seq`` path — the relay (a keyholder proxy) injects the
    key and pins the flow. Returns the relay's ``(status, body)`` verbatim; the
    shared driver maps a non-2xx to a typed exception.

    Note: the ``(status, body)`` seam carries no HTTP headers, so header-derived
    error fields (``retry_after``, ``www_authenticate``, ``request_id``) are
    ``None`` on errors surfaced through this transport (see
    :func:`_raise_for_execute_status`).

    Args:
        url: The relay endpoint the browser/agent POSTs to.
        client: Optional injected ``httpx.AsyncClient`` (custom config / tests);
            a fresh one is created and closed per call when omitted.
        timeout: Per-request timeout (seconds); defaults to the SDK default.
    """

    def __init__(self, url: str, *, client: Any = None, timeout: float | None = None) -> None:
        self._url = url
        self._client = client
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS

    async def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import httpx

        client = self._client if self._client is not None else httpx.AsyncClient()
        owns = self._client is None
        try:
            resp = await client.post(self._url, json=payload, timeout=self._timeout)
        finally:
            if owns:
                await client.aclose()
        try:
            body = resp.json()
        except Exception:
            body = None
        return resp.status_code, body if isinstance(body, dict) else {}


class RelaySyncExecuteTransport:
    """Sync mirror of :class:`RelayExecuteTransport`.

    Same header caveat: the ``(status, body)`` seam carries no HTTP headers, so
    ``retry_after`` / ``www_authenticate`` / ``request_id`` are ``None`` on
    errors surfaced through this transport (see :func:`_raise_for_execute_status`).
    """

    def __init__(self, url: str, *, client: Any = None, timeout: float | None = None) -> None:
        self._url = url
        self._client = client
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS

    def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import httpx

        client = self._client if self._client is not None else httpx.Client()
        owns = self._client is None
        try:
            resp = client.post(self._url, json=payload, timeout=self._timeout)
        finally:
            if owns:
                client.close()
        try:
            body = resp.json()
        except Exception:
            body = None
        return resp.status_code, body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# Resume attachment (transport-parameterized)
# ---------------------------------------------------------------------------


def _attach_resume(
    paused: PausedResult,
    transport: ExecuteTransport,
    parameters: dict[str, Any] | None,
    block_overrides: dict[str, dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any | None,
    trace: bool,
) -> PausedResult:
    """Bind an async ``_resume`` callable onto *paused* and return it.

    The closure captures the original request params and the ``ExecuteTransport``
    (not the flow). ``paused.resume(tool_results=...)`` rebuilds the resume
    request and sends it through the seam. A resumed ``PausedResult`` is
    recursively re-attached to the same transport (manual-chaining support).
    """

    async def _resume(*, tool_results: list[dict[str, Any]]) -> ExecuteResult | PausedResult:
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
        payload = req.model_dump(by_alias=True, exclude_none=True)
        status, body = await transport.send(payload)
        _raise_for_execute_status(status, body)

        if body.get("status") == "tool_calls_required":
            next_paused = PausedResult.model_validate(body)
            return _attach_resume(
                next_paused,
                transport,
                parameters,
                block_overrides,
                attachments,
                tools,
                tool_choice,
                trace,
            )
        return ExecuteResult.model_validate(body)

    paused._resume = _resume
    return paused


def _attach_resume_sync(
    paused: PausedResult,
    transport: SyncExecuteTransport,
    parameters: dict[str, Any] | None,
    block_overrides: dict[str, dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any | None,
    trace: bool,
) -> PausedResult:
    """Bind a sync ``_resume_sync`` callable onto *paused* and return it.

    Sync mirror of :func:`_attach_resume`."""

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
        payload = req.model_dump(by_alias=True, exclude_none=True)
        status, body = transport.send(payload)
        _raise_for_execute_status(status, body)

        if body.get("status") == "tool_calls_required":
            next_paused = PausedResult.model_validate(body)
            return _attach_resume_sync(
                next_paused,
                transport,
                parameters,
                block_overrides,
                attachments,
                tools,
                tool_choice,
                trace,
            )
        return ExecuteResult.model_validate(body)

    paused._resume_sync = _resume_sync
    return paused


# ---------------------------------------------------------------------------
# Shared fresh-call drivers (used by RelayFlow; flow.execute keeps its own
# session/replay-aware fresh call and only routes resume through the seam)
# ---------------------------------------------------------------------------


async def drive_execute(
    transport: ExecuteTransport,
    req: ExecuteRequest,
    *,
    parameters: dict[str, Any] | None,
    block_overrides: dict[str, dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any | None,
    trace: bool,
    tool_handler: ToolHandler | AsyncToolHandler | None,
    max_rounds: int,
) -> ExecuteResult | PausedResult:
    """Run the fresh call + pause/resume loop over an ``ExecuteTransport``."""
    payload = req.model_dump(by_alias=True, exclude_none=True)
    status, body = await transport.send(payload)
    _raise_for_execute_status(status, body)

    if body.get("status") == "tool_calls_required":
        paused = PausedResult.model_validate(body)
        _attach_resume(
            paused, transport, parameters, block_overrides, attachments, tools, tool_choice, trace
        )
        if tool_handler is not None:
            return await _auto_resume_loop(paused, tool_handler, max_rounds)
        return paused
    return ExecuteResult.model_validate(body)


def drive_execute_sync(
    transport: SyncExecuteTransport,
    req: ExecuteRequest,
    *,
    parameters: dict[str, Any] | None,
    block_overrides: dict[str, dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any | None,
    trace: bool,
    tool_handler: ToolHandler | None,
    max_rounds: int,
) -> ExecuteResult | PausedResult:
    """Sync mirror of :func:`drive_execute`."""
    payload = req.model_dump(by_alias=True, exclude_none=True)
    status, body = transport.send(payload)
    _raise_for_execute_status(status, body)

    if body.get("status") == "tool_calls_required":
        paused = PausedResult.model_validate(body)
        _attach_resume_sync(
            paused, transport, parameters, block_overrides, attachments, tools, tool_choice, trace
        )
        if tool_handler is not None:
            return _auto_resume_loop_sync(paused, tool_handler, max_rounds)
        return paused
    return ExecuteResult.model_validate(body)


# ---------------------------------------------------------------------------
# Auto-resume loops (transport-agnostic — drive via paused.resume)
# ---------------------------------------------------------------------------


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
    """Sync driver loop for tool-handler auto-resume. Mirror of
    :func:`_auto_resume_loop` without ``await``."""
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
