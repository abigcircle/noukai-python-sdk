"""Guard the trace*→replay* rename (design 20260916-SDK-otel-and-replay-rename).

The replay/capture scope was renamed from the misleading ``trace*`` names to
``replay*``. This locks the new public surface, asserts the old names are gone
(a hard rename — no deprecation alias), and confirms the neighbouring
execution-trace API (``run.trace``) is unaffected by the rename.
"""

from __future__ import annotations

import noukai_sdk

NEW_NAMES = ["replay", "replay_scope", "replay_scope_sync", "current_session_id"]
OLD_NAMES = ["trace", "trace_scope", "trace_scope_sync"]


class TestReplayScopeRename:
    def test_new_names_exported(self) -> None:
        for name in NEW_NAMES:
            assert hasattr(noukai_sdk, name), f"{name} should be a public export"
            assert name in noukai_sdk.__all__, f"{name} should be in __all__"

    def test_old_names_removed(self) -> None:
        for name in OLD_NAMES:
            assert not hasattr(noukai_sdk, name), f"{name} should be gone (hard rename)"
            assert name not in noukai_sdk.__all__, f"{name} should not be in __all__"

    def test_replay_decorator_wraps_callables(self) -> None:
        def sync_fn() -> str:
            return "ok"

        wrapped = noukai_sdk.replay(sync_fn)
        assert callable(wrapped)

    def test_run_trace_api_unaffected(self) -> None:
        # The execution-trace API keeps its name — only the scope was renamed.
        from noukai_sdk import Run

        assert hasattr(Run, "trace")
        assert hasattr(Run, "step_trace")
        assert hasattr(Run, "live_trace")
