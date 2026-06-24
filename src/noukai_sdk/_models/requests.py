"""Outgoing HTTP request bodies. Mirror server SeqflowExecuteRequest /
SeqflowStepRequest with snake_case interface + camelCase wire aliases."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ._aliases import WIRE_CONFIG


class ExecuteRequest(BaseModel):
    """POST /seq/{org}/{project}/{slug}/execute body."""

    model_config = WIRE_CONFIG

    message: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    block_overrides: dict[str, dict[str, Any]] | None = Field(
        default=None, serialization_alias="blockOverrides"
    )
    attachments: list[dict[str, Any]] | None = None

    # Tool calling
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = Field(default=None, serialization_alias="toolChoice")

    # Resume fields
    execution_id: str | None = Field(default=None, serialization_alias="executionId")
    paused_at_step: str | None = Field(default=None, serialization_alias="pausedAtStep")
    iterations_used: int = Field(default=0, serialization_alias="iterationsUsed")
    tool_call_messages: list[dict[str, Any]] | None = Field(
        default=None, serialization_alias="toolCallMessages"
    )
    accumulated_outputs: dict[str, Any] = Field(
        default_factory=dict, serialization_alias="accumulatedOutputs"
    )

    trace: bool = False


class StepRequest(BaseModel):
    """POST /seq/{org}/{project}/{slug}/step body."""

    model_config = WIRE_CONFIG

    execution_id: str | None = Field(default=None, serialization_alias="executionId")
    step_index: int = Field(default=0, serialization_alias="stepIndex")
    accumulated_outputs: dict[str, Any] = Field(
        default_factory=dict, serialization_alias="accumulatedOutputs"
    )
    message: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] | None = None
    input_overrides: dict[str, Any] = Field(
        default_factory=dict, serialization_alias="inputOverrides"
    )
    block_overrides: dict[str, dict[str, Any]] | None = Field(
        default=None, serialization_alias="blockOverrides"
    )
    run_remaining: bool = Field(default=False, serialization_alias="runRemaining")
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = Field(default=None, serialization_alias="toolChoice")
    tool_call_messages: list[dict[str, Any]] | None = Field(
        default=None, serialization_alias="toolCallMessages"
    )
    iterations_used: int = Field(default=0, serialization_alias="iterationsUsed")
    trace: bool = False
