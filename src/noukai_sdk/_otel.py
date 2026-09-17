"""Optional, opt-in OpenTelemetry integration (customer-side tracing).

When a client is constructed with ``otel=True``, each ``flow.execute`` /
``flow.execute_async`` call emits **one parent span of kind CLIENT** into the
caller's own configured OpenTelemetry provider (Datadog / Honeycomb / Jaeger /
any OTLP backend). The SDK produces no new data — the span carries the
org/project/slug/execution_id/status the call already has.

When ``otel`` is ``False`` (the default) this module hands back a no-op factory
and **never imports** ``opentelemetry`` — the off path emits nothing and adds
only a couple of trivial allocations per call.

This module is the SDK's *only* point of contact with the ``opentelemetry`` API.
The rest of the SDK talks to the language-neutral :class:`FlowSpan` handle, so
no other file imports ``opentelemetry``. Follows OTel semantic conventions:
span kind CLIENT; ``gen_ai.*`` is reserved for the (deferred) per-step child
spans, so v1 uses stable, self-namespaced ``noukai.*`` attributes only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from ._errors import NoukaiError
from ._version import __version__


class FlowSpan(Protocol):
    """Language-neutral handle the Flow proxy sets end-of-call attributes on."""

    def set_execution_id(self, execution_id: str | None) -> None: ...

    def set_status(self, status: str | None) -> None: ...


class SpanFactory(Protocol):
    """Opens one parent CLIENT span around a flow call."""

    def flow_span(
        self, op: str, *, org: str, project: str, slug: str, version: str
    ) -> AbstractContextManager[FlowSpan]: ...


# ---------------------------------------------------------------------------
# No-op implementation (default; never touches opentelemetry)
# ---------------------------------------------------------------------------


class _NoopFlowSpan:
    def set_execution_id(self, execution_id: str | None) -> None:
        pass

    def set_status(self, status: str | None) -> None:
        pass


class NoopSpanFactory:
    """No-op factory used when OTel is disabled. Emits nothing and imports no
    OpenTelemetry code."""

    @contextmanager
    def flow_span(
        self, op: str, *, org: str, project: str, slug: str, version: str
    ) -> Iterator[FlowSpan]:
        yield _NoopFlowSpan()


# ---------------------------------------------------------------------------
# Real implementation (only constructed when otel=True)
# ---------------------------------------------------------------------------


class _OtelFlowSpan:
    def __init__(self, span: Any) -> None:
        self._span = span

    def set_execution_id(self, execution_id: str | None) -> None:
        if execution_id is not None:
            self._span.set_attribute("noukai.execution_id", execution_id)

    def set_status(self, status: str | None) -> None:
        if status is not None:
            self._span.set_attribute("noukai.flow.status", status)


class OtelSpanFactory:
    """Emits a CLIENT span per flow call into the caller's OTel provider."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

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
                yield _OtelFlowSpan(span)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise


def get_flow_tracer(enabled: bool, tracer: Any | None = None) -> SpanFactory:
    """Return the span factory for a client.

    - ``enabled=False`` → :class:`NoopSpanFactory` (does not import opentelemetry).
    - ``enabled=True`` + ``tracer`` given → wrap that tracer directly.
    - ``enabled=True`` + no tracer → acquire a named tracer from the globally
      configured provider (``opentelemetry.trace.get_tracer``).

    Raises:
        NoukaiError: ``enabled=True`` with no ``tracer`` and ``opentelemetry-api``
            is not installed. Install the extra: ``pip install noukai-sdk[otel]``.
    """
    if not enabled:
        return NoopSpanFactory()
    if tracer is not None:
        return OtelSpanFactory(tracer)
    try:
        from opentelemetry import trace as _otel_trace
    except ImportError as exc:
        raise NoukaiError(
            "Noukai(otel=True) requires OpenTelemetry. Install the extra: "
            "`pip install noukai-sdk[otel]` (or pass an explicit tracer=)."
        ) from exc
    return OtelSpanFactory(_otel_trace.get_tracer("noukai-sdk", __version__))
