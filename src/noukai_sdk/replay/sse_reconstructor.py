"""Reconstructs the canonical SSE event sequence from a recorded SessionExecution.

The reconstructor is pure: input is one SessionExecution; output is an
ordered sequence of typed StreamEvent objects matching what a live execution
would have emitted.

Per design 20260605-SDK-replay-decorator § Streaming, timing is instant
(no inter-event delay).

Canonical order emitted:
    run_started                               ← once at execution start
    for each step (in stored order, started_at ASC per BE guarantee):
        step_started
        step_completed   (if no error_snapshot)
            OR step_error (if error_snapshot set)
    flow_completed                            ← terminal (even on step failure)

On step failure, a flow_completed with result=None and
summary={"failed_at_step": step_id} is emitted, then iteration stops.
The matcher (Phase 6) raises FlowExecutionError after the stream completes
when error_at_step is set on the execution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from .._models.events import (
    FlowCompleted,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepStarted,
    StreamEvent,
)
from .._models.session import SessionExecution


def _reconstruct_iter(ex: SessionExecution) -> Iterator[StreamEvent]:
    """Pure generator.  Sync iteration over the reconstructed events.

    Both async and sync entry points delegate here — there is no I/O so
    there is no reason to duplicate the logic.
    """
    # 1. RunStarted — once at execution start.
    yield RunStarted.model_validate(
        {
            "eventType": "run_started",
            "runId": ex.execution_id,
            "executionId": ex.execution_id,
            "flowId": ex.flow_id,
            "stepCount": len(ex.steps),
        }
    )

    # 2. Per-step events — trust the wire order (BE guarantees started_at ASC).
    for step in ex.steps:
        yield StepStarted.model_validate(
            {
                "eventType": "step_started",
                "stepId": step.step_id,
                # name is not stored in SessionStepSnapshot; left at default None.
            }
        )

        if step.error_snapshot is not None:
            # Step-level failure → emit step_error then a terminal flow_completed.
            yield StepFailed.model_validate(
                {
                    "eventType": "step_error",
                    "stepId": step.step_id,
                    "error": step.error_snapshot,
                }
            )
            # Q3 resolution: emit flow_completed with a failure summary.
            # Live runs may use a distinct flow_failed event; reconstruction
            # uses flow_completed so the iterator's terminal condition is
            # satisfied uniformly. The Phase 6 matcher raises FlowExecutionError
            # after stream completion when ex.error_at_step is set.
            yield FlowCompleted.model_validate(
                {
                    "eventType": "flow_completed",
                    "executionId": ex.execution_id,
                    "result": None,
                    "summary": {"failed_at_step": step.step_id},
                }
            )
            return  # stop after the failed step; no further steps or events.

        # Normal step completion — output_snapshot becomes the step output.
        yield StepCompleted.model_validate(
            {
                "eventType": "step_completed",
                "stepId": step.step_id,
                "output": step.output_snapshot,
                # durationMs / tokens / costUsd are not stored in the snapshot;
                # they are left at their None defaults.
            }
        )

    # 3. Terminal flow_completed after all steps succeeded.
    final_output = ex.steps[-1].output_snapshot if ex.steps else None
    yield FlowCompleted.model_validate(
        {
            "eventType": "flow_completed",
            "executionId": ex.execution_id,
            "result": final_output,
        }
    )


def reconstruct_events_sync(ex: SessionExecution) -> Iterator[StreamEvent]:
    """Sync generator over reconstructed SSE events.

    Delegates to the shared :func:`_reconstruct_iter` — no I/O, instant timing.
    """
    return _reconstruct_iter(ex)


async def reconstruct_events_async(ex: SessionExecution) -> AsyncIterator[StreamEvent]:
    """Async generator over reconstructed SSE events.

    Same content as :func:`reconstruct_events_sync` — adapted to an async
    surface so callers can ``async for event in reconstruct_events_async(ex)``.
    No actual I/O or awaiting occurs inside; the async keyword is purely for
    interface compatibility with the matcher's async generator.
    """
    for event in _reconstruct_iter(ex):
        yield event
