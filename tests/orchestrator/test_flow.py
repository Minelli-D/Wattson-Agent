"""The whole task over A2A: raw JSON-RPC message/send against a running orchestrator, with fake
triage and knowledge agents. Each interrupt is answered the way the CLI would."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    RUN_CLAUDE_CODE,
    WORKFLOWS,
    ApproveReason,
    ClarifyReason,
    CollectEvidenceReason,
    RunReason,
    TaskOutcome,
)

from .conftest import (
    DOC_SECRET,
    FAKE_GITHUB_TOKEN,
    RUNBOOK_KEY,
    WORKSPACE,
    Orchestrator,
    interrupt_of,
    outcome_of,
)

Handler = Callable[[dict[str, Any]], dict[str, Any]]
MARKER = "zebra-marker-7781"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_for(reason: dict[str, Any], extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    plan = CollectEvidenceReason.model_validate(reason).plan
    items = [
        {
            "request_id": r.id,
            "ok": True,
            "source": f"fake {r.op}",
            "collected_at": "2026-10-22T10:00:00Z",
            "output": f"output of {r.op}: api-7f9 CrashLoopBackOff, env API_TOKEN={FAKE_GITHUB_TOKEN}",
        }
        for r in plan
    ]
    return {"items": items + (extra or [])}


def approve(reason: dict[str, Any], **session: Any) -> dict[str, Any]:
    proposal = ApproveReason.model_validate(reason).proposal
    return {
        "decision": "approve",
        "prompt_sha256": proposal.prompt_sha256,
        "edited": False,
        "session": session or {"id": None, "mode": "interactive"},
    }


def completed_run(reason: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "completed",
        "session_id": "sess-42",
        "summary": "Found the bad env var.",
        "exit_code": 0,
    }


def drive(
    orch: Orchestrator, task: dict[str, Any], handlers: dict[str, Handler]
) -> tuple[dict[str, Any], list[str]]:
    """Answer interrupts until the task completes. Returns the final task and the interrupt names seen."""
    seen: list[str] = []
    while task["status"]["state"] == "input-required":
        interrupt = interrupt_of(task)
        seen.append(interrupt["name"])
        assert len(seen) < 20, seen
        task = orch.answer(task, handlers[interrupt["name"]](interrupt["reason"]))
    return task, seen


def db_bytes(orch: Orchestrator) -> bytes:
    folder = Path(orch.deps.store.path).parent
    return b"".join(p.read_bytes() for p in folder.iterdir() if p.is_file())


def test_devops_happy_path(orch: Orchestrator) -> None:
    request = f"Investigate why pods crash in web ({MARKER}). My token is {FAKE_GITHUB_TOKEN}."
    task = orch.start(request)

    # clarify: no cluster or namespace yet
    interrupt = interrupt_of(task)
    assert interrupt["name"] == CLARIFY
    clarify = ClarifyReason.model_validate(interrupt["reason"])
    assert re.fullmatch(r"t-\d{8}-[0-9a-z]{6}", clarify.task_id)
    assert {q.field for q in clarify.questions} == {"cluster", "namespace"}
    answers = {q.id: ("kind-dev" if q.field == "cluster" else "web") for q in clarify.questions}
    task = orch.answer(task, {"answers": answers})

    # collect evidence: for the answered target
    interrupt = interrupt_of(task)
    assert interrupt["name"] == COLLECT_EVIDENCE
    collect = CollectEvidenceReason.model_validate(interrupt["reason"])
    assert collect.target.cluster == "kind-dev" and collect.target.namespace == "web"
    assert collect.plan
    task = orch.answer(task, evidence_for(interrupt["reason"]))

    # approve: the proposal
    interrupt = interrupt_of(task)
    assert interrupt["name"] == APPROVE
    reason = ApproveReason.model_validate(interrupt["reason"])
    proposal = reason.proposal
    assert reason.workflow == "devops-investigation"
    assert proposal.prompt_sha256 == sha(proposal.prompt)
    assert [s.key for s in proposal.sources] == [RUNBOOK_KEY]
    assert {e.request_id for e in proposal.evidence} == {r.id for r in collect.plan}
    assert all(e.freshness == "live" for e in proposal.evidence)
    assert proposal.redactions >= 3  # the request, the document, the evidence
    for secret in (FAKE_GITHUB_TOKEN, DOC_SECRET):
        assert secret not in proposal.prompt
    assert MARKER in proposal.prompt
    task = orch.answer(task, approve(interrupt["reason"], id="sess-1", mode="headless"))

    # run on the laptop
    interrupt = interrupt_of(task)
    assert interrupt["name"] == RUN_CLAUDE_CODE
    run = RunReason.model_validate(interrupt["reason"])
    assert run.prompt_sha256 == proposal.prompt_sha256
    assert run.session.id == "sess-1" and run.session.mode == "headless"
    task = orch.answer(task, completed_run(interrupt["reason"]))

    outcome = TaskOutcome.model_validate(outcome_of(task))
    assert outcome.status == "completed" and outcome.task_id == clarify.task_id
    assert outcome.source_keys == [RUNBOOK_KEY] and outcome.summary == "Found the bad env var."

    record = orch.deps.store.get_task(outcome.task_id)
    assert record is not None
    assert record.status == "completed" and record.workflow == "devops-investigation"
    assert (record.category, record.method) == ("devops-investigation", "model")
    assert (record.cluster, record.namespace, record.repo) == ("kind-dev", "web", "shop-api")
    assert record.proposal_sha256 == record.approved_sha256 == proposal.prompt_sha256
    assert record.edited is False and record.session_mode == "headless"
    assert record.claude_session_id == "sess-42" and record.source_keys == [RUNBOOK_KEY]
    statuses = [e.to_status for e in orch.deps.store.list_events(outcome.task_id)]
    order = [
        "triage",
        "awaiting-input",
        "collecting-evidence",
        "proposed",
        "approved",
        "submitted",
        "completed",
    ]
    assert [s for i, s in enumerate(statuses) if i == 0 or s != statuses[i - 1]] == order

    # Nothing of the prompt, the request, the documents or the secrets is on disk (SEC-14).
    blob = db_bytes(orch)
    for text in (MARKER, FAKE_GITHUB_TOKEN, DOC_SECRET, "CrashLoopBackOff", "kubectl describe the pod"):
        assert text.encode() not in blob, text
    assert proposal.prompt.encode()[:200] not in blob
    assert b"Found the bad env var" not in blob


def test_prompt_optimization_without_repo_needs_no_evidence(orch: Orchestrator) -> None:
    workspace = {**WORKSPACE, "repo": None}
    task = orch.start("Make my prompt for release notes sharper", workspace=workspace)
    task, seen = drive(orch, task, {APPROVE: approve, RUN_CLAUDE_CODE: completed_run})
    assert seen == [APPROVE, RUN_CLAUDE_CODE]
    outcome = outcome_of(task)
    assert outcome["status"] == "completed" and outcome["workflow"] == "prompt-optimization"
    record = orch.deps.store.get_task(outcome["task_id"])
    assert record is not None and record.repo is None and record.edited is False


def test_approval_answer_composes_again(orch: Orchestrator) -> None:
    task = orch.start("Make my prompt for release notes sharper", workspace={**WORKSPACE, "repo": None})
    first = interrupt_of(task)
    assert first["name"] == APPROVE
    proposal = ApproveReason.model_validate(first["reason"]).proposal
    question = "Which audience is the prompt for?"
    assert question in proposal.missing_info

    task = orch.answer(task, {"decision": "answer", "answers": {question: "the release team"}})
    second = interrupt_of(task)
    assert second["name"] == APPROVE and second["interruptId"] != first["interruptId"]
    again = ApproveReason.model_validate(second["reason"]).proposal
    assert question not in again.missing_info
    assert "the release team" in again.prompt and again.prompt_sha256 != proposal.prompt_sha256

    # The user edits the prompt: another hash is approved, and that is recorded.
    edited_hash = sha(again.prompt + "\nPlease be brief.")
    task = orch.answer(task, {"decision": "approve", "prompt_sha256": edited_hash, "edited": False})
    run = RunReason.model_validate(interrupt_of(task)["reason"])
    assert run.prompt_sha256 == edited_hash
    task = orch.answer(task, {"state": "failed", "error": "claude exited with 1", "exit_code": 1})
    outcome = outcome_of(task)
    assert outcome["status"] == "failed"
    record = orch.deps.store.get_task(outcome["task_id"])
    assert record is not None
    assert record.edited is True and record.approved_sha256 == edited_hash
    assert record.proposal_sha256 == again.prompt_sha256 and record.error_summary == "claude exited with 1"


def test_reject(orch: Orchestrator) -> None:
    task = orch.start("pods crash on kind-dev")
    task, seen = drive(
        orch,
        task,
        {
            COLLECT_EVIDENCE: evidence_for,
            APPROVE: lambda r: {"decision": "reject", "reason": f"not now, {FAKE_GITHUB_TOKEN}"},
        },
    )
    assert seen == [COLLECT_EVIDENCE, APPROVE]
    outcome = outcome_of(task)
    assert outcome["status"] == "rejected"
    record = orch.deps.store.get_task(outcome["task_id"])
    assert record is not None and record.status == "rejected" and record.approved_sha256 is None
    details = " ".join(e.detail or "" for e in orch.deps.store.list_events(record.id))
    assert "rejected by the user" in details and FAKE_GITHUB_TOKEN not in details


def test_triage_down_asks_for_the_workflow(
    orchestrator_factory: Any, dead_url: str, knowledge_url: str
) -> None:
    orch = orchestrator_factory(dead_url, knowledge_url)
    task = orch.start("pods crash on kind-dev")
    interrupt = interrupt_of(task)
    clarify = ClarifyReason.model_validate(interrupt["reason"])
    assert [q.field for q in clarify.questions] == ["workflow"]
    assert clarify.questions[0].options == list(WORKFLOWS)
    assert any("Triage is not available" in w for w in clarify.warnings)

    task = orch.answer(task, {"answers": {"workflow": "engineering-task"}})
    task, seen = drive(
        orch, task, {COLLECT_EVIDENCE: evidence_for, APPROVE: approve, RUN_CLAUDE_CODE: completed_run}
    )
    outcome = outcome_of(task)
    assert outcome["status"] == "completed" and outcome["workflow"] == "engineering-task"
    record = orch.deps.store.get_task(outcome["task_id"])
    assert record is not None
    assert (record.category, record.method, record.confidence) == ("unclear", "rules", 0.0)


def test_override_wins_and_unknown_cluster_is_dropped(orch: Orchestrator) -> None:
    task = orch.start("pods crash on faraway", workflow="devops-investigation", namespace="web")
    clarify = ClarifyReason.model_validate(interrupt_of(task)["reason"])
    assert [q.field for q in clarify.questions] == ["cluster"]
    assert any("faraway-cluster" in w and "dropped" in w for w in clarify.warnings)
    record = orch.deps.store.get_task(clarify.task_id)
    assert record is not None and record.method == "override" and record.confidence == 1.0


def test_bad_answers_are_asked_again(orch: Orchestrator) -> None:
    task = orch.start("pods crash in web")
    first = interrupt_of(task)
    ids = {q.field: q.id for q in ClarifyReason.model_validate(first["reason"]).questions}
    task = orch.answer(task, {"answers": {ids["cluster"]: "prod-elsewhere", ids["namespace"]: "Bad_NS!"}})
    second = interrupt_of(task)
    assert second["name"] == CLARIFY and second["interruptId"] != first["interruptId"]
    again = ClarifyReason.model_validate(second["reason"])
    assert {q.field for q in again.questions} == {"cluster", "namespace"}
    assert len([w for w in again.warnings if "must" in w]) == 2

    task = orch.answer(task, {"answers": {ids["cluster"]: "my-cluster", ids["namespace"]: "web"}})
    assert interrupt_of(task)["name"] == COLLECT_EVIDENCE


def test_clarify_cancel(orch: Orchestrator) -> None:
    task = orch.start("pods crash in web")
    task = orch.answer(task, {"cancel": True})
    assert outcome_of(task)["status"] == "rejected"


def test_knowledge_down_is_a_warning(orchestrator_factory: Any, triage_url: str, dead_url: str) -> None:
    orch = orchestrator_factory(triage_url, dead_url)
    task = orch.start("pods crash on kind-dev")
    task = orch.answer(task, evidence_for(interrupt_of(task)["reason"]))
    reason = ApproveReason.model_validate(interrupt_of(task)["reason"])
    assert reason.proposal.sources == []
    assert any("Grimoire procedures are not available" in w for w in reason.warnings)
    assert any("Wattson procedures are not available" in w for w in reason.warnings)
    assert DOC_SECRET not in reason.proposal.prompt


def test_unknown_evidence_is_refused(orch: Orchestrator) -> None:
    task = orch.start("pods crash on kind-dev")
    reason = interrupt_of(task)["reason"]
    stray = {
        "request_id": "e99",
        "ok": True,
        "source": "cat ~/.ssh/id_rsa",
        "collected_at": "2026-10-22T10:00:00Z",
        "output": "STRAY-EVIDENCE-TEXT",
    }
    task = orch.answer(task, evidence_for(reason, extra=[stray]))
    approve_reason = ApproveReason.model_validate(interrupt_of(task)["reason"])
    planned = {r.id for r in CollectEvidenceReason.model_validate(reason).plan}
    assert {e.request_id for e in approve_reason.proposal.evidence} == planned
    assert "STRAY-EVIDENCE-TEXT" not in approve_reason.proposal.prompt
    assert any("'e99'" in w and "refused" in w for w in approve_reason.warnings)


def test_declined_evidence(orch: Orchestrator) -> None:
    task = orch.start("pods crash on kind-dev")
    task = orch.answer(task, {"declined": True})
    reason = ApproveReason.model_validate(interrupt_of(task)["reason"])
    assert reason.proposal.evidence == []
    assert any("declined" in w for w in reason.warnings)


def test_broken_answer_fails_loudly(orch: Orchestrator) -> None:
    task = orch.start("pods crash on kind-dev")
    task = orch.answer(task, evidence_for(interrupt_of(task)["reason"]))
    task = orch.answer(task, {"decision": "approve"})  # no prompt hash
    assert task["status"]["state"] == "completed"
    assert "prompt_sha256" in outcome_of(task)["error"]
    records = orch.deps.store.list_tasks(1)
    assert records[0].status == "failed" and "prompt_sha256" in (records[0].error_summary or "")


def test_start_without_data_part(orch: Orchestrator) -> None:
    task = orch.rpc(
        {"role": "user", "kind": "message", "messageId": "m1", "parts": [{"kind": "text", "text": "hi"}]}
    )
    assert "wattson.start_task" in outcome_of(task)["error"]
    assert orch.deps.store.count_tasks() == 0


def test_expired_task_refuses_a_late_answer(orch: Orchestrator) -> None:
    task = orch.start("pods crash in web")
    task_id = ClarifyReason.model_validate(interrupt_of(task)["reason"]).task_id
    record = orch.deps.store.get_task(task_id)
    assert record is not None
    orch.deps.store.save_task(record.model_copy(update={"updated_at": "2000-01-01T00:00:00.000Z"}))
    assert orch.deps.expire() == [task_id]

    task = orch.answer(task, {"answers": {"cluster": "kind-dev", "namespace": "web"}})
    assert "not allowed while the task is expired" in outcome_of(task)["error"]
    after = orch.deps.store.get_task(task_id)
    assert after is not None and after.status == "expired"


def test_late_execution_report(orch: Orchestrator) -> None:
    task = orch.start("pods crash on kind-dev")
    task, _ = drive_until(orch, task, RUN_CLAUDE_CODE)
    task_id = RunReason.model_validate(interrupt_of(task)["reason"]).task_id

    body = {"result": {"state": "completed", "session_id": "sess-late", "summary": "done", "exit_code": 0}}
    response = orch.http.post(f"/v1/tasks/{task_id}/execution", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "completed" and response.json()["claude_session_id"] == "sess-late"
    assert orch.http.post(f"/v1/tasks/{task_id}/execution", json=body).status_code == 409  # already over
    assert orch.http.post("/v1/tasks/t-20260101-zzzzzz/execution", json=body).status_code == 404
    assert (
        orch.http.post(f"/v1/tasks/{task_id}/execution", json={"result": {"state": "odd"}}).status_code == 422
    )

    # The A2A run interrupt is still open; answering it now is refused by the gate.
    task = orch.answer(task, completed_run({}))
    assert "error" in outcome_of(task)
    record = orch.deps.store.get_task(task_id)
    assert record is not None and record.status == "completed" and record.claude_session_id == "sess-late"
    assert orch.deps.store.list_events(task_id)[-2].detail.startswith("late report")  # type: ignore[union-attr]


def test_late_report_needs_an_approval(orch: Orchestrator) -> None:
    task = orch.start("pods crash in web")
    task_id = ClarifyReason.model_validate(interrupt_of(task)["reason"]).task_id
    body = {"result": {"state": "completed"}}
    assert orch.http.post(f"/v1/tasks/{task_id}/execution", json=body).status_code == 409


def drive_until(orch: Orchestrator, task: dict[str, Any], stop: str) -> tuple[dict[str, Any], list[str]]:
    handlers: dict[str, Handler] = {COLLECT_EVIDENCE: evidence_for, APPROVE: approve}
    seen: list[str] = []
    while (name := interrupt_of(task)["name"]) != stop:
        seen.append(name)
        task = orch.answer(task, handlers[name](interrupt_of(task)["reason"]))
    return task, seen
