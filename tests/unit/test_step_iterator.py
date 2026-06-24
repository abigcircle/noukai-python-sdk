"""Tests for AsyncFlow.steps() / .events() — the SSE-driven iterators.

These tests use httpx.MockTransport to stand in for /seq/.../step. The
iterator under test must:
- Drive multiple /step calls (carrying executionId + accumulatedOutputs).
- Parse SSE events from each response.
- Filter to StepCompleted when `yield_only_step_completed`.
- Surface every event for `events(...)`.
- Auto-resume tool calls when `tool_handler` is given.
- Yield ToolCallsRequired with `.resume(tool_results=...)` when no handler.
"""

import json

import httpx
import pytest

from noukai_sdk import (
    AsyncNoukai,
    StepCompleted,
    ToolCallsRequired,
)


def sse_response(*event_payloads):
    body = b""
    for p in event_payloads:
        body += f"data: {json.dumps(p)}\n\n".encode()
    return httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "text/event-stream"},
    )


def make_client(handler):
    client = AsyncNoukai(api_key="nk_test")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


class TestSimpleStepFlow:
    async def test_steps_yields_step_completed_only(self):
        """Two /step calls; each emits one step's events; iterator drives both."""
        calls = [0]

        def handler(request):
            calls[0] += 1
            body = json.loads(request.read()) if request.content else {}
            step_index = body.get("stepIndex", 0)
            if step_index == 0:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "runId": "r",
                        "executionId": "e",
                        "flowId": "f",
                        "stepCount": 2,
                    },
                    {"eventType": "step_started", "stepId": "s-1"},
                    {
                        "eventType": "step_completed",
                        "stepId": "s-1",
                        "name": "a",
                        "output": {"x": 1},
                    },
                    {"eventType": "step_paused", "stepId": "s-1", "stepIndex": 1},
                )
            return sse_response(
                {"eventType": "step_started", "stepId": "s-2"},
                {
                    "eventType": "step_completed",
                    "stepId": "s-2",
                    "name": "b",
                    "output": {"y": 2},
                },
                {
                    "eventType": "flow_completed",
                    "runId": "r",
                    "executionId": "e",
                    "result": {"final": True},
                },
            )

        client = make_client(handler)
        steps = []
        async for step in client.flow("a/b/c").steps(message="hi"):
            steps.append(step)
        await client.aclose()
        # steps() filters to StepCompleted
        assert all(isinstance(s, StepCompleted) for s in steps)
        assert [s.name for s in steps] == ["a", "b"]
        # SDK advanced cursor between calls
        assert calls[0] == 2


class TestCursorManagement:
    async def test_execution_id_carried_between_calls(self):
        bodies = []

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            idx = body.get("stepIndex", 0)
            if idx == 0:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "exec-xyz",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 2,
                    },
                    {
                        "eventType": "step_completed",
                        "stepId": "s-1",
                        "output": {"a": 1},
                    },
                    {"eventType": "step_paused", "stepId": "s-1", "stepIndex": 1},
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-2",
                    "output": {"b": 2},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        async for _ in client.flow("a/b/c").steps(message="hi"):
            pass
        await client.aclose()
        assert bodies[1]["executionId"] == "exec-xyz"

    async def test_accumulated_outputs_grow(self):
        bodies = []

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            idx = body.get("stepIndex", 0)
            if idx == 0:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 2,
                    },
                    {
                        "eventType": "step_completed",
                        "stepId": "s-1",
                        "output": {"a": 1},
                    },
                    {"eventType": "step_paused", "stepId": "s-1", "stepIndex": 1},
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-2",
                    "output": {"b": 2},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        async for _ in client.flow("a/b/c").steps(message="hi"):
            pass
        await client.aclose()
        assert bodies[1]["accumulatedOutputs"]["s-1"] == {"a": 1}

    async def test_step_index_increments(self):
        bodies = []

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            idx = body.get("stepIndex", 0)
            if idx == 0:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 2,
                    },
                    {
                        "eventType": "step_completed",
                        "stepId": "s-1",
                        "output": {"a": 1},
                    },
                    {"eventType": "step_paused", "stepId": "s-1", "stepIndex": 1},
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-2",
                    "output": {"b": 2},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        async for _ in client.flow("a/b/c").steps(message="hi"):
            pass
        await client.aclose()
        # First call stepIndex=0, second stepIndex=1.
        assert bodies[0]["stepIndex"] == 0
        assert bodies[1]["stepIndex"] == 1

    async def test_message_only_on_first_call(self):
        bodies = []

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            idx = body.get("stepIndex", 0)
            if idx == 0:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 2,
                    },
                    {
                        "eventType": "step_completed",
                        "stepId": "s-1",
                        "output": {"a": 1},
                    },
                    {"eventType": "step_paused", "stepId": "s-1", "stepIndex": 1},
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-2",
                    "output": {"b": 2},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        async for _ in client.flow("a/b/c").steps(message="hi"):
            pass
        await client.aclose()
        assert bodies[0].get("message") == "hi"
        # Second call (resume) must not re-send message.
        assert "message" not in bodies[1] or bodies[1]["message"] is None


class TestEventsRawMode:
    async def test_events_yields_every_event(self):
        def handler(request):
            return sse_response(
                {
                    "eventType": "run_started",
                    "executionId": "e",
                    "runId": "r",
                    "flowId": "f",
                    "stepCount": 1,
                },
                {"eventType": "step_started", "stepId": "s-1"},
                {
                    "eventType": "step_input",
                    "stepId": "s-1",
                    "inputData": {"x": 1},
                },
                {
                    "eventType": "step_output",
                    "stepId": "s-1",
                    "outputData": "partial",
                },
                {"eventType": "step_completed", "stepId": "s-1", "output": "final"},
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        events = []
        async for e in client.flow("a/b/c").events(message="hi", run_remaining=True):
            events.append(e)
        await client.aclose()
        names = [type(e).__name__ for e in events]
        assert names == [
            "RunStarted",
            "StepStarted",
            "StepInput",
            "StepOutput",
            "StepCompleted",
            "FlowCompleted",
        ]

    async def test_events_surfaces_step_paused(self):
        """events() mode yields StepPaused (protocol pause between steps)."""
        from noukai_sdk import StepPaused

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            idx = body.get("stepIndex", 0)
            if idx == 0:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 2,
                    },
                    {
                        "eventType": "step_completed",
                        "stepId": "s-1",
                        "output": {"a": 1},
                    },
                    {
                        "eventType": "step_paused",
                        "stepId": "s-1",
                        "stepIndex": 1,
                    },
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-2",
                    "output": {"b": 2},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        events = []
        async for e in client.flow("a/b/c").events(message="hi"):
            events.append(e)
        await client.aclose()
        assert any(isinstance(e, StepPaused) for e in events)

    async def test_run_remaining_passes_through(self):
        bodies = []

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            return sse_response(
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(handler)
        async for _ in client.flow("a/b/c").events(message="hi", run_remaining=True):
            pass
        await client.aclose()
        assert bodies[0].get("runRemaining") is True


class TestStepFailed:
    async def test_step_failed_terminates_iterator(self):
        def handler(request):
            return sse_response(
                {
                    "eventType": "run_started",
                    "executionId": "e",
                    "runId": "r",
                    "flowId": "f",
                    "stepCount": 1,
                },
                {
                    "eventType": "step_error",
                    "stepId": "s-1",
                    "error": {"message": "boom"},
                },
            )

        client = make_client(handler)
        events = []
        async for e in client.flow("a/b/c").events(message="hi"):
            events.append(e)
        await client.aclose()
        assert any(type(e).__name__ == "StepFailed" for e in events)


class TestToolCallsInIterator:
    async def test_tool_handler_kwarg_auto_resumes(self):
        """When tool_handler given, ToolCallsRequired events never surface."""
        bodies = []
        handler_calls = []

        def http_handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            if len(bodies) == 1:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 1,
                    },
                    {"eventType": "step_started", "stepId": "s-1"},
                    {
                        "eventType": "step_paused_for_tool_calls",
                        "runId": "r",
                        "executionId": "e",
                        "stepId": "s-1",
                        "stepIndex": 0,
                        "iterationsUsed": 1,
                        "toolCallMessages": [{"role": "assistant"}],
                        "toolCalls": [{"id": "tc-1"}],
                        "accumulatedOutputs": {},
                    },
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-1",
                    "output": {"x": 1},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        def tool_handler(tool_calls):
            handler_calls.append(tool_calls)
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        client = make_client(http_handler)
        events = []
        async for e in client.flow("a/b/c").events(
            message="hi", tools=[{}], tool_handler=tool_handler
        ):
            events.append(e)
        await client.aclose()
        assert len(handler_calls) == 1
        # ToolCallsRequired should not be in surfaced events
        assert not any(isinstance(e, ToolCallsRequired) for e in events)
        # The resumed /step body must carry the tool messages and execution_id
        assert bodies[1]["executionId"] == "e"
        assert bodies[1]["toolCallMessages"] is not None
        assert any(m.get("role") == "tool" for m in bodies[1]["toolCallMessages"])

    async def test_no_handler_yields_tool_calls_required_with_resume(self):
        bodies = []

        def http_handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            if len(bodies) == 1:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 1,
                    },
                    {
                        "eventType": "step_paused_for_tool_calls",
                        "runId": "r",
                        "executionId": "e",
                        "stepId": "s-1",
                        "stepIndex": 0,
                        "iterationsUsed": 1,
                        "toolCallMessages": [{"role": "assistant"}],
                        "toolCalls": [{"id": "tc-1"}],
                        "accumulatedOutputs": {},
                    },
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-1",
                    "output": {"x": 1},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        client = make_client(http_handler)
        events = []
        async for e in client.flow("a/b/c").events(message="hi", tools=[{}]):
            if isinstance(e, ToolCallsRequired):
                await e.resume(
                    tool_results=[
                        {
                            "role": "tool",
                            "tool_call_id": "tc-1",
                            "content": "ok",
                        }
                    ]
                )
            events.append(e)
        await client.aclose()
        assert any(isinstance(e, ToolCallsRequired) for e in events)
        assert any(isinstance(e, StepCompleted) for e in events)
        # Second call carries the resume state.
        assert bodies[1]["toolCallMessages"] is not None

    async def test_async_tool_handler_awaited(self):
        """An async tool_handler is awaited rather than called sync."""
        bodies = []

        def http_handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            if len(bodies) == 1:
                return sse_response(
                    {
                        "eventType": "run_started",
                        "executionId": "e",
                        "runId": "r",
                        "flowId": "f",
                        "stepCount": 1,
                    },
                    {
                        "eventType": "step_paused_for_tool_calls",
                        "runId": "r",
                        "executionId": "e",
                        "stepId": "s-1",
                        "stepIndex": 0,
                        "iterationsUsed": 1,
                        "toolCallMessages": [{"role": "assistant"}],
                        "toolCalls": [{"id": "tc-1"}],
                        "accumulatedOutputs": {},
                    },
                )
            return sse_response(
                {
                    "eventType": "step_completed",
                    "stepId": "s-1",
                    "output": {"x": 1},
                },
                {"eventType": "flow_completed", "runId": "r"},
            )

        invoked = []

        async def async_tool_handler(tool_calls):
            invoked.append(tool_calls)
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        client = make_client(http_handler)
        events = []
        async for e in client.flow("a/b/c").events(
            message="hi", tools=[{}], tool_handler=async_tool_handler
        ):
            events.append(e)
        await client.aclose()
        assert len(invoked) == 1
        assert any(isinstance(e, StepCompleted) for e in events)

    async def test_tool_handler_round_limit(self):
        """tool_handler that keeps causing tool calls eventually trips
        max_tool_rounds."""
        bodies = []

        def http_handler(request):
            body = json.loads(request.read()) if request.content else {}
            bodies.append(body)
            # Always return tool calls; iterator should give up after limit.
            return sse_response(
                {
                    "eventType": "run_started",
                    "executionId": "e",
                    "runId": "r",
                    "flowId": "f",
                    "stepCount": 1,
                },
                {
                    "eventType": "step_paused_for_tool_calls",
                    "runId": "r",
                    "executionId": "e",
                    "stepId": "s-1",
                    "stepIndex": 0,
                    "iterationsUsed": len(bodies),
                    "toolCallMessages": [{"role": "assistant"}],
                    "toolCalls": [{"id": "tc-1"}],
                    "accumulatedOutputs": {},
                },
            )

        def tool_handler(tool_calls):
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        from noukai_sdk import ToolCallLimitError

        client = make_client(http_handler)
        with pytest.raises(ToolCallLimitError):
            async for _ in client.flow("a/b/c").events(
                message="hi",
                tools=[{}],
                tool_handler=tool_handler,
                max_tool_rounds=2,
            ):
                pass
        await client.aclose()
