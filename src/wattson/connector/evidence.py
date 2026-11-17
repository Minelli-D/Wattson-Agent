"""Evidence collection on the laptop (docs/design.md 10.1): check each `EvidenceRequest` against
the connector's own allow-list, run the allowed ones (at most two at a time), redact, cut, and
return `EvidenceItem`s. A refused request runs nothing."""

from __future__ import annotations

import inspect
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from wattson.connector import git, kube
from wattson.connector.run import OpResult, Refused
from wattson.shared.contracts import EvidenceItem, EvidenceOpName, EvidenceRequest
from wattson.shared.redact import redact

OUTPUT_MAX_BYTES = 16 * 1024
ERROR_MAX_CHARS = 500
PARALLEL = 2
ALL_OPS: tuple[str, ...] = get_args(EvidenceOpName)

SEC07_NOTE = "withheld: the output looked like a Kubernetes Secret with data, so it was not sent (SEC-07)"


@dataclass
class EvidenceContext:
    repo_root: Path | None
    contexts: list[str] = field(default_factory=list)  # the user's kube context names
    reader_contexts: list[str] = field(default_factory=list)  # those with a reader kubeconfig
    allow_admin_kubeconfig: bool = False  # --use-my-kubeconfig
    timeout_s: float = 20.0
    # (cluster, namespace) pairs found not to exist (missing_namespaces): their requests do not run.
    missing_namespaces: set[tuple[str, str]] = field(default_factory=set)


def check_allowed(req: EvidenceRequest, ctx: EvidenceContext) -> str | None:
    """None when the request may run; otherwise the reason it is refused."""
    if req.op not in ALL_OPS:
        return f"unknown operation '{req.op}'"
    try:
        if req.op.startswith("git."):
            if ctx.repo_root is None:
                return "no git repository is selected"
            match req.op:
                case "git.file":
                    git.normalise_repo_path(req.path)
                case "git.grep":
                    git.check_pattern(req.pattern)
                case "git.log":
                    if req.limit is not None and not 1 <= req.limit <= git.LOG_MAX:
                        return f"limit must be 1..{git.LOG_MAX}"
                case "git.tree":
                    if req.depth is not None and not 1 <= req.depth <= git.TREE_MAX:
                        return f"depth must be 1..{git.TREE_MAX}"
            return None
        kube.k8s_op_args(req)  # kind, names, numbers; "secrets" never passes
    except Refused as exc:
        return str(exc)
    if not req.cluster:
        return "a cluster (kube context) is needed"
    if req.cluster not in ctx.contexts:
        return f"cluster '{req.cluster}' is not one of your kube contexts"
    if req.cluster not in ctx.reader_contexts and not ctx.allow_admin_kubeconfig:
        return (
            f"cluster '{req.cluster}' has no read-only kubeconfig: run `wattson k8s add {req.cluster}`, "
            "or allow your own kubeconfig with --use-my-kubeconfig"
        )
    if req.namespace and (req.cluster, req.namespace) in ctx.missing_namespaces:
        return f"namespace '{req.namespace}' was not found in context '{req.cluster}'"
    return None


def kubeconfig_for(cluster: str, ctx: EvidenceContext) -> tuple[Path | None, str | None]:
    """(the kubeconfig, None) to read `cluster` with, or (None, why not). The reader kubeconfig
    whenever there is one; the user's own (None) only when allowed and no reader exists (least
    privilege)."""
    if cluster not in ctx.contexts:
        return None, f"cluster '{cluster}' is not one of your kube contexts"
    if cluster in ctx.reader_contexts:
        reader = kube.reader_kubeconfig_path(cluster)
        if reader.is_file():
            return reader, None
        if not ctx.allow_admin_kubeconfig:
            return None, f"the reader kubeconfig of '{cluster}' is missing"
        return None, None
    if not ctx.allow_admin_kubeconfig:
        return None, f"cluster '{cluster}' has no read-only kubeconfig"
    return None, None


def missing_namespaces(
    cluster: str, names: Sequence[str], ctx: EvidenceContext
) -> tuple[list[str], str | None]:
    """The names that are not namespaces of `cluster` (one read-only kubectl call), and why it
    could not be checked, if so. Never raises."""
    reader, why_not = kubeconfig_for(cluster, ctx)
    if why_not is not None:
        return [], why_not
    try:
        return kube.missing_namespaces(cluster, list(names), reader, ctx.timeout_s)
    except Refused as exc:
        return [], str(exc)


# ---- output handling ----------------------------------------------------------------------------

_SECRET_KIND_YAML = re.compile(r"(?m)^[ \t]*-?[ \t]*kind:[ \t]*[\"']?Secret[\"']?[ \t]*(?:#.*)?$")
_DATA_YAML = re.compile(r"(?m)^[ \t]*(?:data|stringData):[ \t]*(?:\{[^}\n]*\S[^}\n]*\}|#.*)?[ \t]*$")
_SECRET_KIND_JSON = re.compile(r"\"kind\"[ \t]*:[ \t]*\"Secret\"")
_DATA_JSON = re.compile(r"\"(?:data|stringData)\"[ \t]*:[ \t]*\{[ \t\r\n]*\"")


def looks_like_secret_manifest(text: str) -> bool:
    """A Kubernetes Secret with data, in YAML or JSON (SEC-07)."""
    if "Secret" not in text:
        return False
    if _SECRET_KIND_YAML.search(text) and _DATA_YAML.search(text):
        return True
    return bool(_SECRET_KIND_JSON.search(text) and _DATA_JSON.search(text))


def cap_bytes(text: str, max_bytes: int = OUTPUT_MAX_BYTES, keep: str = "head") -> tuple[str, bool]:
    """At most `max_bytes` of UTF-8, cut at a line break when one is near. `keep="tail"` keeps
    the end (logs: the newest lines)."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    if keep == "tail":
        part = data[-max_bytes:].decode("utf-8", errors="ignore")
        nl = part.find("\n")
        if 0 <= nl < len(part) - 1:
            part = part[nl + 1 :]
        return part, True
    part = data[:max_bytes].decode("utf-8", errors="ignore")
    nl = part.rfind("\n")
    if nl > 0:
        part = part[: nl + 1]
    return part, True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def describe_request(req: EvidenceRequest) -> str:
    """A short readable line for a request that did not run."""
    fields = (
        "cluster",
        "namespace",
        "all_namespaces",
        "kind",
        "name",
        "pod",
        "deployment",
        "container",
        "path",
        "pattern",
    )
    parts = [req.op] + [f"{f}={getattr(req, f)!r}" for f in fields if getattr(req, f)]
    return " ".join(parts) + " (not run)"


def _safe_source(source: str) -> str:
    """The source line can hold a search text or a path the orchestrator chose: redact it too."""
    return redact(source).text[:ERROR_MAX_CHARS]


def _refused(req: EvidenceRequest, reason: str, source: str | None = None) -> EvidenceItem:
    return EvidenceItem(
        request_id=req.id,
        ok=False,
        source=_safe_source(source or describe_request(req)),
        collected_at=_now(),
        refused=redact(reason).text[:ERROR_MAX_CHARS],
    )


def finish_item(req: EvidenceRequest, op: OpResult) -> EvidenceItem:
    """Redact, cut and wrap one operation's result."""
    if op.refused:
        return _refused(req, op.refused, op.source)
    if looks_like_secret_manifest(op.output):
        return EvidenceItem(
            request_id=req.id,
            ok=False,
            source=_safe_source(op.source),
            collected_at=_now(),
            refused=SEC07_NOTE,
        )
    red = redact(op.output)
    output, cut = cap_bytes(red.text, OUTPUT_MAX_BYTES, op.keep)
    redactions = red.count
    error = None
    if not op.ok:
        err = redact(op.error or "the command failed")
        redactions += err.count
        error = err.text.strip()[:ERROR_MAX_CHARS]
    return EvidenceItem(
        request_id=req.id,
        ok=op.ok,
        source=_safe_source(op.source),
        collected_at=_now(),
        output=output,
        truncated=op.truncated or cut,
        redactions=redactions,
        error=error,
    )


def collect_one(req: EvidenceRequest, ctx: EvidenceContext) -> EvidenceItem:
    """Check and run one request. Never raises."""
    reason = check_allowed(req, ctx)
    if reason is not None:
        return _refused(req, reason)
    try:
        if req.op.startswith("git."):
            if ctx.repo_root is None:  # check_allowed refuses this already
                return _refused(req, "no git repository is selected")
            op = git.run_git_op(req, ctx.repo_root, ctx.timeout_s)
        else:
            if not req.cluster:  # check_allowed refuses this already
                return _refused(req, "a cluster (kube context) is needed")
            reader, why_not = kubeconfig_for(req.cluster, ctx)
            if why_not is not None:
                return _refused(req, why_not)
            op = kube.run_k8s_op(req, reader, ctx.timeout_s)
    except Refused as exc:
        return _refused(req, str(exc))
    except Exception as exc:  # a bug must not take the whole collection down
        return EvidenceItem(
            request_id=req.id,
            ok=False,
            source=_safe_source(describe_request(req)),
            collected_at=_now(),
            error=f"unexpected error: {type(exc).__name__}",
        )
    return finish_item(req, op)


def _accepts_two(cb: Callable) -> bool:
    try:
        inspect.signature(cb).bind(None, None)
        return True
    except TypeError:
        return False
    except ValueError:  # no signature (some builtins): assume (req, item)
        return True


def run_evidence(
    reqs: Sequence[EvidenceRequest],
    ctx: EvidenceContext,
    on_start: Callable[[EvidenceRequest], object] | None = None,
    on_done: Callable[..., object] | None = None,
) -> list[EvidenceItem]:
    """Run the requests, at most two at a time; items come back in the order of `reqs`.
    `on_start(req)` is called before a request runs (not for refused ones); `on_done(req, item)`
    (or `on_done(item)`) after each. Callbacks run on worker threads, one at a time."""
    lock = threading.Lock()
    done_two = on_done is not None and _accepts_two(on_done)

    def one(req: EvidenceRequest) -> EvidenceItem:
        reason = check_allowed(req, ctx)
        if reason is None and on_start is not None:
            with lock:
                on_start(req)
        item = _refused(req, reason) if reason is not None else collect_one(req, ctx)
        if on_done is not None:
            with lock:
                if done_two:
                    on_done(req, item)
                else:
                    on_done(item)
        return item

    if not reqs:
        return []
    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="evidence") as pool:
        futures = [pool.submit(one, req) for req in reqs]
        return [f.result() for f in futures]
