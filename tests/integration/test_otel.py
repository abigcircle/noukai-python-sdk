"""Integration tests: opt-in customer-side OpenTelemetry (design
20260916-SDK-otel-and-replay-rename).

The parent-span test runs against a live flow today. The per-block step-span
test is xfail until the slug-scoped ``run.trace()`` endpoint is deployed (the
same server prerequisite as ``test_run_proxy.py``) — it auto-passes once that
lands, because the SDK swallows the trace-fetch failure until then.

Requires the ``[otel]`` extra plus the ``NOUKAI_INTEGRATION_*`` env (the
package-level skip in ``conftest.py`` gates the whole suite).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from noukai_sdk import Noukai  # noqa: E402

_KEY = os.environ.get("NOUKAI_INTEGRATION_KEY")
_PROJECT = os.environ.get("NOUKAI_INTEGRATION_PROJECT", "")
_HELLO = os.environ.get("NOUKAI_INTEGRATION_HELLO_SLUG", "")


def _tracing() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("integration")


@pytest.mark.integration
def test_parent_span_emitted_end_to_end() -> None:
    """A live execute() with otel=True emits one CLIENT span carrying the real
    execution_id and status."""
    exporter, tracer = _tracing()
    with Noukai(api_key=_KEY, otel=True, tracer=tracer) as client:
        result = client.flow(f"{_PROJECT}/{_HELLO}").execute(message="otel parent span")

    spans = [s for s in exporter.get_finished_spans() if s.name == "noukai.flow.execute"]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes is not None
    assert span.attributes["noukai.flow.slug"] == _HELLO
    assert span.attributes["noukai.execution_id"] == result.execution_id
    assert span.attributes["noukai.flow.status"] == "completed"
    # The live execute() should also surface the flow output.
    assert result.output is not None


@pytest.mark.integration
@pytest.mark.xfail(
    reason="Server prereq pending: slug-scoped /seq/.../runs/{id}/trace endpoint "
    "accepting nk_* keys is not yet deployed. Auto-passes once it lands.",
    strict=False,
)
def test_step_spans_get_per_block_data_end_to_end() -> None:
    """A live execute() with otel_steps=True fetches run.trace() and emits one
    child span per pipeline block, carrying each block's data/results."""
    exporter, tracer = _tracing()
    with Noukai(api_key=_KEY, otel_steps=True, otel_step_payloads=True, tracer=tracer) as client:
        client.flow(f"{_PROJECT}/{_HELLO}").execute(message="otel step spans")

    children = [s for s in exporter.get_finished_spans() if s.name == "noukai.flow.step"]
    assert len(children) >= 1
    for child in children:
        assert child.attributes is not None
        assert "noukai.step.id" in child.attributes
        assert "noukai.step.status" in child.attributes
