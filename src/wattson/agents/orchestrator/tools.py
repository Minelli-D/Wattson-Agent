"""The orchestrator's steps, as Strands tools (docs/design.md, "The orchestrator's steps").

Each tool keeps bulky data in agent.state and returns step(next=..., output=<small summary>).
The planner calls the tool named in "next". Human steps are interrupts; a tool runs again from
the top when the answer arrives, so everything before tool_context.interrupt() must be safe to
repeat. A second round (more questions, "answer" on approval) goes back through the planner:
a new tool call gets a new interrupt id.

The tools never write the store: they queue status changes in the working state and the audit
hook writes them (hooks.py)."""

from __future__ import annotations

import hashlib
import secrets
import string
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError
from strands import tool
from strands.types.tools import ToolContext

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.flow import (
    MAX_ROUNDS,
    TERMINAL,
    describe_errors,
    is_namespace,
    is_session_id,
    is_sha256,
    is_workload,
    now_iso,
)
from wattson.agents.orchestrator.remote import RemoteError
from wattson.agents.orchestrator.telemetry import task_end_of
from wattson.agents.triage import procedures as proc
from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    MAX_NAMESPACES,
    RUN_CLAUDE_CODE,
    START_TASK_KEY,
    WORKFLOWS,
    ApproveReason,
    ApproveResponse,
    ClarifyReason,
    ClarifyResponse,
    Classification,
    CollectEvidenceReason,
    CollectEvidenceResponse,
    DecisionHints,
    EvidenceItem,
    EvidenceRequest,
    EvidenceSummary,
    GuardVerdict,
    KnowledgeRequest,
    KnowledgeResult,
    Overrides,
    ProcedureList,
    ProcedureListRequest,
    ProcedureOption,
    ProcedurePick,
    Proposal,
    Question,
    RetrievedDoc,
    RunReason,
    RunResponse,
    SessionChoice,
    StartTask,
    Target,
    TaskOutcome,
    TriageRequest,
    TriageResult,
    WorkspaceInfo,
)
from wattson.shared.planner import parse_structured, step
from wattson.shared.redact import redact

MAX_DOC_TEXT = 4000
MAX_EXCERPT = 600
MAX_EVIDENCE_OUTPUT = 64_000
MAX_ANSWER = 1000
MAX_SUMMARY = 2000
MAX_ERROR = 500
MAX_WHEN = 300
_BASE36 = string.digits + string.ascii_lowercase


class RemoteAgents(Protocol):
    async def triage(self, request: TriageRequest) -> TriageResult: ...

    async def list_procedures(self, request: ProcedureListRequest) -> ProcedureList: ...

    async def find_procedures(self, request: KnowledgeRequest) -> KnowledgeResult: ...


# ---- small helpers ---------------------------------------------------------------------------


def new_task_id(now: datetime | None = None) -> str:
    day = (now or datetime.now(UTC)).strftime("%Y%m%d")
    return f"t-{day}-" + "".join(secrets.choice(_BASE36) for _ in range(6))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate[M: BaseModel](model: type[M], value: Any, what: str) -> M:
    try:
        return model.model_validate(value)
    except ValidationError as e:
        # A wrong answer from the CLI is a bug on that side: fail loudly, never guess.
        raise ValueError(f"the {what} is not valid: {describe_errors(e)}") from None


def _clean(task: dict[str, Any], text: str | None, limit: int) -> str | None:
    """Redact and cut text that came from outside; count what was removed."""
    if text is None:
        return None
    result = redact(text)
    if result.count:
        task["redactions"] = task.get("redactions", 0) + result.count
    return result.text[:limit]


def _workspace(task: dict[str, Any]) -> WorkspaceInfo:
    return WorkspaceInfo.model_validate(task["workspace"])


def _plan_context(task: dict[str, Any]) -> Any:
    from wattson.agents.orchestrator.workflows import PlanContext

    return PlanContext(
        workflow=task["workflow"],
        target=Target.model_validate(task.get("target") or {}),
        workspace=_workspace(task),
        hints=DecisionHints.model_validate(task.get("hints") or {}),
        request=task.get("request") or "",  # lets the devops plan add the logs of a crashed run
    )


def make_target(
    task: dict[str, Any], workspace: WorkspaceInfo, overrides: Overrides, hints: DecisionHints
) -> Target:
    """The target from overrides first, then hints. Every value is checked: hints are advice."""
    contexts = set(workspace.kube_contexts)
    cluster = overrides.cluster or hints.cluster
    if cluster and cluster not in contexts:
        source = "you named" if overrides.cluster else "triage suggested"
        w.warn(task, f"The cluster {cluster!r} {source} is not one of your kube contexts; it was dropped.")
        cluster = None
    namespaces = overrides.namespaces or ([hints.namespace] if hints.namespace else [])
    if overrides.all_namespaces:
        namespaces = []
    if any(not is_namespace(ns) for ns in namespaces):
        w.warn(task, "A namespace that is not a valid Kubernetes name was dropped.")
        namespaces = [ns for ns in namespaces if is_namespace(ns)]
    if len(namespaces) > MAX_NAMESPACES:
        w.warn(
            task,
            f"At most {MAX_NAMESPACES} namespaces are read one by one; the others were dropped. "
            "For more, use all namespaces (--all-namespaces).",
        )
        namespaces = namespaces[:MAX_NAMESPACES]
    workload = overrides.workload or hints.workload
    if workload and not is_workload(workload):
        w.warn(task, "A workload that is not a valid Kubernetes name was dropped.")
        workload = None
    return Target(
        repo=workspace.repo.name if workspace.repo else None,
        cluster=cluster,
        namespaces=namespaces,
        all_namespaces=overrides.all_namespaces,
        workload=workload,
    )


def parse_namespaces(value: str) -> tuple[list[str], bool, str | None]:
    """An answer to "Which namespace?": (the names, all of them?, the problem if any).
    "web", "web, shop" (at most MAX_NAMESPACES), or "*" for all of them."""
    if value.strip() == "*":
        return [], True, None
    names: list[str] = []
    for raw in value.split(","):
        name = raw.strip()
        if not name or name in names:
            continue
        if not is_namespace(name):
            what = "The namespace must be a Kubernetes name: lower case letters, digits and '-'"
            return [], False, f"{what} ({name[:63]!r} is not)."
        names.append(name)
    if len(names) > MAX_NAMESPACES:
        return [], False, f"There must be at most {MAX_NAMESPACES} namespaces; for all of them, type *."
    return names, False, None


# ---- the Wattson procedure ---------------------------------------------------------------------


def usable_options(task: dict[str, Any], options: list[ProcedureOption]) -> list[ProcedureOption]:
    """The procedures as the triage model will see them: one per name, short, redacted."""
    return [
        o.model_copy(update={"when_to_use": _clean(task, o.when_to_use, MAX_WHEN) or ""})
        for o in proc.usable(options)
    ]


def settle_procedure(
    task: dict[str, Any],
    options: list[ProcedureOption],
    pick: ProcedurePick | None,
    overrides: Overrides,
    workflow: str | None = None,
) -> tuple[ProcedurePick | None, dict[str, str]]:
    """The triage's pick checked against the options (advice: a key that is not an option is
    dropped), and the candidates to ask about ({name: key}; empty: no question). Without a pick
    from triage: the user's procedure, else the signal words of the request. The signal words and
    the question look only at the procedures of the workflow, as the triage did (D42)."""
    named = (overrides.procedure or "").strip()
    if not options:
        if named and named.lower() != proc.NONE:
            w.warn(task, f"The procedure you named ({named[:80]!r}) cannot be used: no Wattson procedures.")
        return None, {}
    request = task.get("request") or ""
    if pick is None:
        chosen, problem = proc.resolve_override(named, options)
        if chosen is not None:
            return chosen, {}
        if problem:
            w.warn(task, f"The procedure you named ({named[:80]!r}) is not a Wattson procedure; ignored.")
        pick = proc.decide(request, proc.for_workflow(options, workflow), None, "triage gave no pick").pick
        if pick.key:
            w.warn(task, f"The procedure {pick.name} was chosen by the words of your request.")
    by_key = {o.key: o for o in options}
    by_name = {o.name: o for o in options}
    if pick.key is not None:
        option = by_key.get(pick.key)
        if option is None:
            w.warn(task, "Triage suggested a procedure that is not a Wattson procedure; it was dropped.")
            return ProcedurePick(method="none"), {}
        pick = pick.model_copy(update={"name": option.name})
    elif pick.name is not None and pick.name not in by_name:
        pick = pick.model_copy(update={"name": None})
    ask = proc.ask_candidates(pick, request, proc.for_workflow(options, workflow))
    return pick, {name: by_name[name].key for name in ask}


def procedure_question(task: dict[str, Any]) -> Question | None:
    """Which procedure fits, when the model and the words of the request do not agree."""
    ask = list((task.get("procedure_ask") or {}).keys())
    if not ask or "procedure" in (task.get("answered_ids") or []):
        return None
    pick = task.get("procedure") or {}
    if pick.get("key"):
        others = ", ".join(n for n in ask if n != pick.get("name"))
        text = (
            f"The small model chose the Wattson procedure {pick.get('name')}, but the words of your "
            f"request point to {others}. Which procedure fits?"
        )
    else:
        text = (
            "The small model found no Wattson procedure, but the words of your request point to "
            f"{ask[0]}. Which procedure fits?"
        )
    return Question(id="procedure", field="procedure", text=text, options=[*ask, proc.NONE])


def describe_procedure(task: dict[str, Any]) -> str | None:
    """For the task events (the record has no field for it): which procedure, and how chosen."""
    pick = task.get("procedure")
    if not pick:
        return None
    if pick.get("key"):
        how = pick.get("method")
        if how == "model" and not pick.get("agreed"):
            how = "model; the words of the request do not confirm it"
        return f"procedure {pick.get('name')} ({how})"
    if task.get("procedure_ask") and "procedure" not in (task.get("answered_ids") or []):
        return f"procedure to ask: {', '.join(task['procedure_ask'])}"
    return f"no procedure ({pick.get('method')})"


def open_questions(task: dict[str, Any]) -> list[Question]:
    """What must be known before evidence is planned. Already answered questions are not asked again."""
    procedure = procedure_question(task)
    extra = [procedure] if procedure is not None else []
    if not task.get("workflow"):
        return [
            Question(
                id="workflow",
                field="workflow",
                text="Which kind of task is this?",
                options=list(WORKFLOWS),
            ),
            *extra,
        ]
    from wattson.agents.orchestrator.workflows import required_questions

    answered = set(task.get("answered_ids") or [])
    return [*extra, *(q for q in required_questions(_plan_context(task)) if q.id not in answered)]


def apply_answers(task: dict[str, Any], questions: list[Question], answers: dict[str, str]) -> list[str]:
    """Check each answer against its question and apply it. Returns the problems, for the next round."""
    problems: list[str] = []
    contexts = set(_workspace(task).kube_contexts)
    target = dict(task.get("target") or {})
    by_id = {q.id: q for q in questions}
    for qid in answers:
        if qid not in by_id:
            problems.append(f"An answer to an unknown question ({qid[:40]!r}) was ignored.")
    for q in questions:
        raw = answers.get(q.id)
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            continue
        if q.options is not None and value not in q.options:
            problems.append(f"{q.text} The answer must be one of: {', '.join(q.options)}.")
            continue
        if q.field == "workflow":
            if value not in WORKFLOWS:
                problems.append(f"The workflow must be one of: {', '.join(WORKFLOWS)}.")
                continue
            task["workflow"] = value
        elif q.field == "cluster":
            if value not in contexts:
                problems.append("The cluster must be one of your kube contexts.")
                continue
            target["cluster"] = value
        elif q.field == "namespace":
            names, everything, problem = parse_namespaces(value)
            if problem is not None or not (names or everything):
                problems.append(problem or "The namespace must be a Kubernetes name.")
                continue
            for key in ("namespace", "namespaces", "all_namespaces"):
                target.pop(key, None)
            if everything:
                target["all_namespaces"] = True
            elif len(names) == 1:
                target["namespace"] = names[0]  # one namespace: the shape of before
            else:
                target["namespaces"] = names
        elif q.field == "workload":
            if not is_workload(value):
                problems.append("The workload must look like 'deployment/api' or 'api'.")
                continue
            target["workload"] = value
        elif q.field == "procedure":
            keys = task.get("procedure_ask") or {}
            if value == proc.NONE:
                task["procedure"] = ProcedurePick(method="user").model_dump(mode="json")
            elif value in keys:
                pick = ProcedurePick(key=keys[value], name=value, method="user")
                task["procedure"] = pick.model_dump(mode="json")
            else:
                problems.append(f"The procedure must be one of: {', '.join([*keys, proc.NONE])}.")
                continue
        else:
            task.setdefault("answers", {})[q.text] = _clean(task, value, MAX_ANSWER)
        task.setdefault("answered_ids", []).append(q.id)
    task["target"] = target
    return problems


def _evidence_summary(request_id: str, item: EvidenceItem) -> EvidenceSummary:
    return EvidenceSummary(
        request_id=request_id,
        source=item.source,
        collected_at=item.collected_at,
        freshness="live",
        ok=item.ok,
        bytes=len(item.output.encode("utf-8")),
        redactions=item.redactions,
        error=item.error,
    )


# ---- the tools ---------------------------------------------------------------------------------


def make_tools(remotes: RemoteAgents) -> list[Any]:
    """The eleven steps, bound to the remote agents. Stateless: one list serves every context."""

    @tool(context=True)
    def start_task(message: str, tool_context: ToolContext) -> str:
        """Entry step: read the StartTask DataPart, make the task id, start the record."""
        agent = tool_context.agent
        data = parse_structured(message).get(START_TASK_KEY)
        if data is None:
            return step(
                output={"error": f"the first message needs a DataPart with the key {START_TASK_KEY!r}"}
            )
        try:
            start = StartTask.model_validate(data)
        except ValidationError as e:
            return step(output={"error": f"the start_task data is not valid: {describe_errors(e)}"})
        w.reset(agent)
        task: dict[str, Any] = {
            "id": new_task_id(),
            "created_at": now_iso(),
            "status": None,
            "workspace": start.workspace.model_dump(mode="json"),
            "overrides": start.overrides.model_dump(mode="json"),
            "warnings": [],
            "answers": {},
            "missing_answers": {},
            "answered_ids": [],
            "rounds": {"clarify": 0, "approve": 0},
            "redactions": 0,
        }
        task["request"] = _clean(task, start.request, 8000)
        if task["redactions"]:
            w.warn(task, f"{task['redactions']} secret(s) were removed from your request.")
        w.mark(task, "triage", "task started")
        w.save_task(agent, task)
        return step("guard", output={"task_id": task["id"], "status": "triage"})

    @tool(context=True)
    def guard(tool_context: ToolContext) -> str:
        """The Guard's step: the request check (rules + the small model) runs in the before-tool
        intervention (guard/interventions.py) and leaves its decision here. In enforce mode a
        blocked request ends the task cleanly; otherwise the flow goes on."""
        agent = tool_context.agent
        task = w.load_task(agent)
        blocked = task.pop("guard_block", None)
        if blocked:
            task["summary"] = blocked
            w.mark(task, "blocked", "the Guard stopped the request")
            w.save_task(agent, task)
            return step("finish", output={"blocked": True})
        verdict = task.get("guard") or {}
        w.save_task(agent, task)
        return step(
            "list_procedures",
            output={"guard": verdict.get("label", "off"), "read_only": bool(task.get("guard_readonly"))},
        )

    @tool(context=True)
    async def list_procedures(tool_context: ToolContext) -> str:
        """Ask the knowledge agent for the Wattson procedures (Grimoire folder "wattson"). A failure
        is a warning: then no procedure is picked and Grimoire is searched freely."""
        agent = tool_context.agent
        task = w.load_task(agent)
        options: list[ProcedureOption] = []
        try:
            result = await remotes.list_procedures(ProcedureListRequest())
            for text in result.warnings:
                w.warn(task, _clean(task, text, 300) or "")
            options = usable_options(task, result.procedures)
        except Exception as e:  # noqa: BLE001 - NFR-09: report the limit, do not invent
            reason = str(e) if isinstance(e, RemoteError) else type(e).__name__
            w.warn(task, f"Wattson procedures are not available ({reason}); Grimoire is searched freely.")
        agent.state.set(w.PROCEDURES, [o.model_dump(mode="json") for o in options])
        w.mark(task, task["status"], f"{len(options)} Wattson procedure(s)", note=True)
        w.save_task(agent, task)
        return step("triage", output={"procedures": len(options)})

    @tool(context=True)
    async def triage(tool_context: ToolContext) -> str:
        """Ask the triage agent for the workflow and hints. Overrides win; hints are checked."""
        agent = tool_context.agent
        task = w.load_task(agent)
        workspace = _workspace(task)
        overrides = Overrides.model_validate(task["overrides"])
        options = [ProcedureOption.model_validate(o) for o in w.get(agent, w.PROCEDURES, [])]
        request = TriageRequest(
            request=task["request"], workspace=workspace, overrides=overrides, procedures=options
        )
        pick: ProcedurePick | None = None
        try:
            result = await remotes.triage(request)
            classification, hints, pick = result.classification, result.hints, result.procedure
            for text in result.warnings:
                w.warn(task, _clean(task, text, 300) or "")
        except Exception as e:  # noqa: BLE001 - any failure here means: decide by asking the user
            classification = Classification(category="unclear", confidence=0.0, method="rules")
            hints = DecisionHints()
            reason = str(e) if isinstance(e, RemoteError) else type(e).__name__
            w.warn(task, f"Triage is not available ({reason}); please choose the workflow.")
        workflow = overrides.workflow or classification.category
        procedure, ask = settle_procedure(task, options, pick, overrides, workflow)
        task["procedure"] = procedure.model_dump(mode="json") if procedure is not None else None
        task["procedure_ask"] = ask
        if overrides.workflow:
            classification = classification.model_copy(
                update={"category": overrides.workflow, "method": "override", "confidence": 1.0}
            )
        task["classification"] = classification.model_dump(mode="json")
        task["hints"] = hints.model_dump(mode="json")
        task["workflow"] = None if classification.category == "unclear" else classification.category
        task["target"] = make_target(task, workspace, overrides, hints).model_dump(mode="json")
        detail = (
            f"classified {classification.category} by {classification.method}"
            f" ({classification.confidence:.2f})"
        )
        about_procedure = describe_procedure(task)
        w.mark(task, "triage", detail + (f"; {about_procedure}" if about_procedure else ""), note=True)
        w.save_task(agent, task)
        return step(
            "clarify",
            output={
                "category": classification.category,
                "method": classification.method,
                "confidence": classification.confidence,
                "procedure": procedure.name if procedure is not None and procedure.key else None,
            },
        )

    @tool(context=True)
    async def find_procedures(tool_context: ToolContext) -> str:
        """Ask the knowledge agent for Grimoire documents: the chosen Wattson procedure and its
        runbooks, or a free search when there is none; and the lessons "seen before" in Cognee
        memory, when the knowledge agent has them on. A failure is a warning, never invented docs.
        The Guard (EvidenceGuard) checks every document and lesson after this step."""
        agent = tool_context.agent
        task = w.load_task(agent)
        hints = DecisionHints.model_validate(task.get("hints") or {})
        queries = [q.strip()[:200] for q in hints.search_queries if q.strip()][:3] or [task["request"][:200]]
        request = KnowledgeRequest(
            request=task["request"],
            queries=queries,
            tags=hints.tags[:8],
            limit=4,
            procedure_key=(task.get("procedure") or {}).get("key"),
        )
        docs: list[RetrievedDoc] = []
        try:
            result = await remotes.find_procedures(request)
            for text in result.warnings:
                w.warn(task, _clean(task, text, 300) or "")
            for doc in result.docs:
                ref = doc.ref.model_copy(update={"excerpt": _clean(task, doc.ref.excerpt, MAX_EXCERPT)})
                docs.append(RetrievedDoc(ref=ref, text=_clean(task, doc.text, MAX_DOC_TEXT) or ""))
        except Exception as e:  # noqa: BLE001 - NFR-09: report the limit, do not invent
            reason = str(e) if isinstance(e, RemoteError) else type(e).__name__
            w.warn(task, f"Grimoire procedures are not available ({reason}); the prompt has none.")
        agent.state.set(w.DOCS, [d.model_dump(mode="json") for d in docs])
        task["source_keys"] = [d.ref.key for d in docs]
        about_procedure = describe_procedure(task)
        lessons = sum(1 for d in docs if d.ref.kind == "cognee")  # "seen before", from Cognee memory
        detail = f"{len(docs) - lessons} Grimoire document(s)"
        detail += f", {lessons} lesson(s) from Cognee memory" if lessons else ""
        detail += f"; {about_procedure}" if about_procedure else ""
        w.mark(task, task["status"], detail, note=True)
        w.save_task(agent, task)
        return step("plan_evidence", output={"docs": len(docs)})

    @tool(context=True)
    def clarify(tool_context: ToolContext) -> str:
        """Ask what must be known (workflow when unclear; the procedure when the model and the words
        of the request disagree; cluster and namespace for devops)."""
        agent = tool_context.agent
        task = w.load_task(agent)
        questions = open_questions(task)
        if not questions:
            if task.pop("clarify_problems", None) is not None:
                w.save_task(agent, task)
            return step("find_procedures", output={"questions": 0})
        if task["rounds"]["clarify"] >= MAX_ROUNDS:
            task["error_summary"] = f"no usable answers after {MAX_ROUNDS} rounds of questions"
            w.mark(task, "failed", task["error_summary"])
            w.save_task(agent, task)
            return step("finish", output={"error": task["error_summary"]})
        w.mark(
            task,
            "awaiting-input",
            f"asked {len(questions)} question(s): {', '.join(q.field for q in questions)}",
        )
        w.save_task(agent, task)
        # This round's problems with the last answers are shown once, next to the questions.
        warnings = [*task["warnings"], *task.get("clarify_problems", [])]
        reason = ClarifyReason(task_id=task["id"], questions=questions, warnings=warnings)
        response = tool_context.interrupt(CLARIFY, reason=reason.model_dump(mode="json"))

        answer = _validate(ClarifyResponse, response, "clarify answer")
        task = w.load_task(agent)
        if answer.cancel:
            task["summary"] = "You cancelled the task."
            w.mark(task, "rejected", "cancelled by the user at a question")
            w.save_task(agent, task)
            return step("finish", output={"cancelled": True})
        problems = apply_answers(task, questions, answer.answers)
        task["clarify_problems"] = problems
        task["rounds"]["clarify"] += 1
        w.save_task(agent, task)
        # Back through the planner: check again, and ask again (new interrupt id) if needed.
        return step("clarify", output={"answered": len(answer.answers), "problems": len(problems)})

    @tool(context=True)
    def plan_evidence(tool_context: ToolContext) -> str:
        """The minimum evidence for the workflow (git and kubectl reads, run by the CLI)."""
        from wattson.agents.orchestrator.workflows import plan_evidence as plan_for

        agent = tool_context.agent
        task = w.load_task(agent)
        if not task.get("workflow"):
            raise ValueError("the workflow is still unknown; cannot plan evidence")
        plan: list[EvidenceRequest] = []
        seen: set[str] = set()
        for request in plan_for(_plan_context(task)):
            if request.id in seen:
                continue
            seen.add(request.id)
            plan.append(request)
        agent.state.set(w.PLAN, [r.model_dump(mode="json") for r in plan])
        if not plan:
            return step("compose", output={"evidence": 0})
        return step("collect_evidence", output={"evidence": len(plan)})

    @tool(context=True)
    def collect_evidence(tool_context: ToolContext) -> str:
        """Ask the CLI to run the plan. Only planned items are kept, and they are redacted again.
        With several namespaces or all of them and a named workload, a second round follows:
        the workload where the first round's listing shows it (workflows.plan_followup)."""
        from wattson.agents.orchestrator.workflows import plan_followup

        agent = tool_context.agent
        task = w.load_task(agent)
        if task.get("evidence_round") == 2 and w.get(agent, w.EVIDENCE_BEFORE) is None:
            # The first round is checked already (the Guard checks "evidence" after each call):
            # keep it apart, so the second round's check reads only the new items. Safe to repeat.
            agent.state.set(w.EVIDENCE_BEFORE, w.get(agent, w.EVIDENCE, []))
            agent.state.set(w.EVIDENCE, [])
        plan = [EvidenceRequest.model_validate(r) for r in w.get(agent, w.PLAN, [])]
        w.mark(task, "collecting-evidence", f"asked for {len(plan)} evidence item(s)")
        w.save_task(agent, task)
        reason = CollectEvidenceReason(
            task_id=task["id"],
            target=Target.model_validate(task["target"]),
            plan=plan,
            warnings=list(task["warnings"]),
        )
        response = tool_context.interrupt(COLLECT_EVIDENCE, reason=reason.model_dump(mode="json"))

        answer = _validate(CollectEvidenceResponse, response, "evidence answer")
        task = w.load_task(agent)
        planned = {r.id: r for r in plan}
        kept: dict[str, EvidenceItem] = {}
        refused = 0
        if answer.declined and task.get("evidence_round") == 2:
            w.warn(task, "You declined the second round of evidence; the prompt has the first round only.")
        elif answer.declined:
            w.warn(task, "You declined the evidence collection; the prompt has no live evidence.")
        else:
            for item in answer.items:
                if item.request_id not in planned:
                    refused += 1
                    w.warn(task, f"Evidence for {item.request_id[:20]!r} was not asked for; it was refused.")
                    continue
                if item.request_id in kept:
                    continue
                text = item.output or ""
                cleaned = redact(text)
                output = cleaned.text
                truncated = item.truncated or len(output) > MAX_EVIDENCE_OUTPUT
                task["redactions"] = task.get("redactions", 0) + cleaned.count
                kept[item.request_id] = item.model_copy(
                    update={
                        "output": output[:MAX_EVIDENCE_OUTPUT],
                        "truncated": truncated,
                        "redactions": item.redactions + cleaned.count,
                        "error": _clean(task, item.error, MAX_ERROR),
                        "refused": _clean(task, item.refused, MAX_ERROR),
                        "source": _clean(task, item.source, 300) or "",
                    }
                )
        pairs = [
            [planned[rid].model_dump(mode="json"), kept[rid].model_dump(mode="json")]
            for rid in planned
            if rid in kept
        ]
        agent.state.set(w.EVIDENCE, pairs)
        detail = (
            "declined by the user" if answer.declined else f"received {len(pairs)} item(s), refused {refused}"
        )
        w.mark(task, "collecting-evidence", f"evidence {detail}", note=True)
        followup: list[EvidenceRequest] = []
        if not answer.declined and task.get("evidence_round", 1) == 1:
            first = [(EvidenceRequest.model_validate(r), EvidenceItem.model_validate(i)) for r, i in pairs]
            followup, note = plan_followup(_plan_context(task), first, len(plan) + 1)
            if note:
                w.warn(task, note)
        if followup:
            task["evidence_round"] = 2
            agent.state.set(w.PLAN, [r.model_dump(mode="json") for r in followup])
        w.save_task(agent, task)
        out = {"items": len(pairs), "refused": refused, "declined": answer.declined}
        if followup:
            return step("collect_evidence", output={**out, "second_round": len(followup)})
        return step("compose", output=out)

    @tool(context=True)
    def compose(tool_context: ToolContext) -> str:
        """Render the deterministic template into a Proposal (prompt, hash, sources, evidence)."""
        from wattson.agents.orchestrator.templates import TemplateInput, render_prompt

        agent = tool_context.agent
        task = w.load_task(agent)
        if not task.get("workflow"):
            raise ValueError("the workflow is still unknown; cannot compose")
        docs = [RetrievedDoc.model_validate(d) for d in w.get(agent, w.DOCS, [])]
        evidence = [
            (EvidenceRequest.model_validate(r), EvidenceItem.model_validate(i))
            for r, i in [*w.get(agent, w.EVIDENCE_BEFORE, []), *w.get(agent, w.EVIDENCE, [])]
        ]
        hints = DecisionHints.model_validate(task.get("hints") or {})
        workspace = _workspace(task)
        classification = task.get("classification")
        rendered = render_prompt(
            TemplateInput(
                task_id=task["id"],
                request=task["request"],
                workflow=task["workflow"],
                target=Target.model_validate(task["target"]),
                repo=workspace.repo,
                docs=docs,
                evidence=evidence,
                # The template drops the missing items the user has answered.
                answers={**(task.get("answers") or {}), **(task.get("missing_answers") or {})},
                missing_info=list(hints.missing_info),
                classification=Classification.model_validate(classification) if classification else None,
                extra_constraints=(
                    [
                        "This is a read-only investigation you confirmed at the safety check: do not "
                        "apply, delete, scale, restart, patch or deploy anything; only read and report."
                    ]
                    if task.get("guard_readonly")
                    else []
                ),
            )
        )
        # Last line of defence: nothing secret goes into the prompt (SEC-05, SEC-06).
        final = redact(rendered.prompt)
        prompt = final.text
        proposal = Proposal(
            prompt=prompt,
            prompt_sha256=sha256_hex(prompt),
            template=rendered.template,
            template_version=rendered.template_version,
            sources=[d.ref for d in docs],
            evidence=[_evidence_summary(r.id, i) for r, i in evidence],
            constraints=list(rendered.constraints),
            missing_info=list(rendered.missing_info),
            redactions=int(rendered.redactions) + final.count + int(task.get("redactions", 0)),
        )
        agent.state.set(w.PROPOSAL, proposal.model_dump(mode="json"))
        first = task.get("proposal_sha256") is None
        task["proposal_sha256"] = proposal.prompt_sha256
        w.mark(
            task,
            "proposed",
            f"proposed prompt {proposal.prompt_sha256[:12]}"
            f" ({proposal.template} {proposal.template_version})",
            note=not first,
        )
        w.save_task(agent, task)
        return step(
            "request_approval",
            output={"prompt_sha256": proposal.prompt_sha256, "template": proposal.template},
        )

    @tool(context=True)
    def request_approval(tool_context: ToolContext) -> str:
        """Show the proposal; the user approves (maybe edited), rejects, or answers missing info."""
        agent = tool_context.agent
        task = w.load_task(agent)
        proposal = Proposal.model_validate(w.get(agent, w.PROPOSAL))
        classification = task.get("classification")
        guard = task.get("guard")
        reason = ApproveReason(
            task_id=task["id"],
            workflow=task["workflow"],
            classification=Classification.model_validate(classification) if classification else None,
            target=Target.model_validate(task["target"]),
            proposal=proposal,
            warnings=list(task["warnings"]),
            guard=GuardVerdict.model_validate(guard) if guard else None,
        )
        response = tool_context.interrupt(APPROVE, reason=reason.model_dump(mode="json"))

        answer = _validate(ApproveResponse, response, "approval answer")
        task = w.load_task(agent)
        if answer.decision == "reject":
            why = _clean(task, answer.reason, MAX_ERROR)
            task["summary"] = "You rejected the task." + (f" Reason: {why}" if why else "")
            w.mark(task, "rejected", "rejected by the user" + (f": {why}" if why else ""))
            w.save_task(agent, task)
            return step("finish", output={"decision": "reject"})
        if answer.decision == "answer":
            task["rounds"]["approve"] += 1
            if task["rounds"]["approve"] > MAX_ROUNDS:
                task["error_summary"] = f"still missing information after {MAX_ROUNDS} rounds"
                w.mark(task, "failed", task["error_summary"])
                w.save_task(agent, task)
                return step("finish", output={"error": task["error_summary"]})
            given = {
                k[:300]: _clean(task, v, MAX_ANSWER) or ""
                for k, v in answer.answers.items()
                if v and v.strip()
            }
            if not given:
                w.warn(task, "The answer held no text; the proposal is shown again.")
            task.setdefault("missing_answers", {}).update(given)
            w.mark(task, "proposed", f"{len(given)} missing item(s) answered; composing again", note=True)
            w.save_task(agent, task)
            return step("compose", output={"decision": "answer", "answers": len(given)})

        if not is_sha256(answer.prompt_sha256):
            raise ValueError("the approval needs prompt_sha256: the sha256 hex of the text to send")
        session = answer.session or SessionChoice()
        if session.id is not None and not is_session_id(session.id):
            raise ValueError("the session id has characters that are not allowed")
        edited = bool(answer.edited or answer.prompt_sha256 != proposal.prompt_sha256)
        agent.state.set(
            w.APPROVAL,
            {
                "decision": "approve",
                "prompt_sha256": answer.prompt_sha256,
                "proposal_sha256": proposal.prompt_sha256,
                "edited": edited,
                "session": session.model_dump(mode="json"),
            },
        )
        task["approved_sha256"] = answer.prompt_sha256
        task["edited"] = edited
        task["session_mode"] = session.mode
        w.mark(
            task,
            "approved",
            f"approved {answer.prompt_sha256[:12]}; edited={str(edited).lower()}; "
            f"{'new' if session.id is None else 'existing'} {session.mode} session",
        )
        w.save_task(agent, task)
        return step("run_on_laptop", output={"decision": "approve", "edited": edited})

    @tool(context=True)
    def run_on_laptop(tool_context: ToolContext) -> str:
        """Hand the approved hash to the CLI, which runs Claude Code. Gated by the approval hook."""
        agent = tool_context.agent
        task = w.load_task(agent)
        approval = w.get(agent, w.APPROVAL, {})
        if approval.get("decision") != "approve":
            raise ValueError("no approval for this task")  # the gate stops this first
        session = SessionChoice.model_validate(approval["session"])
        w.mark(task, "submitted", f"sent to the laptop ({session.mode})")
        w.save_task(agent, task)
        reason = RunReason(task_id=task["id"], prompt_sha256=approval["prompt_sha256"], session=session)
        response = tool_context.interrupt(RUN_CLAUDE_CODE, reason=reason.model_dump(mode="json"))

        answer = _validate(RunResponse, response, "run result")
        task = w.load_task(agent)
        if answer.session_id is not None:
            if is_session_id(answer.session_id):
                task["claude_session_id"] = answer.session_id
            else:
                w.warn(
                    task, "The Claude Code session id had characters that are not allowed; it was not kept."
                )
        task["summary"] = _clean(task, answer.summary, MAX_SUMMARY)
        task["error_summary"] = _clean(task, answer.error, MAX_ERROR)
        bits = [f"Claude Code {answer.state}"]
        if answer.exit_code is not None:
            bits.append(f"exit {answer.exit_code}")
        if answer.duration_ms is not None:
            bits.append(f"{answer.duration_ms} ms")
        w.mark(task, answer.state, "; ".join(bits))
        w.save_task(agent, task)
        return step("finish", output={"state": answer.state})

    @tool(context=True)
    def finish(tool_context: ToolContext) -> str:
        """Record the end state and answer with a TaskOutcome. The working state is dropped."""
        agent = tool_context.agent
        task = w.load_task(agent)
        if task.get("status") not in TERMINAL:
            task["error_summary"] = task.get("error_summary") or "the task ended before a result"
            w.mark(task, "failed", task["error_summary"])
        summary = task.get("summary") or task.get("error_summary")
        outcome = TaskOutcome(
            task_id=task["id"],
            status=task["status"],
            workflow=task.get("workflow"),
            summary=summary,
            source_keys=list(task.get("source_keys") or []),
            warnings=list(task.get("warnings") or []),
        )
        task_end_of(task)  # the wattson.task.end span: ids, names and numbers only
        task["request"] = ""  # nothing of the user's text stays once the task is over
        task.pop("summary", None)
        w.save_task(agent, task)
        w.drop_bulky(agent)
        return step(None, output=outcome.model_dump(mode="json"))

    return [
        start_task,
        guard,
        list_procedures,
        triage,
        clarify,
        find_procedures,
        plan_evidence,
        collect_evidence,
        compose,
        request_approval,
        run_on_laptop,
        finish,
    ]


TOOL_NAMES = (
    "start_task",
    "guard",
    "list_procedures",
    "triage",
    "clarify",
    "find_procedures",
    "plan_evidence",
    "collect_evidence",
    "compose",
    "request_approval",
    "run_on_laptop",
    "finish",
)
