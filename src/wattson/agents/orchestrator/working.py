"""The working state of one task, kept in the Strands agent's state (memory only).

Keys: "task" (small facts and the status), "procedures", "docs", "plan", "evidence",
"evidence_before", "proposal", "approval" (the bulky parts). Values are JSON dicts, as AgentState
requires. Status changes are queued in task["pending_events"]; the audit hook writes them to the
store (it is the only writer)."""

from __future__ import annotations

from typing import Any

from wattson.agents.orchestrator.flow import now_iso
from wattson.shared.contracts import TaskRecord

TASK = "task"
PROCEDURES = "procedures"  # the Wattson procedure options (ProcedureOption dicts)
DOCS = "docs"
PLAN = "plan"
EVIDENCE = "evidence"  # the [request, item] pairs of the last round (the Guard checks these)
EVIDENCE_BEFORE = "evidence_before"  # the pairs of the first round, already checked, when there are two
PROPOSAL = "proposal"
APPROVAL = "approval"
WORK_KEYS = (TASK, PROCEDURES, DOCS, PLAN, EVIDENCE, EVIDENCE_BEFORE, PROPOSAL, APPROVAL)
BULKY_KEYS = (PROCEDURES, DOCS, PLAN, EVIDENCE, EVIDENCE_BEFORE, PROPOSAL, APPROVAL)


def get(agent: Any, key: str, default: Any = None) -> Any:
    value = agent.state.get(key)
    return default if value is None else value


def load_task(agent: Any) -> dict[str, Any]:
    return get(agent, TASK, {})


def save_task(agent: Any, task: dict[str, Any]) -> None:
    agent.state.set(TASK, task)


def drop_bulky(agent: Any) -> None:
    """Forget documents, evidence and the prompt: the task is over (or expired)."""
    for key in BULKY_KEYS:
        agent.state.delete(key)


def reset(agent: Any) -> None:
    for key in WORK_KEYS:
        agent.state.delete(key)


def mark(task: dict[str, Any], status: str, detail: str | None = None, *, note: bool = False) -> None:
    """Set the status; queue an event when it changes (or when note=True: a fact worth keeping).
    A tool may run twice around an interrupt, so an unchanged status adds nothing."""
    old = task.get("status")
    if old == status and not note:
        return
    task["status"] = status
    task.setdefault("pending_events", []).append(
        {"at": now_iso(), "from": old, "to": status, "detail": detail}
    )


def warn(task: dict[str, Any], text: str) -> None:
    warnings = task.setdefault("warnings", [])
    if text not in warnings:
        warnings.append(text)


def record_of(task: dict[str, Any], updated_at: str | None = None) -> TaskRecord:
    """The TaskRecord for the store: metadata only, never the request or the prompt."""
    classification = task.get("classification") or {}
    target = task.get("target") or {}
    return TaskRecord(
        id=task["id"],
        created_at=task["created_at"],
        updated_at=updated_at or now_iso(),
        status=task["status"],
        workflow=task.get("workflow"),
        category=classification.get("category"),
        confidence=classification.get("confidence"),
        method=classification.get("method"),
        repo=target.get("repo"),
        cluster=target.get("cluster"),
        namespace=target.get("namespace"),
        namespaces=list(target.get("namespaces") or []),
        all_namespaces=bool(target.get("all_namespaces")),
        source_keys=list(task.get("source_keys") or []),
        proposal_sha256=task.get("proposal_sha256"),
        approved_sha256=task.get("approved_sha256"),
        edited=task.get("edited"),
        session_mode=task.get("session_mode"),
        claude_session_id=task.get("claude_session_id"),
        error_summary=task.get("error_summary"),
    )
