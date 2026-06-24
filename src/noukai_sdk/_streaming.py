"""SSE byte stream → typed event parser.

Server emits one event per SSE frame, terminated by ``\\n\\n``. Each frame
carries the type in the SSE-spec ``event:`` field and the body in one or
more ``data:`` lines, e.g.::

    event: step_completed
    data: {"stepId": "s-1", "output": {...}}

The ``event:`` field is the authoritative discriminator — the parser uses
it to look up the Pydantic model. If a frame omits ``event:``, the parser
falls back to ``eventType`` inside the JSON payload (back-compat for
upstream serializers that only emit the JSON form). The resolved type is
synced into the payload as ``eventType`` before validation so the existing
Pydantic models (which declare ``eventType`` as a required field) remain
unchanged. Forward-compatible: unknown event types are silently dropped.

The parser is intentionally decoupled from HTTP — it takes either an
``AsyncIterator[bytes]`` (async path) or ``Iterator[bytes]`` (sync path)
and emits typed events. Frame reassembly across chunk boundaries is handled
here. Both share the ``_parse_frame`` helper.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from pydantic import BaseModel, ValidationError

from ._models.events import (
    FlowCompleted,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepInput,
    StepOutput,
    StepPaused,
    StepStarted,
    StreamEvent,
    ToolCallsRequired,
)

_logger = logging.getLogger("noukai_sdk.streaming")

_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "run_started": RunStarted,
    "flow_started": RunStarted,  # legacy alias — see CONTEXT.md gotcha #10
    "step_started": StepStarted,
    "step_input": StepInput,
    "step_output": StepOutput,
    "step_completed": StepCompleted,
    "step_error": StepFailed,
    "step_paused": StepPaused,
    "step_paused_for_tool_calls": ToolCallsRequired,
    "flow_completed": FlowCompleted,
}


async def parse_sse_stream(
    byte_stream: AsyncIterator[bytes],
) -> AsyncIterator[StreamEvent]:
    """Reassemble SSE frames from arbitrary byte chunks and yield typed events.

    SSE frame format: ``data: <payload>\\n\\n`` (double-newline separator).
    Lines starting with ``:`` are SSE comments and are ignored. Multiple
    ``data:`` lines in a single frame are concatenated with ``\\n`` per the
    SSE spec before being JSON-parsed.

    Behaviour:
    - Unknown event types: dropped silently (forward-compat).
    - Malformed JSON: warning logged, frame dropped.
    - Frames with no discriminator (neither ``event:`` nor payload
      ``eventType``): dropped silently.
    - Pydantic validation errors: warning logged, frame dropped.
    """
    buffer = bytearray()
    async for chunk in byte_stream:
        buffer.extend(chunk)
        while True:
            sep = buffer.find(b"\n\n")
            if sep == -1:
                break
            frame_bytes = bytes(buffer[:sep])
            del buffer[: sep + 2]
            event = _parse_frame(frame_bytes)
            if event is not None:
                yield event


def parse_sse_stream_sync(
    byte_stream: Iterator[bytes],
) -> Iterator[StreamEvent]:
    """Reassemble SSE frames from arbitrary byte chunks and yield typed events.

    Sync mirror of :func:`parse_sse_stream`. Shares ``_parse_frame`` for
    frame decoding; differs only in the iteration protocol (no ``await``).

    SSE frame format: ``data: <payload>\\n\\n`` (double-newline separator).
    Lines starting with ``:`` are SSE comments and are ignored. Multiple
    ``data:`` lines in a single frame are concatenated with ``\\n`` per the
    SSE spec before being JSON-parsed.

    Behaviour:
    - Unknown event types: dropped silently (forward-compat).
    - Malformed JSON: warning logged, frame dropped.
    - Frames with no discriminator (neither ``event:`` nor payload
      ``eventType``): dropped silently.
    - Pydantic validation errors: warning logged, frame dropped.
    """
    buffer = bytearray()
    for chunk in byte_stream:
        buffer.extend(chunk)
        while True:
            sep = buffer.find(b"\n\n")
            if sep == -1:
                break
            frame_bytes = bytes(buffer[:sep])
            del buffer[: sep + 2]
            event = _parse_frame(frame_bytes)
            if event is not None:
                yield event


def _parse_frame(frame: bytes) -> StreamEvent | None:
    """Parse one SSE frame body into a typed event, or None.

    Discriminator resolution order:
      1. The SSE ``event:`` field, if present. (Authoritative per SSE spec.)
      2. Otherwise, ``eventType`` inside the JSON payload.

    Skips comment lines (start with ``:``) and blank lines. Concatenates
    multiple ``data:`` payloads with ``\\n`` per SSE spec. Other SSE fields
    (``id:``, ``retry:``) are ignored.
    """
    event_field: str | None = None
    data_lines: list[str] = []
    for line in frame.split(b"\n"):
        if not line or line.startswith(b":"):
            continue  # blank or comment line
        if line.startswith(b"data:"):
            # `data:<space?><payload>` — strip optional leading whitespace.
            data_lines.append(line[5:].lstrip().decode("utf-8"))
        elif line.startswith(b"event:"):
            event_field = line[6:].lstrip().decode("utf-8")
    if not data_lines:
        return None
    payload_text = "\n".join(data_lines)
    try:
        payload: Any = json.loads(payload_text)
    except json.JSONDecodeError as e:
        _logger.warning('0x000997', "Skipping malformed SSE frame: %s", e)
        return None
    if not isinstance(payload, dict):
        return None
    event_type = event_field or payload.get("eventType")
    if not event_type:
        return None
    model = _EVENT_MODELS.get(event_type)
    if model is None:
        return None  # forward-compat: drop unknown events
    # Pydantic models require ``eventType`` as a field. Sync it from the
    # resolved discriminator so the SSE ``event:`` line is authoritative
    # even if payload carries a stale/missing value.
    if payload.get("eventType") != event_type:
        payload = {**payload, "eventType": event_type}
    try:
        validated = model.model_validate(payload)
    except ValidationError as e:
        _logger.warning('0x000996', "Skipping invalid %s frame: %s", event_type, e)
        return None
    # mypy can't narrow union return: this is one of the StreamEvent variants
    # by construction of _EVENT_MODELS.
    return validated  # type: ignore[return-value]
