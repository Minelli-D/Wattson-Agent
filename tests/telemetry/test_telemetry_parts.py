"""The spans of each part on their own: triage when the model is down or not asked, the warm-up,
knowledge when Grimoire is not connected or a call fails, and the CLI's traceparent header."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry import trace

from orchestrator.conftest import free_port
from wattson.agents.knowledge.app import retrieve
from wattson.agents.knowledge.grimoire import GrimoireClient, Session
from wattson.agents.knowledge.retrieval import Retriever
from wattson.agents.knowledge.settings import Settings as KnowledgeSettings
from wattson.agents.triage.app import warm_up_quietly
from wattson.agents.triage.engine import TriageEngine
from wattson.agents.triage.settings import Settings as TriageSettings
from wattson.cli.a2a_client import AgentClient, TaskView, new_trace_id, traceparent
from wattson.cli.portforward import Endpoint
from wattson.shared.contracts import (
    KnowledgeRequest,
    Overrides,
    RunResponse,
    StartTask,
    TriageRequest,
    WorkspaceInfo,
)

from .conftest import MODEL_NAME, Capture, FakeModel, attrs, named, settle

WORKSPACE = WorkspaceInfo(cwd_name="shop", kube_contexts=["kind-dev"])
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01")


def _engine(url: str) -> TriageEngine:
    return TriageEngine(TriageSettings(token="t" * 40, llama_url=url, model_name=MODEL_NAME, timeout_s=2.0))


async def test_triage_model_down_marks_both_spans_not_ok(capture: Capture) -> None:
    engine = _engine(f"http://127.0.0.1:{free_port()}")  # nobody listens: connection refused
    capture.clear()
    try:
        result = await engine.triage(
            TriageRequest(request="pods crash in namespace web", workspace=WORKSPACE)
        )
    finally:
        await engine.aclose()
    assert result.classification.method == "rules"
    spans = settle(capture.clean)
    (classify,) = named(spans, "wattson.model.classify")
    assert attrs(classify) == {
        "wattson.model.name": MODEL_NAME,
        "wattson.model.ok": False,
        "wattson.triage.category": result.classification.category,
        "wattson.triage.confidence": result.classification.confidence,
        "wattson.triage.confidence_pct": round(result.classification.confidence * 100),
        "wattson.triage.method": "rules",
    }
    (hints,) = named(spans, "wattson.model.hints")
    assert attrs(hints)["wattson.model.ok"] is False
    assert attrs(hints)["error.type"] in {"CancelledError", "ModelUnavailable"}


async def test_triage_override_does_not_ask_the_model_to_classify(
    capture: Capture, fake_model: FakeModel
) -> None:
    engine = _engine(fake_model.url)
    capture.clear()
    try:
        request = TriageRequest(
            request="add a healthz route",
            workspace=WORKSPACE,
            overrides=Overrides(workflow="engineering-task"),
        )
        await engine.triage(request)
    finally:
        await engine.aclose()
    spans = settle(capture.clean)
    (classify,) = named(spans, "wattson.model.classify")
    assert attrs(classify) == {  # no wattson.model.ok: the model was not asked
        "wattson.model.name": MODEL_NAME,
        "wattson.triage.category": "engineering-task",
        "wattson.triage.confidence": 1.0,
        "wattson.triage.confidence_pct": 100,
        "wattson.triage.method": "override",
    }
    (hints,) = named(spans, "wattson.model.hints")
    assert attrs(hints)["wattson.model.ok"] is True


async def test_the_warm_up_makes_no_span(capture: Capture, fake_model: FakeModel) -> None:
    engine = _engine(fake_model.url)
    capture.clear()
    try:
        assert await warm_up_quietly(engine, wait_s=5) is True  # as the triage app starts it
    finally:
        await engine.aclose()
    assert settle(capture.clean) == []
    assert len(fake_model.posts) >= 2  # it did call the model


async def test_knowledge_not_connected(capture: Capture, tmp_path: Path) -> None:
    grimoire = GrimoireClient(
        KnowledgeSettings(token="k" * 40, mcp_url=f"http://127.0.0.1:{free_port()}/mcp", data_dir=tmp_path)
    )
    capture.clear()
    result = await retrieve(
        Retriever(grimoire), KnowledgeRequest(request="pods crash", queries=["crashloop"])
    )
    assert result.docs == [] and len(result.warnings) == 1
    spans = settle(capture.clean)
    assert named(spans, "wattson.grimoire.call") == []  # nothing was sent
    (found,) = named(spans, "wattson.knowledge.retrieve")
    assert attrs(found) == {"wattson.knowledge.docs": 0, "wattson.knowledge.warnings": 1}


class FakeMcp:
    """Stands for the mcp Client inside a Session."""

    def __init__(self, error: bool = False, raises: BaseException | None = None) -> None:
        self.error, self.raises = error, raises

    async def call_tool(self, name: str, arguments: dict[str, Any], read_timeout_seconds: float) -> Any:
        if self.raises is not None:
            raise self.raises

        class Block:
            type = "text"
            text = "the document text, never in a span"

        class Result:
            content = [Block()]
            is_error = self.error

        return Result()


@pytest.mark.parametrize(
    ("client", "ok", "error_type"),
    [
        (FakeMcp(), True, None),
        (FakeMcp(error=True), False, None),
        (FakeMcp(raises=ConnectionError("down: the query was 'crashloop'")), False, "ConnectionError"),
    ],
)
async def test_one_span_per_grimoire_call(
    capture: Capture, client: FakeMcp, ok: bool, error_type: str | None
) -> None:
    session = Session(client, timeout_s=2.0, memo={})  # type: ignore[arg-type]
    capture.clear()
    try:
        await session.call("search_documents", {"query": "crashloop", "limit": 5})
    except ConnectionError:
        assert error_type == "ConnectionError"
    spans = settle(capture.clean)
    (call,) = named(spans, "wattson.grimoire.call")
    expected = {"wattson.grimoire.tool": "search_documents", "wattson.grimoire.ok": ok}
    if error_type:
        expected["error.type"] = error_type
    assert attrs(call) == expected
    assert "crashloop" not in json.dumps([attrs(s) for s in spans]) and not call.events


# ---- the CLI's traceparent ------------------------------------------------------------------------


def _outcome_task(task_id: str = "t-20261023-abc123") -> dict[str, Any]:
    outcome = {"task_id": task_id, "status": "completed"}
    return {
        "kind": "task",
        "id": "a2a-1",
        "contextId": "c-1",
        "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(outcome)}]}],
    }


def test_the_cli_sends_one_trace_id_per_task() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("traceparent")))
        if request.url.path == "/":
            body = json.loads(request.content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": _outcome_task()})
        record = {
            "id": "t-20261023-abc123",
            "created_at": "2026-10-23T10:00:00.000Z",
            "updated_at": "2026-10-23T10:01:00.000Z",
            "status": "completed",
        }
        return httpx.Response(200, json=record)

    client = AgentClient(Endpoint(url="http://agent.test"), "o" * 40, transport=httpx.MockTransport(handler))
    report = RunResponse(state="completed")
    client.report_execution("t-20261023-abc123", report)  # no task started in this run: no header
    client.start(StartTask(request="pods crash", workspace=WORKSPACE))
    first = client.trace_id
    view = TaskView(id="a2a-1", context_id="c-1", state="input-required")
    client.answer(view, [])
    client.answer(view, [])
    client.report_execution("t-20261023-abc123", report)
    client.start(StartTask(request="another task", workspace=WORKSPACE))

    assert seen[0] == ("/v1/tasks/t-20261023-abc123/execution", None)
    headers = [TRACEPARENT.fullmatch(h or "") for _, h in seen[1:]]
    assert all(headers), seen
    trace_ids = [m.group(1) for m in headers if m]
    span_ids = [m.group(2) for m in headers if m]
    assert trace_ids[:4] == [first] * 4  # start, two answers, the late report: one trace
    assert trace_ids[4] == client.trace_id != first  # a new task: a new trace
    assert len(set(span_ids)) == len(span_ids)  # a fresh parent span id each request


def test_trace_ids_are_valid() -> None:
    for _ in range(50):
        trace_id = new_trace_id()
        assert re.fullmatch(r"[0-9a-f]{32}", trace_id) and trace_id.strip("0")
        assert TRACEPARENT.fullmatch(traceparent(trace_id))
    # The header is what OpenTelemetry reads on the agents' side.
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    trace_id = new_trace_id()
    context = TraceContextTextMapPropagator().extract({"traceparent": traceparent(trace_id)})
    span_context = trace.get_current_span(context).get_span_context()
    assert span_context.is_valid and span_context.is_remote and span_context.trace_flags.sampled
    assert format(span_context.trace_id, "032x") == trace_id
