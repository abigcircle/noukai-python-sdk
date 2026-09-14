"""Integration tests: the ``messages[]`` fresh-call path (design F6).

These exercise ``flow.execute(messages=[...])`` against a live ``kind=chat``
agent flow (``NOUKAI_INTEGRATION_AGENT_SLUG``) that has the ``get_weather`` tool
enabled and a system prompt that forces a tool call:

- ``messages[]`` + ``tool_handler`` → auto-resume loop completes
- ``messages[]`` without a handler → ``PausedResult`` → manual resume
- async parity via ``async_agent_flow``
- client-side ``message``/``messages`` mutual-exclusion validation

Before this feature the SDK's request model had no ``messages`` field, so a
server-side chat/agent flow could not be driven through the SDK at all — these
tests are the end-to-end proof that F6 is closed.
"""

from __future__ import annotations

from typing import Any

import pytest

from noukai_sdk import AsyncFlow, ExecuteResult, Flow, PausedResult

_GET_WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location (integration test stub).",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name"}},
            "required": ["location"],
        },
    },
}


def _echo_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": "tool", "toolCallId": call.get("id", ""), "content": "sunny, 22C"}
        for call in tool_calls
    ]


async def _async_echo_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _echo_handler(tool_calls)


@pytest.mark.integration
def test_messages_auto_completes(agent_flow: Flow) -> None:
    """A structured ``messages[]`` fresh call with a tool handler completes."""
    result = agent_flow.execute(
        messages=[{"role": "user", "content": "What is the weather in Tokyo?"}],
        tools=[_GET_WEATHER],
        tool_handler=_echo_handler,
    )
    assert isinstance(result, ExecuteResult), (
        f"Expected ExecuteResult after the tool loop, got {type(result).__name__}"
    )
    assert result.status == "completed"


@pytest.mark.integration
def test_messages_manual_pause_resume(agent_flow: Flow) -> None:
    """Without a handler, a ``messages[]`` call yields a PausedResult the caller
    resumes synchronously."""
    result = agent_flow.execute(
        messages=[{"role": "user", "content": "What is the weather in Paris?"}],
        tools=[_GET_WEATHER],
    )

    if isinstance(result, ExecuteResult):
        pytest.skip(
            "Server completed without pausing for tools — verify the agent fixture "
            "(NOUKAI_INTEGRATION_AGENT_SLUG) has tools enabled and a tool-forcing prompt."
        )

    assert isinstance(result, PausedResult)
    assert result.execution_id is not None
    assert result.paused_at_step is not None
    assert len(result.tool_calls) >= 1

    final = result.resume_sync(tool_results=_echo_handler(result.tool_calls))
    assert isinstance(final, (ExecuteResult, PausedResult))


@pytest.mark.integration
async def test_messages_async_completes(async_agent_flow: AsyncFlow) -> None:
    """Async parity: ``messages[]`` + async handler completes via the async client."""
    result = await async_agent_flow.execute(
        messages=[{"role": "user", "content": "What is the weather in Berlin?"}],
        tools=[_GET_WEATHER],
        tool_handler=_async_echo_handler,
    )
    assert isinstance(result, ExecuteResult), (
        f"Expected ExecuteResult after the async tool loop, got {type(result).__name__}"
    )
    assert result.status == "completed"


@pytest.mark.integration
def test_message_and_messages_are_mutually_exclusive(agent_flow: Flow) -> None:
    """The SDK validates the server's fresh-call contract client-side: exactly one
    of ``message`` / ``messages`` (no network round-trip needed)."""
    with pytest.raises(ValueError, match="not both"):
        agent_flow.execute(
            message="What is the weather in Tokyo?",
            messages=[{"role": "user", "content": "What is the weather in Tokyo?"}],
            tools=[_GET_WEATHER],
        )
