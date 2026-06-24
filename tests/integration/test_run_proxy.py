"""Integration tests: Run proxy (trace endpoints).

ALL tests in this file are xfail until the server-side prerequisite lands:
    slug-scoped  GET /seq/{org}/{project}/{slug}/runs/{id}/trace
    and           GET /seq/{org}/{project}/{slug}/runs/{id}/trace/stream
    endpoints accepting ``nk_*`` API keys.

Tests are written to run (not commented-out) so they auto-pass once the
server prereq is deployed — just remove or change the xfail marks.

Server prereq tracking: see design log 20260531-SDK-python-sdk-v1.
"""

from __future__ import annotations

import pytest

from noukai_sdk import (
    Flow,
    FlowCompleted,
    RunStarted,
    StepCompleted,
    Trace,
)

# All tests xfail until slug-scoped trace endpoints are deployed.
_XFAIL_REASON = (
    "Server prereq pending: slug-scoped /seq/.../runs/{id}/trace* endpoints "
    "accepting nk_* API keys are not yet deployed. Tests will auto-pass once "
    "the server prereq lands."
)

pytestmark = pytest.mark.xfail(reason=_XFAIL_REASON, strict=False)


@pytest.mark.integration
def test_trace_returns_typed_trace(hello_flow: Flow) -> None:
    """flow.run(execution_id).trace() returns a Trace with RunSummary + steps.

    Sequence:
    1. Execute the hello-world flow to get an execution_id.
    2. Call flow.run(execution_id).trace() to fetch the persisted trace.
    3. Assert the Trace model is populated and consistent.
    """
    result = hello_flow.execute(message="trace roundtrip test")
    assert result.execution_id is not None

    run = hello_flow.run(result.execution_id)
    trace = run.trace()

    assert isinstance(trace, Trace)
    assert trace.flow_run is not None
    assert trace.flow_run.id is not None
    assert len(trace.steps) >= 1

    for step in trace.steps:
        assert step.step_id is not None
        assert step.status in ("running", "completed", "failed", "skipped")


@pytest.mark.integration
def test_step_trace_attempt_latest(hello_flow: Flow) -> None:
    """run.step_trace(step_id) returns the latest StepTrace for a given step.

    Sequence:
    1. Execute hello-world; collect the first StepCompleted step_id from events().
    2. Call run.step_trace(step_id, attempt="latest").
    3. Assert the StepTrace fields are consistent with the execution.
    """
    from noukai_sdk import StepTrace

    step_ids: list[str] = []
    execution_id: str | None = None

    for event in hello_flow.events(message="step trace latest test"):
        if isinstance(event, RunStarted):
            execution_id = event.execution_id
        if isinstance(event, StepCompleted):
            step_ids.append(event.step_id)

    assert execution_id is not None, "RunStarted must carry execution_id"
    assert len(step_ids) >= 1, "Expected at least one StepCompleted event"

    run = hello_flow.run(execution_id)
    step_trace = run.step_trace(step_ids[0], attempt="latest")

    assert isinstance(step_trace, StepTrace)
    assert step_trace.step_id == step_ids[0]
    assert step_trace.attempt >= 1
    assert step_trace.status in ("running", "completed", "failed", "skipped")


@pytest.mark.integration
def test_step_trace_attempt_all(hello_flow: Flow) -> None:
    """run.step_trace(step_id, attempt="all") returns StepAttempts.

    For a simple hello-world flow without retries, there will be exactly
    one attempt. The model must still parse cleanly.
    """
    from noukai_sdk import StepAttempts

    step_ids: list[str] = []
    execution_id: str | None = None

    for event in hello_flow.events(message="step trace all attempts test"):
        if isinstance(event, RunStarted):
            execution_id = event.execution_id
        if isinstance(event, StepCompleted):
            step_ids.append(event.step_id)

    assert execution_id is not None
    assert len(step_ids) >= 1

    run = hello_flow.run(execution_id)
    all_attempts = run.step_trace(step_ids[0], attempt="all")

    assert isinstance(all_attempts, StepAttempts)
    assert all_attempts.step_id == step_ids[0]
    assert len(all_attempts.attempts) >= 1

    for attempt in all_attempts.attempts:
        assert attempt.attempt >= 1


@pytest.mark.integration
def test_live_trace_yields_events(hello_flow: Flow) -> None:
    """run.live_trace() streams events from the trace endpoint.

    Sequences:
    1. Execute hello-world to get a completed execution_id.
    2. Open live_trace() — should replay the persisted events then close.
    3. Assert at least one RunStarted and one FlowCompleted are yielded.
    """
    # Execute a completed run first so live_trace replays from DB.
    result = hello_flow.execute(message="live trace test")
    assert result.execution_id is not None

    run = hello_flow.run(result.execution_id)

    seen_run_started = False
    seen_flow_completed = False

    for event in run.live_trace():
        if isinstance(event, RunStarted):
            seen_run_started = True
        if isinstance(event, FlowCompleted):
            seen_flow_completed = True

    assert seen_run_started, "live_trace should replay RunStarted from DB"
    assert seen_flow_completed, "live_trace should replay FlowCompleted from DB"
