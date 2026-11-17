"""Calls to the other agents: a DataPart over A2A, the artifact parsed, errors made plain."""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wattson.agents.orchestrator.remote import RemoteError, Remotes
from wattson.shared.contracts import KnowledgeRequest, TriageRequest, WorkspaceInfo

from .conftest import KNOWLEDGE_TOKEN, RUNBOOK_KEY, TRIAGE_TOKEN, FakeCounters, serve


def remotes(
    triage: str, knowledge: str, *, triage_token: str = TRIAGE_TOKEN, timeout: float = 10.0
) -> Remotes:
    return Remotes(
        triage_url=triage,
        triage_token=triage_token,
        knowledge_url=knowledge,
        knowledge_token=KNOWLEDGE_TOKEN,
        triage_timeout_s=timeout,
        knowledge_timeout_s=timeout,
    )


REQUEST = TriageRequest(
    request="pods crash on kind-dev", workspace=WorkspaceInfo(cwd_name="x", kube_contexts=["kind-dev"])
)


async def test_triage_round_trip(triage_url: str, knowledge_url: str, counters: FakeCounters) -> None:
    before = counters.triage_calls
    result = await remotes(triage_url, knowledge_url).triage(REQUEST)
    assert result.classification.category == "devops-investigation"
    assert result.hints.cluster == "kind-dev"
    assert counters.triage_calls == before + 1
    assert counters.last_triage == REQUEST  # the DataPart arrived intact


async def test_knowledge_round_trip(triage_url: str, knowledge_url: str) -> None:
    result = await remotes(triage_url, knowledge_url).find_procedures(
        KnowledgeRequest(request="pods crash", queries=["crashloop"])
    )
    assert [d.ref.key for d in result.docs] == [RUNBOOK_KEY]


async def test_wrong_token(triage_url: str, knowledge_url: str) -> None:
    with pytest.raises(RemoteError, match="HTTP 401"):
        await remotes(triage_url, knowledge_url, triage_token="x" * 40).triage(REQUEST)


async def test_unreachable_is_retried_once(dead_url: str) -> None:
    started = time.monotonic()
    with pytest.raises(RemoteError, match="not reachable"):
        await remotes(dead_url, dead_url).triage(REQUEST)
    assert time.monotonic() - started >= 0.5  # the pause before the one retry


async def test_timeout_and_bad_answers() -> None:
    async def slow(request: Request) -> JSONResponse:
        await asyncio.sleep(3)
        return JSONResponse({})

    async def nonsense(request: Request) -> JSONResponse:
        body = await request.json()
        task = {
            "kind": "task",
            "id": "t1",
            "contextId": "c1",
            "status": {"state": "completed"},
            "artifacts": [{"artifactId": "a1", "parts": [{"kind": "text", "text": '{"error": "no"}'}]}],
        }
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": task})

    app = Starlette(
        routes=[Route("/slow/", slow, methods=["POST"]), Route("/bad/", nonsense, methods=["POST"])]
    )
    with serve(app) as url:
        with pytest.raises(RemoteError, match="did not answer within"):
            await remotes(f"{url}/slow", url, timeout=1).triage(REQUEST)
        with pytest.raises(RemoteError, match="not a valid TriageResult"):
            await remotes(f"{url}/bad", url).triage(REQUEST)


async def test_health_and_grimoire(triage_url: str, knowledge_url: str, dead_url: str) -> None:
    r = remotes(triage_url, knowledge_url)
    assert (await r.triage_health()).ok
    assert (await r.grimoire_status()).connected
    down = await remotes(dead_url, dead_url).triage_health()
    assert not down.ok and "triage" in (down.detail or "")
