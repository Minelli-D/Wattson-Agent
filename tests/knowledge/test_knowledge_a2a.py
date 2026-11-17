"""A2A round trips through the real A2AServer app: JSON-RPC message/send with a DataPart
{"wattson.find_procedures": KnowledgeRequest} -> a completed task whose artifact is a KnowledgeResult,
and {"wattson.list_procedures": ProcedureListRequest} -> a ProcedureList."""

from __future__ import annotations

import uuid
from typing import Any

from grimoire_stub import GrimoireStub
from knowledge_helpers import Knowledge

from wattson.shared.contracts import (
    FIND_PROCEDURES_KEY,
    LIST_PROCEDURES_KEY,
    KnowledgeRequest,
    KnowledgeResult,
    ProcedureList,
    ProcedureListRequest,
)


def message_send(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "kind": "message",
                "messageId": uuid.uuid4().hex,
                "parts": [{"kind": "data", "data": data}],
            },
            "configuration": {"blocking": True},
        },
    }


def artifact_text(answer: dict[str, Any]) -> str:
    task = answer["result"]
    assert task["status"]["state"] == "completed", task["status"]
    return "".join(p.get("text", "") for a in task.get("artifacts", []) for p in a["parts"])


async def test_agent_card(knowledge: Knowledge) -> None:
    card = (await knowledge.http.get("/.well-known/agent-card.json")).json()
    assert card["name"] == "wattson-knowledge"
    assert [s["id"] for s in card["skills"]] == ["find_procedures", "list_procedures"]


async def test_round_trip(connected: Knowledge, stub: GrimoireStub) -> None:
    request = KnowledgeRequest(
        request="pods restart", queries=["crashloop", "kubectl logs previous"], tags=["k8s"], limit=2
    )
    answer = await connected.http.post("/", json=message_send({FIND_PROCEDURES_KEY: request.model_dump()}))
    assert answer.status_code == 200
    result = KnowledgeResult.model_validate_json(artifact_text(answer.json()))
    assert result.warnings == []
    assert result.docs[0].ref.key == "infra/k8s/crashloop-runbook"
    assert {d.ref.why for d in result.docs} == {"search", "related", "context"}
    assert "search_documents" in stub.tool_calls()


async def test_round_trip_when_not_connected(knowledge: Knowledge, stub: GrimoireStub) -> None:
    request = KnowledgeRequest(request="pods restart", queries=["crashloop"])
    answer = await knowledge.http.post("/", json=message_send({FIND_PROCEDURES_KEY: request.model_dump()}))
    result = KnowledgeResult.model_validate_json(artifact_text(answer.json()))
    assert result.docs == []
    assert result.warnings == ["Grimoire is not connected: run wattson grimoire connect"]
    assert stub.calls == []


async def test_a_bad_request_is_a_warning(knowledge: Knowledge) -> None:
    for data in ({"other.key": {}}, {FIND_PROCEDURES_KEY: {"request": "x", "queries": [], "limit": 99}}):
        answer = await knowledge.http.post("/", json=message_send(data))
        result = KnowledgeResult.model_validate_json(artifact_text(answer.json()))
        assert result.docs == []
        assert len(result.warnings) == 1 and result.warnings[0].startswith("Grimoire was not asked:")


async def test_list_procedures_round_trip(connected: Knowledge, stub: GrimoireStub) -> None:
    data = {LIST_PROCEDURES_KEY: ProcedureListRequest().model_dump()}
    answer = await connected.http.post("/", json=message_send(data))
    assert answer.status_code == 200
    result = ProcedureList.model_validate_json(artifact_text(answer.json()))
    assert result.warnings == []
    assert [p.key for p in result.procedures] == [
        "wattson/code-bug-fix.md",
        "wattson/k8s-tls-ingress.md",
        "wattson/prompt-rewrite.md",
    ]
    assert "search_documents" not in stub.tool_calls() and "get_context" in stub.tool_calls()


async def test_list_procedures_round_trip_when_not_connected(
    knowledge: Knowledge, stub: GrimoireStub
) -> None:
    for request in ({}, None):  # an empty request is the folder "wattson"
        answer = await knowledge.http.post("/", json=message_send({LIST_PROCEDURES_KEY: request}))
        result = ProcedureList.model_validate_json(artifact_text(answer.json()))
        assert result.procedures == []
        assert result.warnings == ["Grimoire is not connected: run wattson grimoire connect"]
    assert stub.calls == []
    bad = await knowledge.http.post("/", json=message_send({LIST_PROCEDURES_KEY: {"folder": 5}}))
    result = ProcedureList.model_validate_json(artifact_text(bad.json()))
    assert result.warnings == ["Grimoire was not asked: the ProcedureListRequest is not valid: folder"]


async def test_find_with_a_procedure_round_trip(connected: Knowledge, stub: GrimoireStub) -> None:
    request = KnowledgeRequest(
        request="https on my ingress fails",
        queries=["tls certificate"],
        procedure_key="wattson/k8s-tls-ingress.md",
    )
    answer = await connected.http.post("/", json=message_send({FIND_PROCEDURES_KEY: request.model_dump()}))
    result = KnowledgeResult.model_validate_json(artifact_text(answer.json()))
    assert result.warnings == []
    assert [d.ref.why for d in result.docs] == ["procedure", "related", "related"]
    assert result.docs[0].ref.key == "wattson/k8s-tls-ingress.md"
    assert stub.tool_calls() == ["read_document"] * 3
