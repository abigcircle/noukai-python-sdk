"""Verify every model parses real server payload shapes and serializes back
to the camelCase wire format the server expects."""

from noukai_sdk._models.events import (
    StepCompleted,
    ToolCallsRequired,
)
from noukai_sdk._models.requests import ExecuteRequest, StepRequest
from noukai_sdk._models.responses import (
    ExecuteResult,
    JobAccepted,
    JobStatus,
    PausedResult,
)
from noukai_sdk._models.trace import Trace


class TestRequestSerialization:
    def test_execute_request_emits_camelcase_aliases(self):
        req = ExecuteRequest(
            message="hi",
            execution_id="abc-123",
            block_overrides={"step-1": {"model": "anthropic/claude-haiku-4-5"}},
            accumulated_outputs={"step-0": {"result": "ok"}},
        )
        wire = req.model_dump(by_alias=True, exclude_none=True)
        assert wire["executionId"] == "abc-123"
        assert wire["blockOverrides"] == {"step-1": {"model": "anthropic/claude-haiku-4-5"}}
        assert wire["accumulatedOutputs"] == {"step-0": {"result": "ok"}}
        assert "execution_id" not in wire

    def test_step_request_run_remaining_aliased(self):
        req = StepRequest(step_index=2, run_remaining=True)
        wire = req.model_dump(by_alias=True)
        assert wire["stepIndex"] == 2
        assert wire["runRemaining"] is True


class TestResponseParsing:
    def test_execute_result_from_server(self):
        payload = {
            "status": "completed",
            "result": {"answer": 42},
            "flowId": "flow-xyz",
            "blockCount": 3,
        }
        result = ExecuteResult.model_validate(payload)
        assert result.status == "completed"
        assert result.output == {"answer": 42}  # property aliases .result
        assert result.flow_id == "flow-xyz"
        assert result.block_count == 3
        assert result.requires_tool_calls is False

    def test_paused_result_from_server(self):
        payload = {
            "status": "tool_calls_required",
            "executionId": "exec-123",
            "pausedAtStep": "step-1",
            "iterationsUsed": 1,
            "toolCallMessages": [{"role": "assistant", "tool_calls": [{"id": "tc-1"}]}],
            "toolCalls": [{"id": "tc-1", "function": {"name": "search"}}],
            "accumulatedOutputs": {"step-0": "done"},
            "flowId": "flow-xyz",
            "blockCount": 3,
        }
        result = PausedResult.model_validate(payload)
        assert result.execution_id == "exec-123"
        assert result.paused_at_step == "step-1"
        assert result.requires_tool_calls is True
        assert result.tool_calls[0]["function"]["name"] == "search"

    def test_job_lifecycle(self):
        accepted = JobAccepted.model_validate(
            {
                "executionId": "exec-1",
                "status": "started",
                "flowId": "f",
                "blockCount": 2,
            }
        )
        assert accepted.execution_id == "exec-1"

        status = JobStatus.model_validate(
            {
                "executionId": "exec-1",
                "status": "completed",
                "result": {"x": 1},
                "error": None,
            }
        )
        assert status.status == "completed"
        assert status.result == {"x": 1}


class TestEventParsing:
    def test_step_completed_with_tokens(self):
        event = StepCompleted.model_validate(
            {
                "eventType": "step_completed",
                "stepId": "step-1",
                "name": "summarize",
                "output": {"summary": "..."},
                "durationMs": 1240,
                "tokens": {"prompt": 100, "completion": 50, "total": 150},
                "costUsd": "0.000150",
            }
        )
        assert event.step_id == "step-1"
        assert event.tokens.total == 150
        assert event.cost_usd == "0.000150"

    def test_tool_calls_required_event(self):
        event = ToolCallsRequired.model_validate(
            {
                "eventType": "step_paused_for_tool_calls",
                "runId": "run-1",
                "executionId": "exec-1",
                "stepId": "step-1",
                "stepIndex": 2,
                "iterationsUsed": 1,
                "toolCallMessages": [{"role": "assistant"}],
                "toolCalls": [{"id": "tc-1"}],
                "accumulatedOutputs": {},
            }
        )
        assert event.execution_id == "exec-1"
        assert event.tool_calls[0]["id"] == "tc-1"

    def test_flow_completed_accepts_legacy_run_started(self):
        """run_started is legacy but still emitted by /seq/.../step today."""
        from noukai_sdk._models.events import RunStarted

        event = RunStarted.model_validate(
            {
                "eventType": "run_started",
                "runId": "run-1",
                "flowId": "f",
                "stepCount": 3,
            }
        )
        assert event.event_type == "run_started"


class TestTraceParsing:
    def test_full_trace_with_steps(self):
        payload = {
            "flowRun": {
                "id": "run-1",
                "flowId": "f",
                "status": "completed",
                "triggerType": "ad_hoc",
                "stepCount": 2,
            },
            "steps": [
                {
                    "stepId": "step-1",
                    "attempt": 1,
                    "status": "completed",
                    "durationMs": 1500,
                    "modelUsed": "anthropic/claude-sonnet-4-6",
                    "tokens": {"prompt": 200, "completion": 80, "total": 280},
                    "costUsd": "0.000320",
                },
            ],
        }
        trace = Trace.model_validate(payload)
        assert trace.flow_run.id == "run-1"
        assert len(trace.steps) == 1
        assert trace.steps[0].duration_ms == 1500


class TestForwardCompatibility:
    def test_unknown_fields_ignored(self):
        """Server may add fields; SDK must not break."""
        result = ExecuteResult.model_validate(
            {
                "status": "completed",
                "flowId": "f",
                "blockCount": 1,
                "futureField": {"some": "data"},
                "anotherNewThing": 42,
            }
        )
        assert result.flow_id == "f"
