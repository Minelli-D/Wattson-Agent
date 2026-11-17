"""A fake orchestrator for the CLI tests: a real Strands A2AServer whose agent runs the
deterministic PlannerModel over tools that raise the five interrupts with canned reasons, plus fake
admin routes, behind BearerAuth. A `Script` says what it does and records what it received."""

from __future__ import annotations

import hashlib
import secrets
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from a2a.server.tasks import InMemoryTaskStore
from fastapi import HTTPException
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer
from strands.types.tools import ToolContext

from wattson.shared.auth import BearerAuth
from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    GUARD_CONFIRM,
    RUN_CLAUDE_CODE,
    START_TASK_KEY,
    ApproveReason,
    ApproveResponse,
    ClarifyReason,
    ClarifyResponse,
    Classification,
    CollectEvidenceReason,
    CollectEvidenceResponse,
    ComponentHealth,
    EvidenceItem,
    EvidenceRequest,
    EvidenceSummary,
    ExecutionReport,
    GrimoireCallback,
    GrimoireConnectRequest,
    GrimoireConnectView,
    GrimoireStatus,
    GuardConfirmReason,
    GuardConfirmResponse,
    GuardVerdict,
    HealthView,
    Proposal,
    Question,
    RunReason,
    RunResponse,
    SessionChoice,
    SourceRef,
    StartTask,
    Target,
    TaskEvent,
    TaskOutcome,
    TaskRecord,
)
from wattson.shared.planner import PlannerModel, parse_structured, step

TOKEN = secrets.token_hex(24)
FIXTURES = Path(__file__).parent / "fixtures"
PROMPT = (
    "## Objective\nFind why deployment/api in namespace web restarts.\n\n"
    "## Evidence\n- pods: api-7d9f4 is in CrashLoopBackOff\n\n"
    "## Constraints\n- Read-only: change nothing in the cluster.\n\n"
    "## Expected outcome\nA short report: cause, proof, a proposed fix.\n"
)
SOURCE = SourceRef(
    key="infra/k8s/crashloop-runbook",
    title="CrashLoopBackOff runbook",
    folder="infra/k8s",
    tags=["k8s"],
    why="search",
    retrieved_at="2026-10-22T10:00:00+00:00",
    freshness="live",
    excerpt="Check the last state of the container and its memory limit first.",
)
MCP_URL = "https://grimoire.example.test/mcp"
GUARD_QUESTION = "This asks to delete pods. Go on with a read-only investigation?"
BLOCKED_ID = "task-blocked"
BLOCKED_SUMMARY = "The request asks to delete every pod of the namespace."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_questions() -> list[Question]:
    return [
        Question(
            id="q1",
            field="workflow",
            text="What kind of task is this?",
            options=["prompt-optimization", "devops-investigation", "engineering-task"],
        ),
        Question(id="q2", field="namespace", text="Which namespace?"),
    ]


def default_plan() -> list[EvidenceRequest]:
    return [
        EvidenceRequest(id="e1", op="git.status", reason="See what changed in the working tree."),
        EvidenceRequest(id="e2", op="git.log", limit=5, reason="Recent commits may explain the restarts."),
        EvidenceRequest(
            id="e3",
            op="k8s.get",
            cluster="my-cluster",
            namespace="web",
            kind="pods",
            reason="See the pod states.",
        ),
    ]


@dataclass
class Script:
    clarify: bool = True
    questions: list[Question] = field(default_factory=default_questions)
    plan: list[EvidenceRequest] = field(default_factory=default_plan)
    target: Target | None = None  # set: the target of the evidence step (default my-cluster/web)
    sources: list[SourceRef] = field(default_factory=lambda: [SOURCE])
    prompt: str = PROMPT
    missing_info: list[str] = field(default_factory=lambda: ["When did the restarts start?"])
    approve_warnings: list[str] = field(
        default_factory=lambda: ["Grimoire answered slowly; one search was skipped."]
    )
    run_hash: str | None = None  # set: the run interrupt names this hash instead of the approved one
    guard: GuardVerdict | None = None  # the Guard's verdict at the review (ApproveReason.guard)
    guard_confirm: GuardVerdict | None = None  # set: the Guard asks first (GUARD_CONFIRM)
    guard_question: str = GUARD_QUESTION  # its question
    guard_read_only: bool = True  # False: only the injection model read orders in the request
    guard_block: GuardVerdict | None = None  # set: the Guard stops the task at once (status blocked)
    list_blocked: bool = False  # /v1/tasks lists a blocked task too
    received: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    started: list[StartTask] = field(default_factory=list)
    executions: list[tuple[str, ExecutionReport]] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)
    callbacks: list[GrimoireCallback] = field(default_factory=list)
    browser_pages: list[str] = field(default_factory=list)
    connected: bool = False


class Holder:
    script = Script()


def _state(ctx: ToolContext) -> Any:
    return ctx.agent.state


def make_agent(holder: type[Holder]) -> Agent:
    @tool(context=True)
    def start_task(message: str, tool_context: ToolContext) -> str:
        """Start a task from the CLI's StartTask."""
        start = StartTask.model_validate(parse_structured(message)[START_TASK_KEY])
        holder.script.started.append(start)
        _state(tool_context).set("task_id", f"task-{secrets.token_hex(4)}")
        _state(tool_context).set("repo", start.workspace.repo.name if start.workspace.repo else None)
        return step("guard")

    @tool(context=True)
    def guard(tool_context: ToolContext) -> str:
        """The Guard: stop the task, ask the user first, or let it go on."""
        s = holder.script
        if s.guard_block is not None:
            summary = s.guard_block.reasons[0] if s.guard_block.reasons else "Stopped by the Guard."
            return step("finish", {"status": "blocked", "summary": summary})
        if s.guard_confirm is None:
            return step("clarify")
        reason = GuardConfirmReason(
            task_id=_state(tool_context).get("task_id"),
            verdict=s.guard_confirm,
            question=s.guard_question,
            read_only=s.guard_read_only,
        )
        answer = GuardConfirmResponse.model_validate(
            tool_context.interrupt(GUARD_CONFIRM, reason=reason.model_dump(mode="json"))
        )
        s.received[GUARD_CONFIRM].append(answer)
        if not answer.proceed:
            return step("finish", {"status": "blocked", "summary": "You said no to the Guard's question."})
        return step("clarify")

    @tool(context=True)
    def clarify(tool_context: ToolContext) -> str:
        """Ask the user the open questions."""
        s = holder.script
        if not s.clarify:
            return step("collect_evidence")
        reason = ClarifyReason(
            task_id=_state(tool_context).get("task_id"),
            questions=s.questions,
            warnings=["The request is short, so the agent asks first."],
        )
        answer = ClarifyResponse.model_validate(
            tool_context.interrupt(CLARIFY, reason=reason.model_dump(mode="json"))
        )
        s.received[CLARIFY].append(answer)
        if answer.cancel:
            return step("finish", {"status": "rejected", "summary": "Cancelled by the user."})
        return step("collect_evidence")

    @tool(context=True)
    def collect_evidence(tool_context: ToolContext) -> str:
        """Ask the CLI to collect evidence."""
        s = holder.script
        reason = CollectEvidenceReason(
            task_id=_state(tool_context).get("task_id"),
            target=s.target
            or Target(repo=_state(tool_context).get("repo"), cluster="my-cluster", namespace="web"),
            plan=s.plan,
        )
        answer = CollectEvidenceResponse.model_validate(
            tool_context.interrupt(COLLECT_EVIDENCE, reason=reason.model_dump(mode="json"))
        )
        s.received[COLLECT_EVIDENCE].append(answer)
        _state(tool_context).set("evidence", [i.model_dump(mode="json") for i in answer.items])
        return step("request_approval")

    @tool(context=True)
    def request_approval(tool_context: ToolContext) -> str:
        """Ask the user to approve the prompt."""
        s = holder.script
        st = _state(tool_context)
        answers = st.get("answers") or {}
        prompt = s.prompt + ("".join(f"\n- {k} {v}" for k, v in answers.items()) if answers else "")
        items = [EvidenceItem.model_validate(i) for i in st.get("evidence") or []]
        proposal = Proposal(
            prompt=prompt,
            prompt_sha256=sha(prompt),
            template="devops-investigation",
            template_version="1",
            sources=s.sources,
            evidence=[
                EvidenceSummary(
                    request_id=i.request_id,
                    source=i.source,
                    collected_at=i.collected_at,
                    freshness="live",
                    ok=i.ok,
                    bytes=len(i.output),
                    redactions=i.redactions,
                    error=i.error or i.refused,
                )
                for i in items
            ],
            constraints=["Read-only: change nothing in the cluster."],
            missing_info=[] if answers else s.missing_info,
            redactions=0,
        )
        reason = ApproveReason(
            task_id=st.get("task_id"),
            workflow="devops-investigation",
            classification=Classification(
                category="devops-investigation", confidence=0.91, method="model", model="qwen2.5-1.5b"
            ),
            target=Target(
                repo=st.get("repo"), cluster="my-cluster", namespace="web", workload="deployment/api"
            ),
            proposal=proposal,
            warnings=s.approve_warnings,
            guard=s.guard,
        )
        answer = ApproveResponse.model_validate(
            tool_context.interrupt(APPROVE, reason=reason.model_dump(mode="json"))
        )
        s.received[APPROVE].append(answer)
        if answer.decision == "reject":
            return step("finish", {"status": "rejected", "summary": answer.reason or "Rejected."})
        if answer.decision == "answer":
            st.set("answers", {**answers, **answer.answers})
            return step("request_approval")
        st.set("approved_sha", answer.prompt_sha256)
        st.set("session", answer.session.model_dump(mode="json") if answer.session else None)
        return step("run_on_laptop")

    @tool(context=True)
    def run_on_laptop(tool_context: ToolContext) -> str:
        """Ask the CLI to run Claude Code with the approved prompt."""
        s = holder.script
        st = _state(tool_context)
        reason = RunReason(
            task_id=st.get("task_id"),
            prompt_sha256=s.run_hash or st.get("approved_sha"),
            session=SessionChoice.model_validate(st.get("session")),
        )
        answer = RunResponse.model_validate(
            tool_context.interrupt(RUN_CLAUDE_CODE, reason=reason.model_dump(mode="json"))
        )
        s.received[RUN_CLAUDE_CODE].append(answer)
        return step("finish", {"status": answer.state, "summary": answer.summary or answer.error or ""})

    @tool(context=True)
    def finish(tool_context: ToolContext, status: str = "completed", summary: str = "") -> str:
        """End the task."""
        outcome = TaskOutcome(
            task_id=_state(tool_context).get("task_id"),
            status=status,  # type: ignore[arg-type]
            workflow="devops-investigation",
            summary=summary or None,
            source_keys=[SOURCE.key],
            warnings=["Fake orchestrator: nothing real happened."],
        )
        return step(None, output=outcome.model_dump(mode="json"))

    return Agent(
        name="fake-orchestrator",
        description="A fake orchestrator for the CLI tests.",
        model=PlannerModel("start_task"),
        tools=[start_task, guard, clarify, collect_evidence, request_approval, run_on_laptop, finish],
        callback_handler=None,
    )


def record(task_id: str, status: str = "completed") -> TaskRecord:
    if task_id == BLOCKED_ID:
        status = "blocked"
    return TaskRecord(
        id=task_id,
        created_at="2026-10-22T10:00:00+00:00",
        updated_at="2026-10-22T10:05:00+00:00",
        status=status,  # type: ignore[arg-type]
        workflow="devops-investigation",
        category="devops-investigation",
        confidence=0.91,
        method="model",
        repo="demo",
        cluster="my-cluster",
        namespace="web",
        source_keys=[SOURCE.key],
        error_summary=BLOCKED_SUMMARY if status == "blocked" else None,
    )


def _browser(url: str, holder: type[Holder]) -> None:
    """The user's browser coming back from the Grimoire login."""
    time.sleep(0.3)
    resp = httpx.get(url, params={"code": "c0de", "state": "st4te"}, timeout=5)
    holder.script.browser_pages.append(resp.text)


def build_app(holder: type[Holder], store: InMemoryTaskStore) -> Any:
    server = A2AServer(
        agent_factory=lambda _ctx: make_agent(holder),
        host="127.0.0.1",
        port=9000,
        task_store=store,
        enable_a2a_compliant_streaming=True,
    )
    app = server.to_fastapi_app()

    def grimoire_status() -> GrimoireStatus:
        tools = ["search_documents", "read_document"] if holder.script.connected else []
        return GrimoireStatus(connected=holder.script.connected, mcp_url=MCP_URL, tools=tools)

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        view = HealthView(
            ok=True,
            version="0.1.0",
            triage=ComponentHealth(ok=True, detail="model qwen2.5-1.5b"),
            knowledge=ComponentHealth(ok=True),
            grimoire=grimoire_status(),
            history={"tasks": 3, "retention_days": 90},
        )
        return view.model_dump(mode="json")

    @app.get("/v1/tasks")
    def tasks(limit: int = 20) -> list[dict[str, Any]]:
        records = [record("task-aaa"), record("task-bbb", "failed")]
        if holder.script.list_blocked:
            records.append(record(BLOCKED_ID))
        return [r.model_dump(mode="json") for r in records][:limit]

    @app.get("/v1/tasks/{task_id}")
    def task(task_id: str) -> dict[str, Any]:
        if task_id == "missing":
            raise HTTPException(status_code=404, detail="no such task")
        return record(task_id).model_dump(mode="json")

    @app.get("/v1/tasks/{task_id}/events")
    def events(task_id: str) -> list[dict[str, Any]]:
        return [
            TaskEvent(task_id=task_id, at="2026-10-22T10:00:00+00:00", to_status="triage").model_dump(
                mode="json"
            ),
            TaskEvent(
                task_id=task_id,
                at="2026-10-22T10:05:00+00:00",
                from_status="submitted",
                to_status="completed",
                detail="Claude Code finished.",
            ).model_dump(mode="json"),
        ]

    @app.post("/v1/tasks/{task_id}/execution")
    def execution(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        report = ExecutionReport.model_validate(body)
        holder.script.executions.append((task_id, report))
        return record(task_id, report.result.state).model_dump(mode="json")

    @app.get("/v1/grimoire/status")
    def g_status() -> dict[str, Any]:
        return grimoire_status().model_dump(mode="json")

    @app.post("/v1/grimoire/connect")
    def g_connect(body: dict[str, Any]) -> dict[str, Any]:
        req = GrimoireConnectRequest.model_validate(body)
        holder.script.redirects.append(req.redirect_uri)
        threading.Thread(target=_browser, args=(req.redirect_uri, holder), daemon=True).start()
        return GrimoireConnectView(
            authorize_url="https://grimoire.example.test/authorize?client_id=x"
        ).model_dump()

    @app.post("/v1/grimoire/callback")
    def g_callback(body: dict[str, Any]) -> dict[str, Any]:
        cb = GrimoireCallback.model_validate(body)
        holder.script.callbacks.append(cb)
        holder.script.connected = cb.code == "c0de" and cb.state == "st4te"
        return grimoire_status().model_dump(mode="json")

    @app.post("/v1/grimoire/disconnect")
    def g_disconnect() -> dict[str, Any]:
        holder.script.connected = False
        return grimoire_status().model_dump(mode="json")

    return BearerAuth(app, TOKEN)


class FakeOrchestrator:
    def __init__(self) -> None:
        self.holder = Holder
        self.store = InMemoryTaskStore()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(build_app(self.holder, self.store), log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)

    def start(self) -> FakeOrchestrator:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the fake orchestrator did not start")
            time.sleep(0.05)
        return self

    def reset(self) -> Script:
        self.holder.script = Script()
        return self.holder.script

    def drop_tasks(self) -> None:
        """Forget every A2A task, like a pod restart."""
        self.store.tasks.clear()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
