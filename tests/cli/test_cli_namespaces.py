"""`ask` with several namespaces or all of them: the flags, the checks, the shape sent, and how the
review and the task list name them."""

from __future__ import annotations

import argparse

import pytest

from wattson.cli.commands import ask
from wattson.cli.commands.common import UsageError
from wattson.cli.commands.tasks import _target
from wattson.cli.review import _target_text
from wattson.shared.contracts import (
    COLLECT_EVIDENCE,
    ApproveReason,
    EvidenceRequest,
    Proposal,
    Target,
    TaskRecord,
)

ASK = ["ask", "why does deployment/api restart?", "--new-session", "--headless"]
ANSWERS = ["2", "web", "y", "a", "yes"]


def parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ask.add_parser(parser.add_subparsers())
    return parser.parse_args(["ask", "x", *argv])


# ---- the flags ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "names"),
    [
        ([], []),
        (["--namespace", "web"], ["web"]),
        (["--namespace", "web,shop", "--namespace", "kubeai"], ["web", "shop", "kubeai"]),
        (["--namespace", " web , shop,web "], ["web", "shop"]),
    ],
)
def test_namespace_flag_repeated_or_with_commas(argv: list[str], names: list[str]) -> None:
    assert ask.namespaces_of(parsed(*argv)) == names


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["--namespace", "web,Kube_AI"], "'Kube_AI' is not a namespace name"),
        (["--namespace", ","], "needs a name"),
        (["--namespace", "web", "-A"], "do not go together"),
        (["--namespace", "a,b,c,d,e,f"], "At most 5 namespaces (you gave 6)"),
    ],
)
def test_namespace_flag_problems(argv: list[str], says: str) -> None:
    with pytest.raises(UsageError) as err:
        ask.namespaces_of(parsed(*argv))
    assert says in str(err.value)
    if "At most" in says:
        assert "--all-namespaces (-A)" in str(err.value)


def test_all_namespaces_flag() -> None:
    assert parsed("-A").all_namespaces and parsed("--all-namespaces").all_namespaces
    assert ask.namespaces_of(parsed("-A")) == []


# ---- what is sent ---------------------------------------------------------------------------------


def test_no_flag_sends_the_shape_of_before(env, cli) -> None:
    r = cli(ASK, ANSWERS)
    assert r.code == 0, r.out
    sent = env.script.started[0].overrides.model_dump(mode="json")
    assert (
        set(sent) == {"workflow", "cluster", "namespace", "workload", "procedure"}
        and sent["namespace"] is None
    )


def test_one_namespace_sends_the_old_field(env, cli) -> None:
    r = cli([*ASK, "--cluster", "my-cluster", "--namespace", "web"], ANSWERS)
    assert r.code == 0, r.out
    sent = env.script.started[0].overrides.model_dump(mode="json")
    assert sent["namespace"] == "web" and "namespaces" not in sent and "all_namespaces" not in sent
    assert "Namespace (yours)" in r.out


def test_several_namespaces_are_sent_and_shown(env, cli) -> None:
    r = cli([*ASK, "--cluster", "my-cluster", "--namespace", "grimoire,kubeai"], ANSWERS)
    assert r.code == 0, r.out
    overrides = env.script.started[0].overrides
    assert overrides.namespaces == ["grimoire", "kubeai"] and overrides.namespace is None
    assert "Namespaces (yours)" in r.out and "grimoire, kubeai" in r.out
    checks = [c for c in env.kubectl_log.read_text().splitlines() if "--ignore-not-found" in c]
    # At the start (--cluster given); then again for the plan's target (here my-cluster/web).
    assert '"namespaces", "grimoire", "kubeai", "-o", "name"' in checks[0]


def test_all_namespaces_is_sent(env, cli) -> None:
    r = cli([*ASK, "-A"], ANSWERS)
    assert r.code == 0, r.out
    assert env.script.started[0].overrides.all_namespaces
    assert "Namespaces (yours)" in r.out and " all" in r.out


def test_an_unknown_namespace_is_named_and_nothing_is_sent(env, cli, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_KUBECTL_MISSING_NS", "kubeia")
    r = cli([*ASK, "--cluster", "my-cluster", "--namespace", "grimoire,kubeia"], ANSWERS)
    assert r.code == 2
    assert "There is no namespace 'kubeia' in context 'my-cluster'" in r.out
    assert env.script.started == []


def test_usage_errors_send_nothing(env, cli) -> None:
    assert cli([*ASK, "--namespace", "web", "--all-namespaces"]).code == 2
    assert cli([*ASK, "--namespace", "a,b,c,d,e,f"]).code == 2
    assert cli([*ASK, "--namespace", "NOT_OK"]).code == 2
    assert env.script.started == []


# ---- at the evidence step -----------------------------------------------------------------------


def test_plan_items_in_a_missing_namespace_are_refused(env, cli, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_KUBECTL_MISSING_NS", "kubeia")
    env.script.target = Target(repo="demo", cluster="my-cluster", namespaces=["web", "kubeia"])
    env.script.plan = [
        EvidenceRequest(
            id="e1", op="k8s.get", cluster="my-cluster", namespace="web", kind="pods", reason="a"
        ),
        EvidenceRequest(
            id="e2", op="k8s.get", cluster="my-cluster", namespace="kubeia", kind="pods", reason="b"
        ),
    ]
    r = cli(ASK, ANSWERS)
    assert r.code == 0, r.out
    assert "Not found in context 'my-cluster': namespace 'kubeia'" in r.out
    assert "Namespaces" in r.out and "web, kubeia" in r.out
    items = {i.request_id: i for i in env.script.received[COLLECT_EVIDENCE][0].items}
    assert (
        items["e1"].ok and items["e2"].refused == "namespace 'kubeia' was not found in context 'my-cluster'"
    )


def test_all_namespaces_items_are_described() -> None:
    req = EvidenceRequest(
        id="e1", op="k8s.get", cluster="my-cluster", kind="pods", all_namespaces=True, reason="r"
    )
    assert ask.describe_request(req) == "k8s.get (cluster my-cluster, all namespaces, kind pods)"


# ---- the review and the task list ---------------------------------------------------------------


def review_target(target: Target) -> str:
    proposal = Proposal(
        prompt="p",
        prompt_sha256="0" * 64,
        template="t",
        template_version="2",
        sources=[],
        evidence=[],
        constraints=[],
        missing_info=[],
        redactions=0,
    )
    reason = ApproveReason(
        task_id="t-1", workflow="devops-investigation", classification=None, target=target, proposal=proposal
    )
    return _target_text(reason)


def test_review_names_every_namespace() -> None:
    assert review_target(Target(cluster="my-cluster", namespace="web")) == "cluster my-cluster, namespace web"
    several = Target(cluster="my-cluster", namespaces=["grimoire", "kubeai"])
    assert review_target(several) == "cluster my-cluster, namespaces grimoire, kubeai"
    assert (
        review_target(Target(cluster="my-cluster", all_namespaces=True))
        == "cluster my-cluster, all namespaces"
    )


def test_task_list_target() -> None:
    def rec(**kw) -> TaskRecord:
        return TaskRecord(id="t", created_at="x", updated_at="x", status="completed", repo="shop", **kw)

    assert _target(rec(cluster="my-cluster", namespace="web")) == "shop, my-cluster/web"
    assert (
        _target(rec(cluster="my-cluster", namespaces=["grimoire", "kubeai"]))
        == "shop, my-cluster/{grimoire,kubeai}"
    )
    assert _target(rec(cluster="my-cluster", all_namespaces=True)) == "shop, my-cluster/*"
    assert _target(rec()) == "shop"
