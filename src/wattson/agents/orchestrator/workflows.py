"""Workflows: what to ask and the minimum evidence to collect, per task type (FR-PROMPT-06).

Data-driven: each workflow is one entry in `WORKFLOW_TABLE` (title, expected outcome, template,
questions, evidence plan) plus one template in `templates/`. A new task type = one entry + one
template. The plans ask only for what the task needs (SEC-01..03): never `secrets`, never a whole
repository, every Kubernetes read names its cluster and its namespace, or says "all namespaces"
(FR-K8S-06).

Several namespaces, or all of them: the listings and the events first, one per namespace (or one
across all). A named workload is then looked at (describe, rollout, logs) only where the listing
shows it, in a second round (`plan_followup`), in at most FOLLOWUP_NAMESPACES namespaces.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wattson.shared.contracts import (
    K8S_KINDS,
    MAX_NAMESPACES,
    DecisionHints,
    EvidenceItem,
    EvidenceRequest,
    Question,
    Target,
    Workflow,
    WorkspaceInfo,
)
from wattson.shared.redact import is_secret_path, redact

__all__ = [
    "WORKFLOW_TABLE",
    "PlanContext",
    "WorkflowSpec",
    "parse_workload",
    "plan_evidence",
    "plan_followup",
    "required_questions",
]


@dataclass(frozen=True)
class PlanContext:
    workflow: Workflow
    target: Target
    workspace: WorkspaceInfo
    hints: DecisionHints
    request: str = ""  # optional: lets the plan add the logs of the run that crashed


Step = dict[str, Any]  # EvidenceRequest fields without the id


@dataclass(frozen=True)
class WorkflowSpec:
    name: Workflow
    title: str
    template: str  # the template's name in templates.TEMPLATES
    expected_outcome: tuple[str, ...]
    questions: Callable[[PlanContext], list[Question]]
    plan: Callable[[PlanContext], list[Step]]


# ---- limits (SEC-01, SEC-02) ------------------------------------------------------------------

LOG_TAIL = 200
FOLLOWUP_NAMESPACES = 3  # a workload found in more namespaces is looked at in the first three
MAX_PATTERN = 200
MAX_PATH = 300
CRASH_WORDS = re.compile(r"crash|restart|crashloopbackoff|oom", re.IGNORECASE)

# "deployment/api" -> ("deployments", "api"). A bare name is taken as a deployment.
_KIND_ALIASES = {
    "deployment": "deployments",
    "deploy": "deployments",
    "statefulset": "statefulsets",
    "sts": "statefulsets",
    "daemonset": "daemonsets",
    "ds": "daemonsets",
    "replicaset": "replicasets",
    "rs": "replicasets",
    "pod": "pods",
    "po": "pods",
    "job": "jobs",
    "cronjob": "cronjobs",
    "cj": "cronjobs",
    "service": "services",
    "svc": "services",
    "ingress": "ingresses",
    "ing": "ingresses",
    "hpa": "horizontalpodautoscalers",
    "horizontalpodautoscaler": "horizontalpodautoscalers",
    "pvc": "persistentvolumeclaims",
    "persistentvolumeclaim": "persistentvolumeclaims",
}
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_DESCRIBABLE = frozenset(K8S_KINDS) - {"configmaps", "nodes"}


def parse_workload(workload: str | None) -> tuple[str, str] | None:
    """("deployments", "api") from "deployment/api" or "api"; None when it is not usable."""
    if not workload:
        return None
    kind, _, name = workload.strip().lower().rpartition("/")
    kind = _KIND_ALIASES.get(kind, kind) if kind else "deployments"
    if kind not in _DESCRIBABLE or not _NAME.fullmatch(name):
        return None  # never "secrets", never configmap contents, never nodes
    return kind, name


# ---- hints: advice, so every value is checked ---------------------------------------------------


def _grep_patterns(hints: DecisionHints, limit: int) -> list[str]:
    out: list[str] = []
    for raw in hints.grep:
        pattern = raw.strip()
        if not (2 <= len(pattern) <= MAX_PATTERN) or "\n" in pattern or pattern in out:
            continue
        if redact(pattern).count:
            continue  # never search for (and so never send) something that looks like a secret
        out.append(pattern)
        if len(out) == limit:
            break
    return out


def _hint_files(hints: DecisionHints, limit: int) -> list[str]:
    out: list[str] = []
    for raw in hints.files:
        path = raw.strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        parts = path.split("/")
        if not path or len(path) > MAX_PATH or path.startswith(("/", "~")) or ".." in parts:
            continue
        if is_secret_path(path) or path in out:
            continue  # SEC-01: secret-like files are never read
        out.append(path)
        if len(out) == limit:
            break
    return out


# ---- steps ----------------------------------------------------------------------------------


def _git_status() -> Step:
    return {"op": "git.status", "reason": "to see which files are changed and not committed"}


def _git_log(limit: int, reason: str) -> Step:
    return {"op": "git.log", "limit": limit, "reason": reason}


def _git_tree() -> Step:
    return {"op": "git.tree", "depth": 2, "reason": "to see how the repository is organized"}


def _git_files(ctx: PlanContext, limit: int) -> list[Step]:
    return [
        {"op": "git.file", "path": p, "reason": f"to read {p}, which the request mentions"}
        for p in _hint_files(ctx.hints, limit)
    ]


def _git_greps(ctx: PlanContext, limit: int) -> list[Step]:
    return [
        {"op": "git.grep", "pattern": p, "reason": f"to find where {p!r} appears in the code"}
        for p in _grep_patterns(ctx.hints, limit)
    ]


def _k8s_ready(ctx: PlanContext) -> bool:
    t = ctx.target
    return bool(t.cluster and (t.namespaces or t.all_namespaces) and t.cluster in ctx.workspace.kube_contexts)


def _crash_mentioned(ctx: PlanContext) -> bool:
    texts = [ctx.request, *ctx.hints.grep, *ctx.hints.search_queries]
    return any(CRASH_WORDS.search(t) for t in texts if t)


# ---- devops-investigation -------------------------------------------------------------------


def _devops_questions(ctx: PlanContext) -> list[Question]:
    contexts = ctx.workspace.kube_contexts
    if not contexts:
        return []  # no Kubernetes here: the template lists the cluster as missing
    questions: list[Question] = []
    if ctx.target.cluster is None:
        questions.append(
            Question(
                id="cluster",
                field="cluster",
                text="Which cluster (kube context) should I investigate?",
                options=list(contexts),
            )
        )
    if not ctx.target.namespaces and not ctx.target.all_namespaces:
        questions.append(
            Question(
                id="namespace",
                field="namespace",
                text=f"Which namespace? For several, separate them with commas (at most {MAX_NAMESPACES}); "
                "for all of them, type *.",
            )
        )
    return questions


def _listing_steps(cluster: str, namespace: str, in_ns: str) -> list[Step]:
    """Deployments, pods and events of one namespace. `in_ns` names it in the reasons ("" when
    the task has one namespace: then the reasons are the ones of before)."""
    where = {"cluster": cluster, "namespace": namespace}
    return [
        {
            "op": "k8s.get",
            "kind": "deployments",
            **where,
            "reason": f"to see which deployments{in_ns} do not have all replicas ready",
        },
        {
            "op": "k8s.get",
            "kind": "pods",
            **where,
            "reason": f"to see which pods{in_ns} are not ready or keep restarting",
        },
        {
            "op": "k8s.events",
            **where,
            "reason": f"to see the recent warnings in {in_ns.removeprefix(' in ') or 'the namespace'}",
        },
    ]


def _all_namespaces_steps(cluster: str) -> list[Step]:
    """Deployments, pods and the warnings across all namespaces (the connector keeps the size
    down: the rows with a problem first, then a cut)."""
    where = {"cluster": cluster, "all_namespaces": True}
    return [
        {
            "op": "k8s.get",
            "kind": "deployments",
            **where,
            "reason": "to see which deployments do not have all replicas ready, in all namespaces "
            "(those with a problem first)",
        },
        {
            "op": "k8s.get",
            "kind": "pods",
            **where,
            "reason": "to see which pods are not ready or keep restarting, in all namespaces "
            "(those with a problem first)",
        },
        {"op": "k8s.events", **where, "reason": "to see the newest warnings in all namespaces"},
    ]


def _workload_steps(ctx: PlanContext, kind: str, name: str, namespace: str, in_ns: str = "") -> list[Step]:
    """Describe, rollout and logs of the workload in one namespace."""
    where = {"cluster": ctx.target.cluster, "namespace": namespace}
    steps: list[Step] = [
        {
            "op": "k8s.describe",
            "kind": kind,
            "name": name,
            **where,
            "reason": f"to see the conditions, image and recent events of {name}{in_ns}",
        }
    ]
    if kind == "deployments":
        steps.append(
            {
                "op": "k8s.rollout",
                "deployment": name,
                **where,
                "reason": f"to see whether the last rollout of {name}{in_ns} finished",
            }
        )
    target = {"deployments": "deployment", "pods": "pod"}.get(kind)
    if target:
        steps.append(
            {
                "op": "k8s.logs",
                target: name,
                "tail": LOG_TAIL,
                **where,
                "reason": f"to see the last {LOG_TAIL} log lines of {name}{in_ns}",
            }
        )
        if _crash_mentioned(ctx):
            steps.append(
                {
                    "op": "k8s.logs",
                    target: name,
                    "tail": LOG_TAIL,
                    "previous": True,
                    **where,
                    "reason": f"to see the logs of the run of {name}{in_ns} that crashed",
                }
            )
    return steps


def _kubernetes_steps(ctx: PlanContext) -> list[Step]:
    t = ctx.target
    cluster = t.cluster
    assert cluster is not None  # _k8s_ready
    workload = parse_workload(t.workload)
    if t.namespace is not None:  # one namespace: everything in one round, as before
        steps = _listing_steps(cluster, t.namespace, "")
        if workload:
            steps += _workload_steps(ctx, *workload, t.namespace)
        return steps
    if t.all_namespaces:
        steps = _all_namespaces_steps(cluster)
        if workload:
            kind, name = workload
            steps.append(
                {
                    "op": "k8s.get",
                    "kind": kind,
                    "name": name,
                    "cluster": cluster,
                    "all_namespaces": True,
                    "reason": f"to find the namespaces where {name} is",
                }
            )
        return steps
    steps = []
    for ns in t.namespaces:
        steps += _listing_steps(cluster, ns, f" in {ns}")
        if workload and workload[0] not in ("deployments", "pods"):
            kind, name = workload
            steps.append(
                {
                    "op": "k8s.get",
                    "kind": kind,
                    "cluster": cluster,
                    "namespace": ns,
                    "reason": f"to see whether {name} is in {ns}",
                }
            )
    return steps


def _devops_plan(ctx: PlanContext) -> list[Step]:
    steps: list[Step] = []
    if _k8s_ready(ctx):
        steps += _kubernetes_steps(ctx)
    if ctx.workspace.repo is not None:
        steps.append(_git_status())
        steps.append(_git_log(10, "to see the recent commits that may have caused the problem"))
        steps += _git_greps(ctx, 2)
        steps += _git_files(ctx, 3)
    return steps


# ---- prompt-optimization ----------------------------------------------------------------------


def _no_questions(ctx: PlanContext) -> list[Question]:
    return []


def _prompt_plan(ctx: PlanContext) -> list[Step]:
    if ctx.workspace.repo is None:
        return []
    return [
        _git_status(),
        _git_log(10, "to see what was worked on recently"),
        _git_tree(),
        *_git_files(ctx, 3),
    ]


# ---- engineering-task -----------------------------------------------------------------------


def _engineering_plan(ctx: PlanContext) -> list[Step]:
    if ctx.workspace.repo is None:
        return []  # the template lists the repository as missing
    return [
        _git_status(),
        _git_log(15, "to see what was worked on recently"),
        _git_tree(),
        {"op": "git.diffstat", "reason": "to see the size of the changes not yet committed"},
        *_git_files(ctx, 4),
        *_git_greps(ctx, 3),
    ]


# ---- the table ------------------------------------------------------------------------------

WORKFLOW_TABLE: dict[str, WorkflowSpec] = {
    "devops-investigation": WorkflowSpec(
        name="devops-investigation",
        title="DevOps investigation",
        template="devops-investigation",
        expected_outcome=(
            "The root cause, backed by the evidence: quote the lines that prove it.",
            "A fix written as a plan: numbered steps, each with a command that verifies it.",
            "Nothing applied: no change to the cluster, no deploy, no push.",
        ),
        questions=_devops_questions,
        plan=_devops_plan,
    ),
    "prompt-optimization": WorkflowSpec(
        name="prompt-optimization",
        title="Prompt optimization",
        template="prompt-optimization",
        expected_outcome=(
            "A rewritten, structured prompt, ready for a coding agent: goal, context, steps, "
            "constraints and how to know it is done.",
            "A short list of what you assumed, and the questions that are still open.",
        ),
        questions=_no_questions,
        plan=_prompt_plan,
    ),
    "engineering-task": WorkflowSpec(
        name="engineering-task",
        title="Engineering task",
        template="engineering-task",
        expected_outcome=(
            "The change, made in the repository.",
            "Tests that cover the change, and the test suite passing.",
            "A short summary: what changed, in which files, and how it was tested.",
        ),
        questions=_no_questions,
        plan=_engineering_plan,
    ),
}


def _spec(workflow: str) -> WorkflowSpec:
    try:
        return WORKFLOW_TABLE[workflow]
    except KeyError:
        raise ValueError(f"unknown workflow {workflow!r}") from None


def required_questions(ctx: PlanContext) -> list[Question]:
    """What must be known before evidence is planned (cluster and namespace for devops)."""
    return _spec(ctx.workflow).questions(ctx)


def plan_evidence(ctx: PlanContext) -> list[EvidenceRequest]:
    """The minimum evidence for the workflow, in a fixed order, with ids e1..en."""
    return [EvidenceRequest(id=f"e{i}", **step) for i, step in enumerate(_spec(ctx.workflow).plan(ctx), 1)]


# ---- the second round: a workload in several namespaces, or in all of them ------------------


def _rows(item: EvidenceItem) -> list[list[str]]:
    """The rows of a kubectl table, split on spaces; not the header, not a note."""
    lines = item.output.splitlines() if item.ok and not item.refused else []
    return [line.split() for line in lines[1:] if line.strip() and not line.startswith("[")]


def where_found(ctx: PlanContext, evidence: list[tuple[EvidenceRequest, EvidenceItem]]) -> list[str]:
    """The namespaces where the first round's listings show the named workload, in order."""
    t = ctx.target
    workload = parse_workload(t.workload)
    if workload is None or t.namespace is not None:
        return []
    kind, name = workload
    found: list[str] = []
    for req, item in evidence:
        if req.op != "k8s.get" or (req.kind or "") != kind:
            continue
        if t.all_namespaces and req.all_namespaces and req.name == name:
            names = [row[0] for row in _rows(item) if len(row) > 1 and row[1] == name]
        elif not t.all_namespaces and req.namespace in t.namespaces and req.name is None:
            names = [req.namespace] if any(row and row[0] == name for row in _rows(item)) else []
        else:
            continue
        found += [ns for ns in names if ns not in found and _LABEL.fullmatch(ns)]
    return found


def plan_followup(
    ctx: PlanContext, evidence: list[tuple[EvidenceRequest, EvidenceItem]], first_id: int
) -> tuple[list[EvidenceRequest], str | None]:
    """The second round, for a workload named with several namespaces or all of them: describe,
    rollout and logs in the namespaces where the listing shows it (at most FOLLOWUP_NAMESPACES),
    with ids from e{first_id}. And a note for the review when it is nowhere, or in too many."""
    t = ctx.target
    workload = parse_workload(t.workload)
    if ctx.workflow != "devops-investigation" or workload is None or t.namespace is not None:
        return [], None
    if not _k8s_ready(ctx):
        return [], None
    kind, name = workload
    found = where_found(ctx, evidence)
    where = "any namespace" if t.all_namespaces else "the namespaces " + ", ".join(t.namespaces)
    if not found:
        return [], f"{t.workload} was not found in {where}: nothing more was read about it."
    note = None
    if len(found) > FOLLOWUP_NAMESPACES:
        note = (
            f"{t.workload} is in {len(found)} namespaces; it was looked at in the first "
            f"{FOLLOWUP_NAMESPACES}: {', '.join(found[:FOLLOWUP_NAMESPACES])}."
        )
    steps: list[Step] = []
    for ns in found[:FOLLOWUP_NAMESPACES]:
        steps += _workload_steps(ctx, kind, name, ns, f" in {ns}")
    return [EvidenceRequest(id=f"e{i}", **step) for i, step in enumerate(steps, first_id)], note
