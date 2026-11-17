"""The triage agent over A2A: the real A2AServer app, a JSON-RPC message/send with a DataPart."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from wattson.agents.triage.app import create_app
from wattson.agents.triage.engine import TriageEngine
from wattson.shared.contracts import TRIAGE_KEY, ComponentHealth, TriageRequest, TriageResult, WorkspaceInfo

TOKEN = "t" * 40


@pytest.fixture
async def client(fake_llama: Any, make_settings: Any) -> Any:
    settings = make_settings(fake_llama.url, token=TOKEN, public_url="http://wattson-triage:9001/")
    engine = TriageEngine(settings)
    app = create_app(settings, engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c
    await engine.aclose()


def _send(data: dict[str, Any]) -> dict[str, Any]:
    message = {
        "role": "user",
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "parts": [{"kind": "data", "data": data}],
    }
    return {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}}


def _artifact_text(task: dict[str, Any]) -> str:
    parts = [p for a in task.get("artifacts", []) for p in a["parts"]]
    return "".join(p.get("text", "") for p in parts if p["kind"] == "text")


AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def test_a2a_round_trip(client: httpx.AsyncClient, workspace: WorkspaceInfo) -> None:
    request = TriageRequest(request="pods of checkout crash on my-cluster -n payments", workspace=workspace)
    response = await client.post("/", json=_send({TRIAGE_KEY: request.model_dump(mode="json")}), headers=AUTH)
    assert response.status_code == 200
    task = response.json()["result"]
    assert task["status"]["state"] == "completed"
    result = TriageResult.model_validate(json.loads(_artifact_text(task)))
    assert result.classification.category == "devops-investigation"
    assert result.classification.method == "model"
    assert (result.hints.cluster, result.hints.namespace) == ("my-cluster", "payments")


async def test_a2a_bad_request_gives_an_error_artifact(client: httpx.AsyncClient) -> None:
    response = await client.post("/", json=_send({TRIAGE_KEY: {"request": "x"}}), headers=AUTH)
    task = response.json()["result"]
    assert task["status"]["state"] == "completed"
    assert json.loads(_artifact_text(task))["error"].startswith("the TriageRequest is not valid: workspace")


async def test_agent_card(client: httpx.AsyncClient) -> None:
    card = (await client.get("/.well-known/agent-card.json", headers=AUTH)).json()
    assert card["name"] == "wattson-triage"
    assert card["url"] == "http://wattson-triage:9001/"
    assert [s["id"] for s in card["skills"]] == ["triage"]
    assert card["capabilities"]["streaming"] is True


async def test_token_is_required(client: httpx.AsyncClient) -> None:
    assert (await client.post("/", json=_send({}))).status_code == 401
    assert (await client.get("/.well-known/agent-card.json")).status_code == 401
    assert (await client.get("/v1/health")).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 40}
    assert (await client.post("/", json=_send({}), headers=wrong)).status_code == 401


async def test_admin_routes(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"ok": True}
    health = ComponentHealth.model_validate((await client.get("/v1/health", headers=AUTH)).json())
    assert health.ok and "fake-model" in (health.detail or "")


async def test_lifespan_warms_the_model_up(fake_llama: Any, make_settings: Any) -> None:
    app = create_app(make_settings(fake_llama.url, token=TOKEN))
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
    running = asyncio.create_task(app(scope, inbox.get, outbox.put))
    assert (await asyncio.wait_for(outbox.get(), 5))["type"] == "lifespan.startup.complete"
    for _ in range(100):
        if fake_llama.classify_calls() and fake_llama.hint_calls():
            break
        await asyncio.sleep(0.05)
    assert fake_llama.classify_calls() == 1 and fake_llama.hint_calls() == 1
    await inbox.put({"type": "lifespan.shutdown"})
    assert (await asyncio.wait_for(outbox.get(), 5))["type"] == "lifespan.shutdown.complete"
    await running
