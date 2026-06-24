"""GET /seq/sessions/{session_id} response models.

Mirrors the BE response model in
``services/executor/router-ai-slugs/src/router_ai_slugs/models/session.py``.

Wire format: camelCase. See BE design
``20260605-BE-execution-session-grouping``.

IMPORTANT -- these models must stay aligned with the BE serializer:

- ``slug`` is the BARE flow slug (e.g. ``"grade-3"``), not ``org/project/slug``.
  Several fields are ``Optional`` because the BE may emit ``null`` for them
  (e.g. when the underlying flow has been deleted).
- ``status`` includes ``"pending"`` and ``"cancelled"`` per the BE Literal,
  with a ``str`` fallback for forward compatibility.
- ``SessionStepSnapshot`` carries ``status``, ``loop_index``, ``duration_ms``
  on top of the snapshot fields, matching BE ``SessionStep``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ._aliases import WIRE_CONFIG


class SessionStepSnapshot(BaseModel):
    """One per-step snapshot inside a session execution.

    Matches BE ``SessionStep`` in
    ``router-ai-slugs/models/session.py``.
    """

    model_config = WIRE_CONFIG

    step_id: str = Field(alias="stepId")
    attempt: int = 1
    loop_index: int | None = Field(default=None, alias="loopIndex")
    # Step-level status -- distinct from the parent execution.status.
    status: Literal["running", "completed", "failed", "skipped"] | str = "completed"
    started_at: str | None = Field(default=None, alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    duration_ms: int | None = Field(default=None, alias="durationMs")
    input_snapshot: dict[str, Any] | None = Field(default=None, alias="inputSnapshot")
    output_snapshot: dict[str, Any] | Any = Field(default=None, alias="outputSnapshot")
    error_snapshot: dict[str, Any] | None = Field(default=None, alias="errorSnapshot")
    truncated: bool = False
    # Legacy alias accepted from older fixtures; not emitted by the BE today.
    block_id: str | None = Field(default=None, alias="blockId")


class SessionExecution(BaseModel):
    """One flow_run inside a session, with its per-step snapshots.

    Matches BE ``SessionExecution``. Several fields are Optional because
    the BE may emit ``null`` -- the SDK matcher tolerates these and falls
    back to ``flow_id`` when ``slug`` is unavailable.
    """

    model_config = WIRE_CONFIG

    execution_id: str = Field(alias="executionId")
    # UUID; Optional because BE schema marks it Optional even though
    # FlowRun.flow_id is non-null in practice. Used as the matcher fallback
    # when slug is None.
    flow_id: str | None = Field(default=None, alias="flowId")
    # BARE flow.slug (e.g. "grade-3"). NO org/project prefix.
    # None when the underlying flow has been deleted -- match by flow_id then.
    slug: str | None = None
    # How this run was triggered. None for legacy rows.
    trigger_type: Literal["execute", "step", "job"] | str | None = Field(
        default=None, alias="triggerType"
    )
    # Run-level status. BE emits Literal["pending", "running", "completed",
    # "failed", "cancelled"]. `| str` keeps forward-compat with future values.
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | str
    started_at: str | None = Field(default=None, alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    # None when no steps were recorded so the BE cannot derive a canonical mode.
    trace_capture_mode: Literal["full", "redacted", "metadata_only", "off"] | str | None = Field(
        default=None, alias="traceCaptureMode"
    )
    # True iff SDK can replay (i.e. capture mode was "full" or "redacted").
    snapshots_available: bool = Field(alias="snapshotsAvailable")
    steps: list[SessionStepSnapshot] = Field(default_factory=list)
    error_at_step: str | None = Field(default=None, alias="errorAtStep")


class SessionResponse(BaseModel):
    """GET /seq/sessions/{session_id} response envelope."""

    model_config = WIRE_CONFIG

    session_id: str = Field(alias="sessionId")
    executions: list[SessionExecution] = Field(default_factory=list)
