"""`wattson tasks`, `wattson show <id>`, `wattson recover <id>`: the history, and late results."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from wattson.cli import localstate
from wattson.cli.a2a_client import AgentError, NotFound
from wattson.cli.commands.common import ERROR, OK, UsageError, agent, clean_result
from wattson.cli.localstate import LocalTask
from wattson.cli.ui import UI, short
from wattson.shared.contracts import RunResponse, TaskRecord


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("tasks", help="list recent tasks (and the ones still open on this laptop)")
    p.add_argument("--limit", type=int, default=20, metavar="N", help="how many (default 20)")
    p.set_defaults(func=run_tasks)

    p = sub.add_parser("show", help="show one task and its history")
    p.add_argument("task_id")
    p.set_defaults(func=run_show)

    p = sub.add_parser("recover", help="report the result of a Claude Code run that was not reported")
    p.add_argument("task_id")
    p.add_argument("--session", metavar="ID", help="the Claude Code conversation that ran the task")
    p.set_defaults(func=run_recover)


def when(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _target(rec: TaskRecord) -> str:
    """ "shop, my-cluster/web"; several namespaces "my-cluster/{web,shop}"; all of them "my-cluster/*"."""
    parts = [p for p in (rec.repo, "/".join(x for x in (rec.cluster, rec.namespace_short()) if x)) if p]
    return ", ".join(parts)


def status_text(status: str) -> str:
    """The status for the task list. "blocked" says who stopped it."""
    return "blocked (stopped by the Guard)" if status == "blocked" else status


def local_note(task: LocalTask | None) -> str:
    if task is None or not task.unfinished:
        return ""
    if not task.reported:
        return f"result not reported: wattson recover {task.task_id}"
    return f"run interrupted: wattson recover {task.task_id} if Claude Code finished"


def run_tasks(ui: UI, args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 500:
        raise UsageError("--limit must be between 1 and 500.")
    unfinished = {t.task_id: t for t in localstate.list_unfinished()}
    code = OK
    records: list[TaskRecord] = []
    try:
        with agent(ui) as client:
            records = client.tasks(limit=args.limit)
    except AgentError as exc:
        ui.error(str(exc))
        code = ERROR
    if records:
        ui.table(
            ["Task", "Created", "Status", "Workflow", "Target", "On this laptop"],
            [
                (
                    r.id,
                    when(r.created_at),
                    status_text(r.status),
                    r.workflow,
                    _target(r),
                    local_note(unfinished.get(r.id)),
                )
                for r in records
            ],
            title="Tasks on the agent",
        )
    elif code == OK:
        ui.info("The agent has no tasks yet.")
    only_local = [t for t in unfinished.values() if t.task_id not in {r.id for r in records}]
    if only_local:
        ui.table(
            ["Task", "Approved", "State", "Request", "What to do"],
            [
                (t.task_id, when(t.approved_at), t.state, short(t.request, 50), local_note(t))
                for t in only_local
            ],
            title="Open on this laptop",
        )
    return code


def run_show(ui: UI, args: argparse.Namespace) -> int:
    local = localstate.load(args.task_id)
    code = OK
    try:
        with agent(ui) as client:
            record = client.task(args.task_id)
            events = client.events(args.task_id)
        ui.heading(f"Task {record.id}")
        blocked = record.status == "blocked"
        if blocked:
            ui.line(f"Stopped by the Guard: {record.error_summary or 'nothing ran'}", "bold red")
        ui.kv(
            [
                ("Status", "blocked: stopped by the Guard, nothing ran" if blocked else record.status),
                ("Created", when(record.created_at)),
                ("Updated", when(record.updated_at)),
                ("Workflow", record.workflow),
                (
                    "Classified as",
                    f"{record.category} ({record.confidence:.2f}, {record.method})"
                    if record.category and record.confidence is not None
                    else record.category,
                ),
                ("Repository", record.repo),
                ("Cluster", record.cluster),
                (
                    "Namespaces" if record.all_namespaces or len(record.namespaces) > 1 else "Namespace",
                    "all" if record.all_namespaces else ", ".join(record.namespaces),
                ),
                ("Sources", ", ".join(record.source_keys)),
                ("Proposed prompt hash", record.proposal_sha256),
                ("Approved prompt hash", record.approved_sha256),
                (
                    "Edited by you",
                    {True: "yes", False: "no"}.get(record.edited) if record.edited is not None else None,
                ),
                ("Claude Code", f"{record.session_mode or ''} {record.claude_session_id or ''}".strip()),
                ("Error", record.error_summary),
            ]
        )
        if events:
            ui.table(
                ["At", "From", "To", "Detail"],
                [(when(e.at), e.from_status, e.to_status, e.detail) for e in events],
                title="History",
            )
    except NotFound:
        ui.error(f"The agent has no task {args.task_id}.")
        code = ERROR
    except AgentError as exc:
        ui.error(str(exc))
        code = ERROR
    if local is not None:
        ui.heading("On this laptop")
        ui.kv(
            [
                ("State", local.state),
                ("Result reported", "yes" if local.reported else "no"),
                ("Claude Code", f"{local.session.id or 'new conversation'} ({local.session.mode})"),
                ("Folder", local.cwd),
                ("Submitted", when(local.submitted_at)),
                ("Prompt kept", "yes" if local.prompt else "no (dropped after the report)"),
                ("What to do", local_note(local)),
            ]
        )
    return code


def _guess_session(ui: UI, local: LocalTask) -> str | None:
    """A new conversation's id is only known after the run: look for one active since the submit."""
    from wattson.connector import claude as cc

    since = local.submitted_at or local.approved_at
    try:
        sessions = cc.list_sessions(Path(local.cwd))
    except Exception as exc:  # noqa: BLE001
        ui.warn(f"Cannot list the Claude Code conversations: {exc}")
        return None
    recent = [s for s in sessions if since is None or _after(s.last_activity, since)]
    return recent[0].id if len(recent) == 1 else None


def _after(value: Any, since: str) -> bool:
    try:
        limit = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if isinstance(value, (int, float)):
            return value >= limit.timestamp()
        moment = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment >= limit
    except (TypeError, ValueError):
        return False


def run_recover(ui: UI, args: argparse.Namespace) -> int:
    local = localstate.load(args.task_id)
    if local is None:
        ui.error(f"This laptop has no record of task {args.task_id}. Open tasks are listed by: wattson tasks")
        return ERROR
    if local.reported and local.state in ("completed", "failed"):
        ui.info(f"The result of task {local.task_id} was already reported ({local.state}). Nothing to do.")
        return OK

    result: RunResponse | None = None
    if local.result is not None and not local.reported and local.state in ("completed", "failed"):
        result = local.result  # known, only the report is missing
    else:
        from wattson.connector import claude as cc

        session_id = (
            args.session
            or (local.result.session_id if local.result else None)
            or local.session.id
            or local.new_session_id
        )
        if local.session.fork and not args.session and not (local.result and local.result.session_id):
            session_id = None  # a fork got a new id: look for it
        session_id = session_id or _guess_session(ui, local)
        found: RunResponse | None = None
        if session_id:
            since = local.submitted_at or local.approved_at or ""
            ui.dim(f"Looking for Claude Code's answer in conversation {session_id} after {when(since)}...")
            try:
                found = cc.recover(Path(local.cwd), session_id, since)
            except Exception as exc:  # noqa: BLE001
                ui.error(f"Cannot read the Claude Code conversation: {exc}")
                return ERROR
        elif local.result is None or local.reported:
            ui.error(
                "Cannot tell which Claude Code conversation ran this task. "
                "Pass it with --session ID (wattson sessions lists them)."
            )
            return ERROR
        if found is None and local.result is not None and not local.reported:
            ui.info("No answer from Claude Code in that conversation. Reporting what this laptop knows.")
            result = local.result
        elif found is None:
            ui.info(
                "No answer from Claude Code yet in that conversation. Nothing was reported. "
                "Try again when Claude Code has finished."
            )
            return ERROR
        else:
            result = clean_result(found)
            localstate.record_result(local, result)

    with agent(ui) as client:
        record = client.report_execution(local.task_id, result)
    localstate.mark_reported(local)
    ui.ok(f"Reported. The task {record.id} is now: {record.status}.")
    if result.summary:
        ui.panel(result.summary, title="Claude Code's answer (short)")
    return OK
