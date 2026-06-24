"""Typed SSE events emitted by /seq/.../step.

Server's wire vocabulary lives in flow_run_trace.TraceEventType (see CONTEXT
gotcha #10 in router-ai-slugs/CONTEXT.md). Each event arrives as one SSE
frame with eventType={...} in the JSON payload."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ._aliases import WIRE_CONFIG


class _BaseEvent(BaseModel):
    model_config = WIRE_CONFIG
    event_type: str = Field(alias="eventType")


class RunStarted(_BaseEvent):
    event_type: Literal["run_started", "flow_started"] = Field(alias="eventType")
    run_id: str = Field(alias="runId")
    execution_id: str | None = Field(default=None, alias="executionId")
    flow_id: str | None = Field(default=None, alias="flowId")
    step_count: int | None = Field(default=None, alias="stepCount")


class StepStarted(_BaseEvent):
    event_type: Literal["step_started"] = Field(alias="eventType")
    step_id: str = Field(alias="stepId")
    name: str | None = None
    step_index: int | None = Field(default=None, alias="stepIndex")


class StepInput(_BaseEvent):
    event_type: Literal["step_input"] = Field(alias="eventType")
    step_id: str = Field(alias="stepId")
    input_data: dict[str, Any] | None = Field(default=None, alias="inputData")


class StepOutput(_BaseEvent):
    """Partial or final output emitted during a step."""

    event_type: Literal["step_output"] = Field(alias="eventType")
    step_id: str = Field(alias="stepId")
    output_data: Any = Field(default=None, alias="outputData")


class _TokenBreakdown(BaseModel):
    model_config = WIRE_CONFIG
    prompt: int = 0
    completion: int = 0
    total: int = 0


class StepCompleted(_BaseEvent):
    event_type: Literal["step_completed"] = Field(alias="eventType")
    step_id: str = Field(alias="stepId")
    name: str | None = None
    output: Any = None
    duration_ms: int | None = Field(default=None, alias="durationMs")
    tokens: _TokenBreakdown | None = None
    cost_usd: str | None = Field(default=None, alias="costUsd")


class StepFailed(_BaseEvent):
    event_type: Literal["step_error"] = Field(alias="eventType")
    step_id: str = Field(alias="stepId")
    name: str | None = None
    error: dict[str, Any] | None = None


class StepPaused(_BaseEvent):
    """Step-protocol pause between steps (not a tool-call pause)."""

    event_type: Literal["step_paused"] = Field(alias="eventType")
    step_id: str = Field(alias="stepId")
    step_index: int | None = Field(default=None, alias="stepIndex")


class ToolCallsRequired(_BaseEvent):
    """Step paused for tool calls. Carries .resume() method (wired in Phase 6)."""

    event_type: Literal["step_paused_for_tool_calls"] = Field(alias="eventType")
    run_id: str = Field(alias="runId")
    execution_id: str = Field(alias="executionId")
    step_id: str = Field(alias="stepId")
    step_index: int = Field(alias="stepIndex")
    iterations_used: int = Field(alias="iterationsUsed")
    tool_call_messages: list[dict[str, Any]] = Field(alias="toolCallMessages")
    tool_calls: list[dict[str, Any]] = Field(alias="toolCalls")
    accumulated_outputs: dict[str, Any] = Field(default_factory=dict, alias="accumulatedOutputs")


class FlowCompleted(_BaseEvent):
    event_type: Literal["flow_completed"] = Field(alias="eventType")
    run_id: str | None = Field(default=None, alias="runId")
    execution_id: str | None = Field(default=None, alias="executionId")
    result: Any = None
    summary: dict[str, Any] | None = None


# Tagged union for runtime parsing in Phase 6
StreamEvent = (
    RunStarted
    | StepStarted
    | StepInput
    | StepOutput
    | StepCompleted
    | StepFailed
    | StepPaused
    | ToolCallsRequired
    | FlowCompleted
)
