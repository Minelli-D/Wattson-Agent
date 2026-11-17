"""Telemetry tests: traces on, into an in-memory exporter, for the whole session of this folder.

The whole system runs in this process: the orchestrator, the real triage app (its engine against a
fake OpenAI-like model server), the real knowledge app (logged in to the Grimoire stub). Each is
served over HTTP, so the trace context really travels in headers. The CLI's own client drives the
task, with httpx instrumentation suppressed around it: the laptop has no OpenTelemetry.

At the end the global OpenTelemetry state is put back, so later test folders run with traces off."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

FIXTURES = str(Path(__file__).resolve().parents[1] / "fixtures")
if FIXTURES not in sys.path:
    sys.path.insert(0, FIXTURES)

from grimoire_stub import GrimoireStub, start_grimoire_stub  # noqa: E402

from orchestrator.conftest import KNOWLEDGE_TOKEN, TRIAGE_TOKEN, make_settings, serve  # noqa: E402
from wattson.agents.knowledge.app import KnowledgeService  # noqa: E402
from wattson.agents.knowledge.app import create_app as knowledge_app  # noqa: E402
from wattson.agents.knowledge.settings import Settings as KnowledgeSettings  # noqa: E402
from wattson.agents.orchestrator.app import create_app as orchestrator_app  # noqa: E402
from wattson.agents.triage.app import create_app as triage_app  # noqa: E402
from wattson.agents.triage.engine import TriageEngine  # noqa: E402
from wattson.agents.triage.settings import Settings as TriageSettings  # noqa: E402
from wattson.shared import telemetry  # noqa: E402

MODEL_NAME = "fake-granite"
MARKER = "zebra-marker-7781"  # in the request; the hint model also echoes it (grep)
REDIRECT = "http://127.0.0.1:47615/callback"


# ---- the session's telemetry -----------------------------------------------------------------


def _forget_global_provider() -> None:
    """OpenTelemetry sets the global provider once per process; tests put it back (private)."""
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


@dataclass
class Capture:
    clean: InMemorySpanExporter  # what leaves the process: after SafeSpanExporter
    raw: InMemorySpanExporter  # what Strands and the instrumentations made (only tests see this)

    def clear(self) -> None:
        self.clean.clear()
        self.raw.clear()


@pytest.fixture(scope="session")
def capture() -> Iterator[Capture]:
    saved = os.environ.get(telemetry.SEMCONV_ENV)
    clean, raw = InMemorySpanExporter(), InMemorySpanExporter()
    assert telemetry.setup_telemetry("wattson-test", exporter=clean) is True
    assert telemetry.setup_telemetry("again", exporter=InMemorySpanExporter()) is True  # once only
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(raw))
    yield Capture(clean, raw)
    telemetry.shutdown_telemetry()
    _forget_global_provider()
    if saved is None:
        os.environ.pop(telemetry.SEMCONV_ENV, None)
    else:
        os.environ[telemetry.SEMCONV_ENV] = saved
    from strands.telemetry import tracer as strands_tracer

    strands_tracer._tracer_instance = None


def settle(memory: InMemorySpanExporter, quiet_s: float = 0.4, timeout_s: float = 10.0) -> list[ReadableSpan]:
    """The spans, once no new one came for quiet_s (a server span ends just after its answer)."""
    deadline = time.monotonic() + timeout_s
    count, since = -1, time.monotonic()
    while time.monotonic() < deadline:
        now = len(memory.get_finished_spans())
        if now != count:
            count, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            break
        time.sleep(0.05)
    return list(memory.get_finished_spans())


def span_text(spans: list[ReadableSpan]) -> str:
    """Everything a span carries to the exporter, as one text to search."""
    parts: list[str] = []
    for s in spans:
        parts += [s.name, json.dumps(dict(s.attributes or {}), default=str), str(s.status.description)]
        parts += [json.dumps(dict(s.resource.attributes), default=str)]
        for e in s.events:
            parts += [e.name, json.dumps(dict(e.attributes or {}), default=str)]
        for link in s.links:
            parts += [json.dumps(dict(link.attributes or {}), default=str)]
    return "\n".join(parts)


def ours(spans: list[ReadableSpan]) -> list[ReadableSpan]:
    """Without the Grimoire stub's own spans: it runs in this process here (its MCP server makes
    server spans, some outside any trace), while the real Grimoire is another service."""
    return [
        s
        for s in spans
        if not (s.instrumentation_scope.name == "mcp-python-sdk" and s.kind == SpanKind.SERVER)
    ]


def named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def attrs(s: ReadableSpan) -> dict[str, Any]:
    return dict(s.attributes or {})


# ---- a fake OpenAI-like model server -----------------------------------------------------------


HINTS = {
    "search_queries": ["crashloop"],
    "tags": ["k8s"],
    "cluster": None,
    "namespace": None,
    "workload": None,
    "files": [],
    "grep": [MARKER],
    "missing_info": [],
}


@dataclass
class FakeModel:
    url: str = ""
    posts: list[dict[str, Any]] = field(default_factory=list)


def _model_handler(fake: FakeModel) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._send({"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            fake.posts.append(body)
            if body.get("logprobs"):
                tops = [{"token": t, "logprob": lp} for t, lp in (("B", -0.02), ("C", -4.0), ("A", -5.0))]
                choice = {
                    "message": {"role": "assistant", "content": "B"},
                    "logprobs": {"content": [{"token": "B", "logprob": -0.02, "top_logprobs": tops}]},
                }
            else:
                choice = {"message": {"role": "assistant", "content": json.dumps(HINTS)}, "logprobs": None}
            self._send({"choices": [{"index": 0, "finish_reason": "stop", **choice}]})

    return Handler


@pytest.fixture(scope="session")
def fake_model() -> Iterator[FakeModel]:
    fake = FakeModel()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _model_handler(fake))
    server.daemon_threads = True
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()


# ---- the whole system ----------------------------------------------------------------------------


@dataclass
class System:
    orchestrator_url: str
    orchestrator_app: Any
    triage_url: str
    knowledge_url: str
    stub: GrimoireStub
    model: FakeModel


def _login(knowledge_url: str) -> None:
    """The Grimoire login, as the CLI and the browser do it."""
    with httpx.Client(base_url=knowledge_url, headers={"Authorization": f"Bearer {KNOWLEDGE_TOKEN}"}) as http:
        started = http.post("/v1/grimoire/connect", json={"redirect_uri": REDIRECT}, timeout=30)
        assert started.status_code == 200, started.text
        answer = httpx.get(started.json()["authorize_url"], follow_redirects=False, timeout=10)
        query = parse_qs(urlparse(answer.headers["location"]).query)
        done = http.post(
            "/v1/grimoire/callback", json={"code": query["code"][0], "state": query["state"][0]}, timeout=30
        )
        assert done.status_code == 200 and done.json()["connected"] is True, done.text


@pytest.fixture(scope="session")
def system(
    capture: Capture, fake_model: FakeModel, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[System]:
    root = tmp_path_factory.mktemp("telemetry")
    stub = start_grimoire_stub()
    triage_settings = TriageSettings(
        token=TRIAGE_TOKEN, llama_url=fake_model.url, model_name=MODEL_NAME, timeout_s=5.0
    )
    engine = TriageEngine(triage_settings)
    knowledge_settings = KnowledgeSettings(
        token=KNOWLEDGE_TOKEN,
        mcp_url=stub.mcp_url,
        data_dir=root / "knowledge",
        timeout_s=5.0,
        retrieval_budget_s=20.0,
        connect_wait_s=30.0,
    )
    service = KnowledgeService(knowledge_settings)
    with (
        serve(triage_app(triage_settings, engine)) as triage_url,
        serve(knowledge_app(knowledge_settings, service)) as knowledge_url,
    ):
        with suppress_http_instrumentation():
            _login(knowledge_url)
        app = orchestrator_app(
            make_settings(root / "orchestrator", triage_url, knowledge_url), background=False
        )
        with serve(app) as orchestrator_url:
            deadline = time.monotonic() + 10  # the triage warm-up: two model calls, done before tests
            while len(fake_model.posts) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            yield System(orchestrator_url, app, triage_url, knowledge_url, stub, fake_model)
    stub.close()
