"""One task through the whole system with traces on: the CLI's client, the orchestrator, the real
triage and knowledge apps. Checks the contract spans (the Grafana dashboard reads exactly these),
one trace id for the whole task across the waits, and that no content reaches any span."""

from __future__ import annotations

import time
from typing import Any

from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from opentelemetry.trace import SpanKind

from orchestrator.conftest import FAKE_GITHUB_TOKEN, KNOWLEDGE_TOKEN, TOKEN, TRIAGE_TOKEN, WORKSPACE
from wattson.agents.orchestrator.tools import TOOL_NAMES
from wattson.cli.a2a_client import AgentClient, PendingInterrupt, TaskView
from wattson.cli.portforward import Endpoint
from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    RUN_CLAUDE_CODE,
    ApproveResponse,
    ClarifyResponse,
    CollectEvidenceResponse,
    EvidenceItem,
    RunResponse,
    SessionChoice,
    StartTask,
    WorkspaceInfo,
)

from .conftest import MARKER, MODEL_NAME, Capture, System, attrs, named, ours, settle, span_text

REQUEST = (
    f"Investigate why the api pods crash since the last deploy ({MARKER}). My token is {FAKE_GITHUB_TOKEN}."
)
EVIDENCE_MARKER = "evidence-marker-5521"
SUMMARY = "claude-summary-marker-9034: the API_URL env var pointed at the old service."
DOC_TEXT = "raise the memory limit or fix the leak"  # in the stub's crashloop runbook
APPROVE_AFTER_S = 0.35
STATUSES = {
    "triage",
    "awaiting-input",
    "collecting-evidence",
    "proposed",
    "approved",
    "submitted",
    "completed",
    "rejected",
    "failed",
    "interrupted",
    "expired",
}


class Laptop:
    """The CLI's side: its AgentClient, answering each interrupt the way the CLI does."""

    def __init__(self, system: System) -> None:
        self.client = AgentClient(Endpoint(url=system.orchestrator_url), TOKEN)
        self.prompt: str | None = None
        self.task_id: str | None = None

    def start(self) -> TaskView:
        workspace = WorkspaceInfo.model_validate(WORKSPACE)
        with suppress_http_instrumentation():  # the laptop has no OpenTelemetry
            return self.client.start(StartTask(request=REQUEST, workspace=workspace))

    def answer(self, view: TaskView, response: Any) -> TaskView:
        with suppress_http_instrumentation():
            return self.client.answer(view, [(view.interrupts[0].id, response)])

    def response(self, interrupt: PendingInterrupt) -> Any:
        reason = interrupt.reason
        self.task_id = reason.task_id
        if interrupt.name == CLARIFY:
            answers = {q.id: ("kind-dev" if q.field == "cluster" else "web") for q in reason.questions}
            return ClarifyResponse(answers=answers)
        if interrupt.name == COLLECT_EVIDENCE:
            output = f"{EVIDENCE_MARKER} api-7f9 CrashLoopBackOff, env API_TOKEN={FAKE_GITHUB_TOKEN}"
            items = [
                EvidenceItem(
                    request_id=r.id,
                    ok=True,
                    source=f"fake {r.op}",
                    collected_at="2026-10-23T10:00:00Z",
                    output=output,
                )
                for r in reason.plan
            ]
            return CollectEvidenceResponse(items=items)
        if interrupt.name == APPROVE:
            self.prompt = reason.proposal.prompt
            time.sleep(APPROVE_AFTER_S)  # the user reads the prompt: a wait the span must show
            return ApproveResponse(
                decision="approve", prompt_sha256=reason.proposal.prompt_sha256, session=SessionChoice()
            )
        assert interrupt.name == RUN_CLAUDE_CODE
        return RunResponse(state="completed", session_id="sess-42", summary=SUMMARY, exit_code=0)

    def until(self, view: TaskView, stop_at: str | None = None) -> tuple[TaskView, list[str]]:
        """Answer interrupts until the task is over, or until the interrupt `stop_at` is asked."""
        seen: list[str] = []
        while view.state == "input-required":
            interrupt = view.interrupts[0]
            seen.append(interrupt.name)
            if interrupt.name == stop_at:
                self.response(interrupt)
                break
            assert len(seen) < 12, seen
            view = self.answer(view, self.response(interrupt))
        return view, seen


def test_one_task_is_one_trace_with_the_contract_spans(system: System, capture: Capture) -> None:
    capture.clear()
    laptop = Laptop(system)
    view, seen = laptop.until(laptop.start())
    assert view.state == "completed" and view.outcome is not None, view
    assert view.outcome.status == "completed"
    assert seen == [CLARIFY, COLLECT_EVIDENCE, APPROVE, RUN_CLAUDE_CODE]
    task_id = view.outcome.task_id
    everything = settle(capture.clean)
    spans = ours(everything)
    assert {s.name for s in everything if s not in spans} <= {"tools/list", "server/discover"} | {
        f"tools/call {tool}"
        for tool in ("search_documents", "read_document", "get_related_documents", "get_context")
    }

    # One trace: every span of the task (orchestrator, triage, knowledge, five A2A requests).
    trace_id = laptop.client.trace_id
    assert trace_id is not None
    assert {format(s.context.trace_id, "032x") for s in spans} == {trace_id}
    # The MCP client's spans are in it too (mcp puts the trace context in the request's _meta),
    # and the HTTP calls: A2A and the model server (httpx), Grimoire's MCP endpoint (httpx2).
    assert "MCP send tools/call search_documents" in {s.name for s in spans}
    # url.full; http.url when an earlier test fixed the old HTTP names (read once per process).
    client_urls = {
        str(attrs(s).get("url.full") or attrs(s).get("http.url"))
        for s in spans
        if s.kind == SpanKind.CLIENT and s.name == "POST"
    }
    assert {system.triage_url + "/", system.knowledge_url + "/", system.stub.mcp_url} <= client_urls
    servers = {s.name for s in spans if s.instrumentation_scope.name == "opentelemetry.instrumentation.asgi"}
    assert servers == {"POST /"}
    # 5 to the orchestrator, 1 to triage, 2 to knowledge (list_procedures, then find_procedures)
    assert len([s for s in spans if s.name == "POST /"]) == 8

    # wattson.step: one per tool run, with the status after it.
    steps = named(spans, "wattson.step")
    assert {attrs(s)["wattson.step"] for s in steps} == set(TOOL_NAMES)
    for s in steps:
        a = attrs(s)
        assert a["wattson.task_id"] == task_id
        assert a["wattson.status"] in STATUSES
        assert a.get("wattson.workflow") in (None, "devops-investigation")
        assert set(a) <= {
            "wattson.task_id",
            "wattson.step",
            "wattson.workflow",
            "wattson.status",
            "wattson.interrupt",
            "wattson.wait_s",
            "wattson.wait_ms",
        }
    assert (
        attrs(next(s for s in steps if attrs(s)["wattson.step"] == "finish"))["wattson.status"] == "completed"
    )
    resumed = {attrs(s)["wattson.interrupt"]: attrs(s) for s in steps if "wattson.interrupt" in attrs(s)}
    assert set(resumed) == {"clarify", "collect_evidence", "approve", "run_claude_code"}
    assert resumed["approve"]["wattson.step"] == "request_approval"
    assert resumed["run_claude_code"]["wattson.step"] == "run_on_laptop"
    assert all(isinstance(a["wattson.wait_s"], float) and a["wattson.wait_s"] >= 0 for a in resumed.values())
    assert resumed["approve"]["wattson.wait_s"] >= APPROVE_AFTER_S
    # The same wait as an int (ms): Tempo computes quantiles of integer attributes only.
    for a in resumed.values():
        assert isinstance(a["wattson.wait_ms"], int)
        assert abs(a["wattson.wait_ms"] - a["wattson.wait_s"] * 1000) <= 1
    assert resumed["approve"]["wattson.wait_ms"] >= APPROVE_AFTER_S * 1000

    # wattson.task.end: once, with the outcome and the triage decision.
    (end,) = named(spans, "wattson.task.end")
    a = attrs(end)
    assert a["wattson.task_id"] == task_id
    assert a["wattson.outcome"] == "completed"
    assert a["wattson.workflow"] == "devops-investigation"
    assert a["wattson.triage.category"] == "devops-investigation"
    assert a["wattson.triage.method"] == "model"
    assert isinstance(a["wattson.triage.confidence"], float) and a["wattson.triage.confidence"] >= 0.8
    assert isinstance(a["wattson.triage.confidence_pct"], int)
    assert a["wattson.triage.confidence_pct"] == round(a["wattson.triage.confidence"] * 100) >= 80
    assert isinstance(a["wattson.task_duration_s"], float) and a["wattson.task_duration_s"] >= APPROVE_AFTER_S

    # Triage: the two model steps, in the task's trace.
    (classify,) = named(spans, "wattson.model.classify")
    assert attrs(classify) == {
        "wattson.model.name": MODEL_NAME,
        "wattson.model.ok": True,
        "wattson.triage.category": "devops-investigation",
        "wattson.triage.confidence": a["wattson.triage.confidence"],
        "wattson.triage.confidence_pct": a["wattson.triage.confidence_pct"],
        "wattson.triage.method": "model",
    }
    (hints,) = named(spans, "wattson.model.hints")
    assert attrs(hints) == {"wattson.model.name": MODEL_NAME, "wattson.model.ok": True}

    # Knowledge: one span per MCP call, and the retrieval with its counts.
    calls = [attrs(s) for s in named(spans, "wattson.grimoire.call")]
    assert {"search_documents", "read_document"} <= {c["wattson.grimoire.tool"] for c in calls}
    assert all(
        c["wattson.grimoire.ok"] is True and set(c) == {"wattson.grimoire.tool", "wattson.grimoire.ok"}
        for c in calls
    )
    (retrieve,) = named(spans, "wattson.knowledge.retrieve")
    r = attrs(retrieve)
    assert isinstance(r["wattson.knowledge.docs"], int) and r["wattson.knowledge.docs"] >= 1
    assert isinstance(r["wattson.knowledge.warnings"], int)

    # Strands' own spans stay (the agents, the tools), with their content redacted.
    names = {s.name for s in spans}
    assert {
        "invoke_agent wattson-orchestrator",
        "invoke_agent wattson-triage",
        "invoke_agent wattson-knowledge",
        "execute_tool triage",
        "execute_tool find_procedures",
    } <= names

    # The content went through the system...
    assert laptop.prompt is not None
    for text in (MARKER, DOC_TEXT, EVIDENCE_MARKER):
        assert text in laptop.prompt
    assert view.outcome.summary is not None and "claude-summary-marker-9034" in view.outcome.summary
    # ...and reached no span: not exported, and not even made by Strands or the instrumentations.
    prompt_lines = [line.strip() for line in laptop.prompt.splitlines() if len(line.strip()) >= 25]
    assert len(prompt_lines) >= 5
    secrets = [
        REQUEST,
        MARKER,
        laptop.prompt,
        DOC_TEXT,
        EVIDENCE_MARKER,
        SUMMARY,
        "claude-summary-marker-9034",
        FAKE_GITHUB_TOKEN,
        TOKEN,
        TRIAGE_TOKEN,
        KNOWLEDGE_TOKEN,
        *prompt_lines,
    ]
    for found in (span_text(spans), span_text(settle(capture.raw))):
        for text in secrets:
            assert text not in found
        assert "Bearer" not in found
    # Strands made its content attributes and events, as "[REDACTED]"; the exporter dropped them.
    raw = span_text(capture.raw.get_finished_spans())
    assert "[REDACTED]" in raw
    assert "[REDACTED]" not in span_text(spans)
    # Only exception events are left (from a2a-sdk's spans, when its tracing is on), with the type only.
    assert {(e.name, tuple(e.attributes or {})) for s in spans for e in s.events} <= {
        ("exception", ("exception.type",))
    }
    assert not any(
        k.startswith(
            ("gen_ai.input", "gen_ai.output", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result")
        )
        for s in spans
        for k in attrs(s)
    )


def test_expiry_ends_the_task_in_its_trace(system: System, capture: Capture) -> None:
    capture.clear()
    laptop = Laptop(system)
    view, seen = laptop.until(laptop.start(), stop_at=CLARIFY)
    assert seen == [CLARIFY] and laptop.task_id is not None
    deps = system.orchestrator_app.state.deps
    record = deps.store.get_task(laptop.task_id)
    deps.store.save_task(record.model_copy(update={"updated_at": "2000-01-01T00:00:00.000Z"}))
    assert deps.expire() == [laptop.task_id]  # the background job, outside any request

    (end,) = named(settle(capture.clean), "wattson.task.end")
    assert format(end.context.trace_id, "032x") == laptop.client.trace_id
    a = attrs(end)
    assert a["wattson.outcome"] == "expired" and a["wattson.task_id"] == laptop.task_id
    assert a["wattson.workflow"] == "devops-investigation"


def test_a_late_result_ends_the_task_in_its_trace(system: System, capture: Capture) -> None:
    """The A2A task is gone (pod restart): the CLI reports with POST /v1/tasks/{id}/execution and
    the same traceparent."""
    capture.clear()
    laptop = Laptop(system)
    view, seen = laptop.until(laptop.start(), stop_at=RUN_CLAUDE_CODE)
    assert seen[-1] == RUN_CLAUDE_CODE and laptop.task_id is not None
    with suppress_http_instrumentation():
        record = laptop.client.report_execution(
            laptop.task_id, RunResponse(state="failed", summary=SUMMARY, error="exit 1", exit_code=1)
        )
    assert record.status == "failed"

    spans = ours(settle(capture.clean))
    assert {format(s.context.trace_id, "032x") for s in spans} == {laptop.client.trace_id}
    (end,) = named(spans, "wattson.task.end")
    assert attrs(end)["wattson.outcome"] == "failed" and attrs(end)["wattson.task_id"] == laptop.task_id
    assert "POST /v1/tasks/{task_id}/execution" in {s.name for s in spans}
    assert SUMMARY not in span_text(spans)
