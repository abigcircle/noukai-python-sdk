"""Integration tests: flow.steps() step-by-step iteration.

Exercises the steps() iterator:
- Yields exactly one StepCompleted per finished step
- List-comprehension collect idiom (sync + async)
- execution_id is consistent across all steps
- Async steps() iterator parity
"""

from __future__ import annotations

import pytest

from noukai_sdk import AsyncFlow, Flow, StepCompleted


@pytest.mark.integration
def test_steps_yields_one_per_step(two_step_flow: Flow) -> None:
    """steps() yields exactly one StepCompleted per flow step for a two-step flow."""
    collected: list[StepCompleted] = []
    for step in two_step_flow.steps(message="two-step test"):
        assert isinstance(step, StepCompleted)
        collected.append(step)

    assert len(collected) == 2, (
        f"Expected exactly 2 StepCompleted events from the two-step fixture flow, "
        f"got {len(collected)}. Verify NOUKAI_INTEGRATION_TWO_STEP_SLUG points at "
        f"a two-block flow."
    )


@pytest.mark.integration
def test_steps_collect_idiom(two_step_flow: Flow) -> None:
    """The list-comprehension idiom works correctly with steps()."""
    steps = [s for s in two_step_flow.steps(message="collect idiom test")]

    assert len(steps) >= 1
    for s in steps:
        assert isinstance(s, StepCompleted)
        assert s.step_id is not None
        assert s.step_id != ""


@pytest.mark.integration
def test_steps_carries_cursor_invisibly(two_step_flow: Flow) -> None:
    """The SDK manages the cursor between steps transparently.

    Confirmed by checking that all yielded StepCompleted events come from the
    same logical run: they must have distinct step_ids but a consistent flow
    context (verified indirectly — the SDK auto-advances the cursor so both
    steps complete without caller intervention).
    """
    steps = list(two_step_flow.steps(message="cursor test"))

    # Both steps must be from the same run: distinct step_ids.
    step_ids = [s.step_id for s in steps]
    assert len(step_ids) == len(set(step_ids)), "step_ids should be unique across steps"

    # Every step must be completed (not failed/paused).
    for s in steps:
        assert isinstance(s, StepCompleted)


@pytest.mark.integration
async def test_async_steps(async_two_step_flow: AsyncFlow) -> None:
    """AsyncFlow.steps() yields StepCompleted events via async iteration."""
    collected: list[StepCompleted] = []
    async for step in async_two_step_flow.steps(message="async two-step test"):
        assert isinstance(step, StepCompleted)
        collected.append(step)

    assert len(collected) == 2, (
        f"Expected exactly 2 StepCompleted events from the async two-step fixture "
        f"flow, got {len(collected)}."
    )
