"""Keyword rules: classification and hint extraction."""

from __future__ import annotations

import pytest

from wattson.agents.triage import rules
from wattson.shared.contracts import WorkspaceInfo

WS = WorkspaceInfo(cwd_name="shop", kube_contexts=["k8s-lab", "prod-eu", "kind-dev"])


@pytest.mark.parametrize(
    ("text", "category", "strength"),
    [
        ("Rewrite this prompt for Claude: fix the crashloop in payments", "prompt-optimization", "strong"),
        ("improve my prompt, it is too long", "prompt-optimization", "strong"),
        (
            "pods in payments are in CrashLoopBackOff since the deploy, check why",
            "devops-investigation",
            "strong",
        ),
        ("ingress gives 502 bad gateway on prod-eu", "devops-investigation", "strong"),
        ("add pagination to the orders endpoint and write unit tests", "engineering-task", "strong"),
        ("refactor src/lib/dates.ts", "engineering-task", "medium"),
        ("help", "unclear", "medium"),
        ("it's broken again", "unclear", None),
    ],
)
def test_classify(text: str, category: str, strength: str | None) -> None:
    verdict = rules.classify(text)
    assert verdict.category == category
    if strength is not None:
        assert verdict.strength == strength
    if category != "unclear":
        assert verdict.confidence == rules.CONFIDENCE[verdict.strength]


def test_weak_rules_say_unclear_but_keep_scores() -> None:
    verdict = rules.classify("the thing with the service from yesterday")
    assert verdict.category == "unclear"
    assert verdict.strength in ("weak", "medium")
    assert set(verdict.scores) == {
        "prompt-optimization",
        "devops-investigation",
        "engineering-task",
        "unclear",
    }


@pytest.mark.parametrize(
    ("text", "namespace"),
    [
        ("kubectl get pods -n payments", "payments"),
        ("kubectl get pods --namespace=web", "web"),
        ("look in namespace monitoring", "monitoring"),
        ("ns cert-manager has a problem", "cert-manager"),
        ("the pods in the data namespace", "data"),
        ("which namespace is it?", None),
        ("dns is slow", None),
    ],
)
def test_namespace(text: str, namespace: str | None) -> None:
    assert rules.find_namespace(text) == namespace


@pytest.mark.parametrize(
    ("text", "workload"),
    [
        ("rollout of deployment/checkout is stuck", "deployment/checkout"),
        ("deploy notifier to staging", "deployment/notifier"),
        ("pod api-7d9f8-x2k keeps restarting", "pod/api-7d9f8-x2k"),
        ("statefulset postgres has a pending pvc", "statefulset/postgres"),
        ("sts/redis is not ready", "statefulset/redis"),
        ("the ledger deployment is slow", "deployment/ledger"),
        ("the pod keeps restarting", None),
        ("the orders pods restart", None),
        ("migrate it to a k8s cronjob", None),
    ],
)
def test_workload(text: str, workload: str | None) -> None:
    assert rules.find_workload(text) == workload


def test_cluster_only_from_workspace() -> None:
    assert rules.find_cluster("pods on prod-eu crash", WS.kube_contexts) == "prod-eu"
    assert rules.find_cluster("kubectl --context kind-dev get pods", WS.kube_contexts) == "kind-dev"
    assert rules.find_cluster("pods on prod-us crash", WS.kube_contexts) is None
    assert rules.find_cluster("pods on prod-eu-2 crash", WS.kube_contexts) is None


def test_grep_quotes_and_errors() -> None:
    text = 'helm says "Error: UPGRADE FAILED: another operation is in progress" and pods are OOMKilled'
    grep = rules.find_grep(text)
    assert grep[0] == "Error: UPGRADE FAILED: another operation is in progress"
    assert "OOMKilled" in grep
    assert rules.find_grep("x509: certificate has expired or is not yet valid") == [
        "x509: certificate has expired or is not yet valid"
    ]
    assert rules.find_grep("it's the user's problem, don't worry") == []


def test_files() -> None:
    text = "change ./src/app/page.tsx and charts/api/values.yaml, see ../secret.env and Dockerfile; next.js"
    assert rules.find_files(text) == ["src/app/page.tsx", "charts/api/values.yaml", "Dockerfile"]
    assert rules.find_files("see https://example.com/a.py") == []


def test_tags_and_queries() -> None:
    hints = rules.extract_hints("cert-manager certificate not ready on k8s-lab -n web, ingress 502", WS)
    assert {"kubernetes", "cert-manager", "tls", "ingress"} <= set(hints.tags)
    assert (hints.cluster, hints.namespace) == ("k8s-lab", "web")
    assert 1 <= len(hints.search_queries) <= 3
    assert all(len(q) <= 80 for q in hints.search_queries)
