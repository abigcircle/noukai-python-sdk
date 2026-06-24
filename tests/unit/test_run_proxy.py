"""Tests for AsyncRun proxy — trace(), step_trace(), live_trace().

All tests use httpx.MockTransport so no network calls are made.
Phase 7 RED → GREEN.
"""

import httpx
import pytest

from noukai_sdk import (
    AsyncNoukai,
    StepAttempts,
    StepTrace,
    Trace,
)


def make_client(handler):
    client = AsyncNoukai(api_key="nk_test")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


def trace_payload():
    return {
        "flowRun": {
            "id": "run-1",
            "flowId": "f",
            "status": "completed",
            "triggerType": "ad_hoc",
            "stepCount": 2,
            "durationMs": 3000,
        },
        "steps": [
            {
                "stepId": "s-1",
                "attempt": 1,
                "status": "completed",
                "durationMs": 1200,
                "modelUsed": "anthropic/claude-sonnet-4-6",
                "tokens": {"prompt": 100, "completion": 50, "total": 150},
                "costUsd": "0.0001",
            },
            {
                "stepId": "s-2",
                "attempt": 1,
                "status": "completed",
                "durationMs": 1800,
                "tokens": {"prompt": 200, "completion": 80, "total": 280},
                "costUsd": "0.00015",
            },
        ],
    }


class TestTrace:
    @pytest.mark.asyncio
    async def test_returns_typed_trace(self):
        def handler(request):
            return httpx.Response(200, json=trace_payload())

        client = make_client(handler)
        trace = await client.flow("a/b/c").run("run-1").trace()
        await client.aclose()
        assert isinstance(trace, Trace)
        assert trace.flow_run.id == "run-1"
        assert len(trace.steps) == 2

    @pytest.mark.asyncio
    async def test_uses_slug_scoped_url(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json=trace_payload())

        client = make_client(handler)
        await client.flow("acme/spelling/grade-3").run("run-1").trace()
        await client.aclose()
        assert "/seq/acme/spelling/grade-3/runs/run-1/trace" in captured["url"]


class TestStepTrace:
    @pytest.mark.asyncio
    async def test_latest_returns_single_step(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "stepId": "s-1",
                    "attempt": 2,
                    "status": "completed",
                    "durationMs": 800,
                    "loopIndex": None,
                },
            )

        client = make_client(handler)
        result = await client.flow("a/b/c").run("run-1").step_trace("s-1")
        await client.aclose()
        assert isinstance(result, StepTrace)
        assert result.attempt == 2

    @pytest.mark.asyncio
    async def test_all_returns_attempts(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "stepId": "s-1",
                    "attempts": [
                        {"stepId": "s-1", "attempt": 1, "status": "failed"},
                        {"stepId": "s-1", "attempt": 2, "status": "completed"},
                    ],
                },
            )

        client = make_client(handler)
        result = await client.flow("a/b/c").run("run-1").step_trace("s-1", attempt="all")
        await client.aclose()
        assert isinstance(result, StepAttempts)
        assert len(result.attempts) == 2

    @pytest.mark.asyncio
    async def test_attempt_query_param_set(self):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "stepId": "s-1",
                    "attempt": 2,
                    "status": "completed",
                },
            )

        client = make_client(handler)
        await client.flow("a/b/c").run("run-1").step_trace("s-1", attempt=2)
        await client.aclose()
        assert captured["params"]["attempt"] == "2"

    @pytest.mark.asyncio
    async def test_loop_index_query_param_set(self):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "stepId": "s-1",
                    "attempt": 1,
                    "status": "completed",
                    "loopIndex": 0,
                },
            )

        client = make_client(handler)
        await client.flow("a/b/c").run("run-1").step_trace("s-1", loop_index=0)
        await client.aclose()
        assert captured["params"]["loop_index"] == "0"


class TestLiveTrace:
    @pytest.mark.asyncio
    async def test_yields_typed_events(self):
        def handler(request):
            body = (
                b'data: {"eventType": "step_started", "stepId": "s-1"}\n\n'
                b'data: {"eventType": "step_completed", "stepId": "s-1", "output": {"x": 1}}\n\n'
                b'data: {"eventType": "flow_completed", "runId": "r"}\n\n'
            )
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

        client = make_client(handler)
        events = []
        async for e in client.flow("a/b/c").run("run-1").live_trace():
            events.append(e)
        await client.aclose()
        names = [type(e).__name__ for e in events]
        assert names == ["StepStarted", "StepCompleted", "FlowCompleted"]

    @pytest.mark.asyncio
    async def test_uses_slug_scoped_stream_url(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, content=b"", headers={"Content-Type": "text/event-stream"})

        client = make_client(handler)
        async for _ in client.flow("acme/spelling/grade-3").run("run-1").live_trace():
            pass
        await client.aclose()
        assert "/seq/acme/spelling/grade-3/runs/run-1/trace/stream" in captured["url"]
