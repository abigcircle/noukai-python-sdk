"""Optional, opt-in OpenTelemetry integration (customer-side tracing).

When a client is constructed with ``otel=True``, each ``flow.execute`` /
``flow.execute_async`` call emits **one parent span of kind CLIENT** into the
caller's own configured OpenTelemetry provider (Datadog / Honeycomb / Jaeger /
any OTLP backend). The SDK produces no new data — the span carries the
org/project/slug/execution_id/status the call already has.

With ``otel_steps=True`` the SDK additionally fetches ``run.trace()`` after a
completed ``execute`` and synthesizes **one child span per pipeline block**,
backdated to the block's real start/end, nested under the parent — carrying the
block's model, token usage, cost, duration, and status. With
``otel_step_payloads=True`` each child also carries a size-bounded copy of the
block's input data and output results (off by default — this can contain PII).

When ``otel`` is ``False`` (the default) this module hands back a no-op factory
and **never imports** ``opentelemetry`` — the off path emits nothing and adds
only a couple of trivial allocations per call.

This module is the SDK's *only* point of contact with the ``opentelemetry`` API.
The rest of the SDK talks to the language-neutral :class:`FlowSpan` handle, so
no other file imports ``opentelemetry``. Follows OTel semantic conventions:
span kind CLIENT for the call, ``gen_ai.*`` for per-block model/token usage.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Any, Protocol

from ._errors import NoukaiError
from ._models.trace import StepTrace
from ._version import __version__

# Cap the serialized size of a per-block input/output payload attribute so a
# large block context can't blow past OTel backends' attribute-size limits.
_MAX_STEP_PAYLOAD_CHARS = 4096


class FlowSpan(Protocol):
    """Language-neutral handle the Flow proxy sets end-of-call state through."""

    def set_execution_id(self, execution_id: str | None) -> None: ...

    def set_status(self, status: str | None) -> None: ...

    def emit_step_spans(self, steps: Sequence[StepTrace]) -> None: ...


class SpanFactory(Protocol):
    """Opens one parent CLIENT span around a flow call."""

    @property
    def step_spans_enabled(self) -> bool: ...

    def flow_span(
        self, op: str, *, org: str, project: str, slug: str, version: str
    ) -> AbstractContextManager[FlowSpan]: ...


# ---------------------------------------------------------------------------
# Helpers (no opentelemetry import)
# ---------------------------------------------------------------------------


def _iso_to_ns(value: str | None) -> int | None:
    """Convert an ISO-8601 timestamp to epoch nanoseconds, or None if absent
    / unparseable. Handles a trailing ``Z`` on Python 3.10."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return int(dt.timestamp() * 1_000_000_000)


def _bounded_json(obj: Any, max_chars: int = _MAX_STEP_PAYLOAD_CHARS) -> str:
    """Serialize ``obj`` to JSON, truncating to ``max_chars`` characters with a
    marker so an oversized block context never overflows a span attribute."""
    try:
        text = json.dumps(obj, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > max_chars:
        return f"{text[:max_chars]}…[truncated {len(text) - max_chars} chars]"
    return text


# ---------------------------------------------------------------------------
# No-op implementation (default; never touches opentelemetry)
# ---------------------------------------------------------------------------


class _NoopFlowSpan:
    def set_execution_id(self, execution_id: str | None) -> None:
        pass

    def set_status(self, status: str | None) -> None:
        pass

    def emit_step_spans(self, steps: Sequence[StepTrace]) -> None:
        pass


class NoopSpanFactory:
    """No-op factory used when OTel is disabled. Emits nothing and imports no
    OpenTelemetry code."""

    step_spans_enabled = False

    @contextmanager
    def flow_span(
        self, op: str, *, org: str, project: str, slug: str, version: str
    ) -> Iterator[FlowSpan]:
        yield _NoopFlowSpan()


# ---------------------------------------------------------------------------
# Real implementation (only constructed when otel is enabled)
# ---------------------------------------------------------------------------


class _OtelFlowSpan:
    def __init__(self, span: Any, tracer: Any, *, payloads: bool, max_chars: int) -> None:
        self._span = span
        self._tracer = tracer
        self._payloads = payloads
        self._max_chars = max_chars

    def set_execution_id(self, execution_id: str | None) -> None:
        if execution_id is not None:
            self._span.set_attribute("noukai.execution_id", execution_id)

    def set_status(self, status: str | None) -> None:
        if status is not None:
            self._span.set_attribute("noukai.flow.status", status)

    def emit_step_spans(self, steps: Sequence[StepTrace]) -> None:
        """Synthesize one backdated INTERNAL child span per pipeline block,
        nested under this (currently-active) parent span."""
        from opentelemetry.trace import SpanKind, Status, StatusCode

        for st in steps:
            start_ns = _iso_to_ns(st.started_at)
            start_kwargs: dict[str, Any] = {"kind": SpanKind.INTERNAL}
            if start_ns is not None:
                start_kwargs["start_time"] = start_ns
            child = self._tracer.start_span("noukai.flow.step", **start_kwargs)
            try:
                self._set_step_attributes(child, st)
                if st.status == "failed":
                    child.set_status(Status(StatusCode.ERROR))
            finally:
                end_ns = _iso_to_ns(st.completed_at)
                if end_ns is not None:
                    child.end(end_time=end_ns)
                else:
                    child.end()

    def _set_step_attributes(self, span: Any, st: StepTrace) -> None:
        span.set_attribute("noukai.step.id", st.step_id)
        span.set_attribute("noukai.step.attempt", st.attempt)
        span.set_attribute("noukai.step.status", st.status)
        if st.duration_ms is not None:
            span.set_attribute("noukai.step.duration_ms", st.duration_ms)
        if st.loop_index is not None:
            span.set_attribute("noukai.step.loop_index", st.loop_index)
        if st.model_used is not None:
            span.set_attribute("gen_ai.request.model", st.model_used)
        if st.tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", st.tokens.prompt)
            span.set_attribute("gen_ai.usage.output_tokens", st.tokens.completion)
        if st.cost_usd is not None:
            span.set_attribute("noukai.step.cost_usd", st.cost_usd)
        if self._payloads:
            if st.input_context is not None:
                span.set_attribute(
                    "noukai.step.input", _bounded_json(st.input_context, self._max_chars)
                )
            if st.output_context is not None:
                span.set_attribute(
                    "noukai.step.output", _bounded_json(st.output_context, self._max_chars)
                )
            # For a failed block the error context is its "result" — attach it
            # (bounded, gated by the same payloads flag) so the span is diagnostic.
            if st.error_context is not None:
                span.set_attribute(
                    "noukai.step.error", _bounded_json(st.error_context, self._max_chars)
                )


class OtelSpanFactory:
    """Emits a CLIENT span per flow call into the caller's OTel provider,
    optionally with per-block INTERNAL child spans."""

    def __init__(
        self,
        tracer: Any,
        *,
        step_spans: bool = False,
        step_payloads: bool = False,
        max_payload_chars: int = _MAX_STEP_PAYLOAD_CHARS,
    ) -> None:
        self._tracer = tracer
        self._step_spans = step_spans
        self._step_payloads = step_payloads
        self._max_payload_chars = max_payload_chars

    @property
    def step_spans_enabled(self) -> bool:
        return self._step_spans

    @contextmanager
    def flow_span(
        self, op: str, *, org: str, project: str, slug: str, version: str
    ) -> Iterator[FlowSpan]:
        from opentelemetry.trace import SpanKind, Status, StatusCode

        # Disable the context manager's own exception handling: we record the
        # exception + set ERROR status explicitly below (matching the TS SDK).
        # Leaving both on would record the exception twice on the span.
        with self._tracer.start_as_current_span(
            f"noukai.flow.{op}",
            kind=SpanKind.CLIENT,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("noukai.org", org)
            span.set_attribute("noukai.project", project)
            span.set_attribute("noukai.flow.slug", slug)
            span.set_attribute("noukai.flow.version", version)
            try:
                yield _OtelFlowSpan(
                    span,
                    self._tracer,
                    payloads=self._step_payloads,
                    max_chars=self._max_payload_chars,
                )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise


def get_flow_tracer(
    enabled: bool,
    tracer: Any | None = None,
    *,
    step_spans: bool = False,
    step_payloads: bool = False,
) -> SpanFactory:
    """Return the span factory for a client.

    - ``enabled=False`` → :class:`NoopSpanFactory` (does not import opentelemetry).
    - ``enabled=True`` + ``tracer`` given → wrap that tracer directly.
    - ``enabled=True`` + no tracer → acquire a named tracer from the globally
      configured provider (``opentelemetry.trace.get_tracer``).

    ``step_spans`` enables per-block child spans (an extra ``run.trace()`` fetch
    per completed ``execute``); ``step_payloads`` additionally attaches the
    bounded block input/output data.

    Raises:
        NoukaiError: ``enabled=True`` with no ``tracer`` and ``opentelemetry-api``
            is not installed. Install the extra: ``pip install noukai-sdk[otel]``.
    """
    if not enabled:
        return NoopSpanFactory()
    if tracer is not None:
        return OtelSpanFactory(tracer, step_spans=step_spans, step_payloads=step_payloads)
    try:
        from opentelemetry import trace as _otel_trace
    except ImportError as exc:
        raise NoukaiError(
            "Noukai(otel=True) requires OpenTelemetry. Install the extra: "
            "`pip install noukai-sdk[otel]` (or pass an explicit tracer=)."
        ) from exc
    return OtelSpanFactory(
        _otel_trace.get_tracer("noukai-sdk", __version__),
        step_spans=step_spans,
        step_payloads=step_payloads,
    )
