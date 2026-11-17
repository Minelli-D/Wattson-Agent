"""Workflows: questions and the minimum evidence plan (FR-PROMPT-06, FR-K8S-06, SEC-01..03)."""

from __future__ import annotations

from wattson.agents.orchestrator.templates import TEMPLATES
from wattson.agents.orchestrator.workflows import (
    WORKFLOW_TABLE,
    PlanContext,
    parse_workload,
    plan_evidence,
    plan_followup,
    required_questions,
)
from wattson.shared.contracts import WORKFLOWS, DecisionHints, EvidenceItem, RepoInfo, Target, WorkspaceInfo

REPO = RepoInfo(name="shop", root_display="~/shop", branch="main", head="abc1234")
CONTEXTS = ["my-cluster", "prod"]


def ctx(
    workflow: str = "devops-investigation",
    *,
    cluster: str | None = "my-cluster",
    namespace: str | None = "web",
    workload: str | None = None,
    repo: RepoInfo | None = REPO,
    contexts: list[str] | None = None,
    hints: DecisionHints | None = None,
    request: str = "",
) -> PlanContext:
    return PlanContext(
        workflow=workflow,  # type: ignore[arg-type]
        target=Target(
            repo=repo.name if repo else None, cluster=cluster, namespace=namespace, workload=workload
        ),
        workspace=WorkspaceInfo(
            repo=repo, cwd_name="shop", kube_contexts=CONTEXTS if contexts is None else contexts
        ),
        hints=hints or DecisionHints(),
        request=request,
    )


def ops(plan: list) -> list[str]:
    return [r.op for r in plan]


def test_every_workflow_has_a_table_entry_and_a_template() -> None:
    assert set(WORKFLOW_TABLE) == set(WORKFLOWS)
    for name, spec in WORKFLOW_TABLE.items():
        assert spec.name == name
        assert spec.title and spec.expected_outcome
        assert spec.template in TEMPLATES


def test_devops_asks_cluster_and_namespace_when_missing() -> None:
    questions = required_questions(ctx(cluster=None, namespace=None))
    assert [(q.id, q.field) for q in questions] == [("cluster", "cluster"), ("namespace", "namespace")]
    assert questions[0].options == CONTEXTS
    assert questions[1].options is None
    assert [q.id for q in required_questions(ctx(namespace=None))] == ["namespace"]
    assert required_questions(ctx()) == []


def test_devops_without_kube_contexts_asks_nothing_and_plans_no_kubernetes() -> None:
    c = ctx(cluster=None, namespace=None, contexts=[], workload="deployment/api")
    assert required_questions(c) == []
    assert not any(op.startswith("k8s.") for op in ops(plan_evidence(c)))


def test_other_workflows_ask_nothing() -> None:
    for wf in ("prompt-optimization", "engineering-task"):
        assert required_questions(ctx(wf, cluster=None, namespace=None)) == []
        assert required_questions(ctx(wf, repo=None)) == []


def test_devops_plan_basic() -> None:
    plan = plan_evidence(ctx())
    assert ops(plan) == ["k8s.get", "k8s.get", "k8s.events", "git.status", "git.log"]
    assert [r.kind for r in plan[:2]] == ["deployments", "pods"]
    assert plan[4].limit == 10
    assert [r.id for r in plan] == [f"e{i}" for i in range(1, len(plan) + 1)]
    assert all(r.reason.startswith("to ") for r in plan)


def test_devops_plan_with_a_workload_and_a_crash() -> None:
    plan = plan_evidence(ctx(workload="deployment/api", request="api keeps restarting since 10:00"))
    k8s = [r for r in plan if r.op.startswith("k8s.")]
    assert ops(k8s) == [
        "k8s.get",
        "k8s.get",
        "k8s.events",
        "k8s.describe",
        "k8s.rollout",
        "k8s.logs",
        "k8s.logs",
    ]
    describe, rollout, logs, previous = k8s[3:]
    assert (describe.kind, describe.name) == ("deployments", "api")
    assert rollout.deployment == "api"
    assert (logs.deployment, logs.tail, logs.previous) == ("api", 200, False)
    assert (previous.deployment, previous.previous) == ("api", True)


def test_previous_logs_only_when_a_crash_is_mentioned() -> None:
    plan = plan_evidence(ctx(workload="api", request="api is slow"))
    assert sum(r.op == "k8s.logs" for r in plan) == 1
    hinted = ctx(workload="api", hints=DecisionHints(grep=["OOMKilled"]))
    assert any(r.previous for r in plan_evidence(hinted))


def test_every_k8s_op_names_cluster_and_namespace_and_never_secrets() -> None:
    for workload in (
        "deployment/api",
        "pod/api-1",
        "statefulset/db",
        "api",
        "secret/db",
        "secrets/db",
        "configmap/cfg",
        "node/n1",
    ):
        plan = plan_evidence(ctx(workload=workload, request="crash"))
        for r in plan:
            if r.op.startswith("k8s."):
                assert r.cluster == "my-cluster" and r.namespace == "web"
            assert r.kind not in ("secrets", "secret", "nodes")
            assert "secret" not in (r.name or "") or r.kind is None
        assert not any(r.op == "k8s.describe" and r.kind == "configmaps" for r in plan)


def test_workload_parsing() -> None:
    assert parse_workload("deployment/api") == ("deployments", "api")
    assert parse_workload("deploy/api") == ("deployments", "api")
    assert parse_workload("sts/db") == ("statefulsets", "db")
    assert parse_workload("api") == ("deployments", "api")
    assert parse_workload("pod/api-1") == ("pods", "api-1")
    for bad in (None, "", "secret/db", "secrets/db", "configmap/x", "nodes/n1", "deployment/API!", "x/y"):
        assert parse_workload(bad) is None


def test_pod_workload_logs_by_pod_and_statefulset_has_no_logs() -> None:
    plan = plan_evidence(ctx(workload="pod/api-1"))
    logs = [r for r in plan if r.op == "k8s.logs"]
    assert [(r.pod, r.deployment) for r in logs] == [("api-1", None)]
    assert not any(r.op == "k8s.rollout" for r in plan)
    plan = plan_evidence(ctx(workload="statefulset/db"))
    assert [r.op for r in plan if r.op.startswith("k8s.")][-1] == "k8s.describe"


def test_cluster_not_in_workspace_plans_no_kubernetes() -> None:
    assert not any(op.startswith("k8s.") for op in ops(plan_evidence(ctx(cluster="other"))))


def test_devops_hints_are_limited_and_checked() -> None:
    hints = DecisionHints(
        grep=["OOMKilled", "x", "OOMKilled", "connection refused", "third one", "line\nbreak"],
        files=[
            "./src/app.py",
            ".env",
            "../etc/passwd",
            "/etc/shadow",
            "~/.ssh/id_rsa",
            "k8s/db-secret.yaml",
            "id_ed25519",
            "k8s/deploy.yaml",
            "src/a.py",
            "src/b.py",
        ],
    )
    plan = plan_evidence(ctx(hints=hints))
    assert [r.pattern for r in plan if r.op == "git.grep"] == ["OOMKilled", "connection refused"]
    assert [r.path for r in plan if r.op == "git.file"] == ["src/app.py", "k8s/deploy.yaml", "src/a.py"]


def test_secret_looking_grep_hint_is_skipped() -> None:
    hints = DecisionHints(grep=["password=" + "hunter" + "22", "timeout"])
    plan = plan_evidence(ctx("engineering-task", hints=hints))
    assert [r.pattern for r in plan if r.op == "git.grep"] == ["timeout"]


def test_prompt_optimization_plan_has_no_kubernetes() -> None:
    hints = DecisionHints(files=["a.py", "b.py", "c.py", "d.py"], grep=["foo"])
    plan = plan_evidence(ctx("prompt-optimization", workload="deployment/api", hints=hints))
    assert ops(plan) == ["git.status", "git.log", "git.tree", "git.file", "git.file", "git.file"]
    assert plan[1].limit == 10 and plan[2].depth == 2
    assert plan_evidence(ctx("prompt-optimization", repo=None)) == []


def test_engineering_plan() -> None:
    hints = DecisionHints(
        files=["a.py", "b.py", ".env.local", "c.py", "d.py", "e.py"], grep=["f1", "f2", "f3", "f4"]
    )
    plan = plan_evidence(ctx("engineering-task", hints=hints))
    assert ops(plan) == [
        "git.status",
        "git.log",
        "git.tree",
        "git.diffstat",
        *["git.file"] * 4,
        *["git.grep"] * 3,
    ]
    assert plan[1].limit == 15 and plan[2].depth == 2
    assert [r.path for r in plan if r.op == "git.file"] == ["a.py", "b.py", "c.py", "d.py"]
    assert not any(r.op.startswith("k8s.") for r in plan)
    assert plan_evidence(ctx("engineering-task", repo=None, hints=hints)) == []


def test_env_example_hint_is_allowed() -> None:
    plan = plan_evidence(ctx("engineering-task", hints=DecisionHints(files=[".env.example"])))
    assert [r.path for r in plan if r.op == "git.file"] == [".env.example"]


def test_plan_is_deterministic() -> None:
    c = ctx(workload="deployment/api", request="crash", hints=DecisionHints(grep=["a1"], files=["x.py"]))
    assert plan_evidence(c) == plan_evidence(c)


# ---- several namespaces, or all of them ---------------------------------------------------------


def ns_ctx(*, namespaces: list[str] | None = None, everything: bool = False, **kw) -> PlanContext:
    c = ctx(namespace=None, **kw)
    target = c.target.model_copy(update={"namespaces": namespaces or [], "all_namespaces": everything})
    return PlanContext(
        c.workflow, Target.model_validate(target.model_dump()), c.workspace, c.hints, c.request
    )


def item(req_id: str, output: str, ok: bool = True) -> EvidenceItem:
    return EvidenceItem(
        request_id=req_id, ok=ok, source="kubectl", collected_at="2026-10-28T10:00:00Z", output=output
    )


def test_one_namespace_plan_is_the_one_of_before() -> None:
    plan = plan_evidence(ctx(workload="deployment/api", request="it keeps crashing"))
    assert [(r.op, r.namespace) for r in plan if r.op.startswith("k8s.")] == [
        ("k8s.get", "web"),
        ("k8s.get", "web"),
        ("k8s.events", "web"),
        ("k8s.describe", "web"),
        ("k8s.rollout", "web"),
        ("k8s.logs", "web"),
        ("k8s.logs", "web"),
    ]
    assert plan[2].reason == "to see the recent warnings in the namespace"
    assert not any(r.all_namespaces for r in plan)


def test_several_namespaces_plan_one_listing_each_and_no_details_yet() -> None:
    plan = plan_evidence(ns_ctx(namespaces=["grimoire", "kubeai"], workload="deployment/api"))
    k8s = [(r.op, r.kind, r.namespace) for r in plan if r.op.startswith("k8s.")]
    assert k8s == [
        ("k8s.get", "deployments", "grimoire"),
        ("k8s.get", "pods", "grimoire"),
        ("k8s.events", None, "grimoire"),
        ("k8s.get", "deployments", "kubeai"),
        ("k8s.get", "pods", "kubeai"),
        ("k8s.events", None, "kubeai"),
    ]
    assert "in kubeai" in plan[3].reason and plan[5].reason == "to see the recent warnings in kubeai"
    assert [r.id for r in plan] == [f"e{i}" for i in range(1, len(plan) + 1)]


def test_several_namespaces_list_the_kind_of_another_workload() -> None:
    plan = plan_evidence(ns_ctx(namespaces=["web", "shop"], workload="statefulset/kafka"))
    assert [(r.kind, r.namespace) for r in plan if r.kind == "statefulsets"] == [
        ("statefulsets", "web"),
        ("statefulsets", "shop"),
    ]


def test_all_namespaces_plan() -> None:
    plan = plan_evidence(ns_ctx(everything=True, workload="deployment/api"))
    k8s = [r for r in plan if r.op.startswith("k8s.")]
    assert [(r.op, r.kind, r.name) for r in k8s] == [
        ("k8s.get", "deployments", None),
        ("k8s.get", "pods", None),
        ("k8s.events", None, None),
        ("k8s.get", "deployments", "api"),  # where is it?
    ]
    assert all(r.all_namespaces and r.namespace is None and r.cluster == "my-cluster" for r in k8s)
    assert required_questions(ns_ctx(everything=True)) == []
    assert required_questions(ns_ctx(namespaces=["web", "shop"])) == []


def test_namespace_question_says_how_to_give_several() -> None:
    (question,) = required_questions(ctx(namespace=None))
    assert "commas" in question.text and "*" in question.text


def test_followup_where_the_listing_shows_the_workload() -> None:
    c = ns_ctx(namespaces=["web", "shop"], workload="deployment/api", request="api keeps crashing")
    first = plan_evidence(c)
    listing = {
        "web": "NAME   READY   UP-TO-DATE\napi    0/1     1\n",
        "shop": "NAME     READY   UP-TO-DATE\ncart     1/1     1\n",
    }
    evidence = [
        (r, item(r.id, listing[r.namespace or ""] if r.kind == "deployments" else "x")) for r in first
    ]
    plan, note = plan_followup(c, evidence, len(first) + 1)
    assert note is None
    assert [(r.op, r.namespace) for r in plan] == [
        ("k8s.describe", "web"),
        ("k8s.rollout", "web"),
        ("k8s.logs", "web"),
        ("k8s.logs", "web"),
    ]
    assert plan[0].id == f"e{len(first) + 1}" and "in web" in plan[0].reason


def test_followup_across_all_namespaces_is_capped() -> None:
    c = ns_ctx(everything=True, workload="deployment/api")
    first = plan_evidence(c)
    rows = "".join(f"ns-{i}   api   1/1   1   1   2d\n" for i in range(5))
    evidence = [
        (r, item(r.id, "NAMESPACE   NAME   READY\n" + rows if r.name == "api" else "")) for r in first
    ]
    plan, note = plan_followup(c, evidence, 20)
    assert sorted({r.namespace for r in plan}) == ["ns-0", "ns-1", "ns-2"]
    assert note is not None and "5 namespaces" in note and "ns-0, ns-1, ns-2" in note
    assert all(not r.all_namespaces for r in plan)


def test_followup_when_the_workload_is_nowhere() -> None:
    c = ns_ctx(everything=True, workload="deployment/api")
    first = plan_evidence(c)
    evidence = [(r, item(r.id, "No resources found")) for r in first]
    plan, note = plan_followup(c, evidence, 9)
    assert plan == [] and note is not None and "not found in any namespace" in note


def test_no_followup_for_one_namespace_a_failed_listing_or_no_workload() -> None:
    one = ctx(workload="deployment/api")
    assert plan_followup(one, [], 9) == ([], None)
    c = ns_ctx(namespaces=["web", "shop"], workload="deployment/api")
    failed = [(r, item(r.id, "NAME\napi\n", ok=False)) for r in plan_evidence(c)]
    assert plan_followup(c, failed, 9)[0] == []
    assert plan_followup(ns_ctx(namespaces=["web", "shop"]), [], 9) == ([], None)
