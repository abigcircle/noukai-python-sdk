"""Replay feature — 22 scenarios from design 20260605-SDK-replay-decorator.

Capture mode (1–8) verifies session_id propagation through contextvar / kwarg /
client default and through the X-Session-Id outbound header.

Replay mode (9–17) verifies slug-positional matching for execute(), exact
matching by (execution_id, step_index) for step() continuations, error
re-raising, and the leftover/miss detection.

Production safety (18–20) verifies the NOUKAI_REPLAY_ENABLED env var gate
and the 403/410 error mapping.

Edge (21–22) verifies undefined-behavior warning for concurrent same-slug
execute() and clean fall-through outside any scope.
"""

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from noukai_sdk import AsyncNoukai, Noukai
from noukai_sdk._constants import HEADER_REPLAY, HEADER_SESSION_ID
from noukai_sdk._errors import (
    ReplayDisabledError,
    ReplayForbiddenError,
    ReplayInvalidSessionError,
    ReplayLeftoverError,
    ReplayMissError,
    ReplayNoSnapshotsError,
    ReplaySessionExpiredError,
    ReplaySessionNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_client_with_handler(handler: Any) -> AsyncNoukai:
    """Async client with a mocked httpx transport. Records all outbound
    requests via `handler`.

    Tests assert on captured headers and request paths to verify session_id
    propagation."""
    client = AsyncNoukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


def make_sync_client_with_handler(handler: Any) -> Noukai:
    """Sync mirror of make_client_with_handler."""
    client = Noukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


def ok_execute_response(execution_id: str = "exec-1", result: Any = None) -> httpx.Response:
    """Canned successful /execute response."""
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "result": result if result is not None else {"ok": True},
            "executionId": execution_id,
            "flowId": "flow-1",
            "blockCount": 1,
        },
    )


def session_response(
    session_id: str = "11111111-1111-4111-8111-111111111111",
    executions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Canned GET /sessions/{id} response."""
    return {
        "sessionId": session_id,
        "executions": executions or [],
    }


def session_execution(
    *,
    execution_id: str = "exec-rec-1",
    slug: str = "grade-3",
    trigger_type: str = "execute",
    result: Any = None,
    snapshots_available: bool = True,
    error: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One execution entry in a session response.

    Default ``slug`` is the BARE flow slug (no org/project prefix) — matches
    the BE serializer's output (see BE design Q5 resolution).
    """
    return {
        "executionId": execution_id,
        "flowId": "flow-1",
        "slug": slug,
        "triggerType": trigger_type,
        "status": "failed" if error else "completed",
        "startedAt": "2026-06-05T00:00:00Z",
        "completedAt": "2026-06-05T00:00:01Z",
        "traceCaptureMode": "full" if snapshots_available else "off",
        "snapshotsAvailable": snapshots_available,
        "steps": steps or [
            {
                "stepId": "s-1",
                "blockId": "b-1",
                "attempt": 1,
                "inputSnapshot": {"input": "x"},
                "outputSnapshot": result if result is not None else {"ok": True},
                "errorSnapshot": error,
                "truncated": False,
                "startedAt": "2026-06-05T00:00:00Z",
                "completedAt": "2026-06-05T00:00:01Z",
            }
        ],
        "errorAtStep": "s-1" if error else None,
    }


@contextmanager
def replay_enabled():
    """Patch NOUKAI_REPLAY_ENABLED=true for the duration of the test."""
    with patch.dict(os.environ, {"NOUKAI_REPLAY_ENABLED": "true"}):
        yield


@contextmanager
def replay_disabled():
    """Ensure NOUKAI_REPLAY_ENABLED is unset."""
    env = os.environ.copy()
    env.pop("NOUKAI_REPLAY_ENABLED", None)
    with patch.dict(os.environ, env, clear=True):
        yield


# ---------------------------------------------------------------------------
# Capture mode (scenarios 1–8)
# ---------------------------------------------------------------------------


class TestCaptureMode:
    """Decorator/scope tags each outbound execute/step with X-Session-Id."""

    @pytest.mark.asyncio
    async def test_1_single_execute_call(self):
        """Decorator generates a fresh session_id; current_session_id() returns
        the value during the call; outbound request carries the header."""
        from noukai_sdk import current_session_id, trace_scope

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return ok_execute_response()

        client = make_client_with_handler(handler)
        async with trace_scope() as scope:
            inside_id = current_session_id()
            assert inside_id is not None
            assert scope.session_id == inside_id
            await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()

        # Outside scope, current_session_id() is None.
        assert current_session_id() is None
        assert captured["headers"].get(HEADER_SESSION_ID.lower()) == inside_id

    @pytest.mark.asyncio
    async def test_2_multiple_execute_calls_share_session_id(self):
        from noukai_sdk import trace_scope

        captured: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({HEADER_SESSION_ID: request.headers.get(HEADER_SESSION_ID, "")})
            return ok_execute_response()

        client = make_client_with_handler(handler)
        async with trace_scope():
            await client.flow("a/b/c").execute(message="1")
            await client.flow("a/b/c").execute(message="2")
            await client.flow("a/b/d").execute(message="3")
        await client.aclose()

        sids = [h[HEADER_SESSION_ID] for h in captured]
        assert len(set(sids)) == 1
        assert sids[0]  # non-empty UUID

    @pytest.mark.asyncio
    async def test_3_nested_function_propagates_contextvar(self):
        """Contextvar propagates through deeper async calls."""
        from noukai_sdk import trace_scope, current_session_id

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_client_with_handler(handler)

        async def deeper() -> None:
            await client.flow("a/b/c").execute(message="hi")

        async with trace_scope() as scope:
            await deeper()
            assert captured[HEADER_SESSION_ID] == scope.session_id
        await client.aclose()

    @pytest.mark.asyncio
    async def test_4_step_through_flow_shares_session_id(self):
        """First /step call creates execution under session; subsequent
        /step calls share both execution_id and session_id."""
        from noukai_sdk import trace_scope

        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({
                "path": str(request.url.path),
                HEADER_SESSION_ID: request.headers.get(HEADER_SESSION_ID, ""),
            })
            # Minimal SSE-like response stub — Phase 7 supplies a real one.
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'event: run_started\ndata: {"runId":"r-1","executionId":"exec-1"}\n\n'
                    b'event: step_completed\ndata: {"stepId":"s-1","output":{"ok":true}}\n\n'
                    b'event: flow_completed\ndata: {"executionId":"exec-1","result":{"ok":true}}\n\n'
                ),
            )

        client = make_client_with_handler(handler)
        async with trace_scope() as scope:
            async for _ in client.flow("a/b/c").events(message="hi"):
                pass
        await client.aclose()

        sids = {c[HEADER_SESSION_ID] for c in captured if HEADER_SESSION_ID in c}
        assert sids == {scope.session_id}

    @pytest.mark.asyncio
    async def test_5_mixed_execute_and_step_in_same_scope(self):
        """All requests share session_id even when crossing execute/step modes."""
        from noukai_sdk import trace_scope

        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get(HEADER_SESSION_ID, ""))
            if request.url.path.endswith("/execute"):
                return ok_execute_response()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'event: flow_completed\ndata: {"result":{"ok":true}}\n\n',
            )

        client = make_client_with_handler(handler)
        async with trace_scope() as scope:
            await client.flow("a/b/c").execute(message="hi")
            async for _ in client.flow("a/b/c").events(message="hi2"):
                pass
        await client.aclose()
        assert set(captured) == {scope.session_id}

    @pytest.mark.asyncio
    async def test_6_no_decorator_no_session_id(self):
        """No scope → no header. Backwards compatible."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_client_with_handler(handler)
        await client.flow("a/b/c").execute(message="hi")
        await client.aclose()
        assert captured[HEADER_SESSION_ID] == ""

    @pytest.mark.asyncio
    async def test_7_explicit_kwarg_overrides_contextvar(self):
        """session_id kwarg wins over scope-generated session_id."""
        from noukai_sdk import trace_scope

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_client_with_handler(handler)
        async with trace_scope() as scope:
            await client.flow("a/b/c").execute(message="hi", session_id="explicit-sid")
        await client.aclose()
        assert captured[HEADER_SESSION_ID] == "explicit-sid"
        assert scope.session_id != "explicit-sid"

    @pytest.mark.asyncio
    async def test_8_response_header_set_by_framework_adapter(self):
        """Scenario 8 is exercised in Phase 8 framework-adapter tests, NOT
        here — the SDK core does not own the response header. This test
        documents the cross-reference. See test_adapters_fastapi.py."""
        pytest.skip("See tests/unit/test_adapters_fastapi.py — owned by Phase 8.")

    # Phase 5 additions

    @pytest.mark.asyncio
    async def test_log_handler_receives_scope_open_close(self):
        """Log handler observes scope_open and scope_close events."""
        from noukai_sdk import trace_scope

        events: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return ok_execute_response()

        client = AsyncNoukai(api_key="nk_test", env="dev", log_handler=events.append)
        client._transport._httpx_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=client._transport._base_url,
        )
        async with trace_scope(transport=client._transport) as scope:
            await client.flow("a/b/c").execute(message="hi")
        await client.aclose()

        phases = [e.get("phase") for e in events]
        assert "scope_open" in phases
        assert "scope_close" in phases
        open_evt = next(e for e in events if e.get("phase") == "scope_open")
        assert open_evt["session_id"] == scope.session_id

    @pytest.mark.asyncio
    async def test_execute_result_carries_session_id(self):
        """ExecuteResult.session_id == scope.session_id inside a trace scope."""
        from noukai_sdk import trace_scope

        def handler(request: httpx.Request) -> httpx.Response:
            return ok_execute_response()

        client = make_client_with_handler(handler)
        async with trace_scope() as scope:
            result = await client.flow("a/b/c").execute(message="hi")
        assert result.session_id == scope.session_id

        # Outside any scope, session_id is None.
        result2 = await client.flow("a/b/c").execute(message="hi")
        assert result2.session_id is None
        await client.aclose()


# ---------------------------------------------------------------------------
# Replay mode (scenarios 9–17)
# ---------------------------------------------------------------------------


class TestReplayMode:
    """Outbound execute/step calls are intercepted and served from cassette."""

    @pytest.mark.asyncio
    async def test_9_single_execute_serves_recorded_output(self):
        """No outbound /execute call — only the GET /sessions/{id} prefetch."""
        from noukai_sdk import trace_scope

        outbound_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            outbound_paths.append(request.url.path)
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(
                    200,
                    json=session_response(
                        executions=[session_execution(result={"answer": 42})]
                    ),
                )
            pytest.fail(f"Unexpected outbound request in replay mode: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                result = await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()
        assert result.output == {"answer": 42}
        # Only the prefetch GET was made; no /execute.
        assert all("/seq/sessions/" in p for p in outbound_paths)

    @pytest.mark.asyncio
    async def test_10_slug_positional_for_same_slug(self):
        """Two execute(A) → first code call gets first recorded A, second gets second A."""
        from noukai_sdk import trace_scope

        execs = [
            session_execution(execution_id="r-1", result={"n": 1}),
            session_execution(execution_id="r-2", result={"n": 2}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                r1 = await client.flow("acme/spelling/grade-3").execute(message="hi")
                r2 = await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()
        assert r1.output == {"n": 1}
        assert r2.output == {"n": 2}

    @pytest.mark.asyncio
    async def test_11_independent_counters_per_slug(self):
        """execute(A), execute(B), execute(A) → matches A1, B1, A2."""
        from noukai_sdk import trace_scope

        execs = [
            session_execution(execution_id="A1", slug="A", result={"x": "A1"}),
            session_execution(execution_id="B1", slug="B", result={"x": "B1"}),
            session_execution(execution_id="A2", slug="A", result={"x": "A2"}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                a1 = await client.flow("org/proj/A").execute(message="hi")
                b1 = await client.flow("org/proj/B").execute(message="hi")
                a2 = await client.flow("org/proj/A").execute(message="hi")
        await client.aclose()
        assert a1.output == {"x": "A1"}
        assert b1.output == {"x": "B1"}
        assert a2.output == {"x": "A2"}

    @pytest.mark.asyncio
    async def test_12_step_flow_substitutes_recorded_execution_id(self):
        """First step call (exec_id=None) matched by slug-positional; SDK
        substitutes recorded execution_id; subsequent steps match by exact
        (execution_id, step_index)."""
        from noukai_sdk import trace_scope

        recorded_exec_id = "rec-step-exec"
        execs = [
            session_execution(
                execution_id=recorded_exec_id,
                trigger_type="step",
                slug="grade-3",
                steps=[
                    {
                        "stepId": "s-1", "blockId": "b-1", "attempt": 1,
                        "inputSnapshot": {}, "outputSnapshot": {"step": 1},
                        "errorSnapshot": None, "truncated": False,
                        "startedAt": "t", "completedAt": "t",
                    },
                    {
                        "stepId": "s-2", "blockId": "b-2", "attempt": 1,
                        "inputSnapshot": {}, "outputSnapshot": {"step": 2},
                        "errorSnapshot": None, "truncated": False,
                        "startedAt": "t", "completedAt": "t",
                    },
                ],
            ),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected outbound: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                flow = client.flow("org/proj/grade-3")
                steps_seen = []
                async for evt in flow.events(message="hi"):
                    steps_seen.append(evt)
        await client.aclose()
        # Verify SDK reconstructed step_completed events from snapshots.
        step_outputs = [
            e.output for e in steps_seen
            if getattr(e, "event_type", None) == "step_completed"
        ]
        assert step_outputs == [{"step": 1}, {"step": 2}]

    @pytest.mark.asyncio
    async def test_13_parallel_step_flows_to_same_slug(self):
        """Two parallel step-through flows to slug A: first-call slug-positional
        per flow, then exact match per recorded execution_id."""
        from noukai_sdk import trace_scope
        import asyncio

        execs = [
            session_execution(
                execution_id="rec-A1", trigger_type="step", slug="A",
                steps=[{"stepId": "s-1", "blockId": "b", "attempt": 1,
                        "inputSnapshot": {}, "outputSnapshot": {"flow": "A1"},
                        "errorSnapshot": None, "truncated": False,
                        "startedAt": "t", "completedAt": "t"}],
            ),
            session_execution(
                execution_id="rec-A2", trigger_type="step", slug="A",
                steps=[{"stepId": "s-1", "blockId": "b", "attempt": 1,
                        "inputSnapshot": {}, "outputSnapshot": {"flow": "A2"},
                        "errorSnapshot": None, "truncated": False,
                        "startedAt": "t", "completedAt": "t"}],
            ),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)

        async def consume(msg: str) -> list:
            results = []
            async for e in client.flow("org/proj/A").events(message=msg):
                if getattr(e, "event_type", None) == "step_completed":
                    results.append(e.output)
            return results

        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                r1, r2 = await asyncio.gather(consume("1"), consume("2"))
        await client.aclose()
        # Order within asyncio.gather is preserved: r1 → first slug A, r2 → second.
        assert {r1[0]["flow"], r2[0]["flow"]} == {"A1", "A2"}

    @pytest.mark.asyncio
    async def test_14_recorded_error_is_reraised(self):
        """A recorded error_snapshot triggers a re-raise of the same error type."""
        from noukai_sdk import FlowExecutionError, trace_scope

        execs = [session_execution(
            execution_id="r-1",
            result=None,
            error={"code": "FLOW_EXECUTION_ERROR", "message": "boom"},
        )]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                with pytest.raises(FlowExecutionError, match="boom"):
                    await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_14b_null_slug_recording_surfaces_deleted_flow_hint(self):
        """When the BE returns a recording with slug=None (underlying flow
        deleted), the matcher cannot resolve by name. ReplayMissError mentions
        the deleted-flow hint so the user knows to use a session captured
        before the deletion.

        This is the regression test for code-review C1 — the matcher used to
        require the full ``org/project/slug`` prefix, masking the real
        backend's bare-slug + nullable-slug behavior.
        """
        from noukai_sdk import trace_scope

        execs = [
            {
                "executionId": "exec-null",
                "flowId": "flow-deleted",
                "slug": None,  # ← BE sends None when flow has been deleted
                "triggerType": "execute",
                "status": "completed",
                "snapshotsAvailable": True,
                "traceCaptureMode": "full",
                "steps": [],
            }
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        miss_message = ""
        with replay_enabled():
            # The matcher raises ReplayMissError on the user-code call. The
            # scope's exit then sees an unconsumed execution and would raise
            # ReplayLeftoverError. We capture the ReplayMissError message
            # outside the scope by inspecting the chained exception.
            with pytest.raises((ReplayMissError, ReplayLeftoverError)) as exc_info:
                async with trace_scope(
                    replay_session_id="11111111-1111-4111-8111-111111111111",
                    transport=client._transport,
                ):
                    try:
                        await client.flow("acme/spelling/grade-3").execute(message="hi")
                    except ReplayMissError as miss:
                        miss_message = str(miss)
                        raise
        await client.aclose()
        # The matcher raised ReplayMissError with the deleted-flow hint (we
        # captured it before scope-exit replaced it with ReplayLeftoverError).
        assert "deleted flow" in miss_message or "null/empty slug" in miss_message, (
            f"Expected deleted-flow hint in ReplayMissError, got: {miss_message!r}; "
            f"final exception: {exc_info.value!r}"
        )

    @pytest.mark.asyncio
    async def test_14c_bare_slug_matches_real_be_wire_shape(self):
        """End-to-end check that the matcher accepts the BE's bare-slug shape.

        Regression test for code-review C1: fixtures used to ship
        ``"acme/spelling/grade-3"`` in the SessionExecution.slug field, but
        the real BE ships bare ``"grade-3"``. Without this fix, every real
        replay against the backend would miss on the first call.
        """
        from noukai_sdk import trace_scope

        execs = [session_execution(slug="grade-3", result={"matched": True})]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                # Client-facing slug is org/project/slug; matcher uses bare 'grade-3'.
                result = await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()
        assert result.output == {"matched": True}

    @pytest.mark.asyncio
    async def test_15_extra_code_call_raises_replay_miss(self):
        from noukai_sdk import trace_scope

        execs = [session_execution(result={"only": "one"})]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                await client.flow("acme/spelling/grade-3").execute(message="hi")
                with pytest.raises(ReplayMissError):
                    await client.flow("acme/spelling/grade-3").execute(message="hi2")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_16_unconsumed_executions_raise_leftover(self):
        from noukai_sdk import trace_scope

        execs = [
            session_execution(execution_id="a", result={"i": 0}),
            session_execution(execution_id="b", result={"i": 1}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            with pytest.raises(ReplayLeftoverError):
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    await client.flow("acme/spelling/grade-3").execute(message="hi")
                    # Only consumed 1 of 2 — should raise on scope exit.
        await client.aclose()

    @pytest.mark.asyncio
    async def test_17_explicit_session_id_kwarg_in_replay(self):
        """Passing session_id= as an explicit kwarg in replay mode bypasses
        the contextvar lookup for that one call. (Edge case — design § Escape
        hatches.)"""
        from noukai_sdk import trace_scope

        execs = [
            session_execution(execution_id="from-contextvar", result={"x": 0}),
            session_execution(execution_id="from-explicit", result={"x": 1}),
        ]
        explicit_execs = [session_execution(execution_id="from-explicit", result={"x": 99})]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/11111111-1111-4111-8111-111111111111" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            if "/seq/sessions/22222222-2222-4222-8222-222222222222" in request.url.path:
                return httpx.Response(200, json=session_response(executions=explicit_execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                first = await client.flow("acme/spelling/grade-3").execute(message="hi")
                explicit = await client.flow("acme/spelling/grade-3").execute(
                    message="hi", session_id="22222222-2222-4222-8222-222222222222",
                )
        await client.aclose()
        assert first.output == {"x": 0}
        # Verified the explicit kwarg used the override session
        assert explicit.output == {"x": 99}


# ---------------------------------------------------------------------------
# Production safety (scenarios 18–20)
# ---------------------------------------------------------------------------


class TestProductionSafety:
    @pytest.mark.asyncio
    async def test_18_replay_header_ignored_when_env_var_unset(self):
        """X-Noukai-Replay is silently ignored without NOUKAI_REPLAY_ENABLED=true.

        Behavior: scope opens in CAPTURE mode (since trace_scope was opened
        with replay_session_id but the env var is unset). The outbound execute
        proceeds normally, tagged with a *new* capture session_id."""
        from noukai_sdk import trace_scope

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["session_id_header"] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_client_with_handler(handler)
        with replay_disabled():
            async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111") as scope:
                await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()
        assert captured["path"].endswith("/execute"), "Should have made the real call"
        # The header carries the freshly-generated capture session_id, not "11111111-1111-4111-8111-111111111111".
        assert captured["session_id_header"] == scope.session_id
        assert captured["session_id_header"] != "11111111-1111-4111-8111-111111111111"

    @pytest.mark.asyncio
    async def test_19_403_maps_to_replay_forbidden_error(self):
        from noukai_sdk import trace_scope

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(403, json={"detail": {"code": "FORBIDDEN", "message": "no"}})
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            with pytest.raises(ReplayForbiddenError):
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    pass
        await client.aclose()

    @pytest.mark.asyncio
    async def test_20_410_maps_to_replay_session_expired_error(self):
        from noukai_sdk import trace_scope

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(410, json={"detail": {"code": "GONE", "message": "expired"}})
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_client_with_handler(handler)
        with replay_enabled():
            with pytest.raises(ReplaySessionExpiredError):
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    pass
        await client.aclose()


# Additional production-safety tests for sibling errors

class TestProductionSafetyExtras:
    @pytest.mark.asyncio
    async def test_404_maps_to_session_not_found(self):
        from noukai_sdk import trace_scope

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": {"code": "NOT_FOUND", "message": "no"}})

        client = make_client_with_handler(handler)
        with replay_enabled():
            with pytest.raises(ReplaySessionNotFoundError):
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    pass
        await client.aclose()

    @pytest.mark.asyncio
    async def test_400_maps_to_invalid_session(self):
        from noukai_sdk import trace_scope

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": {"code": "BAD_REQUEST", "message": "no"}})

        client = make_client_with_handler(handler)
        with replay_enabled():
            with pytest.raises(ReplayInvalidSessionError):
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    pass
        await client.aclose()

    @pytest.mark.asyncio
    async def test_snapshots_available_false_raises(self):
        from noukai_sdk import trace_scope

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=session_response(executions=[
                    session_execution(snapshots_available=False)
                ]),
            )

        client = make_client_with_handler(handler)
        with replay_enabled():
            with pytest.raises(ReplayNoSnapshotsError):
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    pass
        await client.aclose()


# ---------------------------------------------------------------------------
# Edge / undefined (scenarios 21–22)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_21_concurrent_same_slug_emits_warning(self):
        """Per design, concurrent same-slug execute() is undefined behavior.
        We assert detection: a warning is emitted; result correctness is not
        asserted."""
        from noukai_sdk import trace_scope
        import asyncio
        import warnings

        execs = [
            session_execution(execution_id="A1", slug="A", result={"x": "A1"}),
            session_execution(execution_id="A2", slug="A", result={"x": "A2"}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=session_response(executions=execs))

        client = make_client_with_handler(handler)
        with replay_enabled():
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                async with trace_scope(replay_session_id="11111111-1111-4111-8111-111111111111", transport=client._transport):
                    await asyncio.gather(
                        client.flow("a/b/A").execute(message="1"),
                        client.flow("a/b/A").execute(message="2"),
                    )
                # Either warning is emitted, or implementation chose to skip.
                # Required: detection mechanism exists. Acceptable: warning OR
                # docs note. We test for at least one warning of any kind.
                # If implementation chose silence, this test should be updated
                # to xfail with a clear note.
                replay_warnings = [w for w in caught if "concurrent" in str(w.message).lower()]
                assert len(replay_warnings) >= 1
        await client.aclose()

    @pytest.mark.asyncio
    async def test_22_execute_outside_decorator_behaves_normally(self):
        """No scope → no session header, normal pass-through."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            captured["replay_in"] = request.headers.get(HEADER_REPLAY, "")
            return ok_execute_response()

        client = make_client_with_handler(handler)
        result = await client.flow("acme/spelling/grade-3").execute(message="hi")
        await client.aclose()
        assert result.output == {"ok": True}
        assert captured[HEADER_SESSION_ID] == ""
        # We don't send the replay-in header ourselves.
        assert captured["replay_in"] == ""
