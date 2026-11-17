"""The contracts between the parts of wattson: the A2A payloads between the agents, the interrupts
between the orchestrator and the CLI, and the admin routes. Owner: the integration
(docs/design.md, "Who owns what"). A part that needs more adds it in its own module and says so;
it does not change this file."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


class Contract(BaseModel):
    # Unknown fields are refused: a typo on one side must fail loudly, not be dropped.
    model_config = ConfigDict(extra="forbid")


# ---- workflows and classification ------------------------------------------------------

WORKFLOWS = ("prompt-optimization", "devops-investigation", "engineering-task")
Workflow = Literal["prompt-optimization", "devops-investigation", "engineering-task"]
# "unclear": the request does not say enough to pick a workflow; the orchestrator asks.
CATEGORIES = (*WORKFLOWS, "unclear")
Category = Literal["prompt-optimization", "devops-investigation", "engineering-task", "unclear"]


class Classification(Contract):
    category: Category
    confidence: float = Field(ge=0, le=1)  # softmax of the model's log-probs, or fixed for rules
    method: Literal["model", "rules", "override"]
    model: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)  # per category
    latency_ms: int = 0


# ---- the laptop's context, sent when a task starts (small, no file contents) ----------------


class RepoInfo(Contract):
    name: str  # basename of the top folder
    root_display: str  # the path with the home folder written as "~"
    branch: str | None = None
    head: str | None = None  # short commit hash
    remote: str | None = None  # origin URL with any user:password removed
    dirty_files: int = 0


class WorkspaceInfo(Contract):
    repo: RepoInfo | None = None
    cwd_name: str
    kube_contexts: list[str] = Field(default_factory=list)  # names only
    current_kube_context: str | None = None
    reader_contexts: list[str] = Field(default_factory=list)  # have a read-only kubeconfig


# ---- namespaces: one, several (at most MAX_NAMESPACES), or all of them ------------------------

MAX_NAMESPACES = 5  # more: all namespaces (`--all-namespaces`), which reads less per namespace


def namespace_list(namespace: str | None, namespaces: list[str]) -> list[str]:
    """The old single field and the list as one list: in order, each name once."""
    out: list[str] = []
    for name in [namespace, *namespaces]:
        if name and name not in out:
            out.append(name)
    return out


class Namespaces(Contract):
    """The namespace fields of a model: `namespace`, `namespaces` and `all_namespaces`.

    `namespace` is the old field and still means exactly one namespace: with one, both fields
    hold it; with several, `namespace` is None and `namespaces` has them; with all of them,
    `all_namespaces` is True and both are empty. On the wire, `namespaces` and `all_namespaces`
    are left out when they say nothing more, so a one-namespace message keeps the old shape and
    a part that does not know the new fields still reads it."""

    if TYPE_CHECKING:  # each model declares them, in its own field order
        namespace: str | None
        namespaces: list[str]
        all_namespaces: bool

    @model_validator(mode="after")
    def _one_list(self) -> Self:
        merged = namespace_list(self.namespace, self.namespaces)
        if self.all_namespaces and merged:
            raise ValueError("all namespaces and a namespace list do not go together")
        self.namespaces = merged
        self.namespace = merged[0] if len(merged) == 1 else None
        return self

    @model_serializer(mode="wrap")
    def _old_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if len(self.namespaces) <= 1:
            data.pop("namespaces", None)
        if not self.all_namespaces:
            data.pop("all_namespaces", None)
        return data

    def namespace_words(self) -> str | None:
        """For people: "namespace web", "namespaces web, shop", "all namespaces", or None."""
        if self.all_namespaces:
            return "all namespaces"
        if len(self.namespaces) > 1:
            return "namespaces " + ", ".join(self.namespaces)
        return f"namespace {self.namespace}" if self.namespace else None

    def namespace_short(self) -> str | None:
        """For a list: "web", "{web,shop}", "*", or None."""
        if self.all_namespaces:
            return "*"
        if len(self.namespaces) > 1:
            return "{" + ",".join(self.namespaces) + "}"
        return self.namespace


class Overrides(Namespaces):
    workflow: Workflow | None = None
    cluster: str | None = None
    namespace: str | None = None  # one namespace (the old field; see Namespaces)
    namespaces: list[str] = Field(default_factory=list)  # one or more, at most MAX_NAMESPACES
    all_namespaces: bool = False  # `--all-namespaces`
    workload: str | None = None  # "deployment/api"
    procedure: str | None = None  # a Wattson procedure name ("k8s-pod-crashloop"), or "none"


class Target(Namespaces):
    repo: str | None = None
    cluster: str | None = None  # a kube context name
    namespace: str | None = None
    namespaces: list[str] = Field(default_factory=list)
    all_namespaces: bool = False
    workload: str | None = None


# ---- task states (SEC-15: proposed, approved and completed are different) -------------------

TaskStatus = Literal[
    "triage",  # started, being classified
    "awaiting-input",  # a clarify interrupt is open
    "collecting-evidence",  # a collect-evidence interrupt is open
    "proposed",  # an approve interrupt is open
    "approved",
    "rejected",
    "blocked",  # the Guard stopped it (malicious or not work); nothing ran
    "expired",  # never approved; the working state was dropped
    "submitted",  # a run interrupt is open: Claude Code runs on the laptop
    "completed",
    "failed",
    "interrupted",  # the run stopped without a result, or the CLI went away
]


class Question(Contract):
    id: str
    field: Literal["workflow", "procedure", "cluster", "namespace", "workload", "detail"]
    text: str
    options: list[str] | None = None  # when set, the answer must be one of them


# ---- evidence: only what the orchestrator asks for, run by the CLI's connector --------------

K8S_KINDS = (
    "pods",
    "deployments",
    "statefulsets",
    "daemonsets",
    "replicasets",
    "services",
    "ingresses",
    "jobs",
    "cronjobs",
    "persistentvolumeclaims",
    "horizontalpodautoscalers",
    "configmaps",  # names only: k8s.get lists them, k8s.describe is refused for them
    "nodes",
)
# "secrets" is not a kind anybody can ask for, in any operation.

EvidenceOpName = Literal[
    "git.status",  # no fields
    "git.log",  # limit (default 15, max 50)
    "git.tree",  # depth (default 2), at most 300 lines
    "git.diffstat",  # working tree against HEAD
    "git.file",  # path: one tracked file, at most 12 kB; secret-like names refused
    "git.grep",  # pattern: fixed string, at most 50 lines
    "k8s.get",  # cluster, kind, namespace or all_namespaces (not for nodes), name?
    "k8s.describe",  # cluster, kind (not configmaps), namespace, name
    "k8s.events",  # cluster, namespace: newest 50; all_namespaces: the newest 50 warnings
    "k8s.logs",  # cluster, namespace, pod or deployment, container?, previous?, tail (200, max 500)
    "k8s.rollout",  # cluster, namespace, deployment
]


class EvidenceRequest(Contract):
    # Flat on purpose: a request the connector does not accept is shown as refused, not a crash.
    id: str  # "e1", "e2", ... unique in the task
    op: EvidenceOpName
    reason: str  # shown to the user before anything runs
    cluster: str | None = None
    namespace: str | None = None
    # k8s.get and k8s.events only, instead of a namespace. Left out on the wire when False.
    all_namespaces: bool = False
    kind: str | None = None
    name: str | None = None
    pod: str | None = None
    deployment: str | None = None
    container: str | None = None
    previous: bool = False
    tail: int | None = None
    limit: int | None = None
    depth: int | None = None
    path: str | None = None
    pattern: str | None = None

    @model_serializer(mode="wrap")
    def _old_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if not self.all_namespaces:
            data.pop("all_namespaces", None)
        return data


class EvidenceItem(Contract):
    request_id: str
    ok: bool
    source: str  # what ran, for people: "kubectl --context my-cluster -n web get pods"
    collected_at: str  # ISO time
    output: str = ""  # after redaction and truncation
    truncated: bool = False
    redactions: int = 0
    error: str | None = None
    refused: str | None = None  # the connector refused it; nothing ran


# ---- Grimoire documents and the proposal ---------------------------------------------------


class SourceRef(Contract):
    # A Grimoire document, or (kind "cognee", why "seen_before") a past lesson from Cognee memory.
    kind: Literal["grimoire", "cognee"] = "grimoire"
    # Grimoire document key, e.g. "infra/k8s/crashloop-runbook"; a lesson: "cognee:<dataset>/<title>"
    key: str
    title: str
    folder: str | None = None
    tags: list[str] = Field(default_factory=list)
    # procedure: the chosen Wattson procedure; seen_before: a lesson from Cognee memory
    why: Literal["procedure", "search", "related", "context", "seen_before"]
    retrieved_at: str
    freshness: Literal["live", "cached"]
    excerpt: str | None = None  # at most 600 characters, redacted


class RetrievedDoc(Contract):
    ref: SourceRef
    text: str  # redacted, at most 4000 characters (the lessons from Cognee: 1200 all together)


class EvidenceSummary(Contract):
    request_id: str
    source: str
    collected_at: str
    freshness: Literal["live", "cached"]
    ok: bool
    bytes: int
    redactions: int
    error: str | None = None


class Proposal(Contract):
    prompt: str
    prompt_sha256: str  # hex of the UTF-8 bytes
    template: str
    template_version: str
    sources: list[SourceRef]
    evidence: list[EvidenceSummary]
    constraints: list[str]
    missing_info: list[str]
    redactions: int  # secrets removed while building the prompt


SessionMode = Literal["interactive", "headless"]


class SessionChoice(Contract):
    id: str | None = None  # None: a new Claude Code conversation
    mode: SessionMode = "interactive"
    fork: bool = False


# ---- A2A: orchestrator -> triage agent ----------------------------------------------------


class DecisionHints(Contract):
    # Advice only: the orchestrator checks every value, and nothing here grants anything (FR-DEC-06).
    search_queries: list[str] = Field(default_factory=list)  # 1..3 short queries for Grimoire
    tags: list[str] = Field(default_factory=list)
    cluster: str | None = None  # only a name from workspace.kube_contexts
    namespace: str | None = None
    workload: str | None = None
    files: list[str] = Field(default_factory=list)  # repo paths the request names
    grep: list[str] = Field(default_factory=list)  # error texts or identifiers worth searching
    missing_info: list[str] = Field(default_factory=list)
    recommended_next: Literal["ask", "collect", "propose"] = "collect"  # FR-DEC-03


# ---- Wattson procedures: the documents of Grimoire's folder "wattson" (tag "wattson") -----------

WATTSON_FOLDER = "wattson"


class ProcedureOption(Contract):
    # One Wattson procedure, as the model sees it when it picks one.
    key: str  # Grimoire key, e.g. "wattson/k8s-pod-crashloop"
    name: str  # the last part of the key: the answer the model gives
    when_to_use: str  # its "When to use" line (Grimoire's summary), at most 300 characters
    workflow: Workflow | None = None  # from its tags
    signals: list[str] = Field(default_factory=list)  # its other tags: words that requests contain


class ProcedurePick(Contract):
    # The procedure chosen for a task. Advice like the classification: the user can change it.
    key: str | None = None  # None: no procedure fits
    name: str | None = None
    method: Literal["model", "signals", "user", "none"] = "none"
    agreed: bool = False  # the model's pick and the signal words of the request agree
    latency_ms: int = 0


class TriageRequest(Contract):
    request: str
    workspace: WorkspaceInfo
    overrides: Overrides = Field(default_factory=Overrides)
    procedures: list[ProcedureOption] = Field(default_factory=list)  # empty: nothing to pick


class TriageResult(Contract):
    classification: Classification
    hints: DecisionHints
    warnings: list[str] = Field(default_factory=list)
    procedure: ProcedurePick | None = None  # None: no options were given


# ---- A2A: orchestrator -> knowledge agent ---------------------------------------------------


class KnowledgeRequest(Contract):
    request: str
    queries: list[str]
    tags: list[str] = Field(default_factory=list)
    limit: int = Field(default=4, ge=1, le=8)
    # The chosen Wattson procedure: read it first (why="procedure"), then the runbooks it lists
    # under "Related runbooks" (why="related"). With a procedure, no free search is done.
    procedure_key: str | None = None


class KnowledgeResult(Contract):
    # The Grimoire documents, then the lessons from Cognee memory (kind "cognee"), if any.
    docs: list[RetrievedDoc] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # e.g. "Grimoire is not connected: ..."


class ProcedureListRequest(Contract):
    folder: str = WATTSON_FOLDER


class ProcedureList(Contract):
    procedures: list[ProcedureOption] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---- A2A: CLI -> orchestrator, and the interrupts back ----------------------------------------

# The DataPart keys of the first message of each agent's skill.
START_TASK_KEY = "wattson.start_task"
TRIAGE_KEY = "wattson.triage"
FIND_PROCEDURES_KEY = "wattson.find_procedures"
LIST_PROCEDURES_KEY = "wattson.list_procedures"  # knowledge agent: ProcedureListRequest -> ProcedureList


class StartTask(Contract):
    request: str = Field(min_length=1, max_length=4000)
    workspace: WorkspaceInfo
    overrides: Overrides = Field(default_factory=Overrides)


# ---- the Guard: Strands interventions on the orchestrator -------------------------------------
# Fixed rules first, the small model (the agent's aux_model) second. The model may only make a
# verdict stricter (Deny / Confirm), never allow what a rule forbids (FR-DEC-06).

GuardLabel = Literal["work", "out_of_scope", "risky", "malicious"]
GuardMode = Literal["enforce", "monitor", "off"]  # monitor: record the verdict, block nothing


class GuardVerdict(Contract):
    label: GuardLabel
    reasons: list[str] = Field(default_factory=list)  # short plain sentences; never the request text
    rule_hits: list[str] = Field(default_factory=list)  # names of the fixed rules that matched
    method: Literal["rules", "model", "rules+model", "off"]
    model: str | None = None
    latency_ms: int = 0
    mode: GuardMode = "monitor"
    evidence_removed: int = 0  # evidence/document parts cut out as possible prompt injection
    # The injection model (INJECTION_MODE warn or cut, the pod wattson-shield): its name, the parts
    # it flagged as orders to the agent, and whether it failed to answer (the rules alone checked).
    injection_model: str | None = None
    injection_flagged: int = 0
    injection_failed: bool = False
    # The same model on the request itself (pasted text that gives orders to the agent): its score as
    # a whole percent (None: not checked), whether it reached the threshold, whether it failed.
    request_injection_pct: int | None = None
    request_injection_flagged: bool = False
    request_injection_failed: bool = False


# Interrupt names. Each is raised by one orchestrator tool; the CLI answers each with its own UI.
CLARIFY = "wattson.clarify"
COLLECT_EVIDENCE = "wattson.collect_evidence"
APPROVE = "wattson.approve"
RUN_CLAUDE_CODE = "wattson.run_claude_code"
GUARD_CONFIRM = "wattson.guard_confirm"  # a risky request, or orders in it: go on, or stop


class GuardConfirmReason(Contract):
    task_id: str
    verdict: GuardVerdict
    # plain text for the user, e.g. "This asks to delete pods. Go on with a read-only investigation?"
    question: str
    # True: a yes goes on read-only (a risky request). False: a yes goes on as usual (only the
    # injection model read orders for the agent in the request).
    read_only: bool = True


class GuardConfirmResponse(Contract):
    proceed: bool  # True: go on (read-only when the reason says so); False: stop the task ("blocked")


class ClarifyReason(Contract):
    task_id: str
    questions: list[Question]
    warnings: list[str] = Field(default_factory=list)


class ClarifyResponse(Contract):
    answers: dict[str, str] = Field(default_factory=dict)  # question id -> answer
    cancel: bool = False


class CollectEvidenceReason(Contract):
    task_id: str
    target: Target
    plan: list[EvidenceRequest]
    warnings: list[str] = Field(default_factory=list)


class CollectEvidenceResponse(Contract):
    items: list[EvidenceItem] = Field(default_factory=list)
    declined: bool = False  # the user said no: the orchestrator goes on without evidence


class ApproveReason(Contract):
    task_id: str
    workflow: Workflow
    classification: Classification | None
    target: Target
    proposal: Proposal
    warnings: list[str] = Field(default_factory=list)
    guard: GuardVerdict | None = None  # shown at the review


class ApproveResponse(Contract):
    # "answer": the user answered missing information; the orchestrator renders again.
    decision: Literal["approve", "reject", "answer"]
    prompt_sha256: str | None = None  # of the text that will be sent, after the user's edits
    edited: bool = False
    session: SessionChoice | None = None
    reason: str | None = None  # for reject, at most 500 characters
    answers: dict[str, str] = Field(default_factory=dict)  # for "answer": missing info -> answer


class RunReason(Contract):
    task_id: str
    prompt_sha256: str  # the CLI sends Claude Code only the text with this hash
    session: SessionChoice


class RunResponse(Contract):
    state: Literal["completed", "failed", "interrupted"]
    session_id: str | None = None
    summary: str | None = None  # redacted by the CLI, at most 2000 characters
    error: str | None = None  # at most 500 characters
    exit_code: int | None = None
    duration_ms: int | None = None


class TaskOutcome(Contract):
    # The final A2A artifact of a task.
    task_id: str
    status: TaskStatus
    workflow: Workflow | None = None
    summary: str | None = None
    source_keys: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---- admin routes (plain HTTP next to A2A, same bearer token) --------------------------------


class TaskRecord(Namespaces):
    # What is kept after the working state is gone: metadata only (MEM-01..03, SEC-14).
    id: str
    created_at: str
    updated_at: str
    status: TaskStatus
    workflow: Workflow | None = None
    category: Category | None = None
    confidence: float | None = None
    method: Literal["model", "rules", "override"] | None = None
    repo: str | None = None
    cluster: str | None = None
    namespace: str | None = None
    namespaces: list[str] = Field(default_factory=list)  # the store keeps all three in "namespace"
    all_namespaces: bool = False
    source_keys: list[str] = Field(default_factory=list)
    proposal_sha256: str | None = None
    approved_sha256: str | None = None
    edited: bool | None = None
    session_mode: SessionMode | None = None
    claude_session_id: str | None = None
    error_summary: str | None = None


class TaskEvent(Contract):
    task_id: str
    at: str
    from_status: TaskStatus | None = None
    to_status: TaskStatus
    detail: str | None = None  # redacted, at most 500 characters


class ExecutionReport(Contract):
    # A late result (POST /v1/tasks/{id}/execution) when the A2A task is gone: `wattson recover`.
    result: RunResponse


class GrimoireStatus(Contract):
    connected: bool
    mcp_url: str
    tools: list[str] = Field(default_factory=list)  # the allowed read-only tools it found
    last_error: str | None = None


class GrimoireConnectRequest(Contract):
    redirect_uri: str  # http://127.0.0.1:<port>/callback, served by the CLI


class GrimoireConnectView(Contract):
    authorize_url: str


class GrimoireCallback(Contract):
    code: str
    state: str
    iss: str | None = None  # RFC 9207 issuer, when the login server adds it to the redirect


class ComponentHealth(Contract):
    ok: bool
    detail: str | None = None


class HealthView(Contract):
    ok: bool
    version: str
    triage: ComponentHealth  # includes the model
    knowledge: ComponentHealth
    grimoire: GrimoireStatus | None = None
    history: dict[str, int] = Field(default_factory=dict)  # tasks, retention_days
    # The injection model (the pod wattson-shield); None with INJECTION_MODE=off. Not part of
    # "ok": without it the Guard checks with its rules alone.
    shield: ComponentHealth | None = None
