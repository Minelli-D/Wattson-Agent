"""`wattson ask`: send a request, then answer the agent's questions until the task ends.

Flow: detect the repo and the kube contexts, show them (FR-GIT-05), send StartTask, then loop:
(the Guard's question, for a risky request) -> clarify -> collect evidence -> approve -> run
Claude Code -> the TaskOutcome.
"""

from __future__ import annotations

import argparse
import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from wattson.cli import localstate, review
from wattson.cli.a2a_client import AgentClient, AgentError, PendingInterrupt, TaskGone, TaskView
from wattson.cli.commands.common import ERROR, OK, STOPPED, UsageError, agent, clean_result
from wattson.cli.config import prompt_mode
from wattson.cli.localstate import LocalTask, now_iso
from wattson.cli.ui import UI, Cancelled, short
from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    GUARD_CONFIRM,
    MAX_NAMESPACES,
    RUN_CLAUDE_CODE,
    WORKFLOWS,
    ApproveReason,
    ClarifyReason,
    ClarifyResponse,
    CollectEvidenceReason,
    CollectEvidenceResponse,
    EvidenceItem,
    EvidenceRequest,
    GuardConfirmReason,
    GuardConfirmResponse,
    Overrides,
    RepoInfo,
    RunReason,
    RunResponse,
    StartTask,
    Target,
    TaskOutcome,
    WorkspaceInfo,
)

STATUS_WORDS = {
    "completed": "completed",
    "rejected": "rejected: nothing was sent to Claude Code",
    "expired": "expired: nobody approved it in time",
    "failed": "failed",
    "interrupted": "interrupted: the run stopped without a full result",
    "blocked": "Stopped by the Guard: nothing ran",
}
_DETAIL_FIELDS = (
    "cluster",
    "namespace",
    "kind",
    "name",
    "pod",
    "deployment",
    "container",
    "tail",
    "limit",
    "depth",
    "path",
    "pattern",
)
# A Wattson procedure name ("k8s-pod-crashloop"), its Grimoire key, or "none".
_PROCEDURE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,199}")
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")  # RFC 1123 label


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "ask",
        help="ask the agent to prepare a task for Claude Code",
        description="Send a request to the agent. It may ask questions, collect evidence on this "
        "laptop (after a yes), and show you a prompt. Nothing goes to Claude Code without your yes.",
    )
    p.add_argument("request", help="what you want, in your words (in quotes)")
    p.add_argument("--repo", metavar="PATH", help="use this git repository (default: the current folder)")
    p.add_argument("--cluster", metavar="CTX", help="the kube context to investigate")
    p.add_argument(
        "--namespace",
        metavar="NS",
        action="append",
        help=f"the namespace to investigate; for several, repeat it or use commas (at most {MAX_NAMESPACES})",
    )
    p.add_argument(
        "-A",
        "--all-namespaces",
        action="store_true",
        help="investigate all namespaces of the cluster (not with --namespace)",
    )
    p.add_argument("--workload", metavar="KIND/NAME", help="the workload, for example deployment/api")
    p.add_argument("--workflow", choices=WORKFLOWS, help="skip the classification and use this workflow")
    p.add_argument(
        "--procedure",
        metavar="NAME",
        help="use this Wattson procedure (for example k8s-pod-crashloop), or none for no procedure",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--session", metavar="ID", help="continue this Claude Code conversation")
    group.add_argument("--new-session", action="store_true", help="start a new Claude Code conversation")
    p.add_argument("--fork", action="store_true", help="continue the conversation in a copy (fork)")
    p.add_argument("--headless", action="store_true", help="run Claude Code without a window (claude -p)")
    p.add_argument(
        "--timeout", metavar="MIN", type=int, default=30, help="headless run limit in minutes (default 30)"
    )
    p.add_argument(
        "--use-my-kubeconfig",
        action="store_true",
        help="allow kubectl with your own kubeconfig for contexts without read-only access",
    )
    p.add_argument("--yes-evidence", action="store_true", help="collect the evidence without asking")
    p.set_defaults(func=run)


def describe_request(req: EvidenceRequest) -> str:
    details = []
    for name in _DETAIL_FIELDS:
        value = getattr(req, name)
        if value not in (None, ""):
            details.append(f"{name} {value}")
    if req.all_namespaces:
        details.insert(1 if req.cluster else 0, "all namespaces")
    if req.previous:
        details.append("previous container")
    return req.op + (f" ({', '.join(details)})" if details else "")


def namespaces_of(args: argparse.Namespace) -> list[str]:
    """The --namespace values: repeated or comma separated, each once, in order. Each one must
    be a namespace name; at most MAX_NAMESPACES; never with --all-namespaces."""
    names: list[str] = []
    for value in args.namespace or []:
        for raw in value.split(","):
            name = raw.strip()
            if not name or name in names:
                continue
            if not _NAMESPACE.fullmatch(name):
                raise UsageError(
                    f"'{name}' is not a namespace name: lower case letters, digits and '-', "
                    "at most 63, starting and ending with a letter or a digit."
                )
            names.append(name)
    if args.namespace is not None and not names:
        raise UsageError("--namespace needs a name.")
    if names and args.all_namespaces:
        raise UsageError("--namespace and --all-namespaces (-A) do not go together: use one of them.")
    if len(names) > MAX_NAMESPACES:
        raise UsageError(
            f"At most {MAX_NAMESPACES} namespaces (you gave {len(names)}). To look at all of them, "
            "use --all-namespaces (-A)."
        )
    return names


def namespace_label(target: Target | Overrides) -> tuple[str, str | None]:
    """("Namespace", "web"), ("Namespaces", "web, shop"), ("Namespaces", "all"), or ("Namespace", None)."""
    if target.all_namespaces:
        return "Namespaces", "all"
    if len(target.namespaces) > 1:
        return "Namespaces", ", ".join(target.namespaces)
    return "Namespace", target.namespace


def procedure_of(reason: ApproveReason) -> tuple[str, str] | None:
    """(name, Grimoire key) of the Wattson procedure the prompt follows, if there is one."""
    source = next((s for s in reason.proposal.sources if s.why == "procedure"), None)
    if source is None:
        return None
    last = source.key.rstrip("/").rsplit("/", 1)[-1]
    return (last[:-3] if last.lower().endswith(".md") else last), source.key


def show_outcome(ui: UI, outcome: TaskOutcome) -> None:
    ui.heading("Result")
    ui.warnings(outcome.warnings)
    ui.kv(
        [
            ("Task", outcome.task_id),
            ("Status", STATUS_WORDS.get(outcome.status, outcome.status)),
            ("Workflow", outcome.workflow),
            ("Sources", ", ".join(outcome.source_keys)),
        ]
    )
    if outcome.status == "blocked":
        ui.line(f"Stopped by the Guard: {outcome.summary or 'nothing ran'}", "bold red")
    elif outcome.summary:
        ui.panel(outcome.summary, title="Summary")


class AskFlow:
    def __init__(
        self,
        ui: UI,
        args: argparse.Namespace,
        client: AgentClient,
        cwd: Path,
        repo_root: Path | None,
        workspace: WorkspaceInfo,
    ):
        self.ui = ui
        self.args = args
        self.client = client
        self.cwd = cwd
        self.repo_root = repo_root
        self.workspace = workspace
        self.task_id: str | None = None
        self.current: TaskView | None = None
        self.items: list[EvidenceItem] = []
        self.local: LocalTask | None = None
        self.final_prompt: str | None = None
        self.user_stopped = False
        self.run_interrupted = False

    @property
    def claude_cwd(self) -> Path:
        return self.repo_root or self.cwd

    # ---- the loop -------------------------------------------------------------------------

    def loop(self, task: TaskView) -> int:
        while True:
            self.current = task
            if task.state == "input-required":
                answers: list[tuple[str, BaseModel]] = []
                run_result: RunResponse | None = None
                for interrupt in task.interrupts:
                    response = self.handle(interrupt)
                    if response is None:  # quit at the approval: leave it open
                        return STOPPED
                    if isinstance(response, RunResponse):
                        run_result = response
                    answers.append((interrupt.id, response))
                if run_result is not None:
                    sent = self.send_run_result(task, answers, run_result)
                    if sent is None:
                        return STOPPED if self.user_stopped else ERROR
                    task = sent
                    continue
                self.ui.dim("Waiting for the agent...")
                try:
                    task = self.client.answer(task, answers)
                except TaskGone as exc:
                    self.ui.error(f"{exc}. It may have expired, or the agent restarted. Please ask again.")
                    return ERROR
                continue
            if task.state == "completed":
                return self.finish(task)
            if task.error:
                self.ui.error(f"The agent stopped the task: {task.error}")
            else:
                self.ui.error(f"The agent answered in a state this CLI does not expect: {task.state}")
            return ERROR

    def handle(self, interrupt: PendingInterrupt) -> BaseModel | None:
        reason = interrupt.reason
        self.task_id = getattr(reason, "task_id", None) or self.task_id
        if interrupt.name == GUARD_CONFIRM:
            return self.on_guard_confirm(reason)
        if interrupt.name == CLARIFY:
            return self.on_clarify(reason)
        if interrupt.name == COLLECT_EVIDENCE:
            return self.on_collect(reason)
        if interrupt.name == APPROVE:
            return self.on_approve(reason)
        if interrupt.name == RUN_CLAUDE_CODE:
            return self.on_run(reason)
        raise AgentError(f"the agent asks something this CLI does not know: {interrupt.name}")

    # ---- the Guard ----------------------------------------------------------------------------

    def on_guard_confirm(self, reason: GuardConfirmReason) -> GuardConfirmResponse:
        """A risky request (go on read-only, or stop), or orders for the agent the injection model
        reads in the request (go on, or stop), or both in one question. Never skipped (not by
        "quick", not by --yes-evidence), and the default is no."""
        ui = self.ui
        verdict = reason.verdict
        ui.heading("The Guard has a question")
        rows: list[tuple[str, object]] = [
            ("Task", reason.task_id),
            ("Request looks", review.guard_label(verdict)),
            ("How decided", review.guard_method(verdict)),
            ("Mode", review.guard_mode(verdict)),
        ]
        if verdict.request_injection_flagged:
            rows.append(("Injection model", review.request_injection_text(verdict)))
        ui.kv(rows)
        if verdict.reasons and verdict.label != "work":
            alarm = verdict.label in review.GUARD_ALARM
            body = "\n".join(f"- {r}" for r in verdict.reasons)
            ui.panel(body, title="Why", style="bold red" if alarm else "yellow")
        if verdict.evidence_removed:
            ui.warn(f"{verdict.evidence_removed} evidence part(s) were removed as possible prompt injection.")
        ui.info(reason.question)
        try:
            go = ui.confirm("Go on with a read-only investigation?" if reason.read_only else "Go on?", False)
        except Cancelled:
            go = False
        if not go:
            self.user_stopped = True
            ui.info("Stopped. Nothing ran. The task is marked as blocked.")
            return GuardConfirmResponse(proceed=False)
        ui.info("Going on, read-only only." if reason.read_only else "Going on.")
        return GuardConfirmResponse(proceed=True)

    # ---- clarify ----------------------------------------------------------------------------

    def on_clarify(self, reason: ClarifyReason) -> ClarifyResponse:
        ui = self.ui
        ui.heading("The agent needs some answers")
        ui.dim(f"Task {reason.task_id}. Type cancel to stop the task.")
        ui.warnings(reason.warnings)
        answers: dict[str, str] = {}
        try:
            for q in reason.questions:
                ui.console.print()
                if q.options:
                    ui.info(q.text)
                    answers[q.id] = q.options[int(ui.choose("Your choice (a number, or cancel)", q.options))]
                    continue
                while True:
                    value = ui.ask(q.text)
                    if value.lower() == "cancel":
                        raise Cancelled("cancel")
                    if value:
                        answers[q.id] = value
                        break
                    ui.info("Please type an answer, or cancel.")
        except Cancelled:
            self.user_stopped = True
            ui.info("Cancelled. The agent stops this task.")
            return ClarifyResponse(cancel=True)
        return ClarifyResponse(answers=answers)

    # ---- evidence ---------------------------------------------------------------------------

    def evidence_context(self) -> Any:
        from wattson.connector import evidence as ev

        return ev.EvidenceContext(
            repo_root=self.repo_root,
            contexts=list(self.workspace.kube_contexts),
            reader_contexts=list(self.workspace.reader_contexts),
            allow_admin_kubeconfig=bool(self.args.use_my_kubeconfig),
        )

    def check_namespaces(self, target: Target, ctx: Any) -> None:
        """Mark the plan's namespaces that do not exist in the cluster: their items do not run."""
        from wattson.connector import evidence as ev

        cluster = target.cluster
        if not cluster or not target.namespaces or cluster not in ctx.contexts:
            return
        missing, why_not = ev.missing_namespaces(cluster, target.namespaces, ctx)
        if missing:
            ctx.missing_namespaces = {(cluster, ns) for ns in missing}
            names = ", ".join(f"'{ns}'" for ns in missing)
            self.ui.warn(f"Not found in context '{cluster}': namespace {names}. Its items do not run.")
        elif why_not and cluster in ctx.reader_contexts:
            self.ui.dim(f"The namespaces were not checked ({short(why_not, 120)}).")

    def on_collect(self, reason: CollectEvidenceReason) -> CollectEvidenceResponse:
        from wattson.connector import evidence as ev

        ui = self.ui
        ctx = self.evidence_context()
        # A second round: the workload, where the first round found it (several or all namespaces).
        ui.heading(
            "More evidence the agent wants" if self.items else "Evidence the agent wants from this laptop"
        )
        target = reason.target
        ui.kv(
            [
                ("Repository", target.repo),
                ("Cluster", target.cluster),
                namespace_label(target),
                ("Workload", target.workload),
            ]
        )
        ui.warnings(reason.warnings)
        if not self.items:
            self.check_namespaces(target, ctx)
        rows = []
        refused_count = 0
        for req in reason.plan:
            why_not = ev.check_allowed(req, ctx)
            refused_count += 1 if why_not else 0
            rows.append(
                (req.id, describe_request(req), req.reason, f"REFUSED: {why_not}" if why_not else "will run")
            )
        ui.table(["Id", "What runs", "Why", "Status"], rows, title="Plan (read-only commands)")
        if not reason.plan:
            ui.info("The plan is empty.")
        elif refused_count == len(reason.plan):
            ui.warn("Nothing in this plan can run on this laptop. The refused items are reported as refused.")
        elif refused_count:
            ui.warn(f"{refused_count} item(s) are refused by this laptop's rules and will not run.")
        if self.args.use_my_kubeconfig:
            ui.warn(
                "--use-my-kubeconfig: kubectl may use your own kubeconfig, "
                "which can have more than read rights."
            )

        if not self.args.yes_evidence:
            try:
                go = ui.confirm("Collect this?", True)
            except Cancelled:
                go = False
            if not go:
                ui.info("Not collected. The agent goes on without this evidence.")
                return CollectEvidenceResponse(declined=True)

        def on_start(*args: Any) -> None:
            req = next((a for a in args if isinstance(a, EvidenceRequest)), None)
            if req is not None:
                ui.dim(f"Running {req.id}: {describe_request(req)}")

        def on_done(*args: Any) -> None:
            item = next((a for a in args if isinstance(a, EvidenceItem)), None)
            if item is not None:
                state = (
                    "refused" if item.refused else ("ok" if item.ok else f"failed: {short(item.error, 80)}")
                )
                ui.dim(f"  {item.request_id}: {state}")

        try:
            items = list(ev.run_evidence(reason.plan, ctx, on_start=on_start, on_done=on_done))
        except KeyboardInterrupt:
            ui.info("Stopped. The agent goes on without this evidence.")
            return CollectEvidenceResponse(declined=True)
        known = {i.request_id for i in items}
        self.items = [i for i in self.items if i.request_id not in known] + items
        ok = sum(1 for i in items if i.ok and not i.refused)
        refused = sum(1 for i in items if i.refused)
        failed = len(items) - ok - refused
        hidden = sum(i.redactions for i in items)
        ui.info(
            f"Collected {ok} of {len(items)}: {failed} failed, {refused} refused, {hidden} secret(s) hidden."
        )
        return CollectEvidenceResponse(items=items)

    # ---- approval ---------------------------------------------------------------------------

    def on_approve(self, reason: ApproveReason) -> BaseModel | None:
        procedure = procedure_of(reason)
        if procedure is not None:
            name, key = procedure
            self.ui.console.print()
            self.ui.ok(f"Wattson procedure: {name} ({key}). The prompt follows its steps.")
        opts = review.SessionOptions(
            session_id=self.args.session,
            new_session=self.args.new_session,
            fork=self.args.fork,
            headless=self.args.headless,
            quick=(prompt_mode() == "quick"),
        )
        result = review.run_review(self.ui, reason, self.items, opts, self.claude_cwd)
        if result.decision == "quit":
            self.user_stopped = True
            self.ui.info(
                f"Left open. Nothing was sent to Claude Code. The task {reason.task_id} expires after "
                "120 minutes if nobody answers."
            )
            return None
        if result.decision == "reject":
            self.user_stopped = True
            self.ui.info("Rejected. Nothing is sent to Claude Code.")
            return result.response
        if result.decision == "answer":
            self.ui.info("Sending your answers. The agent writes the prompt again.")
            return result.response
        assert result.response is not None and result.prompt is not None
        assert result.response.session is not None and result.response.prompt_sha256 is not None
        self.final_prompt = result.prompt
        self.local = LocalTask(
            task_id=reason.task_id,
            a2a_task_id=self.current.id if self.current else None,
            context_id=self.current.context_id if self.current else None,
            request=self.args.request,
            prompt=result.prompt,
            prompt_sha256=result.response.prompt_sha256,
            session=result.response.session,
            cwd=str(self.claude_cwd),
            approved_at=now_iso(),
            state="approved",
        )
        localstate.save(self.local)
        return result.response

    # ---- the Claude Code run ------------------------------------------------------------------

    def on_run(self, reason: RunReason) -> RunResponse:
        from wattson.connector import claude as cc

        ui = self.ui
        local, prompt = self.local, self.final_prompt
        if local is None or prompt is None:
            ui.error("This CLI run did not approve this task. Nothing was sent to Claude Code.")
            return RunResponse(
                state="failed", error="the CLI holds no approved prompt for this task: nothing was sent"
            )
        if reason.prompt_sha256 != local.prompt_sha256 or review.sha256_hex(prompt) != local.prompt_sha256:
            ui.error(
                "The agent asks to run a text that is not the one you approved (the hashes differ). "
                "Nothing was sent to Claude Code."
            )
            result = RunResponse(
                state="failed", error="prompt hash mismatch: nothing was sent to Claude Code"
            )
            localstate.record_result(local, result)
            return result
        session = local.session
        if reason.session != session:
            ui.warn(
                f"The agent named another Claude Code conversation. Using the one you picked: "
                f"{review.describe_choice(session)}."
            )
        if session.id is None and not local.new_session_id:
            local.new_session_id = str(uuid.uuid4())  # saved before the run: recover can find it
        local.state = "submitted"
        local.submitted_at = now_iso()
        localstate.save(local)

        timeout_s = max(1, int(self.args.timeout)) * 60
        if session.mode == "interactive":
            ui.info(
                "Claude Code opens now. It asks its own permission questions as usual. "
                "When you leave it, Wattson Agent reports the result."
            )
        else:
            ui.info(
                f"Claude Code runs without a window (headless), for at most {timeout_s // 60} minutes. "
                "Ctrl-C stops it."
            )
        try:
            # The connector gives Ctrl-C to Claude Code while it has the terminal (interactive), and
            # stops a headless run on Ctrl-C; both come back as state "interrupted".
            result = cc.submit(
                prompt,
                self.claude_cwd,
                session,
                timeout_s=timeout_s,
                new_session_id=local.new_session_id if session.id is None else None,
            )
        except KeyboardInterrupt:
            result = RunResponse(state="interrupted", error="stopped with Ctrl-C on the laptop")
        except Exception as exc:  # noqa: BLE001 - any connector failure is reported, never retried
            result = RunResponse(state="failed", error=f"could not run Claude Code: {exc}")
        result = clean_result(result)
        localstate.record_result(local, result)
        if result.state == "interrupted":
            self.run_interrupted = True
            self.user_stopped = self.user_stopped or "Ctrl-C" in (result.error or "")
            ui.warn(
                f"Claude Code stopped before the end: {result.error or 'no detail'}. "
                "The task is marked as interrupted."
            )
            return result
        words = "finished" if result.state == "completed" else "failed"
        extra = f" (exit code {result.exit_code})" if result.exit_code not in (None, 0) else ""
        ui.info(f"Claude Code {words}{extra}.")
        if result.error:
            ui.error(result.error)
        return result

    def send_run_result(
        self, task: TaskView, answers: list[tuple[str, BaseModel]], result: RunResponse
    ) -> TaskView | None:
        """Deliver the run result; when the A2A task is gone, use the late-report route."""
        ui = self.ui
        task_id = self.task_id or ""
        try:
            ui.dim("Reporting the result to the agent...")
            sent = self.client.answer(task, answers)
        except TaskGone:
            try:
                record = self.client.report_execution(task_id, result)
            except AgentError as exc:
                return self._report_failed(task_id, exc)
            self._reported()
            ui.info(
                f"The task had closed on the agent; the result was reported late. Status: {record.status}."
            )
            self._recover_hint(task_id)
            outcome = TaskOutcome(
                task_id=task_id,
                status=record.status,
                workflow=record.workflow,
                summary=result.summary,
                source_keys=record.source_keys,
            )
            return TaskView(id=task.id, context_id=task.context_id, state="completed", outcome=outcome)
        except AgentError as exc:
            return self._report_failed(task_id, exc)
        self._reported()
        self._recover_hint(task_id)
        return sent

    def _reported(self) -> None:
        if self.local is not None:
            localstate.mark_reported(self.local)

    def _recover_hint(self, task_id: str) -> None:
        if self.run_interrupted:
            self.ui.info(f"If Claude Code still finishes, report its answer with: wattson recover {task_id}")

    def _report_failed(self, task_id: str, exc: Exception) -> None:
        self.ui.error(f"Could not report the result: {exc}")
        self.ui.info(f"The result is kept on this laptop. Report it later with: wattson recover {task_id}")
        return None

    # ---- the end ------------------------------------------------------------------------------

    def finish(self, task: TaskView) -> int:
        if task.outcome is None:
            if task.error:
                self.ui.error(f"The agent stopped the task: {task.error}")
                return ERROR
            self.ui.info(task.message or "The agent finished without a result.")
            return STOPPED if self.user_stopped else OK
        show_outcome(self.ui, task.outcome)
        if self.user_stopped or task.outcome.status in ("rejected", "expired", "blocked"):
            return STOPPED
        if task.outcome.status in ("failed", "interrupted"):
            return ERROR
        return OK


def detect_workspace(
    ui: UI, cwd: Path, repo_given: bool
) -> tuple[RepoInfo | None, Path | None, WorkspaceInfo]:
    from wattson.connector import git as cgit
    from wattson.connector import kube

    found = cgit.detect_repo(cwd)
    if repo_given and found is None:
        raise UsageError(f"{cwd} is not inside a git repository.")
    repo, root = found if found else (None, None)
    try:
        contexts, current = kube.list_contexts()
    except Exception as exc:  # noqa: BLE001 - no kubectl, or no kubeconfig: go on without clusters
        ui.warn(f"Cannot read your kube contexts: {exc}")
        contexts, current = [], None
    try:
        readers = [c for c in kube.reader_contexts() if c in contexts]
    except Exception as exc:  # noqa: BLE001
        ui.warn(f"Cannot list the read-only kubeconfigs: {exc}")
        readers = []
    workspace = WorkspaceInfo(
        repo=repo,
        cwd_name=cwd.name or str(cwd),
        kube_contexts=list(contexts),
        current_kube_context=current,
        reader_contexts=readers,
    )
    return repo, root, workspace


def show_context(ui: UI, request: str, cwd: Path, workspace: WorkspaceInfo, overrides: Overrides) -> None:
    """FR-GIT-05: what the agent will know about this laptop, before anything is sent."""
    ui.heading("What will be used")
    repo = workspace.repo
    rows: list[tuple[str, object]] = [("Request", request)]
    if repo:
        branch = repo.branch or "no branch (detached)"
        rows += [
            ("Repository", f"{repo.name} ({repo.root_display})"),
            ("Branch", f"{branch}" + (f" at {repo.head}" if repo.head else "")),
            ("Changed files", repo.dirty_files),
            ("Remote", repo.remote),
        ]
    else:
        rows.append(("Repository", f"none (the folder {cwd} is not in a git repository)"))
    if workspace.kube_contexts:
        marks = []
        for c in workspace.kube_contexts:
            notes = []
            if c == workspace.current_kube_context:
                notes.append("current")
            notes.append(
                "read-only access ready" if c in workspace.reader_contexts else "no read-only access"
            )
            marks.append(f"{c} ({', '.join(notes)})")
        rows.append(("Kube contexts", "; ".join(marks)))
    else:
        rows.append(("Kube contexts", "none"))
    for label, value in (
        ("Workflow", overrides.workflow),
        ("Cluster", overrides.cluster),
        namespace_label(overrides),
        ("Workload", overrides.workload),
        ("Procedure", overrides.procedure),
    ):
        if value:
            rows.append((f"{label} (yours)", value))
    ui.kv(rows)
    ui.dim("Only these names are sent now. File contents and cluster data are sent only after you say yes.")


def check_namespaces_exist(
    ui: UI, args: argparse.Namespace, workspace: WorkspaceInfo, names: list[str]
) -> None:
    """--cluster with --namespace: every namespace must exist there (one read-only kubectl call).
    When it cannot be checked (no read-only access yet), the plan shows that later."""
    from wattson.connector import evidence as ev

    ctx = ev.EvidenceContext(
        repo_root=None,
        contexts=list(workspace.kube_contexts),
        reader_contexts=list(workspace.reader_contexts),
        allow_admin_kubeconfig=bool(args.use_my_kubeconfig),
    )
    missing, why_not = ev.missing_namespaces(args.cluster, names, ctx)
    if missing:
        which = ", ".join(f"'{ns}'" for ns in missing)
        word = "namespace" if len(missing) == 1 else "namespaces"
        raise UsageError(
            f"There is no {word} {which} in context '{args.cluster}'. "
            f"See them with: kubectl --context {args.cluster} get namespaces"
        )
    if why_not and args.cluster in workspace.reader_contexts:
        ui.warn(f"The namespaces were not checked: {short(why_not, 200)}.")


def run(ui: UI, args: argparse.Namespace) -> int:
    request = args.request.strip()
    if not request:
        raise UsageError("The request is empty.")
    if len(request) > 4000:
        raise UsageError("The request is longer than 4000 characters.")
    if args.workload and "/" not in args.workload:
        raise UsageError("--workload must look like KIND/NAME, for example deployment/api.")
    if args.timeout < 1:
        raise UsageError("--timeout must be at least 1 minute.")
    procedure = (args.procedure or "").strip().lower() or None
    if procedure is not None and not _PROCEDURE.fullmatch(procedure):
        raise UsageError("--procedure must be a procedure name, for example k8s-pod-crashloop, or none.")
    cwd = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    if not cwd.is_dir():
        raise UsageError(f"There is no folder {cwd}.")
    args.request = request

    namespaces = namespaces_of(args)
    _repo, root, workspace = detect_workspace(ui, cwd, bool(args.repo))
    if args.cluster and args.cluster not in workspace.kube_contexts:
        known = ", ".join(workspace.kube_contexts) or "none"
        raise UsageError(f"There is no kube context named {args.cluster}. Your contexts: {known}.")
    if args.cluster and namespaces:
        check_namespaces_exist(ui, args, workspace, namespaces)
    overrides = Overrides(
        workflow=args.workflow,
        cluster=args.cluster,
        # One namespace: the old field, so the message has the shape of before.
        namespace=namespaces[0] if len(namespaces) == 1 else None,
        namespaces=namespaces if len(namespaces) > 1 else [],
        all_namespaces=bool(args.all_namespaces),
        workload=args.workload,
        procedure=procedure,
    )
    show_context(ui, request, cwd, workspace, overrides)

    with agent(ui) as client:
        ui.dim("Sending the request to the agent...")
        task = client.start(StartTask(request=request, workspace=workspace, overrides=overrides))
        flow = AskFlow(ui, args, client, cwd, root, workspace)
        return flow.loop(task)
