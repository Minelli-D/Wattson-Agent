import json
import stat
from pathlib import Path

import pytest

from wattson.connector import kube
from wattson.connector.run import Refused
from wattson.shared.contracts import EvidenceRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_VERBS = {
    "apply",
    "create",
    "delete",
    "patch",
    "edit",
    "replace",
    "scale",
    "annotate",
    "label",
    "exec",
    "cp",
    "port-forward",
    "drain",
    "cordon",
    "uncordon",
    "taint",
    "set",
    "run",
    "expose",
    "attach",
    "debug",
    "proxy",
}


def subcommand(args: list[str]) -> str:
    """The kubectl command after the global flags."""
    i = 0
    while i < len(args):
        if args[i] in ("--kubeconfig", "--context", "-n"):
            i += 2
        elif args[i].startswith("--request-timeout"):
            i += 1
        else:
            return args[i]
    return ""


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(kube, "SLEEP", lambda s: None)


def test_manifest_file_and_embedded_copy_are_equal():
    assert (REPO_ROOT / "k8s" / "reader.yaml").read_text() == kube.READER_MANIFEST


def test_manifest_is_read_only():
    m = kube.READER_MANIFEST
    assert "\nkind: Namespace\n" not in m  # it goes into an existing namespace, never a new one
    assert m.count("namespace: __NAMESPACE__") == 4
    filled = kube.reader_manifest("grimoire")
    assert filled.count("namespace: grimoire") == 4 and "__NAMESPACE__" not in filled
    with pytest.raises(kube.KubeError, match="not a valid namespace"):
        kube.reader_manifest("Bad_Name")
    for kind in ("ServiceAccount", "Secret", "ClusterRoleBinding"):
        assert f"\nkind: {kind}\n" in m
    assert "type: kubernetes.io/service-account-token" in m
    assert "kubernetes.io/service-account.name: wattson-reader" in m
    assert "  kind: ClusterRole\n  name: view\n" in m
    assert "kind: Role\n" not in m and "cluster-admin" not in m
    # Exactly one own rule set: read-only on nodes, which "view" does not cover.
    assert m.count("kind: ClusterRole\nmetadata") == 1 and m.count("rules:") == 1
    rules = m.split("rules:", 1)[1].split("---", 1)[0]
    assert 'resources: ["nodes"]' in rules
    assert 'verbs: ["get", "list", "watch"]' in rules
    for verb in ("create", "update", "patch", "delete", "escalate", "bind", "impersonate", "*"):
        assert f'"{verb}"' not in rules
    assert "automountServiceAccountToken: false" in m


def test_list_contexts(kubectl_calls, monkeypatch: pytest.MonkeyPatch):
    assert kube.list_contexts() == (["dev", "prod"], "dev")
    monkeypatch.setenv("FAKE_KUBE_CURRENT", "")
    assert kube.list_contexts() == (["dev", "prod"], None)
    assert all(c["args"][0] == "config" for c in kubectl_calls())


def test_list_contexts_without_kubectl(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WATTSON_KUBECTL_BIN", "/nonexistent/kubectl")
    assert kube.list_contexts() == ([], None)


def test_reader_paths(kubectl_calls, tmp_path: Path):
    assert kube.reader_kubeconfig_path("dev") == tmp_path / "config" / "kube" / "dev.yaml"
    a = kube.reader_kubeconfig_path("arn:aws:eks:eu-north-1:1:cluster/x")
    b = kube.reader_kubeconfig_path("arn:aws:eks:eu-north-1:1:cluster:x")
    assert a.parent == b.parent == tmp_path / "config" / "kube"
    assert a != b and "/" not in a.name and ":" not in a.name
    assert kube.reader_kubeconfig_path("../../etc").parent == tmp_path / "config" / "kube"


def test_add_reader_does_nothing_on_no(kubectl_calls):
    logs: list[str] = []
    questions: list[str] = []

    def no(q: str) -> bool:
        questions.append(q)
        return False

    kube.add_reader("dev", no, logs.append, namespace="team")
    assert len(questions) == 1
    assert any("kind: ClusterRoleBinding" in line for line in logs)  # the manifest was shown
    assert logs[-1] == "Nothing was changed."
    assert [c["args"][:2] for c in kubectl_calls()] == [
        ["config", "get-contexts"],
        ["config", "current-context"],
        ["--context", "dev"],  # only the check that the namespace exists
    ]
    assert kubectl_calls()[-1]["args"][-4:] == ["namespace", "team", "-o", "name"]
    assert not kube.reader_kubeconfig_path("dev").exists()
    assert kube.reader_contexts() == []


def test_add_reader_after_yes(kubectl_calls):
    logs: list[str] = []
    kube.add_reader("dev", lambda q: True, logs.append, namespace="team")

    calls = kubectl_calls()
    applies = [c for c in calls if "apply" in c["args"]]
    assert len(applies) == 1
    assert applies[0]["args"] == ["--context", "dev", "--request-timeout=15s", "apply", "-f", "-"]
    assert applies[0]["stdin"] == kube.reader_manifest("team")
    assert "namespace: team" in applies[0]["stdin"]
    token_reads = [c["args"] for c in calls if c["args"][-4:-2] == ["secret", "wattson-reader-token"]]
    assert token_reads and all(["-n", "team"] == a[3:5] for a in token_reads)
    # The only write in any call is that one apply ("auth can-i delete pods" only asks).
    for c in calls:
        if c is not applies[0]:
            assert subcommand(c["args"]) not in WRITE_VERBS, c["args"]

    path = kube.reader_kubeconfig_path("dev")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    cfg = json.loads(path.read_text())
    assert cfg["current-context"] == "dev"
    assert cfg["clusters"][0]["cluster"]["server"] == "https://dev.example.test:6443"
    assert cfg["clusters"][0]["cluster"]["certificate-authority-data"]
    assert cfg["users"][0]["user"] == {"token": "fake-reader-token-0123456789abcdef"}

    text = "\n".join(logs)
    assert "can-i get pods: yes" in text
    assert "can-i get secrets: no" in text
    assert "can-i delete pods: no" in text
    assert "fake-reader-token" not in text and "ADMIN-token" not in text
    assert "ADMIN-token" not in path.read_text()
    assert kube.reader_contexts() == ["dev"]

    can_i = [c["args"] for c in calls if "can-i" in c["args"]]
    assert len(can_i) == 3 and all(a[:2] == ["--kubeconfig", str(path)] for a in can_i)


def test_add_reader_removes_a_reader_that_can_read_secrets(kubectl_calls, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_KUBE_CAN_I_SECRETS", "yes")
    logs: list[str] = []
    with pytest.raises(kube.KubeError, match="get secrets is 'yes'"):
        kube.add_reader("dev", lambda q: True, logs.append, namespace="team")
    assert not kube.reader_kubeconfig_path("dev").exists()
    assert "can-i get secrets: yes" in logs


def test_add_reader_unknown_context(kubectl_calls):
    with pytest.raises(kube.KubeError, match="no kube context"):
        kube.add_reader("staging", lambda q: True, lambda s: None, namespace="team")
    assert not any("apply" in c["args"] for c in kubectl_calls())


def test_add_reader_apply_fails(kubectl_calls, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_KUBE_APPLY_FAIL", "1")
    with pytest.raises(kube.KubeError, match="kubectl apply failed.*Forbidden"):
        kube.add_reader("dev", lambda q: True, lambda s: None, namespace="team")
    assert not kube.reader_kubeconfig_path("dev").exists()


def test_add_reader_refuses_a_namespace_that_does_not_exist(kubectl_calls):
    asked: list[str] = []
    with pytest.raises(kube.KubeError, match="namespace 'missing' was not found.*never creates"):
        kube.add_reader("dev", lambda q: asked.append(q) or True, lambda s: None, namespace="missing")
    assert asked == []  # stopped before the question
    assert not any("apply" in c["args"] for c in kubectl_calls())


def req(**kw) -> EvidenceRequest:
    kw.setdefault("id", "e1")
    kw.setdefault("reason", "test")
    kw.setdefault("cluster", "dev")
    return EvidenceRequest(**kw)


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        (req(op="k8s.get", kind="pods", namespace="web"), ["get", "pods", "-n", "web", "-o", "wide"]),
        (
            req(op="k8s.get", kind="deploy", namespace="web", name="api"),
            ["get", "deployments", "api", "-n", "web", "-o", "wide"],
        ),
        (req(op="k8s.get", kind="nodes", namespace="web"), ["get", "nodes", "-o", "wide"]),
        (
            req(op="k8s.get", kind="configmaps", namespace="web"),
            ["get", "configmaps", "-n", "web", "-o", "wide"],
        ),
        (
            req(op="k8s.describe", kind="pods", namespace="web", name="api-1"),
            ["describe", "pods", "api-1", "-n", "web"],
        ),
        (req(op="k8s.describe", kind="nodes", name="node-1"), ["describe", "nodes", "node-1"]),
        (req(op="k8s.events", namespace="web"), ["get", "events", "-n", "web", "--sort-by=.lastTimestamp"]),
        (
            req(op="k8s.logs", namespace="web", deployment="api"),
            ["logs", "deploy/api", "-n", "web", "--tail=200", "--timestamps"],
        ),
        (
            req(op="k8s.logs", namespace="web", pod="api-1", container="app", previous=True, tail=50),
            ["logs", "api-1", "-n", "web", "--tail=50", "--timestamps", "--previous", "-c", "app"],
        ),
        (
            req(op="k8s.rollout", namespace="web", deployment="api"),
            ["rollout", "status", "deployment/api", "-n", "web", "--watch=false"],
        ),
    ],
)
def test_op_args(r: EvidenceRequest, expected: list[str]):
    args = kube.k8s_op_args(r)
    assert args == expected
    assert not WRITE_VERBS & set(args)
    assert not any("secret" in a.lower() for a in args)
    assert "-o" not in args or args[args.index("-o") + 1] == "wide"  # never yaml/json


def test_command_uses_the_reader_kubeconfig(kubectl_calls):
    reader = kube.reader_kubeconfig_path("dev")
    argv, source = kube.k8s_command(req(op="k8s.get", kind="pods", namespace="web"), reader)
    assert argv[1:6] == ["--kubeconfig", str(reader), "--context", "dev", "--request-timeout=15s"]
    assert source == "kubectl --context dev get pods -n web -o wide"
    assert str(reader) not in source


def test_command_with_the_users_own_kubeconfig(kubectl_calls):
    argv, source = kube.k8s_command(req(op="k8s.get", kind="pods", namespace="web"), None)
    assert "--kubeconfig" not in argv
    assert argv[1:4] == ["--context", "dev", "--request-timeout=15s"]
    assert source.endswith("(your own kubeconfig)")


@pytest.mark.parametrize(
    ("r", "why"),
    [
        (req(op="k8s.get", kind="secrets", namespace="web"), "Secrets"),
        (req(op="k8s.get", kind="Secret", namespace="web"), "Secrets"),
        (req(op="k8s.get", kind="pods,secrets", namespace="web"), "Secrets"),
        (req(op="k8s.get", kind="all", namespace="web"), "not allowed"),
        (req(op="k8s.describe", kind="configmaps", namespace="web", name="app"), "configmap"),
        (req(op="k8s.describe", kind="pods", namespace="web"), "name is needed"),
        (req(op="k8s.get", kind="pods"), "namespace is needed"),
        (req(op="k8s.get", kind="pods", namespace="Web"), "not a valid"),
        (req(op="k8s.get", kind="pods", namespace="web;rm"), "not a valid"),
        (req(op="k8s.get", kind="pods", namespace="web", name="--all"), "not a valid"),
        (req(op="k8s.logs", namespace="web"), "exactly one"),
        (req(op="k8s.logs", namespace="web", pod="a", deployment="b"), "exactly one"),
        (req(op="k8s.logs", namespace="web", pod="a", tail=501), "tail"),
        (req(op="k8s.logs", namespace="web", pod="a", tail=0), "tail"),
        (req(op="k8s.logs", namespace="web", pod="a", container="A_B"), "not a valid"),
        (req(op="k8s.rollout", namespace="web"), "deployment is needed"),
    ],
)
def test_op_refusals(r: EvidenceRequest, why: str):
    with pytest.raises(Refused, match=why):
        kube.k8s_op_args(r)


# ---- all namespaces -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        (req(op="k8s.get", kind="pods", all_namespaces=True), ["get", "pods", "--all-namespaces"]),
        (
            req(op="k8s.get", kind="deploy", name="api", all_namespaces=True),
            ["get", "deployments", "--all-namespaces", "--field-selector", "metadata.name=api"],
        ),
        (req(op="k8s.get", kind="nodes", all_namespaces=True), ["get", "nodes"]),
        (
            req(op="k8s.events", all_namespaces=True),
            [
                "get",
                "events",
                "--all-namespaces",
                "--field-selector",
                "type=Warning",
                "--sort-by=.lastTimestamp",
            ],
        ),
    ],
)
def test_all_namespaces_args(r: EvidenceRequest, expected: list[str]):
    args = kube.k8s_op_args(r)
    assert args == expected
    assert not WRITE_VERBS & set(args) and "-o" not in args


@pytest.mark.parametrize(
    ("r", "why"),
    [
        (req(op="k8s.get", kind="secrets", all_namespaces=True), "Secrets"),
        (req(op="k8s.get", kind="pods", namespace="web", all_namespaces=True), "do not go together"),
        (req(op="k8s.events", namespace="web", all_namespaces=True), "do not go together"),
        (req(op="k8s.describe", kind="pods", name="api-1", all_namespaces=True), "needs one namespace"),
        (req(op="k8s.logs", deployment="api", all_namespaces=True), "needs one namespace"),
        (req(op="k8s.rollout", deployment="api", all_namespaces=True), "needs one namespace"),
        (req(op="k8s.get", kind="pods", name="a,b", all_namespaces=True), "not a valid"),
    ],
)
def test_all_namespaces_refusals(r: EvidenceRequest, why: str):
    with pytest.raises(Refused, match=why):
        kube.k8s_op_args(r)


def test_all_namespaces_source_line(kubectl_calls):
    _, source = kube.k8s_command(
        req(op="k8s.get", kind="pods", all_namespaces=True), kube.reader_kubeconfig_path("dev")
    )
    assert source == "kubectl --context dev get pods --all-namespaces"


def test_all_namespaces_listing_puts_problems_first_and_cuts_with_a_note(kubectl_calls):
    reader = kube.reader_kubeconfig_path("dev")
    kube._write_private(reader, '{"users": [{"user": {"token": "fake-reader-token-0123456789abcdef"}}]}')
    op = kube.run_k8s_op(req(op="k8s.get", kind="pods", all_namespaces=True), reader)
    lines = op.output.splitlines()
    assert op.ok and op.truncated
    assert lines[0].startswith("NAMESPACE")
    # the crash loop and the restart 45 s ago come first, though they are the last rows
    assert "CrashLoopBackOff" in lines[1] and "worker-" in lines[2]
    assert (
        lines[-1].startswith("[cut: ") and "of 302 rows" in lines[-1] and "the 2 with a problem" in lines[-1]
    )
    assert len(op.output.encode()) <= kube.ALL_NAMESPACES_MAX_BYTES + 200


def test_problems_first_keeps_a_small_table_whole():
    text = (
        "NAMESPACE   NAME     READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "shop        cart     1/1     1            1           2d\n"
        "web         api      0/2     2            0           9d\n"
    )
    out, cut = kube.problems_first(text)
    assert not cut and out.splitlines()[1].split()[:2] == ["web", "api"] and len(out.splitlines()) == 3
    assert kube.problems_first("No resources found\n") == ("No resources found\n", False)


def test_completed_pods_and_old_restarts_are_not_problems():
    header = "NAMESPACE   NAME   READY   STATUS      RESTARTS        AGE"
    cols = kube._columns(header)
    assert not kube._has_problem("batch       job1   0/1     Completed   0               1d", cols)
    assert not kube._has_problem("web         api    1/1     Running     1 (12d ago)     20d", cols)
    assert kube._has_problem("web         api    1/1     Running     4 (2m30s ago)   20d", cols)
    assert kube._has_problem("web         api    0/1     Running     0               20d", cols)
    assert kube._has_problem("web         api    0/1     Pending     0               20d", cols)


def test_missing_namespaces(kubectl_calls, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_KUBE_MISSING_NS", "kubeia")
    assert kube.missing_namespaces("dev", ["web", "kubeia", "missing"], None) == (["kubeia", "missing"], None)
    call = kubectl_calls()[-1]["args"]
    assert subcommand(call) == "get" and not WRITE_VERBS & set(call)
    get = ["get", "namespaces", "web", "kubeia", "missing", "-o", "name", "--ignore-not-found"]
    assert call[call.index("get") :] == get
    assert kube.missing_namespaces("nope", ["web"], None)[1] is not None  # an unknown context: not checked
    with pytest.raises(Refused, match="not a valid"):
        kube.missing_namespaces("dev", ["Bad_NS"], None)
