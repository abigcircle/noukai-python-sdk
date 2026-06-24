"""Trace endpoint response models. Mirror server StepTraceResponse +
FlowRunTraceResponse with snake_case access."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ._aliases import WIRE_CONFIG


class TokenBreakdown(BaseModel):
    model_config = WIRE_CONFIG
    prompt: int = 0
    completion: int = 0
    total: int = 0


class StepTrace(BaseModel):
    model_config = WIRE_CONFIG

    step_id: str = Field(alias="stepId")
    attempt: int
    loop_index: int | None = Field(default=None, alias="loopIndex")
    status: Literal["running", "completed", "failed", "skipped"]
    started_at: str | None = Field(default=None, alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    duration_ms: int | None = Field(default=None, alias="durationMs")
    model_used: str | None = Field(default=None, alias="modelUsed")
    tokens: TokenBreakdown | None = None
    cost_usd: str | None = Field(default=None, alias="costUsd")
    input_context: dict[str, Any] | None = Field(default=None, alias="inputContext")
    output_context: dict[str, Any] | None = Field(default=None, alias="outputContext")
    error_context: dict[str, Any] | None = Field(default=None, alias="errorContext")
    input_size_bytes: int | None = Field(default=None, alias="inputSizeBytes")
    output_size_bytes: int | None = Field(default=None, alias="outputSizeBytes")
    truncated: bool = False


class RunSummary(BaseModel):
    model_config = WIRE_CONFIG

    id: str
    flow_id: str = Field(alias="flowId")
    status: str
    trigger_type: str | None = Field(default=None, alias="triggerType")
    started_at: str | None = Field(default=None, alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    duration_ms: int | None = Field(default=None, alias="durationMs")
    step_count: int | None = Field(default=None, alias="stepCount")


class Trace(BaseModel):
    model_config = WIRE_CONFIG

    flow_run: RunSummary = Field(alias="flowRun")
    steps: list[StepTrace]


class StepAttempts(BaseModel):
    model_config = WIRE_CONFIG

    step_id: str = Field(alias="stepId")
    attempts: list[StepTrace]
