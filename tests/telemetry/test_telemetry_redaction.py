"""What may leave the process. Three proofs:
1. SafeSpanExporter, alone: content-like attributes, events, headers, query strings and status texts
   are gone, and every string left is redacted.
2. Strands, alone, with OTEL_SEMCONV_STABILITY_OPT_IN: which value redacts what (raw spans, before
   our exporter). An empty `gen_ai_unredacted_attributes=` list redacts everything; without the
   token Strands sends the content as is, and only our exporter stops it.
3. The real path in a fresh process: OTEL_EXPORTER_OTLP_ENDPOINT -> OTLP/HTTP protobuf, read by a
   fake collector."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, StatusCode
from strands import Agent, tool
from strands.telemetry import tracer as strands_tracer

from wattson.shared import telemetry
from wattson.shared.planner import PlannerModel, step

from .conftest import Capture, attrs, span_text

SECRET = "s3cr3t-Hunter2-Value"  # noqa: S105 - a fake secret
PASSWORD_LINE = f"password={SECRET}"
CONTENT = "the user's request text: zebra-content-1122"


# ---- 1. the filter -----------------------------------------------------------------------------


def test_the_exporter_drops_content_and_redacts_what_is_left() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()  # a private provider: the filter alone
    provider.add_span_processor(SimpleSpanProcessor(telemetry.SafeSpanExporter(memory)))
    tracer = provider.get_tracer("test")
    other = tracer.start_span("other")
    other.end()
    link_attrs = {"gen_ai.input.messages": CONTENT, "link.kind": "follows"}
    span = tracer.start_span(f"step with {PASSWORD_LINE}", links=[Link(other.get_span_context(), link_attrs)])
    span.set_attributes(
        {
            "gen_ai.input.messages": CONTENT,
            "gen_ai.output.messages": CONTENT,
            "gen_ai.prompt.0.content": CONTENT,
            "gen_ai.completion": CONTENT,
            "gen_ai.system_instructions": CONTENT,
            "gen_ai.tool.call.arguments": CONTENT,
            "gen_ai.tool.call.result": CONTENT,
            "llm.messages": CONTENT,
            "doc.content": CONTENT,
            "db.query": CONTENT,
            "url.query": f"code=abc&token={SECRET}",
            "url.full": f"http://grimoire.test/cb?code=abc&token={SECRET}",
            "http.target": f"/v1/tasks?state={SECRET}",
            "http.request.header.authorization": f"Bearer {SECRET}",
            "error.message": CONTENT,
            "note": f"export {PASSWORD_LINE}",
            "notes": [f"export {PASSWORD_LINE}", "fine"],
            "wattson.task_id": "t-20261023-abc123",
            "wattson.wait_s": 1.5,
            "wattson.model.ok": True,
            "gen_ai.tool.name": "triage",
        }
    )
    span.add_event("gen_ai.client.inference.operation.details", {"gen_ai.input.messages": CONTENT})
    span.add_event("gen_ai.user.message", {"content": CONTENT})
    span.record_exception(ValueError(CONTENT))
    span.set_status(StatusCode.ERROR, CONTENT)
    span.end()

    (out,) = [s for s in memory.get_finished_spans() if s.name != "other"]
    assert attrs(out) == {
        "url.full": "http://grimoire.test/cb",
        "http.target": "/v1/tasks",
        "note": "export password=[REDACTED:password]",
        "notes": ("export password=[REDACTED:password]", "fine"),
        "wattson.task_id": "t-20261023-abc123",
        "wattson.wait_s": 1.5,
        "wattson.model.ok": True,
        "gen_ai.tool.name": "triage",
    }
    assert out.name == "step with password=[REDACTED:password]"
    assert [(e.name, dict(e.attributes or {})) for e in out.events] == [
        ("exception", {"exception.type": "ValueError"})
    ]
    assert out.status.status_code == StatusCode.ERROR and out.status.description is None
    assert [dict(link.attributes or {}) for link in out.links] == [{"link.kind": "follows"}]
    text = span_text([out])
    assert SECRET not in text and CONTENT not in text and "zebra-content" not in text


# ---- 2. Strands, per opt-in value ------------------------------------------------------------


@pytest.fixture
def strands_opt_in(monkeypatch: pytest.MonkeyPatch, capture: Capture) -> Iterator[Any]:
    """Run a small Strands agent under a given OTEL_SEMCONV_STABILITY_OPT_IN; give back the raw and
    the exported spans. The Strands tracer is made again before and after (it reads the value once)."""

    def run(value: str) -> tuple[list[Any], list[Any]]:
        monkeypatch.setenv(telemetry.SEMCONV_ENV, value)
        strands_tracer._tracer_instance = None

        @tool
        def echo(message: str) -> str:
            """Echo the message back."""
            return step(output={"echo": message})

        agent = Agent(
            name="echo-agent",
            model=PlannerModel("echo"),
            tools=[echo],
            system_prompt=f"System rules. {CONTENT}",
            callback_handler=None,
        )
        capture.clear()
        agent(f"{CONTENT} {PASSWORD_LINE}")
        return list(capture.raw.get_finished_spans()), list(capture.clean.get_finished_spans())

    yield run
    monkeypatch.undo()
    strands_tracer._tracer_instance = None


def _event_names(spans: list[Any]) -> set[str]:
    return {e.name for s in spans for e in s.events}


def test_our_value_redacts_every_strands_content(strands_opt_in: Any) -> None:
    assert telemetry.SEMCONV_OPT_IN == "http,gen_ai_latest_experimental,gen_ai_unredacted_attributes="
    raw, clean = strands_opt_in(telemetry.SEMCONV_OPT_IN)
    tracer = strands_tracer.get_tracer()
    assert tracer._redaction_enabled and tracer.use_latest_genai_conventions
    assert tracer._unredacted_exact == frozenset() and tracer._unredacted_globs == ()
    text = span_text(raw)
    assert CONTENT not in text and SECRET not in text and "zebra-content" not in text
    # Latest conventions: one aggregated content event per span, no per-message events.
    assert _event_names(raw) == {"gen_ai.client.inference.operation.details"}
    tool_span = next(s for s in raw if s.name == "execute_tool echo")
    assert attrs(tool_span)["gen_ai.tool.call.arguments"] == "[REDACTED]"
    assert attrs(tool_span)["gen_ai.tool.call.result"] == "[REDACTED]"
    agent_span = next(s for s in raw if s.name == "invoke_agent echo-agent")
    assert attrs(agent_span)["gen_ai.system_instructions"] == "[REDACTED]"
    assert all(v == "[REDACTED]" for s in raw for e in s.events for v in (e.attributes or {}).values())
    # And the exporter drops even the "[REDACTED]" placeholders.
    assert "[REDACTED]" not in span_text(clean) and not any(s.events for s in clean)


def test_legacy_events_are_redacted_too(strands_opt_in: Any) -> None:
    """Without gen_ai_latest_experimental Strands sends one event per message (gen_ai.user.message,
    gen_ai.tool.message, gen_ai.choice, gen_ai.system.message): the empty list redacts them all."""
    raw, clean = strands_opt_in("gen_ai_unredacted_attributes=")
    assert {"gen_ai.user.message", "gen_ai.choice", "gen_ai.system.message"} <= _event_names(raw)
    text = span_text(raw)
    assert CONTENT not in text and SECRET not in text
    for s in raw:
        for e in s.events:
            for key in ("content", "message", "tool.result"):
                if key in (e.attributes or {}):
                    assert e.attributes[key] == "[REDACTED]"
    assert not any(s.events for s in clean)


def test_without_the_token_strands_sends_content_and_only_the_exporter_stops_it(strands_opt_in: Any) -> None:
    raw, clean = strands_opt_in("gen_ai_latest_experimental")
    assert CONTENT in span_text(raw)  # the control: the leak is real without the token
    text = span_text(clean)
    assert CONTENT not in text and SECRET not in text and "zebra-content" not in text


# ---- 3. the real OTLP path, in a fresh process ---------------------------------------------------


class Collector:
    def __init__(self) -> None:
        self.bodies: list[tuple[str, bytes]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                collector.bodies.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


SCRIPT = textwrap.dedent(
    """
    import json, os
    from opentelemetry import trace
    from wattson.shared import telemetry
    from strands.telemetry.tracer import get_tracer

    assert telemetry.setup_telemetry("wattson-default") is True
    strands = get_tracer()
    with telemetry.span("wattson.step", **{"wattson.task_id": "t-20261023-abc123", "wattson.wait_s": 2.5}):
        leak = trace.get_tracer("leaky").start_span("leaky")
        leak.set_attribute("gen_ai.input.messages", os.environ["T_CONTENT"])
        leak.set_attribute("note", "password=" + os.environ["T_SECRET"])
        leak.add_event("gen_ai.user.message", {"content": os.environ["T_CONTENT"]})
        leak.set_status(trace.StatusCode.ERROR, os.environ["T_CONTENT"])
        leak.end()
    telemetry.shutdown_telemetry()
    print(json.dumps({
        "opt_in": os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN"),
        "strands_redacts": strands._redaction_enabled,
        "strands_unredacted": sorted(strands._unredacted_exact) + list(strands._unredacted_globs),
    }))
    """
)


def _clean_env(**changes: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.update(changes)
    return env


def test_the_otlp_endpoint_gets_only_clean_spans() -> None:
    collector = Collector()
    try:
        env = _clean_env(
            OTEL_EXPORTER_OTLP_ENDPOINT=collector.url,
            OTEL_SERVICE_NAME="wattson-orchestrator",
            T_CONTENT=CONTENT,
            T_SECRET=SECRET,
        )
        done = subprocess.run(  # noqa: S603 - our own interpreter and script
            [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60, check=False
        )
        assert done.returncode == 0, done.stderr
        facts = json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        collector.close()

    assert facts == {
        "opt_in": telemetry.SEMCONV_OPT_IN,
        "strands_redacts": True,
        "strands_unredacted": [],
    }
    assert collector.bodies and {path for path, _ in collector.bodies} == {"/v1/traces"}
    raw = b"".join(body for _, body in collector.bodies)
    assert CONTENT.encode() not in raw and SECRET.encode() not in raw and b"zebra-content" not in raw

    spans: dict[str, dict[str, Any]] = {}
    services: set[str] = set()
    for _, body in collector.bodies:
        request = ExportTraceServiceRequest.FromString(body)
        for resource_spans in request.resource_spans:
            for kv in resource_spans.resource.attributes:
                if kv.key == "service.name":
                    services.add(kv.value.string_value)
            for scope_spans in resource_spans.scope_spans:
                for s in scope_spans.spans:
                    values = {kv.key: kv.value for kv in s.attributes}
                    spans[s.name] = {
                        "attributes": values,
                        "events": list(s.events),
                        "status": (s.status.code, s.status.message),
                    }
    assert services == {"wattson-orchestrator"}
    assert set(spans) == {"wattson.step", "leaky"}
    step_attrs = spans["wattson.step"]["attributes"]
    assert step_attrs["wattson.task_id"].string_value == "t-20261023-abc123"
    assert step_attrs["wattson.wait_s"].double_value == 2.5
    leaky = spans["leaky"]
    assert set(leaky["attributes"]) == {"note"}
    assert leaky["attributes"]["note"].string_value == "password=[REDACTED:password]"
    assert leaky["events"] == [] and leaky["status"][1] == ""
