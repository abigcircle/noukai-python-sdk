import json

import httpx
import pytest

from noukai_sdk import (
    AsyncNoukai,
    ExecuteResult,
    FlowExecutionError,
    PausedResult,
    ToolCallLimitError,
)


def make_client(handler):
    client = AsyncNoukai(api_key="nk_test")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


def paused_payload(iterations=1, tool_id="tc-1"):
    return {
        "status": "tool_calls_required",
        "executionId": "exec-1",
        "pausedAtStep": "step-1",
        "iterationsUsed": iterations,
        "toolCallMessages": [
            {"role": "user", "content": "search for X"},
            {
                "role": "assistant",
                "tool_calls": [{"id": tool_id, "function": {"name": "search", "arguments": "{}"}}],
            },
        ],
        "toolCalls": [{"id": tool_id, "function": {"name": "search", "arguments": "{}"}}],
        "accumulatedOutputs": {"step-0": {"context": "..."}},
        "flowId": "f",
        "blockCount": 2,
    }


def completed_payload():
    return {
        "status": "completed",
        "result": {"answer": "found"},
        "flowId": "f",
        "blockCount": 2,
        "executionId": "exec-1",
    }


class TestManualResume:
    @pytest.mark.asyncio
    async def test_paused_result_has_resume_method(self):
        def handler(request):
            return httpx.Response(200, json=paused_payload())

        client = make_client(handler)
        result = await client.flow("a/b/c").execute(
            message="hi",
            tools=[{"type": "function", "function": {"name": "search"}}],
        )
        await client.aclose()
        assert isinstance(result, PausedResult)
        assert hasattr(result, "resume")
        assert callable(result.resume)

    @pytest.mark.asyncio
    async def test_resume_sends_tool_results(self):
        captured_bodies = []

        def handler(request):
            body = json.loads(request.read()) if request.content else {}
            captured_bodies.append(body)
            if len(captured_bodies) == 1:
                return httpx.Response(200, json=paused_payload())
            return httpx.Response(200, json=completed_payload())

        client = make_client(handler)
        flow = client.flow("a/b/c")
        paused = await flow.execute(
            message="hi",
            tools=[{"type": "function", "function": {"name": "search"}}],
        )
        assert isinstance(paused, PausedResult)
        final = await paused.resume(
            tool_results=[{"role": "tool", "tool_call_id": "tc-1", "content": "result"}]
        )
        await client.aclose()
        assert isinstance(final, ExecuteResult)
        # Second request must carry resume context
        second = captured_bodies[1]
        assert second["executionId"] == "exec-1"
        assert second["pausedAtStep"] == "step-1"
        assert second["iterationsUsed"] == 1  # carries from server-supplied value
        # Last message in toolCallMessages must be a tool result
        msgs = second["toolCallMessages"]
        assert msgs[-1]["role"] == "tool"
        assert msgs[-1]["tool_call_id"] == "tc-1"

    @pytest.mark.asyncio
    async def test_resume_can_yield_another_pause(self):
        """Resume may produce another pause if model wants more tools."""
        states = [
            paused_payload(iterations=1, tool_id="tc-1"),
            paused_payload(iterations=2, tool_id="tc-2"),
            completed_payload(),
        ]

        def handler(request):
            return httpx.Response(200, json=states.pop(0))

        client = make_client(handler)
        flow = client.flow("a/b/c")
        first = await flow.execute(message="hi", tools=[{}])
        assert isinstance(first, PausedResult)
        second = await first.resume(
            tool_results=[{"role": "tool", "tool_call_id": "tc-1", "content": "x"}]
        )
        assert isinstance(second, PausedResult)
        third = await second.resume(
            tool_results=[{"role": "tool", "tool_call_id": "tc-2", "content": "y"}]
        )
        await client.aclose()
        assert isinstance(third, ExecuteResult)


class TestSyncResumeUX:
    """PausedResult.resume() vs resume_sync() error-message clarity (Important #8)."""

    @pytest.mark.asyncio
    async def test_manually_constructed_async_raises_runtime_error(self):
        paused = PausedResult.model_validate(paused_payload())
        # Neither _resume nor _resume_sync attached.
        with pytest.raises(RuntimeError, match="manually constructed"):
            await paused.resume(tool_results=[])

    def test_manually_constructed_sync_raises_runtime_error(self):
        paused = PausedResult.model_validate(paused_payload())
        with pytest.raises(RuntimeError, match="manually constructed"):
            paused.resume_sync(tool_results=[])

    @pytest.mark.asyncio
    async def test_async_paused_resume_sync_raises_type_error(self):
        """A PausedResult from AsyncNoukai cannot be resumed via resume_sync."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=paused_payload())

        client = make_client(handler)
        flow = client.flow("acme/p/test")
        paused = await flow.execute(message="hi", tools=[{"x": 1}])
        await client.aclose()
        assert isinstance(paused, PausedResult)
        with pytest.raises(TypeError, match="AsyncNoukai"):
            paused.resume_sync(tool_results=[])


class TestAutoResume:
    @pytest.mark.asyncio
    async def test_handler_invoked_once_then_completes(self):
        handler_calls = []
        responses = [paused_payload(), completed_payload()]

        def http_handler(request):
            return httpx.Response(200, json=responses.pop(0))

        def tool_handler(tool_calls):
            handler_calls.append(tool_calls)
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        client = make_client(http_handler)
        result = await client.flow("a/b/c").execute(
            message="hi",
            tools=[{"type": "function", "function": {"name": "search"}}],
            tool_handler=tool_handler,
        )
        await client.aclose()
        assert isinstance(result, ExecuteResult)
        assert len(handler_calls) == 1
        assert handler_calls[0][0]["id"] == "tc-1"

    @pytest.mark.asyncio
    async def test_handler_loops_until_complete(self):
        handler_calls = []
        responses = [
            paused_payload(iterations=1, tool_id="tc-1"),
            paused_payload(iterations=2, tool_id="tc-2"),
            paused_payload(iterations=3, tool_id="tc-3"),
            completed_payload(),
        ]

        def http_handler(request):
            return httpx.Response(200, json=responses.pop(0))

        def tool_handler(tool_calls):
            handler_calls.append(tool_calls)
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        client = make_client(http_handler)
        result = await client.flow("a/b/c").execute(
            message="hi",
            tools=[{}],
            tool_handler=tool_handler,
        )
        await client.aclose()
        assert isinstance(result, ExecuteResult)
        assert len(handler_calls) == 3

    @pytest.mark.asyncio
    async def test_async_handler_awaited(self):
        responses = [paused_payload(), completed_payload()]

        def http_handler(request):
            return httpx.Response(200, json=responses.pop(0))

        async def async_tool_handler(tool_calls):
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        client = make_client(http_handler)
        result = await client.flow("a/b/c").execute(
            message="hi",
            tools=[{}],
            tool_handler=async_tool_handler,
        )
        await client.aclose()
        assert isinstance(result, ExecuteResult)

    @pytest.mark.asyncio
    async def test_max_tool_rounds_raises(self):
        # Server keeps pausing indefinitely
        def http_handler(request):
            return httpx.Response(200, json=paused_payload())

        def tool_handler(tool_calls):
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        client = make_client(http_handler)
        with pytest.raises(ToolCallLimitError):
            await client.flow("a/b/c").execute(
                message="hi",
                tools=[{}],
                tool_handler=tool_handler,
                max_tool_rounds=3,
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_server_tool_iteration_limit_propagates(self):
        # Server itself enforces TOOL_ITERATION_LIMIT
        calls = [0]

        def http_handler(request):
            calls[0] += 1
            if calls[0] == 1:
                return httpx.Response(200, json=paused_payload())
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "TOOL_ITERATION_LIMIT",
                        "message": "Server iteration limit hit",
                    }
                },
            )

        def tool_handler(tool_calls):
            return [{"role": "tool", "tool_call_id": tc["id"], "content": "x"} for tc in tool_calls]

        client = make_client(http_handler)
        with pytest.raises(FlowExecutionError) as exc:
            await client.flow("a/b/c").execute(
                message="hi",
                tools=[{}],
                tool_handler=tool_handler,
                max_tool_rounds=10,
            )
        await client.aclose()
        assert exc.value.code == "TOOL_ITERATION_LIMIT"
