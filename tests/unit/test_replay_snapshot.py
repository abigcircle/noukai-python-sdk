"""Tests for the replay snapshot helper (``strip_trace_sidecars``).

Mirrors the TypeScript SDK's ``snapshot.ts`` helper. The invariant under
test: on the replay path, reserved trace sidecar keys (e.g.
``__rendered_prompt__``) must be projected out of ``output_snapshot`` so
the replayed ``result`` / ``output`` matches the live-execution shape,
which excludes them.
"""

from __future__ import annotations

from noukai_sdk.replay.snapshot import RESERVED_SNAPSHOT_KEYS, strip_trace_sidecars


class TestStripTraceSidecars:
    def test_removes_rendered_prompt_key(self):
        snap = {"answer": 42, "__rendered_prompt__": "system: ..."}
        assert strip_trace_sidecars(snap) == {"answer": 42}

    def test_returns_input_untouched_when_no_reserved_keys(self):
        snap = {"answer": 42}
        # Identity — no allocation when nothing to strip.
        assert strip_trace_sidecars(snap) is snap

    def test_passes_through_non_dict(self):
        assert strip_trace_sidecars(None) is None
        assert strip_trace_sidecars("string") == "string"
        assert strip_trace_sidecars(42) == 42
        lst = [1, 2, 3]
        assert strip_trace_sidecars(lst) is lst

    def test_only_strips_top_level_keys(self):
        """Nested reserved keys are NOT stripped — mirrors TS shallow-copy semantics."""
        snap = {
            "nested": {"__rendered_prompt__": "should stay"},
            "__rendered_prompt__": "should go",
        }
        stripped = strip_trace_sidecars(snap)
        assert stripped == {"nested": {"__rendered_prompt__": "should stay"}}

    def test_returns_new_dict_when_stripping(self):
        snap = {"a": 1, "__rendered_prompt__": "x"}
        result = strip_trace_sidecars(snap)
        assert result is not snap  # new dict, so caller's original is untouched
        assert "__rendered_prompt__" in snap  # confirmed non-mutating

    def test_reserved_key_list_contains_rendered_prompt(self):
        """Guard against accidental removal of the key from the list."""
        assert "__rendered_prompt__" in RESERVED_SNAPSHOT_KEYS


class TestReconstructorAppliesStrip:
    """End-to-end: reconstructor emits step_completed / flow_completed with
    sidecars stripped from output_snapshot."""

    def test_step_completed_output_has_sidecars_stripped(self):
        from noukai_sdk._models.events import FlowCompleted, StepCompleted
        from noukai_sdk._models.session import SessionExecution
        from noukai_sdk.replay.sse_reconstructor import reconstruct_events_sync

        ex = SessionExecution.model_validate(
            {
                "executionId": "e-1",
                "flowId": "f-1",
                "slug": "test-flow",
                "status": "completed",
                "triggerType": "execute",
                "traceCaptureMode": "full",
                "snapshotsAvailable": True,
                "steps": [
                    {
                        "stepId": "s-1",
                        "outputSnapshot": {
                            "answer": 42,
                            "__rendered_prompt__": "system: ...",
                        },
                    },
                ],
            }
        )

        events = list(reconstruct_events_sync(ex))
        step_completed = next(e for e in events if isinstance(e, StepCompleted))
        flow_completed = next(e for e in events if isinstance(e, FlowCompleted))

        assert step_completed.output == {"answer": 42}
        assert flow_completed.result == {"answer": 42}


class TestMaterializeExecuteStripsResult:
    """The Flow.execute() replay path also strips sidecars from the result."""

    def test_execute_result_strips_rendered_prompt(self):
        from noukai_sdk._models.session import SessionExecution
        from noukai_sdk.replay.matcher import _materialize_execute_result

        ex = SessionExecution.model_validate(
            {
                "executionId": "e-1",
                "flowId": "f-1",
                "slug": "test-flow",
                "status": "completed",
                "triggerType": "execute",
                "traceCaptureMode": "full",
                "snapshotsAvailable": True,
                "steps": [
                    {
                        "stepId": "s-1",
                        "outputSnapshot": {
                            "answer": 42,
                            "__rendered_prompt__": "system: ...",
                        },
                    },
                ],
            }
        )

        result = _materialize_execute_result(ex, scope_session_id="sess-abc")
        assert result.output == {"answer": 42}
