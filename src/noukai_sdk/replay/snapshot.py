"""Output-snapshot helpers for replay reconstruction.

Trace snapshots persisted in ``output_snapshot`` carry reserved sidecar keys
that are NOT part of the business result. On the live execution path the
backend pops these out of the block output before returning it, and
re-persists them into the snapshot purely for trace viewing (see
``TrackedBlockExecutor``, which pops ``__rendered_prompt__`` from
``raw_output`` and stores it under ``output_snapshot.__rendered_prompt__``).

Replay reconstruction reads the raw snapshot, so it must project those
sidecars back out — otherwise the replayed ``result`` / ``output`` would be
a superset of the live shape and round-trip equality checks fail.

Mirrors ``src/replay/snapshot.ts`` in the TypeScript SDK.
"""

from __future__ import annotations

from typing import Any

# Reserved sidecar keys that live inside ``output_snapshot`` but are not part
# of the business result. Mirrors the keys the backend strips on the live
# path. ``__rendered_prompt__`` is the only one re-persisted into the
# snapshot today; the list is kept extensible for future sidecars.
RESERVED_SNAPSHOT_KEYS: tuple[str, ...] = ("__rendered_prompt__",)


def strip_trace_sidecars(snapshot: Any) -> Any:
    """Return a shallow copy of ``snapshot`` with reserved trace sidecar keys
    removed, recovering the business-result shape produced by a live execution.

    Non-dict inputs (``None``, list, primitive, etc.) pass through unchanged,
    as do dicts that contain no reserved keys (returned as-is to avoid needless
    allocation).
    """
    if not isinstance(snapshot, dict):
        return snapshot
    if not any(k in snapshot for k in RESERVED_SNAPSHOT_KEYS):
        return snapshot
    return {k: v for k, v in snapshot.items() if k not in RESERVED_SNAPSHOT_KEYS}
