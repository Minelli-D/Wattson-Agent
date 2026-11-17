"""Several namespaces, or all of them, over A2A: the listings first, then a second round for the
named workload where the listing shows it; the review and the record name every namespace."""

from __future__ import annotations

from typing import Any

from wattson.shared.contracts import APPROVE, COLLECT_EVIDENCE, ApproveReason, CollectEvidenceReason

from .conftest import Orchestrator, interrupt_of, outcome_of


def answer_with(reason: dict[str, Any], listing: dict[str, str]) -> dict[str, Any]:
    """The CLI's answer: `listing[namespace]` for the deployments listings, a line for the rest."""
    plan = CollectEvidenceReason.model_validate(reason).plan
    items = []
    for r in plan:
        output = f"output of {r.op}"
        if r.op == "k8s.get" and r.kind == "deployments":
            output = listing.get("*" if r.all_namespaces else r.namespace or "", "No resources found")
        items.append(
            {
                "request_id": r.id,
                "ok": True,
                "source": f"fake {r.op}",
                "collected_at": "2026-10-28T10:00:00Z",
                "output": output,
            }
        )
    return {"items": items}


def test_several_namespaces_then_the_workload_where_it_is(orch: Orchestrator) -> None:
    task = orch.start(
        "the api pods crash",
        workflow="devops-investigation",
        cluster="my-cluster",
        namespaces=["web", "shop"],
        workload="deployment/api",
    )
    interrupt = interrupt_of(task)
    assert interrupt["name"] == COLLECT_EVIDENCE
    first = CollectEvidenceReason.model_validate(interrupt["reason"])
    assert first.target.namespaces == ["web", "shop"] and first.target.namespace is None
    k8s = [(r.op, r.namespace) for r in first.plan if r.op.startswith("k8s.")]
    assert k8s == [(op, ns) for ns in ("web", "shop") for op in ("k8s.get", "k8s.get", "k8s.events")]
    listing = {"web": "NAME   READY\napi    0/1\n", "shop": "NAME   READY\ncart   1/1\n"}
    task = orch.answer(task, answer_with(interrupt["reason"], listing))

    # The second round: only in web, where the listing shows api; the ids go on.
    interrupt = interrupt_of(task)
    assert interrupt["name"] == COLLECT_EVIDENCE
    second = CollectEvidenceReason.model_validate(interrupt["reason"])
    assert [(r.op, r.namespace) for r in second.plan] == [
        ("k8s.describe", "web"),
        ("k8s.rollout", "web"),
        ("k8s.logs", "web"),
        ("k8s.logs", "web"),  # "crash": the previous logs too
    ]
    assert second.plan[0].id == f"e{len(first.plan) + 1}"
    task = orch.answer(task, answer_with(interrupt["reason"], {}))

    interrupt = interrupt_of(task)
    assert interrupt["name"] == APPROVE
    reason = ApproveReason.model_validate(interrupt["reason"])
    ids = {e.request_id for e in reason.proposal.evidence}
    assert ids == {r.id for r in [*first.plan, *second.plan]}  # both rounds are in the prompt
    assert "- Namespaces: web, shop" in reason.proposal.prompt
    assert "Which namespace is affected?" not in reason.proposal.prompt
    record = orch.deps.store.get_task(reason.task_id)
    assert record is not None and record.namespaces == ["web", "shop"] and record.namespace is None
    task = orch.answer(task, {"decision": "reject", "reason": "test"})
    assert outcome_of(task)["status"] == "rejected"


def test_all_namespaces_workload_nowhere_goes_on_with_a_warning(orch: Orchestrator) -> None:
    task = orch.start(
        "pods crash",
        workflow="devops-investigation",
        cluster="my-cluster",
        all_namespaces=True,
        workload="deployment/api",
    )
    interrupt = interrupt_of(task)
    first = CollectEvidenceReason.model_validate(interrupt["reason"])
    assert first.target.all_namespaces
    assert all(r.all_namespaces for r in first.plan if r.op.startswith("k8s."))
    task = orch.answer(task, answer_with(interrupt["reason"], {}))

    interrupt = interrupt_of(task)
    assert interrupt["name"] == APPROVE  # no second round
    reason = ApproveReason.model_validate(interrupt["reason"])
    assert any("not found in any namespace" in w for w in reason.warnings)
    assert "- Namespaces: all" in reason.proposal.prompt
    record = orch.deps.store.get_task(reason.task_id)
    assert record is not None and record.all_namespaces


def test_all_namespaces_finds_the_workload(orch: Orchestrator) -> None:
    task = orch.start(
        "why is api not ready",
        workflow="devops-investigation",
        cluster="my-cluster",
        all_namespaces=True,
        workload="deployment/api",
    )
    interrupt = interrupt_of(task)
    rows = "NAMESPACE   NAME   READY\nshop        api    0/1\n"
    task = orch.answer(task, answer_with(interrupt["reason"], {"*": rows}))
    interrupt = interrupt_of(task)
    assert interrupt["name"] == COLLECT_EVIDENCE
    second = CollectEvidenceReason.model_validate(interrupt["reason"])
    assert {r.namespace for r in second.plan} == {"shop"}
    assert [r.op for r in second.plan][:3] == ["k8s.describe", "k8s.rollout", "k8s.logs"]


def test_declined_first_round_has_no_second(orch: Orchestrator) -> None:
    task = orch.start(
        "pods crash",
        workflow="devops-investigation",
        cluster="my-cluster",
        namespaces=["web", "shop"],
        workload="deployment/api",
    )
    task = orch.answer(task, {"declined": True})
    assert interrupt_of(task)["name"] == APPROVE


def test_declined_second_round_keeps_the_first(orch: Orchestrator) -> None:
    task = orch.start(
        "pods crash",
        workflow="devops-investigation",
        cluster="my-cluster",
        namespaces=["web", "shop"],
        workload="deployment/api",
    )
    first = CollectEvidenceReason.model_validate(interrupt_of(task)["reason"])
    task = orch.answer(task, answer_with(interrupt_of(task)["reason"], {"web": "NAME   READY\napi    0/1\n"}))
    assert interrupt_of(task)["name"] == COLLECT_EVIDENCE
    task = orch.answer(task, {"declined": True})
    reason = ApproveReason.model_validate(interrupt_of(task)["reason"])
    assert {e.request_id for e in reason.proposal.evidence} == {r.id for r in first.plan}
    assert any("second round" in w for w in reason.warnings)
