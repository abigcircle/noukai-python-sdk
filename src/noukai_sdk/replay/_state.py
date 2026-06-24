"""Replay scope state — internal types held by the contextvar."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._models.session import SessionResponse


class ScopeMode(str, Enum):
    NORMAL = "normal"      # no decorator; existing behavior
    CAPTURE = "capture"    # decorator on, no replay header — tag with session_id
    REPLAY = "replay"      # decorator on + replay header + env var set — serve from cassette


@dataclass
class ReplayCursor:
    """Per-slug counter for slug-positional matching of execute() calls."""

    by_slug: dict[str, int] = field(default_factory=dict)

    def next_index(self, slug: str) -> int:
        i = self.by_slug.get(slug, 0)
        self.by_slug[slug] = i + 1
        return i


@dataclass
class StepFlowMapping:
    """Maps a user-code execution_id (created on first step call with None) to
    the recorded execution_id from the session.

    For replay only. User code calls step(slug, None, 0); the SDK returns the
    recorded execution_id back to user code; subsequent step(slug, exec_id, N)
    calls match by exact (recorded_exec_id, N)."""

    code_exec_id_to_recorded: dict[str, str] = field(default_factory=dict)


@dataclass
class ScopeState:
    """Held by the contextvar for the duration of a trace_scope."""

    mode: ScopeMode
    session_id: str | None = None
    # Replay-only:
    fetched_session: SessionResponse | None = None
    execute_cursor: ReplayCursor = field(default_factory=ReplayCursor)
    step_first_call_cursor: ReplayCursor = field(default_factory=ReplayCursor)
    step_mapping: StepFlowMapping = field(default_factory=StepFlowMapping)
    consumed_execution_ids: set[str] = field(default_factory=set)
