"""Test helpers: fake triage and knowledge agents (real Strands A2A servers driven by the planner,
with canned answers), a uvicorn thread runner, and JSON-RPC helpers for the orchestrator.

The fake knowledge agent lists the Wattson procedures in `counters.procedures` (none by default:
the flow without procedures). The fake triage picks one with the real decision rules
(wattson.agents.triage.procedures), as if the model had answered `counters.model_pick`."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer

from wattson.agents.orchestrator.app import create_app
from wattson.agents.orchestrator.settings import Settings
from wattson.agents.triage import procedures as proc
from wattson.shared.auth import BearerAuth
from wattson.shared.contracts import (
    FIND_PROCEDURES_KEY,
    LIST_PROCEDURES_KEY,
    START_TASK_KEY,
    TRIAGE_KEY,
    Classification,
    DecisionHints,
    KnowledgeRequest,
    KnowledgeResult,
    ProcedureList,
    ProcedureListRequest,
    ProcedureOption,
    RetrievedDoc,
    SourceRef,
    TriageRequest,
    TriageResult,
)
from wattson.shared.planner import PlannerModel, parse_structured, step

TOKEN = "o" * 40
TRIAGE_TOKEN = "t" * 40
KNOWLEDGE_TOKEN = "k" * 40
FAKE_GITHUB_TOKEN = "ghp_" + "Zx9" * 12  # 36 chars after the prefix: looks real to the redactor
DOC_SECRET = "s3cr3t-Hunter2-Value"  # noqa: S105 - a fake secret the redactor must catch
RUNBOOK_KEY = "infra/k8s/crashloop-runbook"
PROCEDURE_STEP = "Read the Last State of the container first."


def procedure_option(name: str, when: str, *signals: str) -> ProcedureOption:
    return ProcedureOption(
        key=f"wattson/{name}.md",
        name=name,
        when_to_use=when,
        workflow="devops-investigation",
        signals=list(signals),
    )


PROCEDURE_OPTIONS = [
    procedure_option(
        "k8s-pod-crashloop",
        "A pod starts and then dies again and again: CrashLoopBackOff, OOMKilled.",
        "crashloopbackoff",
        "oomkilled",
        "crash",
        "restart",
    ),
    procedure_option(
        "k8s-tls-ingress",
        "An app cannot be reached: certificate errors, or the Ingress answers 502.",
        "tls",
        "certificate",
        "ingress",
    ),
    procedure_option(
        "k8s-storage",
        "A volume or disk problem: a PersistentVolumeClaim stays Pending, a disk is full.",
        "pvc",
        "volume",
        "disk",
    ),
]


# ---- running ASGI apps in threads ------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serve(app: Any, port: int | None = None) -> Iterator[str]:
    port = port or free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---- fake triage and knowledge agents --------------------------------------------------------


def triage_pick(req: TriageRequest, counters: FakeCounters) -> Any:
    """The real decision rules, with `counters.model_pick` as the model's answer."""
    if not req.procedures:
        return None
    chosen, _ = proc.resolve_override(req.overrides.procedure, req.procedures)
    if chosen is not None:
        return chosen
    return proc.decide(req.request, req.procedures, counters.model_pick, latency_ms=7).pick


def triage_answer(req: TriageRequest, counters: FakeCounters | None = None) -> TriageResult:
    """Canned decisions by keyword, like a tiny rules classifier."""
    text = req.request.lower()
    hints = DecisionHints(search_queries=["crashloop runbook"], tags=["k8s"])
    if "unclear" in text:
        category, confidence = "unclear", 0.3
    elif "prompt" in text:
        category, confidence = "prompt-optimization", 0.88
        hints = DecisionHints(
            search_queries=["prompt style"], missing_info=["Which audience is the prompt for?"]
        )
    elif "pods" in text or "crash" in text:
        category, confidence = "devops-investigation", 0.91
        if "kind-dev" in text:
            hints = hints.model_copy(
                update={"cluster": "kind-dev", "namespace": "web", "workload": "deployment/api"}
            )
        if "faraway" in text:
            hints = hints.model_copy(update={"cluster": "faraway-cluster"})
    else:
        category, confidence = "engineering-task", 0.7
    return TriageResult(
        classification=Classification(
            category=category,
            confidence=confidence,
            method="model",
            model="fake",
            scores={category: confidence},
        ),
        hints=hints,
        procedure=triage_pick(req, counters) if counters is not None else None,
    )


def knowledge_answer(req: KnowledgeRequest) -> KnowledgeResult:
    ref = SourceRef(
        key=RUNBOOK_KEY,
        title="CrashLoopBackOff runbook",
        folder="infra/k8s",
        tags=["k8s"],
        why="related" if req.procedure_key else "search",
        retrieved_at="2026-10-22T10:00:00Z",
        freshness="live",
        excerpt="Look at the events first.",
    )
    text = f"1. kubectl describe the pod.\n2. Read the previous logs.\nThe old admin password={DOC_SECRET}\n"
    docs = [RetrievedDoc(ref=ref, text=text)]
    if req.procedure_key:
        name = proc.name_of(req.procedure_key)
        procedure = SourceRef(
            key=req.procedure_key,
            title=name,
            folder="wattson",
            tags=["wattson"],
            why="procedure",
            retrieved_at="2026-10-22T10:00:00Z",
            freshness="live",
        )
        body = f"## When to use\n\nFor {name}.\n\n## Steps for Claude Code\n\n1. {PROCEDURE_STEP}\n"
        docs.insert(0, RetrievedDoc(ref=procedure, text=body))
    return KnowledgeResult(docs=docs)


class FakeCounters:
    def __init__(self) -> None:
        self.triage_calls = 0
        self.knowledge_calls = 0
        self.list_calls = 0
        self.last_triage: TriageRequest | None = None
        self.last_knowledge: KnowledgeRequest | None = None
        self.grimoire_calls: list[tuple[str, Any]] = []
        self.procedures: list[ProcedureOption] = []  # what the fake knowledge agent lists
        self.model_pick: str | None = "none"  # what the fake triage's "model" answers; None: failed


def _answer_agent(name: str, routes: dict[str, tuple[Any, Any]]) -> A2AServer:
    """routes: DataPart key -> (request model, answer function)."""

    @tool
    def answer_request(message: str) -> str:
        """Answer one request with a canned result."""
        data = parse_structured(message)
        key = next((k for k in routes if k in data), None)
        if key is None:
            return step(output={"error": f"no {' or '.join(routes)} DataPart"})
        model, answer = routes[key]
        return step(output=answer(model.model_validate(data[key])).model_dump(mode="json"))

    def factory(context_id: str) -> Agent:
        return Agent(
            name=name,
            description=f"fake {name}",
            model=PlannerModel("answer_request"),
            tools=[answer_request],
            callback_handler=None,
        )

    return A2AServer(agent_factory=factory, enable_a2a_compliant_streaming=True)


def fake_triage_app(counters: FakeCounters) -> Any:
    def answer(request: TriageRequest) -> TriageResult:
        counters.triage_calls += 1
        counters.last_triage = request
        return triage_answer(request, counters)

    a2a = _answer_agent("fake-triage", {TRIAGE_KEY: (TriageRequest, answer)})

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "detail": "model fake-1.5b loaded"})

    app = Starlette(routes=[Route("/v1/health", health), Mount("/", app=a2a.to_starlette_app())])
    return BearerAuth(app, TRIAGE_TOKEN, open_paths={"/healthz"})


def fake_knowledge_app(counters: FakeCounters) -> Any:
    def find(request: KnowledgeRequest) -> KnowledgeResult:
        counters.knowledge_calls += 1
        counters.last_knowledge = request
        return knowledge_answer(request)

    def listing(request: ProcedureListRequest) -> ProcedureList:
        counters.list_calls += 1
        return ProcedureList(procedures=list(counters.procedures))

    a2a = _answer_agent(
        "fake-knowledge",
        {FIND_PROCEDURES_KEY: (KnowledgeRequest, find), LIST_PROCEDURES_KEY: (ProcedureListRequest, listing)},
    )
    status = {
        "connected": True,
        "mcp_url": "https://grimoire.example.test/mcp",
        "tools": ["search_documents"],
    }

    async def grimoire_status(request: Request) -> JSONResponse:
        counters.grimoire_calls.append(("status", None))
        return JSONResponse(status)

    async def grimoire_connect(request: Request) -> JSONResponse:
        counters.grimoire_calls.append(("connect", await request.json()))
        return JSONResponse({"authorize_url": "https://grimoire.example.test/authorize?state=abc"})

    async def grimoire_callback(request: Request) -> JSONResponse:
        counters.grimoire_calls.append(("callback", await request.json()))
        return JSONResponse(status)

    async def grimoire_disconnect(request: Request) -> JSONResponse:
        counters.grimoire_calls.append(("disconnect", None))
        return JSONResponse({**status, "connected": False, "tools": []})

    app = Starlette(
        routes=[
            Route("/v1/grimoire/status", grimoire_status, methods=["GET"]),
            Route("/v1/grimoire/connect", grimoire_connect, methods=["POST"]),
            Route("/v1/grimoire/callback", grimoire_callback, methods=["POST"]),
            Route("/v1/grimoire/disconnect", grimoire_disconnect, methods=["POST"]),
            Mount("/", app=a2a.to_starlette_app()),
        ]
    )
    return BearerAuth(app, KNOWLEDGE_TOKEN, open_paths={"/healthz"})


@pytest.fixture(scope="session")
def counters() -> FakeCounters:
    return FakeCounters()


@pytest.fixture
def with_procedures(counters: FakeCounters) -> Iterator[FakeCounters]:
    """The fake knowledge agent lists PROCEDURE_OPTIONS for one test."""
    counters.procedures = list(PROCEDURE_OPTIONS)
    counters.model_pick = "none"
    try:
        yield counters
    finally:
        counters.procedures = []
        counters.model_pick = "none"


@pytest.fixture(scope="session")
def triage_url(counters: FakeCounters) -> Iterator[str]:
    with serve(fake_triage_app(counters)) as url:
        yield url


@pytest.fixture(scope="session")
def knowledge_url(counters: FakeCounters) -> Iterator[str]:
    with serve(fake_knowledge_app(counters)) as url:
        yield url


@pytest.fixture
def dead_url() -> str:
    """A port nobody listens on: connection refused."""
    return f"http://127.0.0.1:{free_port()}"


def make_settings(data_dir: Path, triage: str, knowledge: str, **changes: Any) -> Settings:
    values: dict[str, Any] = {
        "token": TOKEN,
        "triage_url": triage,
        "triage_token": TRIAGE_TOKEN,
        "knowledge_url": knowledge,
        "knowledge_token": KNOWLEDGE_TOKEN,
        "data_dir": data_dir,
        "triage_timeout_s": 10.0,
        "knowledge_timeout_s": 10.0,
    }
    values.update(changes)
    return Settings(**values)


class Orchestrator:
    """A running orchestrator and a client that speaks raw JSON-RPC to it."""

    def __init__(self, url: str, app: Any) -> None:
        self.url = url
        self.app = app
        self.deps = app.state.deps
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)

    def rpc(self, message: dict[str, Any]) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {"message": message},
        }
        response = self.http.post("/", json=body)
        response.raise_for_status()
        data = response.json()
        assert "result" in data, data
        return data["result"]

    def start(
        self, request: str, workspace: dict[str, Any] | None = None, **overrides: Any
    ) -> dict[str, Any]:
        start = {"request": request, "workspace": workspace or WORKSPACE, "overrides": overrides}
        return self.rpc(
            {
                "role": "user",
                "kind": "message",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "data", "data": {START_TASK_KEY: start}}],
            }
        )

    def answer(self, task: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        interrupt = interrupt_of(task)
        return self.rpc(
            {
                "role": "user",
                "kind": "message",
                "messageId": uuid.uuid4().hex,
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "interruptResponse": {
                                "interruptId": interrupt["interruptId"],
                                "response": response,
                            }
                        },
                    }
                ],
            }
        )


WORKSPACE: dict[str, Any] = {
    "repo": {
        "name": "shop-api",
        "root_display": "~/src/shop-api",
        "branch": "main",
        "head": "abc1234",
        "remote": "git@github.com:me/shop-api.git",
        "dirty_files": 2,
    },
    "cwd_name": "shop-api",
    "kube_contexts": ["kind-dev", "my-cluster"],
    "current_kube_context": "kind-dev",
    "reader_contexts": ["kind-dev"],
}


def interrupt_of(task: dict[str, Any]) -> dict[str, Any]:
    assert task["status"]["state"] == "input-required", task["status"]
    parts = task["status"]["message"]["parts"]
    data = [p["data"] for p in parts if p["kind"] == "data"][0]
    assert len(data["interrupts"]) == 1
    return data["interrupts"][0]


def outcome_of(task: dict[str, Any]) -> dict[str, Any]:
    assert task["status"]["state"] == "completed", task["status"]
    text = "".join(p.get("text", "") for a in task.get("artifacts", []) for p in a["parts"])
    return json.loads(text)


@pytest.fixture
def orchestrator_factory(tmp_path: Path) -> Iterator[Any]:
    stack: list[Any] = []

    def make(triage: str, knowledge: str, **changes: Any) -> Orchestrator:
        settings = make_settings(tmp_path / f"data{len(stack)}", triage, knowledge, **changes)
        app = create_app(settings, background=False)
        cm = serve(app)
        url = cm.__enter__()
        stack.append(cm)
        return Orchestrator(url, app)

    yield make
    for cm in reversed(stack):
        cm.__exit__(None, None, None)


@pytest.fixture
def orch(orchestrator_factory: Any, triage_url: str, knowledge_url: str) -> Orchestrator:
    return orchestrator_factory(triage_url, knowledge_url)


def run(coro: Any) -> Any:
    return asyncio.run(coro)
