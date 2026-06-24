"""Tests for the pure SSE byte-stream parser in noukai_sdk._streaming.

The parser is decoupled from HTTP: it takes an AsyncIterator[bytes] and
yields typed Pydantic event instances. These tests exercise frame
boundaries, multi-line data, comments, unknown-event-type forward
compatibility, and malformed-JSON robustness.
"""

import json

from noukai_sdk._models.events import (
    FlowCompleted,
    RunStarted,
    StepCompleted,
    ToolCallsRequired,
)
from noukai_sdk._streaming import parse_sse_stream


async def feed(byte_chunks):
    """Helper: turn a list of byte chunks into an async iterator."""
    for chunk in byte_chunks:
        yield chunk


def sse_frame(event_type: str, **payload) -> bytes:
    """Build a single SSE `data:` frame (terminated by \\n\\n)."""
    data = {"eventType": event_type, **payload}
    return f"data: {json.dumps(data)}\n\n".encode()


class TestSingleFrameParsing:
    async def test_run_started_frame(self):
        chunks = [sse_frame("run_started", runId="r-1", flowId="f", stepCount=3)]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert len(events) == 1
        assert isinstance(events[0], RunStarted)
        assert events[0].run_id == "r-1"

    async def test_flow_started_alias_maps_to_run_started(self):
        """Server emits `flow_started` as a legacy alias of `run_started`."""
        chunks = [sse_frame("flow_started", runId="r-1", flowId="f", stepCount=2)]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert len(events) == 1
        assert isinstance(events[0], RunStarted)

    async def test_step_completed_frame(self):
        chunks = [
            sse_frame(
                "step_completed",
                stepId="s-1",
                name="x",
                output={"y": 1},
                durationMs=100,
                tokens={"prompt": 10, "completion": 5, "total": 15},
                costUsd="0.0001",
            )
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert isinstance(events[0], StepCompleted)
        assert events[0].step_id == "s-1"
        assert events[0].tokens is not None
        assert events[0].tokens.total == 15

    async def test_tool_calls_required_frame(self):
        chunks = [
            sse_frame(
                "step_paused_for_tool_calls",
                runId="r-1",
                executionId="e-1",
                stepId="s-1",
                stepIndex=2,
                iterationsUsed=1,
                toolCallMessages=[{"role": "assistant"}],
                toolCalls=[{"id": "tc-1"}],
                accumulatedOutputs={"s-0": "x"},
            )
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert isinstance(events[0], ToolCallsRequired)
        assert events[0].execution_id == "e-1"


class TestMultiFrameStream:
    async def test_typical_step_stream(self):
        chunks = [
            sse_frame("run_started", runId="r", flowId="f", stepCount=2),
            sse_frame("step_started", stepId="s-1", name="a"),
            sse_frame("step_completed", stepId="s-1", name="a", output={"x": 1}),
            sse_frame("flow_completed", runId="r", result={"final": "x"}),
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        types = [type(e).__name__ for e in events]
        assert types == ["RunStarted", "StepStarted", "StepCompleted", "FlowCompleted"]


class TestFrameBoundaries:
    async def test_split_across_chunks(self):
        """Parser must reassemble frames split mid-bytes."""
        full = sse_frame("step_completed", stepId="s-1", output={})
        # Split into 3 arbitrary pieces
        a, b, c = full[:5], full[5:20], full[20:]
        events = [e async for e in parse_sse_stream(feed([a, b, c]))]
        assert len(events) == 1
        assert isinstance(events[0], StepCompleted)

    async def test_multiple_frames_in_one_chunk(self):
        first = sse_frame("step_started", stepId="s-1")
        second = sse_frame("step_completed", stepId="s-1", output={})
        events = [e async for e in parse_sse_stream(feed([first + second]))]
        assert len(events) == 2

    async def test_three_frames_in_one_chunk(self):
        a = sse_frame("step_started", stepId="s-1")
        b = sse_frame("step_completed", stepId="s-1", output={})
        c = sse_frame("flow_completed", runId="r")
        events = [e async for e in parse_sse_stream(feed([a + b + c]))]
        assert len(events) == 3

    async def test_trailing_partial_frame_does_not_yield(self):
        """If the last chunk lacks the \\n\\n terminator, that frame is held."""
        full = sse_frame("step_completed", stepId="s-1", output={})
        # Drop the trailing \n\n
        partial = full[:-2]
        events = [e async for e in parse_sse_stream(feed([partial]))]
        assert len(events) == 0


class TestRobustness:
    async def test_comments_ignored(self):
        """SSE spec: lines starting with ':' are comments."""
        chunks = [
            b": heartbeat\n\n",
            sse_frame("step_completed", stepId="s-1", output={}),
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert len(events) == 1

    async def test_unknown_event_type_skipped(self):
        chunks = [
            sse_frame("future_event_we_dont_know", x=1),
            sse_frame("step_completed", stepId="s-1", output={}),
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        # Unknown is dropped; known passes through.
        assert len(events) == 1

    async def test_malformed_json_skipped(self):
        chunks = [
            b"data: {not json}\n\n",
            sse_frame("step_completed", stepId="s-1", output={}),
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert len(events) == 1  # malformed dropped

    async def test_multi_line_data_concatenated(self):
        """SSE spec: multiple `data:` lines in one frame join with \\n.

        A common server practice is to pretty-print JSON across lines; the
        parser must reassemble before json.loads. We construct a pretty-
        printed payload (which contains literal newlines) and split it onto
        multiple ``data:`` lines.
        """
        pretty = json.dumps(
            {"eventType": "step_completed", "stepId": "s-1", "output": {}},
            indent=2,
        )
        body = b""
        for line in pretty.split("\n"):
            body += f"data: {line}\n".encode()
        body += b"\n"  # frame terminator
        events = [e async for e in parse_sse_stream(feed([body]))]
        assert len(events) == 1
        assert isinstance(events[0], StepCompleted)

    async def test_validation_error_skipped(self):
        """A known event type with invalid payload is silently dropped."""
        # `step_completed` requires stepId; omit it.
        bad = b'data: {"eventType": "step_completed"}\n\n'
        good = sse_frame("flow_completed", runId="r")
        events = [e async for e in parse_sse_stream(feed([bad, good]))]
        assert len(events) == 1
        assert isinstance(events[0], FlowCompleted)

    async def test_empty_stream_yields_nothing(self):
        events = [e async for e in parse_sse_stream(feed([]))]
        assert events == []

    async def test_event_without_event_type_skipped(self):
        chunks = [
            b'data: {"foo": "bar"}\n\n',
            sse_frame("flow_completed", runId="r"),
        ]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert len(events) == 1


class TestEventFieldDiscriminator:
    """The SSE-spec ``event:`` field is the authoritative discriminator.

    Payload ``eventType`` is a back-compat fallback only.
    """

    async def test_event_field_alone_resolves_type(self):
        """``event:`` line is sufficient — payload need not carry eventType."""
        body = b'event: step_completed\ndata: {"stepId": "s-1", "output": {}}\n\n'
        events = [e async for e in parse_sse_stream(feed([body]))]
        assert len(events) == 1
        assert isinstance(events[0], StepCompleted)
        assert events[0].step_id == "s-1"

    async def test_event_field_overrides_mismatched_payload(self):
        """If ``event:`` and payload ``eventType`` disagree, ``event:`` wins."""
        body = (
            b"event: step_completed\n"
            b'data: {"eventType": "step_started", "stepId": "s-1", "output": {}}\n'
            b"\n"
        )
        events = [e async for e in parse_sse_stream(feed([body]))]
        assert len(events) == 1
        assert isinstance(events[0], StepCompleted)

    async def test_event_field_with_unknown_type_skipped(self):
        """Forward-compat: unknown event in ``event:`` line is dropped."""
        body = b"event: future_event_we_dont_know\ndata: {}\n\n"
        events = [e async for e in parse_sse_stream(feed([body]))]
        assert events == []

    async def test_payload_eventtype_fallback_when_no_event_field(self):
        """Back-compat: payload ``eventType`` is used if ``event:`` is absent."""
        chunks = [sse_frame("flow_completed", runId="r-1")]
        events = [e async for e in parse_sse_stream(feed(chunks))]
        assert len(events) == 1
        assert isinstance(events[0], FlowCompleted)

    async def test_event_field_tool_calls_required(self):
        """The tool-calls pause event, delivered SSE-style without payload eventType."""
        body = (
            b"event: step_paused_for_tool_calls\n"
            b'data: {"runId": "r-1", "executionId": "e-1", "stepId": "s-1",'
            b' "stepIndex": 2, "iterationsUsed": 1, "toolCallMessages": [],'
            b' "toolCalls": [], "accumulatedOutputs": {}}\n'
            b"\n"
        )
        events = [e async for e in parse_sse_stream(feed([body]))]
        assert len(events) == 1
        assert isinstance(events[0], ToolCallsRequired)
        assert events[0].execution_id == "e-1"
