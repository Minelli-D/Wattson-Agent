"""Seen before (D47): the lessons of Cognee memory, read-only. No Cognee here: the answer text has
the shape of cognee-mcp 1.6.5's recall, and SeenBefore.call is replaced where a call would go out."""

from __future__ import annotations

import time
from typing import Any

import pytest

from wattson.agents.knowledge.cognee import (
    MAX_QUERY,
    MAX_TEXT,
    SeenBefore,
    as_docs,
    check_allowed,
    lessons_of,
    pick,
    pieces,
)
from wattson.agents.knowledge.grimoire import ToolRefused
from wattson.agents.knowledge.settings import Settings
from wattson.shared.contracts import KnowledgeRequest

TOKEN = "knowledge-test-token-0123456789abcdef"  # noqa: S105 - a test token
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"  # noqa: S105 - a made-up GitHub token
ANSWER = f"""2 memories found (2 from claude_code_memory)
[graph] # 2026-10-28 cognee pod on the cluster
Context: deploy of cognee-mcp.

## The api container was OOMKilled during GLiNER
Problem: the pod wattson-cognee restarted with exit 137 on the first upload.
Cause: GLiNER needs about 1.7 GB more while it reads a note; the limit was 5Gi.
Fix: raised COGNEE_LIMITS to 4000m/8Gi. token {SECRET}
Lesson: size the memory limit for the model peak, not for idle.
Tags: k8s, cognee, oom, memory

[graph] ## The panel showed no data
Problem: the Tempo data source had the wrong URL.
Fix: use the Service name with the port 3200.
Lesson: check the data source first.
Tags: grafana, tempo"""
QUERIES = ["cognee pod oomkilled memory", "exit 137 restart"]


def settings(**changes: Any) -> Settings:
    values: dict[str, Any] = {"token": TOKEN, "cognee_recall": True, "cognee_url": "http://127.0.0.1:9/mcp"}
    return Settings(**(values | changes))


def test_only_recall_is_allowed() -> None:
    check_allowed("recall")
    for name in ("remember", "forget", "improve", "code_search"):
        with pytest.raises(ToolRefused):
            check_allowed(name)
    # Refused before any connection: port 9 has nothing, so a connection would fail otherwise.
    with pytest.raises(ToolRefused):
        SeenBefore(settings()).call("forget", {"dataset": "claude_code_memory"})


def test_the_lessons_of_an_answer() -> None:
    found = pieces(ANSWER)
    assert len(found) == 2
    first = lessons_of(found[0])
    assert [lesson.title for lesson in first] == ["2026-10-28 The api container was OOMKilled during GLiNER"]
    assert [name for name, _ in first[0].fields] == ["Problem", "Cause", "Fix", "Lesson"]
    assert first[0].tags == ("k8s", "cognee", "oom", "memory")
    assert pieces("no matching memories") == []


def test_only_lessons_that_fit_are_kept() -> None:
    found = pieces(ANSWER)
    assert [lesson.title for lesson in pick(found, QUERIES, 3)] == [
        "2026-10-28 The api container was OOMKilled during GLiNER"
    ]
    assert pick(found, ["certificate expired ingress"], 3) == []
    assert len(pick(found + found, QUERIES, 3)) == 1  # each lesson once


def test_the_documents_are_redacted_short_and_credited() -> None:
    docs = as_docs(pick(pieces(ANSWER), QUERIES, 3), "claude_code_memory")
    assert len(docs) == 1
    ref = docs[0].ref
    assert (ref.kind, ref.why, ref.freshness) == ("cognee", "seen_before", "live")
    assert ref.key.startswith("cognee:claude_code_memory/2026-10-28-the-api-container")
    assert SECRET not in docs[0].text and "[REDACTED:" in docs[0].text
    assert len(docs[0].text) <= MAX_TEXT


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_TOKEN", TOKEN)
    for name in (
        "COGNEE_RECALL",
        "COGNEE_RECALL_URL",
        "COGNEE_RECALL_DATASET",
        "COGNEE_RECALL_LESSONS",
        "COGNEE_RECALL_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().cognee_recall is False  # off unless asked
    monkeypatch.setenv("COGNEE_RECALL", "on")
    monkeypatch.setenv("COGNEE_RECALL_LESSONS", "2")
    s = Settings.from_env()
    assert (s.cognee_recall, s.cognee_url, s.cognee_dataset, s.cognee_lessons) == (
        True,
        "http://wattson-cognee:8000/mcp",
        "claude_code_memory",
        2,
    )
    for name, bad in (
        ("COGNEE_RECALL", "yes"),
        ("COGNEE_RECALL_LESSONS", "4"),
        ("COGNEE_RECALL_DATASET", "a,b"),
        ("COGNEE_RECALL_TIMEOUT_S", "11"),
    ):
        monkeypatch.setenv(name, bad)
        with pytest.raises(SystemExit):
            Settings.from_env()
        monkeypatch.delenv(name)
        monkeypatch.setenv("COGNEE_RECALL", "on")


async def test_find_sends_only_the_redacted_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def call(self: SeenBefore, name: str, arguments: dict[str, Any]) -> str:
        sent.append((name, arguments))
        return ANSWER

    monkeypatch.setattr(SeenBefore, "call", call)
    request = KnowledgeRequest(
        request="the whole request text stays here", queries=[*QUERIES, f"token {SECRET}"]
    )
    docs = await SeenBefore(settings()).find(request)
    assert len(docs) == 1
    [(name, arguments)] = sent
    assert name == "recall"
    assert arguments["search_type"] == "CHUNKS" and arguments["datasets"] == "claude_code_memory"
    assert SECRET not in arguments["query"] and "whole request" not in arguments["query"]
    assert len(arguments["query"]) <= MAX_QUERY


@pytest.mark.parametrize("trouble", ["error", "slow"])
async def test_find_never_breaks_a_task(monkeypatch: pytest.MonkeyPatch, trouble: str) -> None:
    def call(self: SeenBefore, name: str, arguments: dict[str, Any]) -> str:
        if trouble == "error":
            raise ConnectionError("Cognee is down")
        time.sleep(2)
        return ANSWER

    monkeypatch.setattr(SeenBefore, "call", call)
    seen = SeenBefore(settings(cognee_timeout_s=0.2))
    start = time.monotonic()
    docs = await seen.find(KnowledgeRequest(request="r", queries=QUERIES))
    assert docs == [] and time.monotonic() - start < 1.5
