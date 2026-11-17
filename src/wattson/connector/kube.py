"""Kubernetes access for evidence (docs/design.md 10.1): the user's kube contexts, the read-only
"reader" kubeconfig per context, `add_reader` (the only write wattson ever makes to a cluster,
after a yes), and the read-only kubectl commands of the evidence operations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path

from wattson.connector.run import OpResult, Refused, RunResult, limit_lines, run
from wattson.shared.contracts import K8S_KINDS, EvidenceRequest
from wattson.shared.redact import redact

NAMESPACE_PLACEHOLDER = "__NAMESPACE__"  # in READER_MANIFEST, filled in by reader_manifest()
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
READER_ACCOUNT = "wattson-reader"
READER_TOKEN_SECRET = "wattson-reader-token"  # noqa: S105 - a name, not a value
REQUEST_TIMEOUT = "--request-timeout=15s"
KUBECTL_TIMEOUT_S = 20.0
TOKEN_ATTEMPTS = 30  # one second apart
EVENTS_NEWEST = 50
LOGS_TAIL_DEFAULT, LOGS_TAIL_MAX = 200, 500
# All namespaces: a listing keeps whole rows up to this size, then says how many it left out.
# 12 kB is about 3 parts for the injection model (about 1.8 s per 4000 characters).
ALL_NAMESPACES_MAX_BYTES = 12 * 1024

# The same text as k8s/reader.yaml (a test keeps them equal). Kept here because only src/ is
# installed.
READER_MANIFEST = """\
# wattson read-only access to one cluster (docs/design.md 10.1).
# Applied only by `wattson k8s add <context>`, after the user says yes. It is the only write
# wattson ever makes to a cluster. The connector then builds ~/.config/wattson/kube/<context>.yaml
# (mode 600) from this service account's token.
# The account lives in an EXISTING namespace, filled in for __NAMESPACE__: on the cluster that
# hosts wattson, its own namespace (grimoire); elsewhere the one given with --namespace.
# wattson never creates a namespace.
# Built-in ClusterRole "view": get/list/watch on most namespaced objects, pods/log and events;
# it cannot read Secrets and cannot change anything. "view" does not cover nodes, so a small
# extra ClusterRole gives get/list/watch on nodes only (for scheduling problems). Remove with:
#   kubectl delete clusterrolebinding wattson-reader-view wattson-reader-nodes
#   kubectl delete clusterrole wattson-reader-nodes
#   kubectl -n <namespace> delete serviceaccount wattson-reader
#   kubectl -n <namespace> delete secret wattson-reader-token
apiVersion: v1
kind: ServiceAccount
metadata:
  name: wattson-reader
  namespace: __NAMESPACE__
  labels:
    app.kubernetes.io/name: wattson-reader
    app.kubernetes.io/part-of: wattson
# No pod runs as this account: its token is used only from the laptop.
automountServiceAccountToken: false
---
apiVersion: v1
kind: Secret
metadata:
  name: wattson-reader-token
  namespace: __NAMESPACE__
  labels:
    app.kubernetes.io/name: wattson-reader
    app.kubernetes.io/part-of: wattson
  annotations:
    kubernetes.io/service-account.name: wattson-reader
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: wattson-reader-view
  labels:
    app.kubernetes.io/name: wattson-reader
    app.kubernetes.io/part-of: wattson
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: wattson-reader
    namespace: __NAMESPACE__
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: wattson-reader-nodes
  labels:
    app.kubernetes.io/name: wattson-reader
    app.kubernetes.io/part-of: wattson
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: wattson-reader-nodes
  labels:
    app.kubernetes.io/name: wattson-reader
    app.kubernetes.io/part-of: wattson
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: wattson-reader-nodes
subjects:
  - kind: ServiceAccount
    name: wattson-reader
    namespace: __NAMESPACE__
"""


class KubeError(RuntimeError):
    """A kube step failed. The message is plain, redacted and safe to show."""


# Tests make waiting instant.
SLEEP: Callable[[float], None] = time.sleep


def kubectl_bin() -> str:
    return os.environ.get("WATTSON_KUBECTL_BIN") or "kubectl"


def config_dir() -> Path:
    override = os.environ.get("WATTSON_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".config" / "wattson"


def kube_dir() -> Path:
    return config_dir() / "kube"


def sanitise_context(context: str) -> str:
    """A file name for a context. Names that had to change get a short hash, so two contexts
    never share a file ("a/b" and "a:b")."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", context).lstrip(".") or "context"
    if safe != context:
        safe = f"{safe[:100]}-{hashlib.sha256(context.encode()).hexdigest()[:8]}"
    return safe


def reader_kubeconfig_path(context: str) -> Path:
    return kube_dir() / f"{sanitise_context(context)}.yaml"


def _kubectl(args: list[str], **kw) -> RunResult:
    kw.setdefault("timeout_s", KUBECTL_TIMEOUT_S)
    return run([kubectl_bin(), *args], **kw)


def _short_error(res: RunResult, what: str) -> str:
    if res.error:
        return f"{what}: {res.error}"
    text = redact(res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}").text
    return f"{what}: {text[:500]}"


def list_contexts() -> tuple[list[str], str | None]:
    """The context names of the user's kubeconfig, and the current one. Names only."""
    res = _kubectl(["config", "get-contexts", "-o", "name"])
    if not res.ok:
        return [], None
    names = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    cur = _kubectl(["config", "current-context"])
    current = cur.stdout.strip() if cur.ok and cur.stdout.strip() else None
    return names, current


def _context_of(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        value = json.loads(text).get("current-context")
        return value if isinstance(value, str) and value else None
    except (ValueError, AttributeError):
        m = re.search(r"^current-context:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text, re.MULTILINE)
        return m.group(1) if m else None


def reader_contexts() -> list[str]:
    """The contexts that have a read-only kubeconfig written by `add_reader`."""
    folder = kube_dir()
    if not folder.is_dir():
        return []
    found = set()
    for path in folder.glob("*.yaml"):
        ctx = _context_of(path)
        if ctx and path == reader_kubeconfig_path(ctx):
            found.add(ctx)
    return sorted(found)


# ---- add_reader -------------------------------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    """Write `text` with mode 600 in a folder with mode 700, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def reader_manifest(namespace: str) -> str:
    """The reader manifest with its account in `namespace` (an existing one)."""
    if not _NAMESPACE_RE.match(namespace):
        raise KubeError(f"'{namespace}' is not a valid namespace name")
    return READER_MANIFEST.replace(NAMESPACE_PLACEHOLDER, namespace)


def _check_namespace(context: str, namespace: str) -> None:
    """The namespace must exist: wattson never creates one."""
    res = _kubectl(["--context", context, REQUEST_TIMEOUT, "get", "namespace", namespace, "-o", "name"])
    if not res.ok:
        raise KubeError(
            f"namespace '{namespace}' was not found in context '{context}'. Wattson Agent never creates a "
            "namespace: pass an existing one with --namespace"
        )


def _wait_for_token(context: str, namespace: str) -> str:
    args = [
        "--context",
        context,
        REQUEST_TIMEOUT,
        "-n",
        namespace,
        "get",
        "secret",
        READER_TOKEN_SECRET,
        "-o",
        "jsonpath={.data.token}",
    ]
    last: RunResult | None = None
    for attempt in range(TOKEN_ATTEMPTS):
        if attempt:
            SLEEP(1.0)
        last = _kubectl(args)
        if last.ok and last.stdout.strip():
            try:
                token = base64.b64decode(last.stdout.strip(), validate=True).decode("utf-8").strip()
            except (binascii.Error, UnicodeDecodeError):
                raise KubeError("the reader token is not valid base64") from None
            if token:
                return token
    if last is not None and not last.ok:
        raise KubeError(_short_error(last, "the reader token did not appear"))
    raise KubeError(f"the reader token did not appear within {TOKEN_ATTEMPTS} s")


_CLUSTER_FIELDS = (
    "server",
    "certificate-authority-data",
    "tls-server-name",
    "insecure-skip-tls-verify",
    "proxy-url",
)


def _cluster_of(context: str) -> dict:
    """Server address and CA of the context's cluster. Read with --raw; never printed."""
    res = _kubectl(["config", "view", "--raw", "--minify", "--flatten", "--context", context, "-o", "json"])
    if not res.ok:
        raise KubeError(_short_error(res, "cannot read the cluster address from your kubeconfig"))
    try:
        cfg = json.loads(res.stdout)
        wanted = next(c["context"]["cluster"] for c in cfg["contexts"] if c["name"] == context)
        cluster = next(c["cluster"] for c in cfg["clusters"] if c["name"] == wanted)
    except (ValueError, KeyError, TypeError, StopIteration):
        raise KubeError(f"context '{context}' has no usable cluster entry in your kubeconfig") from None
    out = {k: cluster[k] for k in _CLUSTER_FIELDS if k in cluster}
    if not out.get("server"):
        raise KubeError(f"context '{context}' has no server address")
    return out


def build_reader_kubeconfig(context: str, cluster: dict, token: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": context, "cluster": cluster}],
        "users": [{"name": READER_ACCOUNT, "user": {"token": token}}],
        "contexts": [
            {"name": context, "context": {"cluster": context, "user": READER_ACCOUNT, "namespace": "default"}}
        ],
        "current-context": context,
        "preferences": {},
    }


CAN_I_CHECKS = (
    (("get", "pods"), "yes"),
    (("get", "secrets"), "no"),
    (("delete", "pods"), "no"),
)


def _can_i(context: str, path: Path, verb: str, resource: str) -> str:
    res = _kubectl(
        [
            "--kubeconfig",
            str(path),
            "--context",
            context,
            REQUEST_TIMEOUT,
            "auth",
            "can-i",
            verb,
            resource,
            "--all-namespaces",
        ]
    )
    if res.error:
        return f"unknown ({res.error})"
    answer = res.stdout.strip().split()[:1]
    return answer[0] if answer and answer[0] in ("yes", "no") else "unknown"


def verify_reader(context: str, path: Path) -> list[tuple[str, str, str]]:
    """(question, expected, answer) for the three checks."""
    results = []
    for (verb, resource), expected in CAN_I_CHECKS:
        answer = _can_i(context, path, verb, resource)
        if expected == "yes" and answer != "yes":
            for _ in range(3):  # RBAC can take a moment to see a new binding
                SLEEP(1.0)
                answer = _can_i(context, path, verb, resource)
                if answer == "yes":
                    break
        results.append((f"{verb} {resource}", expected, answer))
    return results


def add_reader(
    context: str, confirm: Callable[[str], bool], log: Callable[[str], None], *, namespace: str
) -> None:
    """Show the manifest, ask, and only after a yes apply it to `context`, with the read-only
    account in `namespace` (it must exist), then write the reader kubeconfig (600, in a 700
    folder) and check it with `kubectl auth can-i`. Raises KubeError."""
    names, _ = list_contexts()
    if context not in names:
        raise KubeError(f"there is no kube context named '{context}' in your kubeconfig")
    manifest = reader_manifest(namespace)
    _check_namespace(context, namespace)
    path = reader_kubeconfig_path(context)
    log(
        f"Wattson Agent wants read-only access to context '{context}', with its account in namespace "
        f"'{namespace}'. It will apply this manifest:"
    )
    log(manifest.rstrip("\n"))
    log(
        "This is the only change Wattson Agent ever makes to a cluster. The built-in role 'view' can read "
        "most objects and logs; it cannot read Secrets and cannot change anything."
    )
    if path.exists():
        log(f"The reader kubeconfig {path} exists and will be replaced.")
    if not confirm(f"Apply it to '{context}' with your own kubeconfig?"):
        log("Nothing was changed.")
        return

    res = _kubectl(
        ["--context", context, REQUEST_TIMEOUT, "apply", "-f", "-"],
        input_text=manifest,
        timeout_s=60.0,
    )
    if not res.ok:
        raise KubeError(_short_error(res, "kubectl apply failed"))
    for line in res.stdout.splitlines():
        if line.strip():
            log(redact(line.strip()).text)

    token = _wait_for_token(context, namespace)
    cluster = _cluster_of(context)
    _write_private(path, json.dumps(build_reader_kubeconfig(context, cluster, token), indent=2) + "\n")
    log(f"Wrote the reader kubeconfig {path} (mode 600).")

    checks = verify_reader(context, path)
    bad = []
    for question, expected, answer in checks:
        log(f"can-i {question}: {answer}")
        if answer != expected:
            bad.append(f"{question} is '{answer}', expected '{expected}'")
    if bad:
        path.unlink(missing_ok=True)
        raise KubeError("the reader kubeconfig was removed because the check failed: " + "; ".join(bad))
    log(f"Context '{context}' now has read-only access for Wattson Agent.")


# ---- evidence operations ------------------------------------------------------------------------

_KIND_ALIASES = {
    "pod": "pods",
    "po": "pods",
    "deployment": "deployments",
    "deploy": "deployments",
    "statefulset": "statefulsets",
    "sts": "statefulsets",
    "daemonset": "daemonsets",
    "ds": "daemonsets",
    "replicaset": "replicasets",
    "rs": "replicasets",
    "service": "services",
    "svc": "services",
    "ingress": "ingresses",
    "ing": "ingresses",
    "job": "jobs",
    "cronjob": "cronjobs",
    "cj": "cronjobs",
    "persistentvolumeclaim": "persistentvolumeclaims",
    "pvc": "persistentvolumeclaims",
    "horizontalpodautoscaler": "horizontalpodautoscalers",
    "hpa": "horizontalpodautoscalers",
    "configmap": "configmaps",
    "cm": "configmaps",
    "node": "nodes",
    "no": "nodes",
}
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


def normalise_kind(kind: str | None) -> str:
    if not kind or not kind.strip():
        raise Refused("a kind is needed")
    k = kind.strip().lower()
    if "secret" in k:
        raise Refused("Secrets are never read")
    k = _KIND_ALIASES.get(k, k)
    if k not in K8S_KINDS:
        raise Refused(f"kind '{kind}' is not allowed (allowed: {', '.join(K8S_KINDS)})")
    return k


def check_name(value: str | None, field: str) -> str:
    if not value:
        raise Refused(f"a {field} is needed")
    if not _DNS_SUBDOMAIN.fullmatch(value):
        raise Refused(f"'{value}' is not a valid Kubernetes {field}")
    return value


def check_label(value: str | None, field: str) -> str:
    if not value:
        raise Refused(f"a {field} is needed")
    if not _DNS_LABEL.fullmatch(value):
        raise Refused(f"'{value}' is not a valid Kubernetes {field}")
    return value


def _all_namespaces_get(req: EvidenceRequest, kind: str) -> list[str]:
    """k8s.get across all namespaces: the narrow default columns (no "-o wide"); a name becomes a
    field selector, because kubectl cannot get one name in every namespace."""
    if req.namespace:
        raise Refused("a namespace and all namespaces do not go together")
    args = ["get", kind]
    if kind != "nodes":
        args.append("--all-namespaces")
    if req.name:
        args += ["--field-selector", "metadata.name=" + check_name(req.name, "name")]
    return args


def k8s_op_args(req: EvidenceRequest) -> list[str]:
    """The kubectl arguments after the global flags, e.g. ["get", "pods", "-n", "web", "-o", "wide"].
    Checks every field; raises Refused when something is not allowed."""
    op = req.op
    if req.all_namespaces and op not in ("k8s.get", "k8s.events"):
        raise Refused(f"{op} needs one namespace: all namespaces is only for k8s.get and k8s.events")
    if op in ("k8s.get", "k8s.describe"):
        kind = normalise_kind(req.kind)
        verb = op.removeprefix("k8s.")
        if op == "k8s.describe" and kind == "configmaps":
            raise Refused("configmap contents are not read, so describe configmaps is refused")
        if req.all_namespaces:
            return _all_namespaces_get(req, kind)
        args = [verb, kind]
        if op == "k8s.describe" or req.name:
            args.append(check_name(req.name, "name"))
        if kind != "nodes":
            args += ["-n", check_label(req.namespace, "namespace")]
        if op == "k8s.get":
            args += ["-o", "wide"]
        return args
    if op == "k8s.events":
        if req.all_namespaces:
            if req.namespace:
                raise Refused("a namespace and all namespaces do not go together")
            # Every namespace has many Normal events: the warnings only, the newest at the end.
            return [
                "get",
                "events",
                "--all-namespaces",
                "--field-selector",
                "type=Warning",
                "--sort-by=.lastTimestamp",
            ]
        return ["get", "events", "-n", check_label(req.namespace, "namespace"), "--sort-by=.lastTimestamp"]
    if op == "k8s.logs":
        if bool(req.pod) == bool(req.deployment):
            raise Refused("logs need exactly one of pod or deployment")
        target = (
            check_name(req.pod, "pod") if req.pod else "deploy/" + check_name(req.deployment, "deployment")
        )
        tail = LOGS_TAIL_DEFAULT if req.tail is None else req.tail
        if not 1 <= tail <= LOGS_TAIL_MAX:
            raise Refused(f"tail must be 1..{LOGS_TAIL_MAX}")
        args = [
            "logs",
            target,
            "-n",
            check_label(req.namespace, "namespace"),
            f"--tail={tail}",
            "--timestamps",
        ]
        if req.previous:
            args.append("--previous")
        if req.container:
            args += ["-c", check_label(req.container, "container")]
        return args
    if op == "k8s.rollout":
        name = check_name(req.deployment, "deployment")
        return [
            "rollout",
            "status",
            f"deployment/{name}",
            "-n",
            check_label(req.namespace, "namespace"),
            "--watch=false",
        ]
    raise Refused(f"unknown Kubernetes operation: {op}")


def k8s_command(req: EvidenceRequest, reader: Path | None) -> tuple[list[str], str]:
    """The full kubectl argument list and a readable source line without the kubeconfig path.
    `reader` None means the user's own kubeconfig (--use-my-kubeconfig)."""
    if not req.cluster:
        raise Refused("a cluster (kube context) is needed")
    op_args = k8s_op_args(req)
    head = ["--kubeconfig", str(reader)] if reader is not None else []
    head += ["--context", req.cluster, REQUEST_TIMEOUT]
    source = f"kubectl --context {shlex.quote(req.cluster)} {shlex.join(op_args)}"
    if reader is None:
        source += "  (your own kubeconfig)"
    return [kubectl_bin(), *head, *op_args], source


def _newest_events(text: str, keep: int = EVENTS_NEWEST) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= keep + 1:
        return text, False
    return "\n".join([lines[0], *lines[-keep:]]) + "\n", True


_READY = re.compile(r"(\d+)/(\d+)")
_RECENT_RESTART = re.compile(r"\(\d+(?:s|m)\d*s? ago\)")  # "3 (2m ago)": restarted in the last hour
_FINE_STATUS = frozenset({"Running", "Completed", "Succeeded"})


_GAP = re.compile(r"\s{2,}")  # kubectl pads its columns with at least 3 spaces


def _columns(header: str) -> dict[str, int]:
    """The index of each column of a kubectl table ("NOMINATED NODE" is one column)."""
    return {name: i for i, name in enumerate(_GAP.split(header.strip()))}


def _cell(row: str, cols: dict[str, int], name: str) -> str:
    cells = _GAP.split(row.strip())
    i = cols.get(name)
    return cells[i] if i is not None and i < len(cells) else ""


def _has_problem(row: str, cols: dict[str, int]) -> bool:
    """Not all ready (pods, deployments, ...), a status other than Running/Completed, or a
    restart in the last hour (pods)."""
    status = _cell(row, cols, "STATUS")
    if status and status not in _FINE_STATUS:
        return True
    ready = _READY.fullmatch(_cell(row, cols, "READY"))
    if ready and int(ready.group(1)) < int(ready.group(2)) and status != "Completed":
        return True
    return bool(_RECENT_RESTART.search(_cell(row, cols, "RESTARTS")))


def problems_first(text: str, max_bytes: int = ALL_NAMESPACES_MAX_BYTES) -> tuple[str, bool]:
    """A listing across all namespaces: the rows with a problem first, then the others, kept
    whole up to `max_bytes`; a last line says how many rows were left out."""
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].startswith("NAMESPACE"):
        return text, False
    header, rows = lines[0], lines[1:]
    cols = _columns(header)
    bad = [r for r in rows if _has_problem(r, cols)]
    rows = bad + [r for r in rows if not _has_problem(r, cols)]
    kept, size = [header], len(header) + 1
    for row in rows:
        size += len(row.encode("utf-8")) + 1
        if size > max_bytes:
            break
        kept.append(row)
    left = len(rows) - (len(kept) - 1)
    if not left:
        return "\n".join(kept) + "\n", False
    note = f"[cut: {left} more of {len(rows)} rows are not shown; the {len(bad)} with a problem come first]"
    return "\n".join([*kept, note]) + "\n", True


def run_k8s_op(req: EvidenceRequest, reader: Path | None, timeout_s: float = KUBECTL_TIMEOUT_S) -> OpResult:
    argv, source = k8s_command(req, reader)
    is_logs = req.op == "k8s.logs"
    res = run(argv, timeout_s=timeout_s, keep="tail" if is_logs else "head")
    keep = "tail" if is_logs else "head"
    if res.error is not None:
        return OpResult(source=source, ok=False, error=res.error, keep=keep)
    if res.returncode != 0:
        msg = res.stderr.strip() or f"kubectl exited with code {res.returncode}"
        return OpResult(source=source, ok=False, error=msg, output=res.stdout, keep=keep)
    text, truncated = res.stdout, res.truncated
    if req.op == "k8s.events":
        text, cut = _newest_events(text)
        truncated = truncated or cut
    elif req.op == "k8s.get" and req.all_namespaces:
        text, cut = problems_first(text)
        truncated = truncated or cut
    if not text.strip() and res.stderr.strip():
        text, _ = limit_lines(res.stderr.strip() + "\n", 5)  # "No resources found in web namespace."
    return OpResult(source=source, output=text, truncated=truncated, keep=keep)


def missing_namespaces(
    context: str, names: list[str], reader: Path | None, timeout_s: float = KUBECTL_TIMEOUT_S
) -> tuple[list[str], str | None]:
    """The names that are not namespaces of `context`, by one read-only call. ([], why) when it
    could not be checked."""
    for name in names:
        check_label(name, "namespace")
    if not names:
        return [], None
    head = ["--kubeconfig", str(reader)] if reader is not None else []
    args = [*head, "--context", context, REQUEST_TIMEOUT, "get", "namespaces", *names, "-o", "name"]
    res = _kubectl([*args, "--ignore-not-found"], timeout_s=timeout_s)
    if res.error is not None or res.returncode != 0:
        return [], _short_error(res, "the namespaces could not be checked")
    found = {line.strip().removeprefix("namespace/") for line in res.stdout.splitlines()}
    return [n for n in names if n not in found], None
