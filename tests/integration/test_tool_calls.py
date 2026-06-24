"""Integration tests: tool-call flows.

Exercises the tool-call resume loop:
- tool_handler closure runs and flow completes automatically
- Manual resume via PausedResult.resume()
- max_tool_rounds=1 raises ToolCallLimitError on a looping flow
- Async tool_handler (coroutine) is awaited by the SDK
"""

from __future__ import annotations

from typing import Any

import pytest

from noukai_sdk import AsyncFlow, ExecuteResult, Flow, PausedResult, ToolCallLimitError


def _echo_tool_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Minimal sync tool handler: echoes back a placeholder result for every call."""
    results: list[dict[str, Any]] = []
    for call in tool_calls:
        call_id = call.get("id", "")
        results.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "integration-test-tool-result",
            }
        )
    return results


async def _async_echo_tool_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Minimal async tool handler: echoes back a placeholder result for every call."""
    results: list[dict[str, Any]] = []
    for call in tool_calls:
        call_id = call.get("id", "")
        results.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "async-integration-test-tool-result",
            }
        )
    return results


_DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location (integration test stub).",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name"},
            },
            "required": ["location"],
        },
    },
}


@pytest.mark.integration
def test_auto_tool_handler(tools_flow: Flow) -> None:
    """tool_handler closure runs automatically and flow completes cleanly."""
    handler_invocations: list[list[dict[str, Any]]] = []

    def tracking_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        handler_invocations.append(tool_calls)
        return _echo_tool_handler(tool_calls)

    result = tools_flow.execute(
        message="What is the weather in Tokyo?",
        tools=[_DUMMY_TOOL],
        tool_handler=tracking_handler,
    )

    assert isinstance(result, ExecuteResult), (
        f"Expected ExecuteResult after tool loop, got {type(result).__name__}"
    )
    assert result.status == "completed"
    assert len(handler_invocations) >= 1, (
        "tool_handler was never called — ensure tools_flow fixture is configured "
        "with a tool-calling LLM block."
    )


@pytest.mark.integration
def test_manual_resume(tools_flow: Flow) -> None:
    """Without tool_handler, execute() returns PausedResult for manual resume.

    Note: sync Flow.execute() returns PausedResult when the server pauses.
    The sync PausedResult exposes resume_sync() for the synchronous path.
    """
    result = tools_flow.execute(
        message="What is the weather in Paris?",
        tools=[_DUMMY_TOOL],
        # No tool_handler — SDK returns PausedResult for manual driving.
    )

    if isinstance(result, ExecuteResult):
        pytest.skip(
            "Server completed the flow without pausing for tools. "
            "The tools_flow fixture may not have tool-calling configured. "
            "Verify NOUKAI_INTEGRATION_TOOLS_SLUG."
        )

    assert isinstance(result, PausedResult)
    assert result.execution_id is not None
    assert result.paused_at_step is not None
    assert len(result.tool_calls) >= 1

    # Build tool results and resume synchronously.
    tool_results = _echo_tool_handler(result.tool_calls)
    final = result.resume_sync(tool_results=tool_results)

    # After resume, we should reach completion (or another pause if multi-turn).
    assert final is not None
    assert isinstance(final, (ExecuteResult, PausedResult))


@pytest.mark.integration
def test_max_tool_rounds_raises(tools_flow: Flow) -> None:
    """max_tool_rounds=1 raises ToolCallLimitError when the flow loops tools.

    This test only passes if the tools_flow fixture is authored to make at
    least two tool calls. If the model resolves in a single round, this test
    is skipped with a descriptive message.
    """
    call_count = 0

    def counting_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        return _echo_tool_handler(tool_calls)

    try:
        result = tools_flow.execute(
            message="Keep calling get_weather for multiple cities: Tokyo, Paris, London.",
            tools=[_DUMMY_TOOL],
            tool_handler=counting_handler,
            max_tool_rounds=1,
        )
    except ToolCallLimitError:
        # Expected outcome when the model requests more than 1 round.
        assert call_count >= 1
        return
    else:
        if isinstance(result, ExecuteResult):
            pytest.skip(
                "Flow completed within 1 tool round — cannot test ToolCallLimitError. "
                "Try a prompt that forces multiple tool calls, or accept this as a "
                "fixture configuration issue."
            )
        raise AssertionError(
            f"Expected ToolCallLimitError or ExecuteResult, got {type(result).__name__}"
        )


@pytest.mark.integration
async def test_async_tool_handler_awaited(async_tools_flow: AsyncFlow) -> None:
    """Async tool_handler coroutine is awaited by the SDK and flow completes."""
    handler_invocations: list[list[dict[str, Any]]] = []

    async def tracking_async_handler(
        tool_calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        handler_invocations.append(tool_calls)
        return await _async_echo_tool_handler(tool_calls)

    result = await async_tools_flow.execute(
        message="What is the weather in Berlin?",
        tools=[_DUMMY_TOOL],
        tool_handler=tracking_async_handler,
    )

    assert isinstance(result, ExecuteResult), (
        f"Expected ExecuteResult after async tool loop, got {type(result).__name__}"
    )
    assert result.status == "completed"
    assert len(handler_invocations) >= 1, (
        "async tool_handler was never called — ensure async_tools_flow fixture is "
        "configured with a tool-calling LLM block."
    )
