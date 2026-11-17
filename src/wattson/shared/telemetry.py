"""OpenTelemetry traces of the agents, for Grafana Tempo.

On only when OTEL_EXPORTER_OTLP_ENDPOINT is set (OTLP over HTTP, e.g. http://wattson-tempo:4318).
Without it nothing is set up: no provider, no exporter, no thread, no instrumentation, and span()
makes a no-op span. OTEL_SERVICE_NAME names the agent (default: the name given to setup_telemetry).

A span may hold ids, names, numbers and statuses. Never a request, a prompt, a document, evidence,
an answer, Claude's output or a token (SEC-14). Three locks:
1. Strands replaces its content attributes with "[REDACTED]": OTEL_SEMCONV_STABILITY_OPT_IN is set
   to SEMCONV_OPT_IN here (an empty `gen_ai_unredacted_attributes=` list means "redact them all").
2. SafeSpanExporter sits in front of the OTLP exporter: it drops span events (an exception keeps
   only its type), content-like attributes, header attributes, query strings and status texts,
   then runs redact() on every string that is left.
3. Our own spans (span(), record_span()) are given only ids, names, numbers and statuses.
   Durations and percentages are also given as integers (*_ms, *_pct): Tempo computes quantiles
   of integer attributes only.

Trace context: httpx and httpx2 calls carry a W3C traceparent (A2A, admin calls, the model server,
Grimoire; the mcp client also puts it in each request's _meta); incoming requests read it
(server_middleware(), traced_asgi()), so one task is one trace across the agents, and the CLI
sends the same trace id with every request of a task. /healthz is never traced.
a2a-sdk's own internal spans are off by default (wattson/agents/__init__.py)."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
)

from wattson import __version__
from wattson.shared.redact import redact

log = logging.getLogger(__name__)

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
SERVICE_ENV = "OTEL_SERVICE_NAME"
SEMCONV_ENV = "OTEL_SEMCONV_STABILITY_OPT_IN"
# http: the stable HTTP attribute names (url.path, url.query, http.request.method, ...).
# gen_ai_latest_experimental: one aggregated content event per span instead of one per message.
# gen_ai_unredacted_attributes= with an empty list: every sensitive gen_ai attribute is redacted.
SEMCONV_OPT_IN = "http,gen_ai_latest_experimental,gen_ai_unredacted_attributes="
TRACER_NAME = "wattson"
EXCLUDED_URLS = "/healthz$"  # probes: never traced
MAX_VALUE = 1024  # a longer string attribute is cut

_provider: TracerProvider | None = None
_quiet: ContextVar[bool] = ContextVar("wattson_telemetry_quiet", default=False)


# ---- the filter in front of the exporter -----------------------------------------------------

# Attribute keys that hold (or may hold) content. Compared in lower case.
_CONTENT_KEYS = frozenset(
    {
        "content",
        "message",
        "messages",
        "query",
        "tool.result",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.tool.definitions",
    }
)
_CONTENT_PREFIXES = (
    "gen_ai.input.",
    "gen_ai.output.",
    "gen_ai.prompt",
    "gen_ai.completion",
    "http.request.header.",
    "http.response.header.",
)
_CONTENT_SUFFIXES = (".messages", ".content", ".query", ".arguments", ".body", ".message", ".stacktrace")
# URL attributes: kept, but without the query string (it can hold a code or a token).
_URL_KEYS = frozenset({"url.full", "url.original", "http.url", "http.target"})


def is_content_key(key: str) -> bool:
    k = key.lower()
    return k in _CONTENT_KEYS or k.startswith(_CONTENT_PREFIXES) or k.endswith(_CONTENT_SUFFIXES)


def _clean_text(text: str) -> str:
    return redact(text).text[:MAX_VALUE]


def _clean_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        if key.lower() in _URL_KEYS:
            value = value.split("?", 1)[0].split("#", 1)[0]
        return _clean_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(_clean_text(v) if isinstance(v, str) else v for v in value)
    return value


def clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Content-like keys out, query strings cut, every string redacted."""
    return {k: _clean_value(k, v) for k, v in (attributes or {}).items() if not is_content_key(k)}


def _clean_events(events: Sequence[Event]) -> list[Event]:
    """Span events carry the content (Strands' messages, exception texts and stack traces). Only
    the exception type is kept."""
    kept: list[Event] = []
    for event in events:
        if event.name == "exception":
            kind = (event.attributes or {}).get("exception.type")
            attributes = {"exception.type": _clean_text(str(kind))} if kind else {}
            kept.append(Event("exception", attributes, timestamp=event.timestamp))
    return kept


def clean_span(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=_clean_text(span.name)[:200],
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=clean_attributes(span.attributes),
        events=_clean_events(span.events),
        links=[Link(link.context, clean_attributes(link.attributes)) for link in span.links],
        kind=span.kind,
        # The description is an error text: it can quote the input. Only the code stays.
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeSpanExporter(SpanExporter):
    """Cleans every span (clean_span) before the real exporter sees it. Defense in depth: Strands
    redacts its content already."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            cleaned = [clean_span(s) for s in spans]
        except Exception:  # noqa: BLE001 - never export what could not be cleaned
            log.warning("telemetry: spans dropped, they could not be cleaned")
            return SpanExportResult.FAILURE
        return self.inner.export(cleaned)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


# ---- set up ----------------------------------------------------------------------------------


def _fresh_strands_tracer() -> None:
    """Strands reads OTEL_SEMCONV_STABILITY_OPT_IN when its tracer is made; make it read again."""
    from strands.telemetry import tracer as strands_tracer

    strands_tracer._tracer_instance = None


def setup_telemetry(service_name_default: str, *, exporter: SpanExporter | None = None) -> bool:
    """Once per process, at start. True when traces are on.

    Off (False, nothing set up) unless OTEL_EXPORTER_OTLP_ENDPOINT is set or `exporter` is given
    (tests: an in-memory exporter, exported at once). A second call changes nothing."""
    global _provider
    if _provider is not None:
        return True
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint and exporter is None:
        return False
    os.environ[SEMCONV_ENV] = SEMCONV_OPT_IN  # before Strands and the instrumentations read it
    _fresh_strands_tracer()
    name = os.environ.get(SERVICE_ENV, "").strip() or service_name_default
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: name, SERVICE_VERSION: __version__}))
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # The exporter adds /v1/traces to OTEL_EXPORTER_OTLP_ENDPOINT.
        provider.add_span_processor(BatchSpanProcessor(SafeSpanExporter(OTLPSpanExporter())))
    else:
        provider.add_span_processor(SimpleSpanProcessor(SafeSpanExporter(exporter)))
    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor

    # Outgoing calls carry the trace context: httpx (A2A, admin routes, the model server) and
    # httpx2 (the mcp client and the OAuth calls to Grimoire). No header or body is recorded.
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    HTTPX2ClientInstrumentor().instrument(tracer_provider=provider)
    _provider = provider
    log.info("telemetry: traces of %s go to %s", name, endpoint or "the given exporter")
    return True


def enabled() -> bool:
    return _provider is not None


def shutdown_telemetry() -> None:
    """Export what is left and stop. Also undoes the httpx instrumentation (for tests)."""
    global _provider
    if _provider is None:
        return
    from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor

    HTTPXClientInstrumentor().uninstrument()
    HTTPX2ClientInstrumentor().uninstrument()
    _provider.shutdown()
    _provider = None


# ---- incoming requests -----------------------------------------------------------------------

_TASK_ID = re.compile(r"t-\d{8}-[0-9a-z]{6}")


def _span_details(scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Span name "METHOD /path", a task id in the path as {task_id}."""
    method = str(scope.get("method") or "")
    path = _TASK_ID.sub("{task_id}", str(scope.get("path") or "/"))
    return f"{method} {path}".strip(), {"http.route": path}


def _middleware_options() -> dict[str, Any]:
    return {
        "excluded_urls": EXCLUDED_URLS,
        "default_span_details": _span_details,
        "exclude_spans": ["receive", "send"],  # no span per ASGI message
        "tracer_provider": _provider,
    }


def server_middleware() -> list[Any]:
    """For Starlette(middleware=[*server_middleware(), ...]): first in the list, so the trace
    context is read before anything else. Empty when telemetry is off."""
    if _provider is None:
        return []
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    from starlette.middleware import Middleware

    return [Middleware(OpenTelemetryMiddleware, **_middleware_options())]


def traced_asgi(app: Any) -> Any:
    """The ASGI app, wrapped so incoming requests read the trace context. As is when off."""
    if _provider is None:
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    return OpenTelemetryMiddleware(app, **_middleware_options())


# ---- our own spans ---------------------------------------------------------------------------


NO_SPAN: Span = trace.INVALID_SPAN  # a span that records nothing


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME, __version__)


def _value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def set_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    """Set the attributes that are not None. Only ids, names, numbers and statuses belong here."""
    if span.is_recording():
        span.set_attributes({k: _value(v) for k, v in attributes.items() if v is not None})


@contextmanager
def quiet() -> Iterator[None]:
    """Work that is not a user's (the triage warm-up): no wattson span, and its HTTP calls make no
    span and carry no trace context."""
    from opentelemetry.instrumentation.utils import suppress_http_instrumentation

    token = _quiet.set(True)
    try:
        with suppress_http_instrumentation():
            yield
    finally:
        _quiet.reset(token)


@contextmanager
def span(name: str, /, **attributes: Any) -> Iterator[Span]:
    """A span around a block, current while the block runs. Dotted keys: span(n, **{"a.b": 1}).
    An exception marks the span as an error with its type only (error.type), never its text."""
    if _quiet.get():
        yield NO_SPAN
        return
    with tracer().start_as_current_span(name, record_exception=False, set_status_on_exception=False) as s:
        set_attributes(s, attributes)
        try:
            yield s
        except BaseException as exc:
            s.set_status(Status(StatusCode.ERROR))
            set_attributes(s, {"error.type": type(exc).__name__})
            raise


def record_span(
    name: str,
    attributes: Mapping[str, Any],
    *,
    start_ns: int,
    end_ns: int | None = None,
    error: bool = False,
    parent: context_api.Context | None = None,
) -> None:
    """A span for work that is already over (a workflow step seen by two hooks)."""
    if _quiet.get():
        return
    s = tracer().start_span(name, context=parent, start_time=start_ns)
    set_attributes(s, attributes)
    if error:
        s.set_status(Status(StatusCode.ERROR))
    s.end(end_time=end_ns or time.time_ns())


async def run_in_span[T](name: str, work: Awaitable[T], /, **attributes: Any) -> T:
    """Await `work` in a span; wattson.model.ok is True when it returns, False when it raises."""
    with span(name, **attributes, **{"wattson.model.ok": False}) as s:
        result = await work
        set_attributes(s, {"wattson.model.ok": True})
        return result


def percent(value: float | None) -> int | None:
    """0..1 as an integer percent, 0..100."""
    return None if value is None else max(0, min(100, round(float(value) * 100)))


def triage_attributes(classification: Any) -> dict[str, Any]:
    """The triage decision (a Classification, a TaskRecord or a dict) as span attributes."""
    if isinstance(classification, Mapping):
        get = classification.get
    else:

        def get(key: str) -> Any:
            return getattr(classification, key, None)

    confidence = get("confidence")
    return {
        "wattson.triage.category": get("category"),
        "wattson.triage.confidence": None if confidence is None else float(confidence),
        "wattson.triage.confidence_pct": percent(confidence),
        "wattson.triage.method": get("method"),
    }


def current_trace() -> dict[str, str] | None:
    """The current trace and span ids (hex), to come back to the trace later. None when off."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


def parent_context(saved: Mapping[str, Any] | None) -> context_api.Context | None:
    """A context whose parent is the span saved by current_trace()."""
    if not saved:
        return None
    try:
        ctx = SpanContext(
            trace_id=int(str(saved["trace_id"]), 16),
            span_id=int(str(saved["span_id"]), 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except (KeyError, ValueError):
        return None
    if not ctx.is_valid:
        return None
    return trace.set_span_in_context(NonRecordingSpan(ctx))
