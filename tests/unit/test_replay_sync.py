"""Sync-client replay scenarios — mirror of test_replay.py for Noukai (sync).

Same 22 scenarios using:
    client = Noukai(api_key="nk_test", env="dev")
    with trace_scope_sync() as scope: ...
    with replay_enabled(): with trace_scope_sync(replay_session_id="s"): ...

Scenario 13 (parallel step flows) is skipped because asyncio.gather-based
parallelism is not available in sync mode. Sync users should use the async
client for parallel flows.

Scenario 4 (step-through SSE events flow) is simplified to a single-request
stub since sync events iteration is different from async.
"""

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from noukai_sdk import Noukai
from noukai_sdk._constants import HEADER_REPLAY, HEADER_SESSION_ID
from noukai_sdk._errors import (
    ReplayForbiddenError,
    ReplayInvalidSessionError,
    ReplayLeftoverError,
    ReplayMissError,
    ReplayNoSnapshotsError,
    ReplaySessionExpiredError,
    ReplaySessionNotFoundError,
)

# ---------------------------------------------------------------------------
# Helpers (mirrors of test_replay.py helpers)
# ---------------------------------------------------------------------------


def make_sync_client_with_handler(handler: Any) -> Noukai:
    """Sync client with a mocked httpx transport."""
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
    """One execution entry in a session response."""
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
        "steps": steps
        or [
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
# Capture mode (scenarios 1–8, sync mirror)
# ---------------------------------------------------------------------------


class TestCaptureModeSync:
    """Sync decorator/scope tags each outbound execute/step with X-Session-Id."""

    def test_1_single_execute_call(self):
        """Decorator generates a fresh session_id; current_session_id() returns
        the value during the call; outbound request carries the header."""
        from noukai_sdk import current_session_id, trace_scope_sync

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)
        with trace_scope_sync() as scope:
            inside_id = current_session_id()
            assert inside_id is not None
            assert scope.session_id == inside_id
            client.flow("acme/spelling/grade-3").execute(message="hi")
        client.close()

        # Outside scope, current_session_id() is None.
        assert current_session_id() is None
        assert captured["headers"].get(HEADER_SESSION_ID.lower()) == inside_id

    def test_2_multiple_execute_calls_share_session_id(self):
        from noukai_sdk import trace_scope_sync

        captured: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({HEADER_SESSION_ID: request.headers.get(HEADER_SESSION_ID, "")})
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)
        with trace_scope_sync():
            client.flow("a/b/c").execute(message="1")
            client.flow("a/b/c").execute(message="2")
            client.flow("a/b/d").execute(message="3")
        client.close()

        sids = [h[HEADER_SESSION_ID] for h in captured]
        assert len(set(sids)) == 1
        assert sids[0]  # non-empty UUID

    def test_3_nested_function_propagates_contextvar(self):
        """Contextvar propagates through deeper sync calls (thread-local via contextvars)."""
        from noukai_sdk import trace_scope_sync

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)

        def deeper() -> None:
            client.flow("a/b/c").execute(message="hi")

        with trace_scope_sync() as scope:
            deeper()
            assert captured[HEADER_SESSION_ID] == scope.session_id
        client.close()

    def test_4_step_through_flow_shares_session_id(self):
        """Sync steps iteration shares session_id across all requests."""
        from noukai_sdk import trace_scope_sync

        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(
                {
                    "path": str(request.url.path),
                    HEADER_SESSION_ID: request.headers.get(HEADER_SESSION_ID, ""),
                }
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'event: run_started\ndata: {"runId":"r-1","executionId":"exec-1"}\n\n'
                    b'event: step_completed\ndata: {"stepId":"s-1","output":{"ok":true}}\n\n'
                    b'event: flow_completed\ndata: {"executionId":"exec-1","result":{"ok":true}}\n\n'  # noqa: E501
                ),
            )

        client = make_sync_client_with_handler(handler)
        with trace_scope_sync() as scope:
            for _ in client.flow("a/b/c").events(message="hi"):
                pass
        client.close()

        sids = {c[HEADER_SESSION_ID] for c in captured if HEADER_SESSION_ID in c}
        assert sids == {scope.session_id}

    def test_5_mixed_execute_and_step_in_same_scope(self):
        """All requests share session_id even when crossing execute/step modes."""
        from noukai_sdk import trace_scope_sync

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

        client = make_sync_client_with_handler(handler)
        with trace_scope_sync() as scope:
            client.flow("a/b/c").execute(message="hi")
            for _ in client.flow("a/b/c").events(message="hi2"):
                pass
        client.close()
        assert set(captured) == {scope.session_id}

    def test_6_no_decorator_no_session_id(self):
        """No scope → no header. Backwards compatible."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)
        client.flow("a/b/c").execute(message="hi")
        client.close()
        assert captured[HEADER_SESSION_ID] == ""

    def test_7_explicit_kwarg_overrides_contextvar(self):
        """session_id kwarg wins over scope-generated session_id."""
        from noukai_sdk import trace_scope_sync

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)
        with trace_scope_sync() as scope:
            client.flow("a/b/c").execute(message="hi", session_id="explicit-sid")
        client.close()
        assert captured[HEADER_SESSION_ID] == "explicit-sid"
        assert scope.session_id != "explicit-sid"

    def test_8_response_header_set_by_framework_adapter(self):
        """Scenario 8 is exercised in Phase 8 framework-adapter tests."""
        pytest.skip("See tests/unit/test_adapters_fastapi.py — owned by Phase 8.")


# ---------------------------------------------------------------------------
# Replay mode (scenarios 9–17, sync mirror)
# ---------------------------------------------------------------------------


class TestReplayModeSync:
    """Sync outbound execute/step calls are intercepted and served from cassette."""

    def test_9_single_execute_serves_recorded_output(self):
        """No outbound /execute call — only the GET /sessions/{id} prefetch."""
        from noukai_sdk import trace_scope_sync

        outbound_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            outbound_paths.append(request.url.path)
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(
                    200,
                    json=session_response(executions=[session_execution(result={"answer": 42})]),
                )
            pytest.fail(f"Unexpected outbound request in replay mode: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            result = client.flow("acme/spelling/grade-3").execute(message="hi")
        client.close()
        assert result.output == {"answer": 42}
        assert all("/seq/sessions/" in p for p in outbound_paths)

    def test_10_slug_positional_for_same_slug(self):
        """Two execute(A) → first code call gets first recorded A, second gets second A."""
        from noukai_sdk import trace_scope_sync

        execs = [
            session_execution(execution_id="r-1", result={"n": 1}),
            session_execution(execution_id="r-2", result={"n": 2}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            r1 = client.flow("acme/spelling/grade-3").execute(message="hi")
            r2 = client.flow("acme/spelling/grade-3").execute(message="hi")
        client.close()
        assert r1.output == {"n": 1}
        assert r2.output == {"n": 2}

    def test_11_independent_counters_per_slug(self):
        """execute(A), execute(B), execute(A) → matches A1, B1, A2."""
        from noukai_sdk import trace_scope_sync

        execs = [
            session_execution(execution_id="A1", slug="A", result={"x": "A1"}),
            session_execution(execution_id="B1", slug="B", result={"x": "B1"}),
            session_execution(execution_id="A2", slug="A", result={"x": "A2"}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            a1 = client.flow("org/proj/A").execute(message="hi")
            b1 = client.flow("org/proj/B").execute(message="hi")
            a2 = client.flow("org/proj/A").execute(message="hi")
        client.close()
        assert a1.output == {"x": "A1"}
        assert b1.output == {"x": "B1"}
        assert a2.output == {"x": "A2"}

    def test_12_step_flow_substitutes_recorded_execution_id(self):
        """First step call matched by slug-positional; subsequent steps match by exact
        (execution_id, step_index)."""
        from noukai_sdk import trace_scope_sync

        recorded_exec_id = "rec-step-exec"
        execs = [
            session_execution(
                execution_id=recorded_exec_id,
                trigger_type="step",
                slug="grade-3",
                steps=[
                    {
                        "stepId": "s-1",
                        "blockId": "b-1",
                        "attempt": 1,
                        "inputSnapshot": {},
                        "outputSnapshot": {"step": 1},
                        "errorSnapshot": None,
                        "truncated": False,
                        "startedAt": "t",
                        "completedAt": "t",
                    },
                    {
                        "stepId": "s-2",
                        "blockId": "b-2",
                        "attempt": 1,
                        "inputSnapshot": {},
                        "outputSnapshot": {"step": 2},
                        "errorSnapshot": None,
                        "truncated": False,
                        "startedAt": "t",
                        "completedAt": "t",
                    },
                ],
            ),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected outbound: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            flow = client.flow("org/proj/grade-3")
            steps_seen = []
            for evt in flow.events(message="hi"):
                steps_seen.append(evt)
        client.close()
        step_outputs = [
            e.output for e in steps_seen if getattr(e, "event_type", None) == "step_completed"
        ]
        assert step_outputs == [{"step": 1}, {"step": 2}]

    def test_13_parallel_step_flows_to_same_slug_skipped(self):
        """Parallel step-through flows require asyncio.gather; not available in sync mode.
        Sync users should use the async client for parallel flows."""
        pytest.skip(
            "step-through parallelism uses asyncio.gather; sync users should use the "
            "async client for parallel flows."
        )

    def test_14_recorded_error_is_reraised(self):
        """A recorded error_snapshot triggers a re-raise of the same error type."""
        from noukai_sdk import FlowExecutionError, trace_scope_sync

        execs = [
            session_execution(
                execution_id="r-1",
                result=None,
                error={"code": "FLOW_EXECUTION_ERROR", "message": "boom"},
            )
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
            pytest.raises(FlowExecutionError, match="boom"),
        ):
            client.flow("acme/spelling/grade-3").execute(message="hi")
        client.close()

    def test_15_extra_code_call_raises_replay_miss(self):
        from noukai_sdk import trace_scope_sync

        execs = [session_execution(result={"only": "one"})]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            client.flow("acme/spelling/grade-3").execute(message="hi")
            with pytest.raises(ReplayMissError):
                client.flow("acme/spelling/grade-3").execute(message="hi2")
        client.close()

    def test_16_unconsumed_executions_raise_leftover(self):
        from noukai_sdk import trace_scope_sync

        execs = [
            session_execution(execution_id="a", result={"i": 0}),
            session_execution(execution_id="b", result={"i": 1}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(200, json=session_response(executions=execs))
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            pytest.raises(ReplayLeftoverError),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            client.flow("acme/spelling/grade-3").execute(message="hi")
            # Only consumed 1 of 2 — should raise on scope exit.
        client.close()

    def test_17_explicit_session_id_kwarg_in_replay(self):
        """Passing session_id= as an explicit kwarg in replay mode bypasses
        the contextvar lookup for that one call."""
        from noukai_sdk import trace_scope_sync

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

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            first = client.flow("acme/spelling/grade-3").execute(message="hi")
            explicit = client.flow("acme/spelling/grade-3").execute(
                message="hi",
                session_id="22222222-2222-4222-8222-222222222222",
            )
        client.close()
        assert first.output == {"x": 0}
        assert explicit.output == {"x": 99}


# ---------------------------------------------------------------------------
# Production safety (scenarios 18–20, sync mirror)
# ---------------------------------------------------------------------------


class TestProductionSafetySync:
    def test_18_replay_header_ignored_when_env_var_unset(self):
        """NOUKAI_REPLAY_ENABLED unset → scope becomes CAPTURE mode."""
        from noukai_sdk import trace_scope_sync

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["session_id_header"] = request.headers.get(HEADER_SESSION_ID, "")
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)
        with (
            replay_disabled(),
            trace_scope_sync(replay_session_id="11111111-1111-4111-8111-111111111111") as scope,
        ):
            client.flow("acme/spelling/grade-3").execute(message="hi")
        client.close()
        assert captured["path"].endswith("/execute"), "Should have made the real call"
        assert captured["session_id_header"] == scope.session_id
        assert captured["session_id_header"] != "11111111-1111-4111-8111-111111111111"

    def test_19_403_maps_to_replay_forbidden_error(self):
        from noukai_sdk import trace_scope_sync

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(403, json={"detail": {"code": "FORBIDDEN", "message": "no"}})
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            pytest.raises(ReplayForbiddenError),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            pass
        client.close()

    def test_20_410_maps_to_replay_session_expired_error(self):
        from noukai_sdk import trace_scope_sync

        def handler(request: httpx.Request) -> httpx.Response:
            if "/seq/sessions/" in request.url.path:
                return httpx.Response(410, json={"detail": {"code": "GONE", "message": "expired"}})
            pytest.fail(f"Unexpected: {request.url.path}")

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            pytest.raises(ReplaySessionExpiredError),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            pass
        client.close()


class TestProductionSafetyExtrasSync:
    def test_404_maps_to_session_not_found(self):
        from noukai_sdk import trace_scope_sync

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": {"code": "NOT_FOUND", "message": "no"}})

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            pytest.raises(ReplaySessionNotFoundError),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            pass
        client.close()

    def test_400_maps_to_invalid_session(self):
        from noukai_sdk import trace_scope_sync

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": {"code": "BAD_REQUEST", "message": "no"}})

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            pytest.raises(ReplayInvalidSessionError),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            pass
        client.close()

    def test_snapshots_available_false_raises(self):
        from noukai_sdk import trace_scope_sync

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=session_response(executions=[session_execution(snapshots_available=False)]),
            )

        client = make_sync_client_with_handler(handler)
        with (
            replay_enabled(),
            pytest.raises(ReplayNoSnapshotsError),
            trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ),
        ):
            pass
        client.close()


# ---------------------------------------------------------------------------
# Edge / undefined (scenarios 21–22, sync mirror)
# ---------------------------------------------------------------------------


class TestEdgeCasesSync:
    @pytest.mark.xfail(
        reason=(
            "Sync concurrent-same-slug detection requires shared mutable state across "
            "threads. Python ContextVars use copy-on-inherit semantics: each thread "
            "copy_context() produces an isolated context, so the in-flight counter is "
            "not shared. True concurrent replay detection in sync/threaded mode requires "
            "an explicit shared lock structure — deferred to v1.1. "
            "The warning mechanism exists and is exercised by the async test_21."
        ),
        strict=False,
    )
    def test_21_concurrent_same_slug_emits_warning(self):
        """Concurrent same-slug in sync mode via threading.

        NOTE: This test is xfail because Python ContextVars do not share state
        between threads even when copy_context() is used. The in-flight counter
        used for concurrent detection is scoped per-context, so two threads
        running the same slug simultaneously cannot see each other's in-flight
        state. The async version (test_replay.py::TestEdgeCases::test_21) works
        correctly because asyncio tasks share the same context.
        """
        import threading
        import warnings
        from contextvars import copy_context

        from noukai_sdk import trace_scope_sync

        execs = [
            session_execution(execution_id="A1", slug="A", result={"x": "A1"}),
            session_execution(execution_id="A2", slug="A", result={"x": "A2"}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=session_response(executions=execs))

        client = make_sync_client_with_handler(handler)

        results: list[Any] = []
        thread_errors: list[Exception] = []

        with replay_enabled(), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with trace_scope_sync(
                replay_session_id="11111111-1111-4111-8111-111111111111",
                transport=client._transport,
            ):
                ctx1 = copy_context()
                ctx2 = copy_context()

                def run_flow_1() -> None:
                    try:
                        results.append(ctx1.run(client.flow("a/b/A").execute, message="1"))
                    except Exception as e:
                        thread_errors.append(e)

                def run_flow_2() -> None:
                    try:
                        results.append(ctx2.run(client.flow("a/b/A").execute, message="2"))
                    except Exception as e:
                        thread_errors.append(e)

                t1 = threading.Thread(target=run_flow_1)
                t2 = threading.Thread(target=run_flow_2)
                t1.start()
                t2.start()
                t1.join()
                t2.join()
            replay_warnings = [w for w in caught if "concurrent" in str(w.message).lower()]
            assert len(replay_warnings) >= 1
        client.close()

    def test_22_execute_outside_decorator_behaves_normally(self):
        """No scope → no session header, normal pass-through."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[HEADER_SESSION_ID] = request.headers.get(HEADER_SESSION_ID, "")
            captured["replay_in"] = request.headers.get(HEADER_REPLAY, "")
            return ok_execute_response()

        client = make_sync_client_with_handler(handler)
        result = client.flow("acme/spelling/grade-3").execute(message="hi")
        client.close()
        assert result.output == {"ok": True}
        assert captured[HEADER_SESSION_ID] == ""
        assert captured["replay_in"] == ""
