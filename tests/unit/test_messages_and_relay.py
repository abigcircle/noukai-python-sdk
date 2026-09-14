"""messages support (F6) + keyless relay flow (design 20260903-SDK-agent-relay, PR-2).

Covers:
- ChatMessage wire shape (OpenAI-style snake_case, permissive extras).
- flow.execute(messages=...) sends `messages` on the wire.
- Client-side validation: message/messages mutual exclusion; system/function role reject.
- RelayFlow / AsyncRelayFlow run the SAME loop over a keyless relay transport,
  in both fresh-call modes, with parity against the direct path and the shared
  round limit (10).
"""

from __future__ import annotations

import warnings
from typing import Any

import httpx
import pytest

from noukai_sdk import (
    AsyncRelayFlow,
    ChatMessage,
    ExecuteResult,
    PausedResult,
    RelayFlow,
    ToolCallLimitError,
)
from noukai_sdk._models.requests import ExecuteRequest

COMPLETED = {"status": "completed", "result": {"ok": True}, "flowId": "f", "blockCount": 1}


def _paused(exec_id: str = "exec-1") -> dict[str, Any]:
    return {
        "status": "tool_calls_required",
        "executionId": exec_id,
        "pausedAtStep": "s1",
        "iterationsUsed": 1,
        "toolCallMessages": [{"role": "assistant", "content": "call"}],
        "toolCalls": [
            {"id": "tc1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ],
        "accumulatedOutputs": {},
        "flowId": "f",
        "blockCount": 1,
    }


# ---------------------------------------------------------------------------
# ChatMessage + ExecuteRequest wire shape
# ---------------------------------------------------------------------------


def test_chat_message_serializes_camel_case() -> None:
    m = ChatMessage(role="assistant", content=None, tool_calls=[{"id": "1"}], tool_call_id="x")
    dumped = m.model_dump(by_alias=True, exclude_none=True)
    # Noukai wire camelCase — snake_case attrs alias to camelCase on the wire.
    assert dumped == {"role": "assistant", "toolCalls": [{"id": "1"}], "toolCallId": "x"}


def test_chat_message_permits_extra_fields() -> None:
    m = ChatMessage.model_validate({"role": "user", "content": "hi", "custom": 7})
    dumped = m.model_dump(by_alias=True, exclude_none=True)
    assert dumped["custom"] == 7


def test_execute_request_messages_on_wire() -> None:
    req = ExecuteRequest(
        messages=[ChatMessage(role="user", content="hi")],
        tools=[{"type": "function"}],
        tool_choice="auto",
    )
    body = req.model_dump(by_alias=True, exclude_none=True)
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["toolChoice"] == "auto"
    assert "message" not in body  # None dropped


# ---------------------------------------------------------------------------
# Client-side validation
# ---------------------------------------------------------------------------


def _mock_flow(handler: Any):
    from noukai_sdk import Noukai

    client = Noukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=client._transport._base_url
    )
    return client.flow("acme/spelling/grade-3")


def test_execute_rejects_both_message_and_messages() -> None:
    flow = _mock_flow(lambda req: httpx.Response(200, json=COMPLETED))
    with pytest.raises(ValueError, match="not both"):
        flow.execute(message="hi", messages=[{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("bad_role", ["system", "function", "developer"])
def test_execute_rejects_disallowed_roles(bad_role: str) -> None:
    flow = _mock_flow(lambda req: httpx.Response(200, json=COMPLETED))
    with pytest.raises(ValueError, match="not allowed"):
        flow.execute(messages=[{"role": bad_role, "content": "x"}])


def test_execute_sends_messages_on_wire() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETED)

    flow = _mock_flow(handler)
    result = flow.execute(
        messages=[{"role": "user", "content": "hello"}], tools=[{"type": "function"}]
    )
    assert isinstance(result, ExecuteResult)
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert "message" not in captured["body"]


def test_messages_soft_size_warns_near_1mb() -> None:
    big = [{"role": "user", "content": "x" * 100_000} for _ in range(10)]  # ~1 MB
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from noukai_sdk._tool_calls import check_messages_payload_size

        check_messages_payload_size(big)
    assert any(issubclass(w.category, UserWarning) for w in caught)


# ---------------------------------------------------------------------------
# RelayFlow (sync) + AsyncRelayFlow — keyless loop over the relay transport
# ---------------------------------------------------------------------------


def _sync_relay(handler: Any, url: str = "https://bff.example.com/agent/execute") -> RelayFlow:
    return RelayFlow(url, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _async_relay(
    handler: Any, url: str = "https://bff.example.com/agent/execute"
) -> AsyncRelayFlow:
    return AsyncRelayFlow(url, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_relay_flow_sync_completes() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")  # keyless — none
        return httpx.Response(200, json=COMPLETED)

    result = _sync_relay(handler).execute(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    # Keyless: POSTs the raw payload to the relay URL, no nk_ bearer, no /seq path.
    assert captured["url"] == "https://bff.example.com/agent/execute"
    assert captured["auth"] is None
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_relay_flow_sync_paused_then_manual_resume() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_paused() if calls["n"] == 1 else COMPLETED)

    paused = _sync_relay(handler).execute(message="hi", tools=[])
    assert isinstance(paused, PausedResult)
    final = paused.resume_sync(
        tool_results=[{"role": "tool", "toolCallId": "tc1", "content": "r"}]
    )
    assert isinstance(final, ExecuteResult)
    assert calls["n"] == 2


def test_relay_flow_sync_auto_loop_with_handler() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_paused() if calls["n"] == 1 else COMPLETED)

    def tool_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "toolCallId": c["id"], "content": "r"} for c in tool_calls]

    result = _sync_relay(handler).execute(message="hi", tools=[], tool_handler=tool_handler)
    assert isinstance(result, ExecuteResult)
    assert calls["n"] == 2


async def test_relay_flow_async_auto_loop() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_paused() if calls["n"] == 1 else COMPLETED)

    async def tool_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "toolCallId": c["id"], "content": "r"} for c in tool_calls]

    result = await _async_relay(handler).execute(
        messages=[{"role": "user", "content": "hi"}], tools=[], tool_handler=tool_handler
    )
    assert isinstance(result, ExecuteResult)
    assert calls["n"] == 2


def test_relay_flow_round_limit_is_ten() -> None:
    """RelayFlow never terminates here (server always pauses) → the shared client
    round limit (DEFAULT_MAX_TOOL_ROUNDS=10) trips ToolCallLimitError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_paused())

    def tool_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "toolCallId": c["id"], "content": "r"} for c in tool_calls]

    with pytest.raises(ToolCallLimitError, match="max_tool_rounds=10"):
        _sync_relay(handler).execute(message="hi", tools=[], tool_handler=tool_handler)


def test_relay_flow_maps_upstream_error_to_typed_exception() -> None:
    from noukai_sdk import InsufficientCreditsError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402, json={"detail": {"code": "INSUFFICIENT_CREDITS", "message": "broke"}}
        )

    with pytest.raises(InsufficientCreditsError):
        _sync_relay(handler).execute(message="hi")


def test_relay_flow_rejects_both_message_and_messages() -> None:
    flow = _sync_relay(lambda req: httpx.Response(200, json=COMPLETED))
    with pytest.raises(ValueError, match="not both"):
        flow.execute(message="hi", messages=[{"role": "user", "content": "hi"}])


def test_direct_and_relay_parity_completed() -> None:
    """The same server response yields the same ExecuteResult whether driven
    directly (flow.execute) or over the relay (RelayFlow.execute)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=COMPLETED)

    direct = _mock_flow(handler).execute(message="hi")
    relay = _sync_relay(handler).execute(message="hi")
    assert isinstance(direct, ExecuteResult)
    assert isinstance(relay, ExecuteResult)
    assert direct.result == relay.result == {"ok": True}
    assert direct.status == relay.status == "completed"
