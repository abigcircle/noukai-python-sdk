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
from noukai_sdk._otel import NoopSpanFactory, OtelSpanFactory, get_flow_tracer
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


class _StubSyncTransport:
    def __init__(self, span_factory: Any, body: dict[str, Any] | None) -> None:
        self._span_factory = span_factory
        self._default_session_id = None
        self._body = body
        self.raise_exc: Exception | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if self.raise_exc is not None:
            raise self.raise_exc
        return Response(status_code=200, body=self._body, request_id="req-1", headers={})


class _StubAsyncTransport(_StubSyncTransport):
    async def request(self, method: str, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
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
    assert span.attributes["noukai.flow.version"] == "draft"
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
