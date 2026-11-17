import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from wattson.connector import evidence, kube
from wattson.connector.evidence import EvidenceContext, cap_bytes, check_allowed, run_evidence
from wattson.shared.contracts import EvidenceItem, EvidenceRequest


def req(op: str, **kw) -> EvidenceRequest:
    kw.setdefault("id", "e1")
    kw.setdefault("reason", "test")
    return EvidenceRequest(op=op, **kw)


CTX = EvidenceContext(repo_root=Path("/nonexistent/repo"), contexts=["dev", "prod"], reader_contexts=["dev"])
NO_REPO = EvidenceContext(repo_root=None, contexts=["dev"], reader_contexts=["dev"])
ADMIN = EvidenceContext(
    repo_root=None, contexts=["dev", "prod"], reader_contexts=["dev"], allow_admin_kubeconfig=True
)


@pytest.mark.parametrize(
    ("r", "ctx", "refusal"),
    [
        (req("git.status"), CTX, None),
        (req("git.log", limit=50), CTX, None),
        (req("git.tree", depth=3), CTX, None),
        (req("git.file", path="src/app.py"), CTX, None),
        (req("git.grep", pattern="DATABASE_URL"), CTX, None),
        (req("git.status"), NO_REPO, "no git repository"),
        (req("git.log", limit=0), CTX, "limit"),
        (req("git.log", limit=51), CTX, "limit"),
        (req("git.tree", depth=0), CTX, "depth"),
        (req("git.tree", depth=7), CTX, "depth"),
        (req("git.file", path=".env"), CTX, "secret"),
        (req("git.file", path="deploy/id_rsa"), CTX, "secret"),
        (req("git.file", path="../x"), CTX, "'..'"),
        (req("git.file", path="/etc/passwd"), CTX, "relative"),
        (req("git.file"), CTX, "path is needed"),
        (req("git.grep", pattern="x" * 201), CTX, "200"),
        (req("git.grep"), CTX, "search text"),
        (req("k8s.get", cluster="dev", kind="pods", namespace="web"), CTX, None),
        (req("k8s.get", cluster="dev", kind="nodes"), CTX, None),
        (req("k8s.logs", cluster="dev", namespace="web", deployment="api", tail=500), CTX, None),
        (req("k8s.get", cluster="dev", kind="secrets", namespace="web"), CTX, "Secrets"),
        (req("k8s.describe", cluster="dev", kind="secret", namespace="web", name="db"), CTX, "Secrets"),
        (
            req("k8s.describe", cluster="dev", kind="configmaps", namespace="web", name="app"),
            CTX,
            "configmap",
        ),
        (req("k8s.get", cluster="dev", kind="namespaces"), CTX, "not allowed"),
        (req("k8s.get", kind="pods", namespace="web"), CTX, "cluster"),
        (
            req("k8s.get", cluster="staging", kind="pods", namespace="web"),
            CTX,
            "not one of your kube contexts",
        ),
        (req("k8s.get", cluster="prod", kind="pods", namespace="web"), CTX, "no read-only kubeconfig"),
        (req("k8s.get", cluster="prod", kind="pods", namespace="web"), ADMIN, None),
        (
            req("k8s.get", cluster="staging", kind="pods", namespace="web"),
            ADMIN,
            "not one of your kube contexts",
        ),
        (req("k8s.get", cluster="dev", kind="pods", namespace="Bad_NS"), CTX, "not a valid"),
        (req("k8s.logs", cluster="dev", namespace="web", pod="a", tail=1000), CTX, "tail"),
        (req("k8s.events", cluster="dev"), CTX, "namespace is needed"),
    ],
)
def test_check_allowed(r: EvidenceRequest, ctx: EvidenceContext, refusal: str | None):
    got = check_allowed(r, ctx)
    if refusal is None:
        assert got is None
    else:
        assert got is not None and refusal in got


def test_unknown_op_is_refused():
    r = EvidenceRequest.model_construct(id="e1", op="k8s.exec", reason="x", cluster="dev")
    assert "unknown operation" in check_allowed(r, CTX)


@pytest.fixture
def kctx(kubectl_calls) -> EvidenceContext:
    """dev has a reader kubeconfig (written like add_reader does); prod has none."""
    path = kube.reader_kubeconfig_path("dev")
    kube._write_private(
        path,
        '{"current-context": "dev", "users": [{"user": {"token": "fake-reader-token-0123456789abcdef"}}]}',
    )
    return EvidenceContext(repo_root=None, contexts=["dev", "prod"], reader_contexts=kube.reader_contexts())


def by_id(items: list[EvidenceItem]) -> dict[str, EvidenceItem]:
    return {i.request_id: i for i in items}


def test_k8s_evidence(kctx: EvidenceContext, kubectl_calls):
    reqs = [
        req("k8s.get", id="get", cluster="dev", kind="pods", namespace="web"),
        req("k8s.logs", id="logs", cluster="dev", namespace="web", deployment="api"),
        req("k8s.describe", id="desc", cluster="dev", kind="pods", namespace="web", name="api-1"),
        req("k8s.events", id="events", cluster="dev", namespace="web"),
        req("k8s.rollout", id="rollout", cluster="dev", namespace="web", deployment="api"),
        req("k8s.get", id="broken", cluster="dev", kind="pods", namespace="broken"),
        req("k8s.get", id="empty", cluster="dev", kind="pods", namespace="empty"),
    ]
    items = run_evidence(reqs, kctx)
    assert [i.request_id for i in items] == [r.id for r in reqs]  # order kept
    got = by_id(items)

    assert got["get"].ok and "CrashLoopBackOff" in got["get"].output
    assert got["get"].source == "kubectl --context dev get pods -n web -o wide"
    datetime.fromisoformat(got["get"].collected_at)

    logs = got["logs"]
    assert logs.ok and logs.redactions >= 1
    assert "fakeBearer0123456789abcdefXYZ" not in logs.output and "[REDACTED:" in logs.output
    assert "connection refused" in logs.output

    assert "s3cr3t-Value-123" not in got["desc"].output and got["desc"].redactions >= 1

    ev = got["events"].output.splitlines()
    assert got["events"].truncated and len(ev) == 51 and ev[0].startswith("LAST SEEN")
    assert "pod/api-059" in ev[-1] and "pod/api-009 " not in got["events"].output

    assert got["rollout"].ok and "Waiting for deployment" in got["rollout"].output

    assert not got["broken"].ok and "NotFound" in got["broken"].error and got["broken"].refused is None

    assert got["empty"].ok and got["empty"].output.startswith("No resources found")

    for c in kubectl_calls():
        assert c["args"][:2] == ["--kubeconfig", str(kube.reader_kubeconfig_path("dev"))]
        assert "--request-timeout=15s" in c["args"]
        assert not any("secret" in a.lower() for a in c["args"])


def test_refused_requests_run_nothing(kctx: EvidenceContext, kubectl_calls):
    reqs = [
        req("k8s.get", id="s", cluster="dev", kind="secrets", namespace="web"),
        req("k8s.get", id="p", cluster="prod", kind="pods", namespace="web"),
        req("git.status", id="g"),
    ]
    items = run_evidence(reqs, kctx)
    assert all(not i.ok and i.refused for i in items)
    assert "Secrets" in by_id(items)["s"].refused
    assert "(not run)" in by_id(items)["s"].source
    assert kubectl_calls() == []


def test_admin_kubeconfig_only_when_allowed(kctx: EvidenceContext, kubectl_calls):
    kctx.allow_admin_kubeconfig = True
    items = run_evidence(
        [
            req("k8s.get", id="p", cluster="prod", kind="pods", namespace="web"),
            req("k8s.get", id="d", cluster="dev", kind="pods", namespace="web"),
        ],
        kctx,
    )
    got = by_id(items)
    assert got["p"].source.endswith("(your own kubeconfig)")
    assert got["d"].source == "kubectl --context dev get pods -n web -o wide"  # the reader still wins
    calls = {c["args"][c["args"].index("--context") + 1]: c["args"] for c in kubectl_calls()}
    assert "--kubeconfig" not in calls["prod"] and calls["dev"][0] == "--kubeconfig"


def test_secret_manifest_output_is_withheld(kctx: EvidenceContext):
    [item] = run_evidence([req("k8s.get", cluster="dev", kind="pods", namespace="leaky")], kctx)
    assert not item.ok and item.refused == evidence.SEC07_NOTE and item.output == ""


def test_logs_keep_the_newest_lines(kctx: EvidenceContext):
    [item] = run_evidence([req("k8s.logs", cluster="dev", namespace="big", pod="api-1", tail=500)], kctx)
    assert item.ok and item.truncated
    assert len(item.output.encode()) <= evidence.OUTPUT_MAX_BYTES
    assert item.output.rstrip().endswith("line 02999 " + "x" * 40)
    assert item.output.startswith("2026-")  # cut at a line break


def test_timeout(kctx: EvidenceContext):
    kctx.timeout_s = 0.5
    [item] = run_evidence([req("k8s.get", cluster="dev", kind="pods", namespace="slow")], kctx)
    assert not item.ok and "timed out" in item.error


def test_missing_kubectl(kctx: EvidenceContext, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WATTSON_KUBECTL_BIN", "/nonexistent/kubectl")
    [item] = run_evidence([req("k8s.get", cluster="dev", kind="pods", namespace="web")], kctx)
    assert not item.ok and item.error == "kubectl not found"


def test_git_evidence(repo: Path):
    ctx = EvidenceContext(repo_root=repo)
    reqs = [
        req("git.status", id="status"),
        req("git.file", id="env", path=".env"),
        req("git.file", id="manifest", path="k8s/all.yaml"),
        req("git.file", id="outside", path="outside"),
        req("git.file", id="app", path="src/app.py"),
        req("git.grep", id="grep", pattern="hunter2"),
        req("git.log", id="log", limit=1),
        req("git.tree", id="tree"),
        req("git.diffstat", id="diff"),
    ]
    got = by_id(run_evidence(reqs, ctx))
    assert got["status"].ok and got["status"].output.startswith("## main")
    assert got["env"].refused and "secret" in got["env"].refused
    assert got["manifest"].refused == evidence.SEC07_NOTE and "aHVudGVyMg" not in got["manifest"].output
    assert got["outside"].refused and "outside" in got["outside"].refused
    assert got["app"].ok and got["app"].source == "read src/app.py (working tree)"
    assert "hunter2" not in got["grep"].output  # only in secret files, which are left out
    assert got["log"].ok and got["tree"].ok and got["diff"].ok


def test_callbacks_and_at_most_two_at_a_time(monkeypatch: pytest.MonkeyPatch):
    running = 0
    peak = 0
    lock = threading.Lock()

    def slow_collect(r, ctx):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.1)
        with lock:
            running -= 1
        return EvidenceItem(request_id=r.id, ok=True, source="x", collected_at="now")

    monkeypatch.setattr(evidence, "collect_one", slow_collect)
    ctx = EvidenceContext(repo_root=Path("/nonexistent/repo"))
    reqs = [req("git.status", id=f"e{i}") for i in range(6)] + [req("git.file", id="bad", path=".env")]
    started: list[str] = []
    done2: list[tuple[str, str]] = []
    done1: list[str] = []
    items = run_evidence(
        reqs,
        ctx,
        on_start=lambda r: started.append(r.id),
        on_done=lambda r, i: done2.append((r.id, i.request_id)),
    )
    assert peak == 2
    assert len(items) == 7 and items[-1].refused
    assert sorted(started) == sorted(f"e{i}" for i in range(6))  # not for the refused one
    assert sorted(done2) == sorted((r.id, r.id) for r in reqs)
    run_evidence(reqs[:2], ctx, on_done=lambda i: done1.append(i.request_id))  # one-argument form
    assert sorted(done1) == ["e0", "e1"]


def test_cap_bytes():
    text = "".join(f"{i:04d}\n" for i in range(5000))
    head, cut = cap_bytes(text, 100)
    assert cut and head.startswith("0000\n") and head.endswith("\n") and len(head.encode()) <= 100
    tail, cut = cap_bytes(text, 100, keep="tail")
    assert cut and tail.endswith("4999\n") and tail.startswith(tail.splitlines()[0]) and len(tail) % 5 == 0
    assert cap_bytes("é" * 100, 51)[0] == "é" * 25  # never half a character
    assert cap_bytes("short", 100) == ("short", False)


@pytest.mark.parametrize(
    ("text", "withheld"),
    [
        ("kind: Secret\ndata:\n  password: aGk=\n", True),
        ("apiVersion: v1\nkind: Secret\nstringData:\n  password: hi\n", True),
        ('{"kind": "Secret", "data": {"password": "aGk="}}', True),
        ("kind: Secret\nmetadata:\n  name: x\ntype: Opaque\n", False),  # no data
        ("kind: SealedSecret\nspec:\n  encryptedData:\n    password: AgB...\n", False),
        ("kind: ConfigMap\ndata:\n  a: b\n", False),
        ("Secrets are never read here; data: none", False),
    ],
)
def test_looks_like_secret_manifest(text: str, withheld: bool):
    assert evidence.looks_like_secret_manifest(text) is withheld


def test_source_is_redacted(repo: Path):
    token = "gh" + "p_FAKE0123456789abcdefFAKE0123456789ab"  # noqa: S105 - a fake
    [item] = run_evidence([req("git.grep", pattern=token)], EvidenceContext(repo_root=repo))
    assert item.ok and token not in item.source and "[REDACTED:" in item.source


def test_a_namespace_that_does_not_exist_is_refused_and_not_run(
    kctx: EvidenceContext, kubectl_calls, monkeypatch
):
    monkeypatch.setenv("FAKE_KUBE_MISSING_NS", "kubeia")
    missing, why_not = evidence.missing_namespaces("dev", ["web", "kubeia"], kctx)
    assert (missing, why_not) == (["kubeia"], None)
    kctx.missing_namespaces = {("dev", ns) for ns in missing}
    calls = len(kubectl_calls())
    items = by_id(
        run_evidence(
            [
                req("k8s.get", id="a", cluster="dev", kind="pods", namespace="kubeia"),
                req("k8s.get", id="b", cluster="dev", kind="pods", namespace="web"),
            ],
            kctx,
        )
    )
    assert items["a"].refused == "namespace 'kubeia' was not found in context 'dev'"
    assert items["b"].ok
    assert len(kubectl_calls()) == calls + 1  # only "b" ran


def test_missing_namespaces_without_access_is_not_checked(kctx: EvidenceContext, kubectl_calls):
    calls = len(kubectl_calls())
    assert evidence.missing_namespaces("prod", ["web"], kctx) == (
        [],
        "cluster 'prod' has no read-only kubeconfig",
    )
    assert len(kubectl_calls()) == calls


def test_all_namespaces_events_keep_the_newest_warnings(kctx: EvidenceContext):
    [item] = run_evidence([req("k8s.events", cluster="dev", all_namespaces=True)], kctx)
    lines = item.output.splitlines()
    assert item.ok and item.truncated and lines[0].startswith("NAMESPACE") and len(lines) == 51
    assert item.source == (
        "kubectl --context dev get events --all-namespaces "
        "--field-selector type=Warning --sort-by=.lastTimestamp"
    )
    assert "all_namespaces=True" in evidence.describe_request(
        req("k8s.events", cluster="dev", all_namespaces=True)
    )
