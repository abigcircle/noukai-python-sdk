"""Outgoing HTTP request bodies. Mirror server SeqflowExecuteRequest /
SeqflowStepRequest with snake_case interface + camelCase wire aliases."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._aliases import WIRE_CONFIG


class ChatMessage(BaseModel):
    """A single structured chat turn (design 20260903-SDK-agent-relay, F6).

    Tracks the shared ``llm_service.models.ChatMessage`` shape:
    ``role``/``content``/``toolCalls``/``toolCallId``/``name``. Deliberately
    **permissive** — the SDK's job is to *express* ``messages``, not re-police
    it; the server does the authoritative validation. Unknown fields pass
    through (``extra="allow"``).

    Snake_case attributes (``tool_calls``/``tool_call_id``) with **camelCase wire
    aliases** (``toolCalls``/``toolCallId``): the router-ai-slugs execute API
    accepts and emits camelCase for message contents, so the whole Noukai wire is
    camelCase; snake_case now lives only at the external LLM-provider boundary.
    ``populate_by_name=True`` means either casing constructs the model; it always
    serializes camelCase under ``model_dump(by_alias=True)``.

    ``role`` is typed permissively as ``str``; the SDK still rejects
    ``system``/``function`` (and any non ``user|assistant|tool`` role)
    client-side before the request goes out, matching the server's
    ``MESSAGES_ROLE_INVALID`` (400).
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    role: str
    content: Any = None
    tool_calls: list[dict[str, Any]] | None = Field(default=None, alias="toolCalls")
    tool_call_id: str | None = Field(default=None, alias="toolCallId")
    name: str | None = None


class ExecuteRequest(BaseModel):
    """POST /seq/{org}/{project}/{slug}/execute body."""

    model_config = WIRE_CONFIG

    message: str | None = None
    # Structured prior conversation for chat/agent flows; the last entry is the
    # current user turn. When set it stands in for ``message`` (server contract).
    messages: list[ChatMessage] | None = None
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
