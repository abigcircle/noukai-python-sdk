"""Integration tests for the replay feature against a live Noukai server.

Covers the @noukai.trace decorator / trace_scope context manager end-to-end:

- **Capture mode** (live today): scope opens, X-Session-Id flows on the wire,
  result.session_id surfaces, no breakage of existing execute() contract.
- **Replay mode** (gated): requires the backend session-grouping endpoint
  (see design 20260605-BE-execution-session-grouping). When that ships, set
  NOUKAI_INTEGRATION_REPLAY_READY=1 to enable the replay round-trip tests.
- **FastAPI adapter** (live today): real route, real backend, asserts that
  the X-Noukai-Session response header is set on the user-facing response.

Inherits the base skipif from conftest — entire file skips when integration
env vars are unset. See conftest.py for the env-var contract.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from noukai_sdk import (
    AsyncFlow,
    AsyncNoukai,
    ExecuteResult,
    Flow,
    FlowCompleted,
    Noukai,
    ReplayError,
    ReplayLeftoverError,
    ReplayMissError,
    RunStarted,
    StepCompleted,
    StreamEvent,
    current_session_id,
    trace_scope,
    trace_scope_sync,
)

# --------------------------------------------------------------------------- #
# Gates                                                                       #
# --------------------------------------------------------------------------- #

REPLAY_READY = os.environ.get("NOUKAI_INTEGRATION_REPLAY_READY") in {"1", "true", "True"}
"""When set, exercise the replay round-trip against the live BE session API."""

requires_replay_backend = pytest.mark.skipif(
    not REPLAY_READY,
    reason="Replay-mode integration requires the BE session-grouping endpoint. "
    "Set NOUKAI_INTEGRATION_REPLAY_READY=1 when it ships.",
)


@contextmanager
def replay_enabled_env() -> Any:
    """Patch NOUKAI_REPLAY_ENABLED=true for the duration of the block."""
    with patch.dict(os.environ, {"NOUKAI_REPLAY_ENABLED": "true"}):
        yield


# --------------------------------------------------------------------------- #
# Capture mode — live against the real backend                                #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_capture_scope_surfaces_session_id_on_result(hello_flow: Flow) -> None:
    """trace_scope_sync opens capture mode; result.session_id is populated.

    This is the headline capture-mode contract: a user wraps their handler in
    the scope, calls execute() normally, and the resulting ExecuteResult
    carries the session id so they can log / surface it later.
    """
    with trace_scope_sync() as scope:
        scope_session_id = scope.session_id
        result = hello_flow.execute(message="capture-mode integration check")

    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    assert result.session_id == scope_session_id
    # session_id is a UUID v4 per R9 (Phase 5 design).
    assert uuid.UUID(scope_session_id).version == 4


@pytest.mark.integration
def test_capture_current_session_id_visible_during_call(hello_flow: Flow) -> None:
    """current_session_id() returns the scope id while the user is inside the
    scope, and returns None after exit. Confirms contextvar propagation
    survives the synchronous execute() round-trip."""
    assert current_session_id() is None  # outside scope

    with trace_scope_sync() as scope:
        observed_inside = current_session_id()
        result = hello_flow.execute(message="contextvar check")

    assert observed_inside == scope.session_id
    assert current_session_id() is None  # restored after exit
    assert result.session_id == scope.session_id


@pytest.mark.integration
def test_capture_multiple_calls_share_session_id(hello_flow: Flow) -> None:
    """Two execute() calls inside the same scope share the session id —
    proves the scope is a request-spanning, not call-spanning, unit. This is
    the property session-grouping on the backend relies on to cluster
    executions."""
    with trace_scope_sync() as scope:
        a = hello_flow.execute(message="first")
        b = hello_flow.execute(message="second")

    assert a.session_id == scope.session_id
    assert b.session_id == scope.session_id
    assert a.session_id == b.session_id
    # Each call is still its own execution (different execution_ids).
    assert a.execution_id != b.execution_id


@pytest.mark.integration
async def test_async_capture_scope_surfaces_session_id(async_hello_flow: AsyncFlow) -> None:
    """Async parity for the headline capture contract."""
    async with trace_scope() as scope:
        result = await async_hello_flow.execute(message="async capture check")

    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    assert result.session_id == scope.session_id


@pytest.mark.integration
def test_capture_does_not_break_unwrapped_calls(hello_flow: Flow) -> None:
    """Calls made outside any scope keep the pre-replay behavior: result.session_id
    is None, no header injected, no error. This is the backwards-compat contract
    — code that hasn't adopted the decorator must see identical behavior."""
    result = hello_flow.execute(message="no scope")
    assert isinstance(result, ExecuteResult)
    assert result.status == "completed"
    assert result.session_id is None


# --------------------------------------------------------------------------- #
# FastAPI adapter — real route, real backend                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_fastapi_adapter_surfaces_session_header() -> None:
    """End-to-end with the FastAPI adapter: real backend call from inside the
    middleware, X-Noukai-Session response header set on the user-facing response.

    Skips automatically if FastAPI / Starlette aren't installed (optional deps).

    Builds its own AsyncNoukai inside the test rather than using the async_client
    fixture. Starlette's TestClient drives the ASGI app on its own internal
    event loop; mixing it with pytest-asyncio's loop (which is what the fixture
    teardown runs on) leads to RuntimeError: Event loop is closed at teardown.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("starlette")

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from noukai_sdk._constants import HEADER_RESPONSE_SESSION
    from noukai_sdk.adapters.fastapi import NoukaiTraceMiddleware

    integration_key = os.environ["NOUKAI_INTEGRATION_KEY"]
    org, project = os.environ["NOUKAI_INTEGRATION_PROJECT"].split("/", 1)
    hello_slug = os.environ["NOUKAI_INTEGRATION_HELLO_SLUG"]

    client = AsyncNoukai(api_key=integration_key, org=org, project=project)
    app = FastAPI()
    app.add_middleware(NoukaiTraceMiddleware, client=client)

    @app.post("/run")
    async def run() -> dict[str, Any]:
        result = await client.flow(hello_slug).execute(message="adapter integration")
        return {"status": result.status, "session_id": result.session_id}

    with TestClient(app) as tc:
        resp = tc.post("/run")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["session_id"] is not None

    # X-Noukai-Session response header set by the middleware, matches the
    # session_id observed by the route via result.session_id.
    header_keys = {k.lower() for k in resp.headers}
    assert HEADER_RESPONSE_SESSION.lower() in header_keys
    assert resp.headers[HEADER_RESPONSE_SESSION] == body["session_id"]


# --------------------------------------------------------------------------- #
# Replay mode — gated on BE session-grouping endpoint                          #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@requires_replay_backend
def test_replay_round_trip_returns_recorded_output(client: Noukai, hello_flow: Flow) -> None:
    """End-to-end capture → replay round trip:

    1. Capture: run a real execute() inside a scope, record the session id and
       the returned output.
    2. Replay: open a new scope bound to the captured session id, run the SAME
       execute() call, and verify the SDK serves the recorded output (does not
       hit the model again).

    The replay assertion ('does not hit the live model') is observable because
    the replay path raises ReplayMissError if the call doesn't match a recorded
    execution. If the BE didn't persist the snapshot, the test fails fast with
    ReplayMissError or ReplaySessionNotFoundError.
    """
    # --- Capture ---
    with trace_scope_sync() as scope:
        captured = hello_flow.execute(message="replay round-trip seed")
        captured_session_id = scope.session_id

    assert captured.status == "completed"
    assert captured.session_id == captured_session_id

    # --- Replay ---
    with replay_enabled_env(), trace_scope_sync(
        replay_session_id=captured_session_id, transport=client._transport
    ) as scope2:
        replayed = hello_flow.execute(message="replay round-trip seed")

    assert scope2.mode.value == "replay"
    assert replayed.status == captured.status
    assert replayed.execution_id == captured.execution_id
    # Same output — the model was not re-invoked.
    assert replayed.output == captured.output


@pytest.mark.integration
@requires_replay_backend
def test_replay_miss_when_call_differs_from_recording(
    client: Noukai, hello_flow: Flow
) -> None:
    """Capture one call; replay a different shape of call → ReplayMissError.

    Specifically: capture a single execute(); then inside the replay scope call
    execute() TWICE for the same slug. The first lookup succeeds; the second
    has no recording at slug position 1 and must raise ReplayMissError."""
    with trace_scope_sync() as scope:
        hello_flow.execute(message="first and only recorded call")
        captured_session_id = scope.session_id

    with replay_enabled_env(), trace_scope_sync(
        replay_session_id=captured_session_id, transport=client._transport
    ):
        hello_flow.execute(message="first replayed call")  # OK — matches position 0
        with pytest.raises(ReplayError):
            hello_flow.execute(message="second replayed call — no recording")


# --------------------------------------------------------------------------- #
# Complex replay — multi-flow, mixed-API scopes                                #
# --------------------------------------------------------------------------- #
#
# Production replay isn't just one execute(): a real route handler typically
# mixes execute() against a worker flow, events() to forward SSE to a frontend,
# and possibly multiple flow slugs in the same request. These tests exercise
# that surface against a live backend.
#
# All gated on NOUKAI_INTEGRATION_REPLAY_READY=1 — they all replay, which
# needs the BE session-grouping endpoint to be live.


@pytest.mark.integration
@requires_replay_backend
def test_complex_replay_mixed_execute_and_events_across_flows(
    client: Noukai, hello_flow: Flow, two_step_flow: Flow
) -> None:
    """Realistic multi-flow scope: capture an execute() on one flow, an events()
    iteration over a different multi-block flow, and a second execute(); then
    replay the same sequence and verify every call returns recorded data.

    This is the production shape: an API handler doing some setup work via
    execute(), then streaming a multi-step inner flow to the user via events(),
    then a post-processing execute(). Replay must serve all three from the
    cassette in the same order they were captured.
    """
    # --- Capture phase ---
    with trace_scope_sync() as scope:
        captured_hello_1 = hello_flow.execute(message="setup call")
        assert isinstance(captured_hello_1, ExecuteResult)
        captured_two_step_events: list[StreamEvent] = list(
            two_step_flow.events(message="streaming inner flow")
        )
        captured_hello_2 = hello_flow.execute(message="post-processing call")
        assert isinstance(captured_hello_2, ExecuteResult)
        captured_session_id = scope.session_id

    # Sanity on the captured shape: 2-block flow → at least 1 RunStarted, 2
    # StepCompleted, 1 FlowCompleted. (live runs may emit additional event
    # types — that's fine, we only assert the canonical core is present.)
    captured_step_completed = [e for e in captured_two_step_events if isinstance(e, StepCompleted)]
    assert len(captured_step_completed) == 2, (
        f"Expected 2 StepCompleted events from the two-step fixture, got "
        f"{len(captured_step_completed)}. Verify NOUKAI_INTEGRATION_TWO_STEP_SLUG."
    )

    # --- Replay phase ---
    with replay_enabled_env(), trace_scope_sync(
        replay_session_id=captured_session_id, transport=client._transport
    ) as replay_scope:
        replayed_hello_1 = hello_flow.execute(message="setup call")
        assert isinstance(replayed_hello_1, ExecuteResult)
        replayed_two_step_events = list(two_step_flow.events(message="streaming inner flow"))
        replayed_hello_2 = hello_flow.execute(message="post-processing call")
        assert isinstance(replayed_hello_2, ExecuteResult)

    assert replay_scope.mode.value == "replay"

    # Same execution_id on each execute() call → the model wasn't re-invoked.
    assert replayed_hello_1.execution_id == captured_hello_1.execution_id
    assert replayed_hello_2.execution_id == captured_hello_2.execution_id
    assert replayed_hello_1.output == captured_hello_1.output
    assert replayed_hello_2.output == captured_hello_2.output

    # Replay events emit the canonical order: RunStarted first, FlowCompleted last,
    # one StepCompleted per block in between.
    assert isinstance(replayed_two_step_events[0], RunStarted)
    assert isinstance(replayed_two_step_events[-1], FlowCompleted)
    replayed_step_completed = [e for e in replayed_two_step_events if isinstance(e, StepCompleted)]
    assert len(replayed_step_completed) == 2

    # Step-level identity: the replayed step_ids match the captured ones in the
    # same order — proves the reconstructor walked the cassette's step snapshots
    # in their original order.
    captured_step_ids = [e.step_id for e in captured_step_completed]
    replayed_step_ids = [e.step_id for e in replayed_step_completed]
    assert replayed_step_ids == captured_step_ids

    # Step-level output fidelity: each replayed step's output payload equals
    # the captured one. This is the real proof that the cassette serves the
    # same content — step_id parity alone could pass on a refactor that
    # preserved ids while changing payloads.
    captured_step_outputs = [e.output for e in captured_step_completed]
    replayed_step_outputs = [e.output for e in replayed_step_completed]
    assert replayed_step_outputs == captured_step_outputs

    # Terminal result fidelity on the events stream's FlowCompleted.
    captured_flow_completed = [e for e in captured_two_step_events if isinstance(e, FlowCompleted)]
    replayed_flow_completed = [e for e in replayed_two_step_events if isinstance(e, FlowCompleted)]
    assert captured_flow_completed
    assert replayed_flow_completed
    assert replayed_flow_completed[-1].result == captured_flow_completed[-1].result


@pytest.mark.integration
@requires_replay_backend
def test_replay_events_reconstructs_canonical_sse_sequence(
    client: Noukai, two_step_flow: Flow
) -> None:
    """events() under replay emits the canonical reconstructed sequence:
    RunStarted → (StepStarted? + StepCompleted) × N → FlowCompleted.

    Phase 7 reconstructs the SSE stream from step snapshots, not from a
    captured byte stream. The test asserts the canonical contract, not
    byte-for-byte equivalence with the live capture (which may include
    extra event types like StepInput/StepOutput that the reconstructor omits).
    """
    with trace_scope_sync() as scope:
        list(two_step_flow.events(message="reconstruction test"))
        captured_session_id = scope.session_id

    with replay_enabled_env(), trace_scope_sync(
        replay_session_id=captured_session_id, transport=client._transport
    ):
        replayed = list(two_step_flow.events(message="reconstruction test"))

    # Canonical reconstruction contract.
    type_seq = [type(e).__name__ for e in replayed]
    assert type_seq[0] == "RunStarted", f"first event must be RunStarted, got {type_seq[0]}"
    assert type_seq[-1] == "FlowCompleted", (
        f"last event must be FlowCompleted, got {type_seq[-1]}"
    )

    step_completed_count = type_seq.count("StepCompleted")
    assert step_completed_count == 2, (
        f"two-block flow must reconstruct 2 StepCompleted events, got "
        f"{step_completed_count}. Full sequence: {type_seq}"
    )


@pytest.mark.integration
@requires_replay_backend
def test_replay_leftover_error_when_scope_closes_with_unconsumed_executions(
    client: Noukai, hello_flow: Flow
) -> None:
    """Capture 3 executions, replay only 2 → ReplayLeftoverError on scope close
    naming the unconsumed slug position.

    This is the "drift detected" signal: user code changed since recording and
    is now making fewer calls than the cassette has. Surfaced as an error so
    the user knows the replay is incomplete (R6).
    """
    with trace_scope_sync() as scope:
        hello_flow.execute(message="call 1")
        hello_flow.execute(message="call 2")
        hello_flow.execute(message="call 3")
        captured_session_id = scope.session_id

    def _replay_with_too_few_calls() -> None:
        with replay_enabled_env(), trace_scope_sync(
            replay_session_id=captured_session_id, transport=client._transport
        ):
            hello_flow.execute(message="call 1")
            hello_flow.execute(message="call 2")
            # No third call → cassette has one execution unconsumed.

    with pytest.raises(ReplayLeftoverError) as exc_info:
        _replay_with_too_few_calls()

    # R6: the error message identifies what was left behind. The exact format
    # is implementation-defined but must mention the slug or position.
    assert "1 unconsumed" in str(exc_info.value) or "hello" in str(exc_info.value).lower()


@pytest.mark.integration
@requires_replay_backend
def test_replay_miss_on_wrong_flow_slug(
    client: Noukai, hello_flow: Flow, two_step_flow: Flow
) -> None:
    """Capture against hello_flow, replay against two_step_flow → ReplayMissError.

    The matcher is slug-positional. A call to a flow slug that has no recording
    in the session is a miss — not a leftover (the cassette has executions, but
    none for the slug being called)."""
    with trace_scope_sync() as scope:
        hello_flow.execute(message="recorded only against hello")
        captured_session_id = scope.session_id

    with (
        replay_enabled_env(),
        trace_scope_sync(
            replay_session_id=captured_session_id, transport=client._transport
        ),
        pytest.raises(ReplayMissError),
    ):
        two_step_flow.execute(message="wrong flow — no recording for this slug")
