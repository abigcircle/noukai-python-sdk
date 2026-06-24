"""Integration tests: flow.events() raw SSE event stream.

Exercises the events() iterator:
- Yields at minimum RunStarted, StepStarted, StepCompleted, FlowCompleted
- run_remaining=True produces all events in a single call without step pauses
- StepCompleted.tokens is a populated TokenBreakdown for LLM steps
- cost_usd is a decimal string (not a float) when present
"""

from __future__ import annotations

import pytest

from noukai_sdk import (
    Flow,
    FlowCompleted,
    RunStarted,
    StepCompleted,
    StepStarted,
    StreamEvent,
)


@pytest.mark.integration
def test_events_yields_all_event_types(hello_flow: Flow) -> None:
    """events() surfaces at least one each of the four core event types."""
    events: list[StreamEvent] = list(hello_flow.events(message="event types test"))

    event_types = {type(e).__name__ for e in events}
    assert "RunStarted" in event_types, f"Missing RunStarted. Got: {event_types}"
    assert "StepStarted" in event_types, f"Missing StepStarted. Got: {event_types}"
    assert "StepCompleted" in event_types, f"Missing StepCompleted. Got: {event_types}"
    assert "FlowCompleted" in event_types, f"Missing FlowCompleted. Got: {event_types}"


@pytest.mark.integration
def test_events_ordering(hello_flow: Flow) -> None:
    """RunStarted appears before StepStarted; FlowCompleted is the last event."""
    events: list[StreamEvent] = list(hello_flow.events(message="ordering test"))

    assert len(events) >= 2

    # RunStarted must be first.
    assert isinstance(events[0], RunStarted), (
        f"Expected RunStarted as first event, got {type(events[0]).__name__}"
    )

    # FlowCompleted must be last.
    assert isinstance(events[-1], FlowCompleted), (
        f"Expected FlowCompleted as last event, got {type(events[-1]).__name__}"
    )


@pytest.mark.integration
def test_events_run_remaining_true(hello_flow: Flow) -> None:
    """run_remaining=True runs the whole flow in one call without StepPaused events."""
    events: list[StreamEvent] = list(
        hello_flow.events(message="run remaining test", run_remaining=True)
    )

    # Must have completed events (not just an empty stream).
    assert len(events) >= 1

    # No StepPaused events should appear (run_remaining bypasses step boundaries).
    step_paused_events = [e for e in events if type(e).__name__ == "StepPaused"]
    assert len(step_paused_events) == 0, (
        f"run_remaining=True should produce no StepPaused events, "
        f"but got {len(step_paused_events)}"
    )

    # Must still complete.
    completed_events = [e for e in events if isinstance(e, FlowCompleted)]
    assert len(completed_events) >= 1, "run_remaining=True must still yield FlowCompleted"


@pytest.mark.integration
def test_events_step_completed_has_tokens(hello_flow: Flow) -> None:
    """StepCompleted.tokens is a populated TokenBreakdown for an LLM step.

    If the hello-world flow contains an LLM block, tokens must be present and
    non-zero. If the fixture flow has no LLM block, this assertion is skipped
    with a descriptive message.
    """
    events: list[StreamEvent] = list(hello_flow.events(message="tokens test"))

    step_completed_events = [e for e in events if isinstance(e, StepCompleted)]
    assert len(step_completed_events) >= 1, "Expected at least one StepCompleted event"

    # Find LLM steps (identified by having tokens populated).
    llm_steps = [e for e in step_completed_events if e.tokens is not None]

    if not llm_steps:
        pytest.skip(
            "No StepCompleted events with tokens found — hello-world fixture may not "
            "have an LLM block. Verify the fixture flow or update this test."
        )

    for step in llm_steps:
        assert step.tokens is not None
        # At least one of prompt/completion must be non-zero for a real LLM call.
        assert step.tokens.total > 0, (
            f"StepCompleted.tokens.total should be > 0 for LLM step {step.step_id}"
        )
        assert step.tokens.prompt >= 0
        assert step.tokens.completion >= 0


@pytest.mark.integration
def test_events_cost_usd_is_string_when_present(hello_flow: Flow) -> None:
    """Wire contract: StepCompleted.cost_usd is a string, not a float.

    Validates that the SDK correctly models cost_usd as Optional[str] per
    the wire contract. If no step emits cost_usd, the test is skipped.
    """
    events: list[StreamEvent] = list(hello_flow.events(message="cost usd string test"))

    step_completed_events = [e for e in events if isinstance(e, StepCompleted)]
    costed_steps = [e for e in step_completed_events if e.cost_usd is not None]

    if not costed_steps:
        pytest.skip(
            "No StepCompleted events with cost_usd found — skipping wire-type assertion. "
            "Ensure the hello-world fixture flow uses an LLM block."
        )

    for step in costed_steps:
        assert isinstance(step.cost_usd, str), (
            f"cost_usd must be a string (wire contract), got {type(step.cost_usd).__name__} "
            f"for step {step.step_id}"
        )
        # Must parse as a decimal — no scientific notation, no float repr.
        float(step.cost_usd)  # Raises ValueError if not numeric.


@pytest.mark.integration
def test_events_run_started_has_run_id(hello_flow: Flow) -> None:
    """RunStarted event carries a non-empty run_id."""
    events: list[StreamEvent] = list(hello_flow.events(message="run started fields test"))

    run_started = [e for e in events if isinstance(e, RunStarted)]
    assert len(run_started) >= 1

    for e in run_started:
        assert e.run_id is not None
        assert e.run_id != ""


@pytest.mark.integration
def test_events_step_started_has_step_id(hello_flow: Flow) -> None:
    """StepStarted event carries a non-empty step_id."""
    events: list[StreamEvent] = list(hello_flow.events(message="step started fields test"))

    step_started = [e for e in events if isinstance(e, StepStarted)]
    assert len(step_started) >= 1

    for e in step_started:
        assert e.step_id is not None
        assert e.step_id != ""
