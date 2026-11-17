"""The admin routes, the bearer token, and the access log."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from wattson.shared.contracts import HealthView, TaskEvent, TaskRecord

from .conftest import TOKEN, FakeCounters, Orchestrator


def test_token_is_required(orch: Orchestrator) -> None:
    anonymous = httpx.Client(base_url=orch.url, timeout=10)
    assert anonymous.get("/healthz").json() == {"ok": True}
    for method, path in [
        ("GET", "/v1/tasks"),
        ("GET", "/v1/health"),
        ("GET", "/.well-known/agent-card.json"),
        ("POST", "/"),
        ("POST", "/v1/grimoire/disconnect"),
    ]:
        assert anonymous.request(method, path).status_code == 401, path
    wrong = httpx.Client(base_url=orch.url, headers={"Authorization": "Bearer " + "w" * 40}, timeout=10)
    assert wrong.get("/v1/tasks").status_code == 401


def test_agent_card(orch: Orchestrator) -> None:
    card = orch.http.get("/.well-known/agent-card.json").json()
    assert card["name"] == "wattson-orchestrator"
    assert [s["id"] for s in card["skills"]] == ["engineering_task"]


def test_health(orch: Orchestrator) -> None:
    view = HealthView.model_validate(orch.http.get("/v1/health").json())
    assert view.ok and view.triage.ok and view.knowledge.ok
    assert view.grimoire is not None and view.grimoire.connected
    assert view.history == {"tasks": 0, "retention_days": 90}


def test_health_when_agents_are_down(orchestrator_factory: Any, dead_url: str) -> None:
    orch = orchestrator_factory(dead_url, dead_url)
    view = HealthView.model_validate(orch.http.get("/v1/health").json())
    assert not view.ok and not view.triage.ok and not view.knowledge.ok and view.grimoire is None


def test_tasks_routes(orch: Orchestrator) -> None:
    orch.start("pods crash in web")
    orch.start("pods crash in web again")
    tasks = [TaskRecord.model_validate(t) for t in orch.http.get("/v1/tasks", params={"limit": 20}).json()]
    assert len(tasks) == 2 and all(t.status == "awaiting-input" for t in tasks)
    assert len(orch.http.get("/v1/tasks", params={"limit": 1}).json()) == 1
    assert orch.http.get("/v1/tasks", params={"limit": "lots"}).status_code == 400
    assert orch.http.get("/v1/tasks", params={"limit": 0}).status_code == 400

    one = TaskRecord.model_validate(orch.http.get(f"/v1/tasks/{tasks[0].id}").json())
    assert one.id == tasks[0].id
    events = [TaskEvent.model_validate(e) for e in orch.http.get(f"/v1/tasks/{one.id}/events").json()]
    assert events[0].to_status == "triage" and events[-1].to_status == "awaiting-input"
    assert orch.http.get("/v1/tasks/t-nope").status_code == 404
    assert orch.http.get("/v1/tasks/t-nope/events").status_code == 404


def test_grimoire_proxy(orch: Orchestrator, counters: FakeCounters) -> None:
    assert orch.http.get("/v1/grimoire/status").json()["connected"] is True
    redirect = {"redirect_uri": "http://127.0.0.1:8765/callback"}
    view = orch.http.post("/v1/grimoire/connect", json=redirect).json()
    assert view["authorize_url"].startswith("https://grimoire.example.test/authorize")
    assert counters.grimoire_calls[-1] == ("connect", redirect)
    assert (
        orch.http.post("/v1/grimoire/callback", json={"code": "one-time", "state": "abc"}).status_code == 200
    )
    assert counters.grimoire_calls[-1] == ("callback", {"code": "one-time", "state": "abc"})
    assert orch.http.post("/v1/grimoire/disconnect").json()["connected"] is False

    before = len(counters.grimoire_calls)
    bad = orch.http.post("/v1/grimoire/connect", json={"redirect_uri": "x", "extra": 1})
    assert bad.status_code == 422 and len(counters.grimoire_calls) == before  # not forwarded


def test_grimoire_proxy_when_knowledge_is_down(
    orchestrator_factory: Any, triage_url: str, dead_url: str
) -> None:
    orch = orchestrator_factory(triage_url, dead_url)
    response = orch.http.get("/v1/grimoire/status")
    assert response.status_code == 502 and response.json()["error"] == "bad_gateway"


def test_access_log(orch: Orchestrator, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="wattson.orchestrator.access"):
        orch.http.get("/v1/tasks", params={"limit": 5, "code": "do-not-log-me"})
        httpx.get(f"{orch.url}/v1/tasks", timeout=10)
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "wattson.orchestrator.access"]
    assert [(line["method"], line["path"], line["status"]) for line in lines] == [
        ("GET", "/v1/tasks", 200),
        ("GET", "/v1/tasks", 401),
    ]
    assert all(set(line) == {"t", "method", "path", "status", "ms"} for line in lines)
    assert "do-not-log-me" not in caplog.text and TOKEN not in caplog.text
