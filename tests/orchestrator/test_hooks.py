"""The hooks: the approval gate, the audit writer, the redaction of tool results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.hooks import GATE_NO_APPROVAL, ApprovalGate, Audit, Redaction
from wattson.agents.orchestrator.store import Store
from wattson.agents.orchestrator.tools import make_tools
from wattson.shared.planner import PlannerModel, step

from .conftest import FAKE_GITHUB_TOKEN

HASH = "a" * 64


class NoRemotes:
    async def triage(self, request: Any) -> Any:
        raise AssertionError("not called")

    async def find_procedures(self, request: Any) -> Any:
        raise AssertionError("not called")

    async def list_procedures(self, request: Any) -> Any:
        raise AssertionError("not called")


def agent_with(task: dict[str, Any] | None, approval: dict[str, Any] | None = None) -> Agent:
    agent = Agent(model=PlannerModel("start_task"), tools=[], callback_handler=None)
    if task is not None:
        agent.state.set(w.TASK, task)
    if approval is not None:
        agent.state.set(w.APPROVAL, approval)
    return agent


def task_at(status: str, **extra: Any) -> dict[str, Any]:
    return {"id": "t-20261022-abc123", "created_at": "2026-10-22T10:00:00.000Z", "status": status, **extra}


def before(agent: Agent, name: str) -> BeforeToolCallEvent:
    event = BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"toolUseId": "p-1", "name": name, "input": {}},
        invocation_state={},
    )
    ApprovalGate().check(event)
    return event


def test_gate_cancels_run_without_approval() -> None:
    assert before(agent_with(task_at("approved")), "run_on_laptop").cancel_tool == GATE_NO_APPROVAL


def test_gate_cancels_run_with_reject_or_bad_hash() -> None:
    reject = {"decision": "reject", "prompt_sha256": HASH}
    assert before(agent_with(task_at("approved", approved_sha256=HASH), reject), "run_on_laptop").cancel_tool
    short = {"decision": "approve", "prompt_sha256": "abc"}
    assert before(agent_with(task_at("approved", approved_sha256="abc"), short), "run_on_laptop").cancel_tool
    other = {"decision": "approve", "prompt_sha256": "b" * 64}
    assert before(agent_with(task_at("approved", approved_sha256=HASH), other), "run_on_laptop").cancel_tool


def test_gate_allows_run_after_approval() -> None:
    approval = {"decision": "approve", "prompt_sha256": HASH}
    for status in ("approved", "submitted"):
        event = before(agent_with(task_at(status, approved_sha256=HASH), approval), "run_on_laptop")
        assert event.cancel_tool is False


@pytest.mark.parametrize(
    ("status", "name", "ok"),
    [
        (None, "start_task", True),
        (None, "triage", False),
        ("triage", "triage", True),
        ("triage", "list_procedures", True),
        ("awaiting-input", "list_procedures", False),
        ("awaiting-input", "find_procedures", True),
        ("proposed", "find_procedures", False),
        ("triage", "request_approval", False),
        ("proposed", "compose", True),
        ("proposed", "request_approval", True),
        ("proposed", "run_on_laptop", False),
        ("submitted", "compose", False),
        ("completed", "start_task", True),
        ("completed", "clarify", False),
        ("expired", "request_approval", False),
        ("expired", "finish", True),
        ("triage", "rm_rf", False),
    ],
)
def test_gate_transition_table(status: str | None, name: str, ok: bool) -> None:
    task = None if status is None else task_at(status)
    event = before(agent_with(task), name)
    assert (event.cancel_tool is False) is ok


def test_gate_in_a_real_agent(tmp_path: Path) -> None:
    """A planner that jumps straight to run_on_laptop is stopped, and the task fails on record."""
    store = Store(tmp_path)
    agent = Agent(
        model=PlannerModel("run_on_laptop"),
        tools=make_tools(NoRemotes()),
        hooks=[ApprovalGate(), Audit(store), Redaction()],
        callback_handler=None,
    )
    agent.state.set(w.TASK, task_at("approved", approved_sha256=HASH))
    result = agent("go")
    assert result.stop_reason == "end_turn"
    assert GATE_NO_APPROVAL in str(result)
    assert not agent._interrupt_state.interrupts  # no run interrupt reached the CLI
    record = store.get_task("t-20261022-abc123")
    assert record is not None and record.status == "failed"
    assert "refused" in (store.list_events(record.id)[-1].detail or "")


def test_audit_flush_writes_once(tmp_path: Path) -> None:
    store = Store(tmp_path)
    audit = Audit(store)
    agent = agent_with(None)
    task = task_at(None)  # type: ignore[arg-type]
    w.mark(task, "triage", "task started")
    w.save_task(agent, task)
    audit.flush(agent)
    audit.flush(agent)  # nothing new: no second event
    events = store.list_events(task["id"])
    assert [(e.from_status, e.to_status) for e in events] == [(None, "triage")]
    task = w.load_task(agent)
    w.mark(task, "awaiting-input", "asked 2 questions")
    w.save_task(agent, task)
    audit.flush(agent)
    assert store.get_task(task["id"]).status == "awaiting-input"  # type: ignore[union-attr]
    assert len(store.list_events(task["id"])) == 2


def after(agent: Agent, text: str, status: str = "success") -> AfterToolCallEvent:
    event = AfterToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"toolUseId": "p-1", "name": "triage", "input": {}},
        invocation_state={},
        result={"toolUseId": "p-1", "status": status, "content": [{"text": text}]},
    )
    Redaction().clean(event)
    return event


def test_redaction_keeps_step_json_readable() -> None:
    text = step("find_procedures", output={"note": f"found {FAKE_GITHUB_TOKEN}"})
    event = after(agent_with(None), text)
    out = event.result["content"][0]["text"]
    assert FAKE_GITHUB_TOKEN not in out
    data = json.loads(out)
    assert data["next"] == "find_procedures" and "[REDACTED:" in data["output"]["note"]


def test_redaction_of_plain_text_and_clean_results() -> None:
    event = after(agent_with(None), f"Error: boom {FAKE_GITHUB_TOKEN}", status="error")
    assert FAKE_GITHUB_TOKEN not in event.result["content"][0]["text"]
    clean = step("clarify", output={"docs": 1})
    assert after(agent_with(None), clean).result["content"][0]["text"] == clean
