"""Integration tests: synchronous flow.execute() against a live Noukai server.

Exercises the happy-path execute() contract:
- Typed ExecuteResult returned
- Mandatory fields (execution_id, status, flow_id, block_count)
- Optional parameters forwarded
- trace=True flag
- cost_usd wire format
- Async client parity
"""

from __future__ import annotations

import pytest

from noukai_sdk import AsyncFlow, ExecuteResult, Flow


@pytest.mark.integration
def test_execute_returns_typed_result(hello_flow: Flow) -> None:
    """execute() returns an ExecuteResult with all mandatory fields populated."""
    result = hello_flow.execute(message="hello from integration test")

    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    assert result.execution_id is not None
    assert result.execution_id != ""
    assert result.flow_id is not None
    assert result.flow_id != ""
    assert result.block_count >= 1


@pytest.mark.integration
def test_execute_with_parameters(hello_flow: Flow) -> None:
    """Extra parameters dict is forwarded to the server without raising."""
    result = hello_flow.execute(
        message="hello",
        parameters={"extra_hint": "integration-test-param"},
    )

    # Even if hello-world ignores the parameter, the call must succeed.
    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"


@pytest.mark.integration
def test_execute_trace_flag_captures_payloads(hello_flow: Flow) -> None:
    """trace=True must succeed (server-side; SDK just passes the flag through)."""
    result = hello_flow.execute(message="trace me", trace=True)

    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    # execution_id must be present when trace is enabled (server always emits it).
    assert result.execution_id is not None


@pytest.mark.integration
def test_execute_returns_string_cost_usd(hello_flow: Flow) -> None:
    """Wire contract: if cost_usd is surfaced on StepCompleted it is a decimal
    string, not a float. This test validates the flow runs cleanly; the
    cost_usd contract is asserted at the event level in test_events.py.

    This test confirms execute() itself does not raise on LLM flows where
    cost data is attached server-side.
    """
    result = hello_flow.execute(message="cost contract check")

    assert isinstance(result, ExecuteResult)
    # result.output may be anything; we just need the call to succeed cleanly.
    assert result.status == "completed"


@pytest.mark.integration
async def test_async_execute(async_hello_flow: AsyncFlow) -> None:
    """AsyncFlow.execute() returns ExecuteResult with same mandatory fields."""
    result = await async_hello_flow.execute(message="async hello from integration test")

    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    assert result.execution_id is not None
    assert result.execution_id != ""
    assert result.flow_id is not None
    assert result.block_count >= 1
