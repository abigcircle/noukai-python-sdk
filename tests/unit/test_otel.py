"""Opt-in customer-side OpenTelemetry integration (design
20260916-SDK-otel-and-replay-rename, PR-B).

Verifies the parent CLIENT span is emitted for execute/execute_async when
`otel=True`, is a true no-op when off, records errors, and fails fast with a
clear message when the optional dependency is missing.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from noukai_sdk import AsyncNoukai, Noukai, NoukaiError
from noukai_sdk._flow import AsyncFlow, Flow
from noukai_sdk._otel import (
    NoopSpanFactory,
    OtelSpanFactory,
    _bounded_json,
    _iso_to_ns,
    get_flow_tracer,
)
from noukai_sdk._transport_shared import Response

COMPLETED_BODY = {
    "status": "completed",
    "result": {"ok": True},
    "flowId": "flow-1",
    "blockCount": 2,
    "executionId": "exec-123",
}


@pytest.fixture
def tracing() -> tuple[InMemorySpanExporter, Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


TRACE_BODY = {
    "flowRun": {"id": "exec-123", "flowId": "flow-1", "status": "completed", "stepCount": 2},
    "steps": [
        {
            "stepId": "block-a",
            "attempt": 1,
            "status": "completed",
            "startedAt": "2026-09-16T00:00:00Z",
            "completedAt": "2026-09-16T00:00:01Z",
            "durationMs": 1000,
            "modelUsed": "claude-haiku-4-5",
            "tokens": {"prompt": 10, "completion": 5, "total": 15},
            "costUsd": "0.0001",
            "inputContext": {"prompt": "grade this"},
            "outputContext": {"text": "graded"},
        },
        {
            "stepId": "block-b",
            "attempt": 1,
            "status": "failed",
            "startedAt": "2026-09-16T00:00:01Z",
            "completedAt": "2026-09-16T00:00:02Z",
            "durationMs": 1000,
            "errorContext": {"message": "block failed"},
        },
    ],
}


class _StubSyncTransport:
    def __init__(
        self,
        span_factory: Any,
        body: dict[str, Any] | None,
        trace_body: dict[str, Any] | None = None,
    ) -> None:
        self._span_factory = span_factory
        self._default_session_id = None
        self._body = body
        self._trace_body = trace_body
        self.raise_exc: Exception | None = None
        self.trace_raises = False
        self.trace_fetched = False

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if method == "GET" and url.endswith("/trace"):
            self.trace_fetched = True
            if self.trace_raises:
                raise NoukaiError("trace boom")
            return Response(status_code=200, body=self._trace_body, request_id="req-2", headers={})
        if self.raise_exc is not None:
            raise self.raise_exc
        return Response(status_code=200, body=self._body, request_id="req-1", headers={})


class _StubAsyncTransport(_StubSyncTransport):
    async def request(self, method: str, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        if method == "GET" and url.endswith("/trace"):
            self.trace_fetched = True
            if self.trace_raises:
                raise NoukaiError("trace boom")
            return Response(status_code=200, body=self._trace_body, request_id="req-2", headers={})
        if self.raise_exc is not None:
            raise self.raise_exc
        return Response(status_code=200, body=self._body, request_id="req-1", headers={})


# ---------------------------------------------------------------------------
# get_flow_tracer / factory wiring
# ---------------------------------------------------------------------------


def test_disabled_returns_noop_factory() -> None:
    assert isinstance(get_flow_tracer(False), NoopSpanFactory)


def test_enabled_with_explicit_tracer_returns_otel_factory(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    _, tracer = tracing
    assert isinstance(get_flow_tracer(True, tracer), OtelSpanFactory)


def test_enabled_without_dependency_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate opentelemetry not being installed: None in sys.modules makes
    # `from opentelemetry import trace` raise ImportError.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with pytest.raises(NoukaiError, match=r"otel"):
        get_flow_tracer(True)


def test_client_off_by_default_uses_noop() -> None:
    client = Noukai(api_key="nk_test", org="acme", project="proj")
    assert isinstance(client._transport._span_factory, NoopSpanFactory)


def test_client_otel_true_uses_otel_factory(tracing: tuple[InMemorySpanExporter, Any]) -> None:
    _, tracer = tracing
    client = Noukai(api_key="nk_test", org="acme", project="proj", otel=True, tracer=tracer)
    assert isinstance(client._transport._span_factory, OtelSpanFactory)
    async_client = AsyncNoukai(
        api_key="nk_test", org="acme", project="proj", otel=True, tracer=tracer
    )
    assert isinstance(async_client._transport._span_factory, OtelSpanFactory)


# ---------------------------------------------------------------------------
# Span emission through the real execute decorators
# ---------------------------------------------------------------------------


def test_sync_execute_emits_client_span(tracing: tuple[InMemorySpanExporter, Any]) -> None:
    exporter, tracer = tracing
    flow = Flow(
        _StubSyncTransport(OtelSpanFactory(tracer), COMPLETED_BODY), "acme", "proj", "grade"
    )
    result = flow.execute(message="hi")
    assert result.status == "completed"  # type: ignore[union-attr]

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "noukai.flow.execute"
    assert span.kind is SpanKind.CLIENT
    assert span.attributes["noukai.org"] == "acme"
    assert span.attributes["noukai.project"] == "proj"
    assert span.attributes["noukai.flow.slug"] == "grade"
    # Default version is now "production" (design 20260917-SDK-version-production-routing).
    assert span.attributes["noukai.flow.version"] == "production"
    assert span.attributes["noukai.execution_id"] == "exec-123"
    assert span.attributes["noukai.flow.status"] == "completed"


async def test_async_execute_emits_client_span(tracing: tuple[InMemorySpanExporter, Any]) -> None:
    exporter, tracer = tracing
    flow = AsyncFlow(
        _StubAsyncTransport(OtelSpanFactory(tracer), COMPLETED_BODY), "acme", "proj", "grade"
    )
    result = await flow.execute(message="hi")
    assert result.status == "completed"  # type: ignore[union-attr]

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "noukai.flow.execute"
    assert spans[0].attributes["noukai.execution_id"] == "exec-123"


def test_off_emits_no_span(tracing: tuple[InMemorySpanExporter, Any]) -> None:
    exporter, _ = tracing
    flow = Flow(_StubSyncTransport(NoopSpanFactory(), COMPLETED_BODY), "acme", "proj", "grade")
    result = flow.execute(message="hi")
    assert result.status == "completed"  # type: ignore[union-attr]
    assert exporter.get_finished_spans() == ()


def test_error_sets_error_status_and_reraises(tracing: tuple[InMemorySpanExporter, Any]) -> None:
    exporter, tracer = tracing
    transport = _StubSyncTransport(OtelSpanFactory(tracer), None)
    transport.raise_exc = NoukaiError("boom")
    flow = Flow(transport, "acme", "proj", "grade")

    with pytest.raises(NoukaiError, match="boom"):
        flow.execute(message="hi")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    # The exception is recorded exactly once (the CM's auto-recording is off).
    exception_events = [e for e in spans[0].events if e.name == "exception"]
    assert len(exception_events) == 1


# ---------------------------------------------------------------------------
# Per-block child spans (otel_steps / otel_step_payloads)
# ---------------------------------------------------------------------------


def test_otel_steps_emits_one_child_span_per_block(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    exporter, tracer = tracing
    transport = _StubSyncTransport(
        get_flow_tracer(True, tracer, step_spans=True), COMPLETED_BODY, TRACE_BODY
    )
    flow = Flow(transport, "acme", "proj", "grade")
    flow.execute(message="hi")

    assert transport.trace_fetched is True
    spans = exporter.get_finished_spans()
    parent = next(s for s in spans if s.name == "noukai.flow.execute")
    children = [s for s in spans if s.name == "noukai.flow.step"]
    assert len(children) == 2

    by_id = {s.attributes["noukai.step.id"]: s for s in children}
    a = by_id["block-a"]
    # nested under the parent call span
    assert a.parent is not None
    assert a.parent.span_id == parent.context.span_id
    # backdated to the block's real start/end
    assert a.start_time == _iso_to_ns("2026-09-16T00:00:00Z")
    assert a.end_time == _iso_to_ns("2026-09-16T00:00:01Z")
    # metadata (gen_ai.* + noukai.*)
    assert a.attributes["gen_ai.request.model"] == "claude-haiku-4-5"
    assert a.attributes["gen_ai.usage.input_tokens"] == 10
    assert a.attributes["gen_ai.usage.output_tokens"] == 5
    assert a.attributes["noukai.step.cost_usd"] == "0.0001"
    assert a.attributes["noukai.step.status"] == "completed"
    # payloads NOT attached by default
    assert "noukai.step.input" not in a.attributes
    assert "noukai.step.output" not in a.attributes
    # failed block → ERROR status
    assert by_id["block-b"].status.status_code is StatusCode.ERROR


def test_otel_step_payloads_attaches_bounded_input_output(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    exporter, tracer = tracing
    transport = _StubSyncTransport(
        get_flow_tracer(True, tracer, step_spans=True, step_payloads=True),
        COMPLETED_BODY,
        TRACE_BODY,
    )
    flow = Flow(transport, "acme", "proj", "grade")
    flow.execute(message="hi")

    a = next(
        s
        for s in exporter.get_finished_spans()
        if s.name == "noukai.flow.step" and s.attributes["noukai.step.id"] == "block-a"
    )
    assert "grade this" in a.attributes["noukai.step.input"]
    assert "graded" in a.attributes["noukai.step.output"]

    # A failed block's error context is attached (bounded) under the payloads flag.
    b = next(
        s
        for s in exporter.get_finished_spans()
        if s.name == "noukai.flow.step" and s.attributes["noukai.step.id"] == "block-b"
    )
    assert "block failed" in b.attributes["noukai.step.error"]


def test_parent_span_only_does_not_fetch_trace(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    exporter, tracer = tracing
    transport = _StubSyncTransport(
        get_flow_tracer(True, tracer, step_spans=False), COMPLETED_BODY, TRACE_BODY
    )
    flow = Flow(transport, "acme", "proj", "grade")
    flow.execute(message="hi")

    assert transport.trace_fetched is False
    assert [s.name for s in exporter.get_finished_spans()] == ["noukai.flow.execute"]


def test_step_span_fetch_failure_is_swallowed(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    exporter, tracer = tracing
    transport = _StubSyncTransport(
        get_flow_tracer(True, tracer, step_spans=True), COMPLETED_BODY, TRACE_BODY
    )
    transport.trace_raises = True
    flow = Flow(transport, "acme", "proj", "grade")

    result = flow.execute(message="hi")  # must not raise
    assert result.status == "completed"  # type: ignore[union-attr]
    # parent span still emitted (not ERROR), no children
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["noukai.flow.execute"]
    assert spans[0].status.status_code is not StatusCode.ERROR


async def test_async_otel_steps_emits_child_spans(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    exporter, tracer = tracing
    transport = _StubAsyncTransport(
        get_flow_tracer(True, tracer, step_spans=True), COMPLETED_BODY, TRACE_BODY
    )
    flow = AsyncFlow(transport, "acme", "proj", "grade")
    await flow.execute(message="hi")

    children = [s for s in exporter.get_finished_spans() if s.name == "noukai.flow.step"]
    assert len(children) == 2


def test_client_otel_steps_flag_enables_step_spans(
    tracing: tuple[InMemorySpanExporter, Any],
) -> None:
    _, tracer = tracing
    client = Noukai(api_key="nk_test", org="a", project="p", otel_steps=True, tracer=tracer)
    factory = client._transport._span_factory
    assert isinstance(factory, OtelSpanFactory)
    assert factory.step_spans_enabled is True


def test_iso_to_ns_and_bounded_json_helpers() -> None:
    assert _iso_to_ns(None) is None
    assert _iso_to_ns("not-a-timestamp") is None
    start = _iso_to_ns("2026-09-16T00:00:00Z")
    end = _iso_to_ns("2026-09-16T00:00:01Z")
    assert start is not None
    assert end is not None
    assert end > start

    bounded = _bounded_json({"x": "a" * 10_000}, max_chars=100)
    assert "truncated" in bounded
    assert len(bounded) < 200
