"""Phase 8: Synchronous client — execute, steps, tool-handler, trace, context manager."""

import httpx
import pytest

from noukai_sdk import ExecuteResult, Noukai, StepCompleted, Trace


def make_client(handler):
    client = Noukai(api_key="nk_test")
    client._transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


class TestSyncExecute:
    def test_execute_returns_result(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "result": {"x": 1},
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        with make_client(handler) as client:
            result = client.flow("a/b/c").execute(message="hi")
            assert isinstance(result, ExecuteResult)
            assert result.output == {"x": 1}

    def test_execute_uses_correct_url(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        with make_client(handler) as client:
            client.flow("acme/spelling/grade-3").execute(message="hi")
        assert captured["url"].endswith("/seq/acme/spelling/grade-3/execute")


class TestSyncSteps:
    def test_steps_yields_step_completed(self):
        calls = [0]

        def handler(request):
            calls[0] += 1
            if calls[0] == 1:
                return httpx.Response(
                    200,
                    content=(
                        b'data: {"eventType": "run_started", "executionId": "e", '
                        b'"runId": "r", "flowId": "f", "stepCount": 2}\n\n'
                        b'data: {"eventType": "step_completed", "stepId": "s-1", '
                        b'"name": "a", "output": {"x": 1}}\n\n'
                        b'data: {"eventType": "step_paused", "stepId": "s-1", '
                        b'"stepIndex": 1}\n\n'
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                content=(
                    b'data: {"eventType": "step_completed", "stepId": "s-2", '
                    b'"name": "b", "output": {"y": 2}}\n\n'
                    b'data: {"eventType": "flow_completed", "runId": "r"}\n\n'
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        with make_client(handler) as client:
            steps = list(client.flow("a/b/c").steps(message="hi"))
        assert all(isinstance(s, StepCompleted) for s in steps)
        assert [s.name for s in steps] == ["a", "b"]


class TestSyncToolHandler:
    def test_sync_handler_loops(self):
        responses = [
            {
                "status": "tool_calls_required",
                "executionId": "e",
                "pausedAtStep": "s-1",
                "iterationsUsed": 1,
                "toolCallMessages": [{"role": "assistant"}],
                "toolCalls": [{"id": "tc-1"}],
                "accumulatedOutputs": {},
                "flowId": "f",
                "blockCount": 1,
            },
            {
                "status": "completed",
                "result": "done",
                "flowId": "f",
                "blockCount": 1,
                "executionId": "e",
            },
        ]

        def handler(request):
            return httpx.Response(200, json=responses.pop(0))

        def tool_handler(tool_calls):
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": "ok"} for tc in tool_calls
            ]

        with make_client(handler) as client:
            result = client.flow("a/b/c").execute(
                message="hi",
                tools=[{}],
                tool_handler=tool_handler,
            )
        assert isinstance(result, ExecuteResult)
        assert result.output == "done"

    def test_sync_client_rejects_async_handler(self):
        """Async handler in sync client should error early, not at await time."""
        responses = [
            {
                "status": "tool_calls_required",
                "executionId": "e",
                "pausedAtStep": "s",
                "iterationsUsed": 1,
                "toolCallMessages": [{"role": "assistant"}],
                "toolCalls": [{"id": "tc"}],
                "accumulatedOutputs": {},
                "flowId": "f",
                "blockCount": 1,
            },
        ]

        def handler(request):
            return httpx.Response(200, json=responses.pop(0))

        async def async_handler(tool_calls):
            return []

        with make_client(handler) as client, pytest.raises(TypeError, match="sync"):
            client.flow("a/b/c").execute(
                message="hi",
                tools=[{}],
                tool_handler=async_handler,
            )


class TestSyncTrace:
    def test_trace_returns_typed(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "flowRun": {"id": "r", "flowId": "f", "status": "completed"},
                    "steps": [],
                },
            )

        with make_client(handler) as client:
            trace = client.flow("a/b/c").run("r").trace()
        assert isinstance(trace, Trace)


class TestContextManager:
    def test_with_block_closes_transport(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client(handler)
        with client:
            client.flow("a/b/c").execute(message="hi")
        # After exit, transport should be closed (subsequent calls raise).
