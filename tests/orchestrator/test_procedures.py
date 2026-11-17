"""The Wattson procedure in the whole task: list_procedures, the pick by triage, the procedure
question, and the procedure key sent to the knowledge agent. Fake triage and knowledge agents."""

from __future__ import annotations

from typing import Any

from wattson.agents.orchestrator.tools import settle_procedure
from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    ApproveReason,
    ClarifyReason,
    Overrides,
    ProcedureOption,
    ProcedurePick,
)

from .conftest import PROCEDURE_OPTIONS, PROCEDURE_STEP, RUNBOOK_KEY, FakeCounters, Orchestrator, interrupt_of
from .test_flow import evidence_for

STORAGE = "the pvc of postgres is stuck and the volume is full"
STORAGE_KEY = "wattson/k8s-storage.md"
TIE = "the ingress certificate is wrong and the pvc volume is full"


def until_approval(orch: Orchestrator, task: dict[str, Any]) -> ApproveReason:
    while (interrupt := interrupt_of(task))["name"] != APPROVE:
        assert interrupt["name"] == COLLECT_EVIDENCE, interrupt["name"]
        task = orch.answer(task, evidence_for(interrupt["reason"]))
    return ApproveReason.model_validate(interrupt["reason"])


def details(orch: Orchestrator, task_id: str) -> str:
    return " | ".join(e.detail or "" for e in orch.deps.store.list_events(task_id))


def test_model_pick_sends_the_procedure_key(orch: Orchestrator, with_procedures: FakeCounters) -> None:
    with_procedures.model_pick = "k8s-pod-crashloop"
    task = orch.start("pods crash on kind-dev with CrashLoopBackOff")
    assert interrupt_of(task)["name"] == COLLECT_EVIDENCE  # nothing to ask: model and words agree
    assert with_procedures.last_triage is not None
    assert [o.name for o in with_procedures.last_triage.procedures] == [o.name for o in PROCEDURE_OPTIONS]

    reason = until_approval(orch, task)
    knowledge = with_procedures.last_knowledge
    assert knowledge is not None and knowledge.procedure_key == "wattson/k8s-pod-crashloop.md"
    sources = reason.proposal.sources
    assert [(s.key, s.why) for s in sources] == [
        ("wattson/k8s-pod-crashloop.md", "procedure"),
        (RUNBOOK_KEY, "related"),
    ]
    prompt = reason.proposal.prompt
    assert prompt.index("## Procedure (Wattson: k8s-pod-crashloop)\n") < prompt.index(
        "## Runbooks from Grimoire\n"
    )
    assert PROCEDURE_STEP in prompt
    record = orch.deps.store.get_task(reason.task_id)
    assert record is not None and record.source_keys == ["wattson/k8s-pod-crashloop.md", RUNBOOK_KEY]
    assert "procedure k8s-pod-crashloop (model)" in details(orch, reason.task_id)


def test_model_none_but_the_words_point_to_one_asks(
    orch: Orchestrator, with_procedures: FakeCounters
) -> None:
    with_procedures.model_pick = "none"
    searches = with_procedures.knowledge_calls
    task = orch.start(STORAGE)
    interrupt = interrupt_of(task)
    assert interrupt["name"] == CLARIFY
    clarify = ClarifyReason.model_validate(interrupt["reason"])
    (question,) = clarify.questions
    assert (question.id, question.field, question.options) == (
        "procedure",
        "procedure",
        ["k8s-storage", "none"],
    )
    assert "found no Wattson procedure" in question.text and "k8s-storage" in question.text
    assert with_procedures.knowledge_calls == searches  # Grimoire is read after the answer

    task = orch.answer(task, {"answers": {"procedure": "k8s-storage"}})
    reason = until_approval(orch, task)
    knowledge = with_procedures.last_knowledge
    assert knowledge is not None and knowledge.request == STORAGE and knowledge.procedure_key == STORAGE_KEY
    assert "## Procedure (Wattson: k8s-storage)" in reason.proposal.prompt
    assert "procedure k8s-storage (user)" in details(orch, reason.task_id)


def test_disagreement_asks_and_none_means_free_search(
    orch: Orchestrator, with_procedures: FakeCounters
) -> None:
    # No tag of the model's pick; two options own two tags each, so no owner wins: ask.
    with_procedures.model_pick = "k8s-pod-crashloop"
    task = orch.start(TIE)
    first = interrupt_of(task)
    (question,) = ClarifyReason.model_validate(first["reason"]).questions
    assert question.options == ["k8s-pod-crashloop", "k8s-tls-ingress", "k8s-storage", "none"]
    assert "chose the Wattson procedure k8s-pod-crashloop" in question.text

    task = orch.answer(task, {"answers": {"procedure": "k8s-everything"}})  # not an option
    second = interrupt_of(task)
    assert second["name"] == CLARIFY and second["interruptId"] != first["interruptId"]
    again = ClarifyReason.model_validate(second["reason"])
    assert [q.id for q in again.questions] == ["procedure"]
    assert any(
        "must be one of: k8s-pod-crashloop, k8s-tls-ingress, k8s-storage, none" in w for w in again.warnings
    )

    task = orch.answer(task, {"answers": {"procedure": "none"}})
    reason = until_approval(orch, task)
    knowledge = with_procedures.last_knowledge
    assert knowledge is not None and knowledge.request == TIE and knowledge.procedure_key is None
    assert "## Procedure (" not in reason.proposal.prompt
    assert [s.why for s in reason.proposal.sources] == ["search"]
    assert "no procedure (user)" in details(orch, reason.task_id)


def test_the_users_procedure_wins(orch: Orchestrator, with_procedures: FakeCounters) -> None:
    with_procedures.model_pick = "k8s-tls-ingress"
    task = orch.start(STORAGE, procedure="k8s-storage")
    reason = until_approval(orch, task)  # no question: the user chose
    assert with_procedures.last_triage is not None
    assert with_procedures.last_triage.overrides.procedure == "k8s-storage"
    knowledge = with_procedures.last_knowledge
    assert knowledge is not None and knowledge.procedure_key == STORAGE_KEY
    assert reason.proposal.sources[0].why == "procedure"


def test_no_procedures_keeps_the_old_flow(orch: Orchestrator, counters: FakeCounters) -> None:
    assert counters.procedures == []
    before = counters.list_calls
    task = orch.start("pods crash on kind-dev")
    reason = until_approval(orch, task)
    assert counters.list_calls == before + 1
    assert counters.last_triage is not None and counters.last_triage.procedures == []
    assert counters.last_knowledge is not None and counters.last_knowledge.procedure_key is None
    assert [s.why for s in reason.proposal.sources] == ["search"]
    prompt = reason.proposal.prompt
    assert "## Runbooks from Grimoire\n" in prompt and "## Procedure (" not in prompt
    events = details(orch, reason.task_id)
    assert "0 Wattson procedure(s)" in events and "; procedure" not in events and "no procedure" not in events


def test_triage_down_the_words_choose(
    orchestrator_factory: Any, dead_url: str, knowledge_url: str, with_procedures: FakeCounters
) -> None:
    orch = orchestrator_factory(dead_url, knowledge_url)
    task = orch.start(STORAGE)
    clarify = ClarifyReason.model_validate(interrupt_of(task)["reason"])
    assert [q.field for q in clarify.questions] == ["workflow"]  # no procedure question: signals chose
    assert any("k8s-storage was chosen by the words of your request" in w for w in clarify.warnings)
    task = orch.answer(task, {"answers": {"workflow": "engineering-task"}})
    until_approval(orch, task)
    knowledge = with_procedures.last_knowledge
    assert knowledge is not None and knowledge.procedure_key == STORAGE_KEY


def test_a_pick_outside_the_options_is_dropped() -> None:
    task: dict[str, Any] = {"request": STORAGE, "warnings": []}
    odd = ProcedurePick(key="wattson/rm-rf.md", name="rm-rf", method="model", agreed=True)
    pick, ask = settle_procedure(task, PROCEDURE_OPTIONS, odd, Overrides())
    assert pick is not None and (pick.key, pick.method) == (None, "none") and ask == {}
    assert any("not a Wattson procedure" in w for w in task["warnings"])
    # A name that is not an option is not asked about.
    hint = ProcedurePick(name="k8s-unknown", method="none")
    pick, ask = settle_procedure(task, PROCEDURE_OPTIONS, hint, Overrides())
    assert pick is not None and pick.name is None and ask == {}
    # No options: nothing to pick, and the user's procedure cannot be used.
    pick, ask = settle_procedure(task, [], None, Overrides(procedure="k8s-storage"))
    assert pick is None and ask == {}
    assert any("cannot be used" in w for w in task["warnings"])


def test_the_question_names_only_procedures_of_the_workflow() -> None:
    docs = ProcedureOption(
        key="wattson/docs-from-data.md",
        name="docs-from-data",
        when_to_use="Write docs from facts.",
        workflow="engineering-task",
        signals=["pvc", "volume", "full"],
    )
    options = [*PROCEDURE_OPTIONS, docs]
    pick = ProcedurePick(key="wattson/k8s-pod-crashloop.md", name="k8s-pod-crashloop", method="model")
    task: dict[str, Any] = {"request": TIE, "warnings": []}
    _, ask = settle_procedure(task, options, pick, Overrides(), "devops-investigation")
    assert list(ask) == ["k8s-pod-crashloop", "k8s-tls-ingress", "k8s-storage"]
    _, ask = settle_procedure(task, options, pick, Overrides(), None)  # workflow not known: all
    assert list(ask) == ["k8s-pod-crashloop", "docs-from-data", "k8s-tls-ingress"]
