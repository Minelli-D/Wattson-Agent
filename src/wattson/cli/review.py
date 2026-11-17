"""The approval screen (FR-APP-01..08): what the agent proposes, where it came from, and the
user's decision. Nothing reaches Claude Code unless the user types the word yes (FR-APP-04)."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from wattson.cli.ui import UI, Cancelled, short
from wattson.shared.contracts import (
    ApproveReason,
    ApproveResponse,
    EvidenceItem,
    GuardVerdict,
    SessionChoice,
)

MENU = (
    "[p] read the full prompt   [e] edit it   [s] sources   [v] evidence   [m] answer missing information\n"
    "[a] approve   [r] reject   [q] quit and leave it open"
)
MENU_COMPACT = "[p]rompt [e]dit [s]ources [v]idence [m]issing [a]pprove [r]eject [q]uit"
METHOD_WORDS = {
    "model": "decided by the small model",
    "rules": "decided by keyword rules",
    "override": "set by you",
}
# The Guard's verdict, in plain words (the review, the Guard question, the task list).
GUARD_LABEL_WORDS = {
    "work": "work (a normal engineering task)",
    "out_of_scope": "out of scope (not engineering work)",
    "risky": "RISKY (it asks to change or delete something)",
    "malicious": "MALICIOUS (it looks like an attack or abuse)",
}
GUARD_METHOD_WORDS = {
    "rules": "decided by fixed rules",
    "model": "decided by the small model",
    "rules+model": "decided by fixed rules and the small model",
    "off": "the Guard is off",
}
GUARD_MODE_WORDS = {
    "enforce": "enforce: it can stop tasks",
    "monitor": "monitor: it only records, it blocks nothing",
    "off": "off",
}
GUARD_ALARM = ("risky", "malicious")  # shown where they cannot be missed
WHY_WORDS = {
    "procedure": "Wattson procedure (chosen)",
    "search": "found by search",
    "related": "related document",
    "context": "project context",
    "seen_before": "seen before (Cognee memory)",
}


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SessionOptions:
    session_id: str | None = None
    new_session: bool = False
    fork: bool = False
    headless: bool = False
    quick: bool = False  # config prompts "quick": a single y approves; the newest conversation continues


@dataclass
class ReviewResult:
    decision: Literal["approve", "reject", "answer", "quit"]
    response: ApproveResponse | None = None
    prompt: str | None = None  # the final text, for "approve"


# ---- the summary ------------------------------------------------------------------------------


def _target_text(reason: ApproveReason) -> str:
    t = reason.target
    parts = []
    if t.repo:
        parts.append(f"repository {t.repo}")
    if t.cluster:
        parts.append(f"cluster {t.cluster}")
    if t.namespace_words():
        parts.append(t.namespace_words())
    if t.workload:
        parts.append(f"workload {t.workload}")
    return ", ".join(parts) or "none named"


def _classification_text(reason: ApproveReason) -> str:
    c = reason.classification
    if c is None:
        return "not classified"
    how = METHOD_WORDS.get(c.method, c.method)
    model = f", model {c.model}" if c.model and c.method == "model" else ""
    return f"{c.category} (confidence {c.confidence:.2f}, {how}{model})"


def guard_label(verdict: GuardVerdict) -> str:
    return GUARD_LABEL_WORDS.get(verdict.label, verdict.label)


def guard_method(verdict: GuardVerdict) -> str:
    how = GUARD_METHOD_WORDS.get(verdict.method, verdict.method)
    return how + (f", model {verdict.model}" if verdict.model and "model" in verdict.method else "")


def guard_mode(verdict: GuardVerdict) -> str:
    return GUARD_MODE_WORDS.get(verdict.mode, verdict.mode)


def request_injection_text(verdict: GuardVerdict) -> str:
    """What the injection model said about the request itself (empty: it did not read it)."""
    if verdict.request_injection_failed:
        return "the injection model did not answer about your request"
    if verdict.request_injection_pct is None:
        return ""
    if verdict.request_injection_flagged:
        return f"orders for the agent in your request (score {verdict.request_injection_pct / 100:.2f})"
    return "no orders for the agent in your request"


def guard_text(verdict: GuardVerdict) -> str:
    """One line: what the Guard thinks of the request, how it decided, its mode."""
    text = f"{guard_label(verdict)}, {guard_method(verdict)}, mode {guard_mode(verdict)}"
    if request := request_injection_text(verdict):
        text += f"; {request}"
    if verdict.evidence_removed:
        text += f"; {verdict.evidence_removed} evidence part(s) removed (possible prompt injection)"
    if verdict.injection_failed:
        text += "; the injection model did not answer (rules only)"
    elif verdict.injection_model:
        n = verdict.injection_flagged
        text += f"; injection model: {n} part(s) flagged" if n else "; injection model: nothing flagged"
    return text


def show_guard_alarm(ui: UI, verdict: GuardVerdict | None) -> None:
    """A risky or malicious verdict, and removed evidence, where they cannot be missed."""
    if verdict is None:
        return
    if verdict.label in GUARD_ALARM:
        lines = [f"The Guard says: {guard_label(verdict)}."]
        lines += [f"- {r}" for r in verdict.reasons]
        if verdict.mode != "enforce":
            lines.append(f"Mode {guard_mode(verdict)}.")
        ui.panel("\n".join(lines), title="Guard", style="bold red")
    if verdict.request_injection_flagged:
        ui.warn(f"The injection model read {request_injection_text(verdict)}.")
    if verdict.evidence_removed:
        ui.warn(f"{verdict.evidence_removed} evidence part(s) were removed as possible prompt injection.")


def show_summary(ui: UI, reason: ApproveReason) -> None:
    p = reason.proposal
    ui.heading("Proposed task for Claude Code")
    ui.warnings(reason.warnings)
    show_guard_alarm(ui, reason.guard)
    rows: list[tuple[str, object]] = [
        ("Task", reason.task_id),
        ("Workflow", reason.workflow),
        ("Classified as", _classification_text(reason)),
    ]
    if reason.guard is not None:
        rows.append(("Guard", guard_text(reason.guard)))
    rows += [
        ("Target", _target_text(reason)),
        ("Template", f"{p.template} (version {p.template_version})"),
        ("Prompt", f"{len(p.prompt.splitlines())} lines, {len(p.prompt)} characters"),
        ("Secrets hidden", p.redactions),
    ]
    ui.kv(rows)
    if p.sources:
        ui.table(
            ["Title", "Key", "Why", "Freshness"],  # a Grimoire key, or cognee:<dataset>/<lesson>
            [(s.title, s.key, WHY_WORDS.get(s.why, s.why), s.freshness) for s in p.sources],
            title="Sources",
        )
    else:
        ui.warn("No Grimoire document was used for this prompt.")
    if p.evidence:
        ui.table(
            ["Source", "Collected at", "Freshness", "Result", "Secrets hidden"],
            [
                (
                    e.source,
                    e.collected_at,
                    e.freshness,
                    "ok" if e.ok else f"failed: {short(e.error, 60)}",
                    e.redactions,
                )
                for e in p.evidence
            ],
            title="Evidence",
        )
    else:
        ui.info("Evidence: none was collected.")
    if p.constraints:
        ui.info("Constraints:")
        for c in p.constraints:
            ui.info(f"  - {c}")
    if p.missing_info:
        ui.console.print()
        ui.line("Missing information (the prompt may be weaker without it):", "bold yellow")
        for m in p.missing_info:
            ui.line(f"  - {m}", "yellow")


def show_summary_compact(ui: UI, reason: ApproveReason) -> None:
    """The quick preset: one line, the warnings, the rest on [s] and [v]."""
    p = reason.proposal
    ui.warnings(reason.warnings)
    show_guard_alarm(ui, reason.guard)
    ok = sum(1 for e in p.evidence if e.ok)
    ui.info(
        f"{reason.workflow} · {_target_text(reason)} · {len(p.prompt.splitlines())} lines · "
        f"{len(p.sources)} sources · evidence {ok}/{len(p.evidence)} ok · {p.redactions} secrets hidden"
    )
    g = reason.guard
    if g is not None:
        ui.info(f"Guard: {g.label} ({GUARD_METHOD_WORDS.get(g.method, g.method)}, {g.mode})")
    if p.missing_info:
        ui.warn(f"Missing: {'; '.join(p.missing_info)}")


def show_sources(ui: UI, reason: ApproveReason) -> None:
    sources = reason.proposal.sources
    if not sources:
        ui.info("No Grimoire document was used.")
        return
    for s in sources:
        ui.heading(s.title)
        ui.kv(
            [
                ("Key", s.key),
                ("Folder", s.folder),
                ("Tags", ", ".join(s.tags)),
                ("Why", WHY_WORDS.get(s.why, s.why)),
                ("Retrieved at", s.retrieved_at),
                ("Freshness", s.freshness),
            ]
        )
        if s.excerpt:
            ui.panel(s.excerpt, title="Excerpt")


def show_evidence(ui: UI, reason: ApproveReason, items: Sequence[EvidenceItem]) -> None:
    if not items:
        ui.info("This CLI run did not collect evidence. What the agent used:")
        for e in reason.proposal.evidence:
            ui.info(
                f"  - {e.source} ({e.freshness}, {'ok' if e.ok else 'failed'}, collected {e.collected_at})"
            )
        if not reason.proposal.evidence:
            ui.info("  nothing")
        return
    chunks = []
    for item in items:
        if item.refused:
            state = f"refused: {item.refused}"
        elif not item.ok:
            state = f"failed: {item.error or 'no detail'}"
        else:
            state = "ok"
        head = [
            f"=== {item.request_id}: {item.source}",
            f"Result: {state}",
            f"Collected at: {item.collected_at}",
            f"Secrets hidden: {item.redactions}"
            + ("   (the output was cut to the size limit)" if item.truncated else ""),
            "",
        ]
        chunks.append("\n".join(head) + (item.output or "(no output)"))
    ui.pager("\n\n".join(chunks))


# ---- editing ----------------------------------------------------------------------------------


def editor_command() -> list[str] | None:
    for value in (os.environ.get("VISUAL"), os.environ.get("EDITOR")):
        if value and value.strip():
            return shlex.split(value)
    for name in ("nano", "vi"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def edit_text(ui: UI, text: str) -> str | None:
    """Open the text in the user's editor. The temp file is private (600) and deleted after."""
    command = editor_command()
    if command is None:
        ui.error("No editor found. Set EDITOR (for example: export EDITOR=nano).")
        return None
    fd, name = tempfile.mkstemp(prefix="wattson-prompt-", suffix=".md")  # created with mode 600
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
        try:
            done = subprocess.run([*command, str(path)], check=False)  # noqa: S603 - the user's own editor
        except OSError as exc:
            ui.error(f"Cannot start the editor {command[0]}: {exc.strerror}")
            return None
        if done.returncode != 0:
            ui.warn(f"The editor stopped with code {done.returncode}. The prompt was not changed.")
            return None
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


# ---- the Claude Code conversation -----------------------------------------------------------------


def ago(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        seconds = (datetime.now(UTC) - value).total_seconds()
        if seconds < 120:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)} minutes ago"
        if seconds < 172800:
            return f"{int(seconds // 3600)} hours ago"
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    return str(value)


def describe_session(s: Any) -> str:
    first = short(s.first_prompt, 60)
    text = f'{s.id[:8]}  last used {ago(s.last_activity)}, {s.messages} messages: "{first}"'
    return text + ("  (maybe open now)" if getattr(s, "maybe_open", False) else "")


def pick_session(ui: UI, opts: SessionOptions, cwd: Path) -> SessionChoice:
    """FR-CC-02/03: --session, --new-session, or ask among this folder's conversations."""
    mode = "headless" if opts.headless else "interactive"
    if opts.session_id:
        return SessionChoice(id=opts.session_id, mode=mode, fork=opts.fork)
    if opts.new_session:
        return SessionChoice(id=None, mode=mode)

    from wattson.connector import claude as cc

    try:
        sessions = list(cc.list_sessions(cwd))
    except Exception as exc:  # noqa: BLE001 - any failure here only means "start a new one"
        ui.warn(f"Cannot list the Claude Code conversations: {exc}. A new conversation starts.")
        return SessionChoice(id=None, mode=mode)
    if not sessions:
        ui.info("No earlier Claude Code conversation for this folder. A new one starts.")
        return SessionChoice(id=None, mode=mode)

    if opts.quick and not getattr(sessions[0], "maybe_open", False):
        ui.info(f"Continuing the newest conversation for this folder: {describe_session(sessions[0])}")
        return SessionChoice(id=sessions[0].id, mode=mode, fork=opts.fork)

    while True:
        if len(sessions) == 1:
            ui.info("One earlier Claude Code conversation for this folder:")
            ui.info(f"  {describe_session(sessions[0])}")
            if not ui.confirm("Continue this conversation? (no starts a new one)", True):
                return SessionChoice(id=None, mode=mode)
            chosen = sessions[0]
        else:
            ui.info("Claude Code conversations for this folder (newest first):")
            pick = ui.choose(
                "Which one? (n for a new conversation)",
                [describe_session(s) for s in sessions],
                extra={"n": "start a new conversation"},
            )
            if pick == "n":
                return SessionChoice(id=None, mode=mode)
            chosen = sessions[int(pick)]

        if not getattr(chosen, "maybe_open", False):
            return SessionChoice(id=chosen.id, mode=mode, fork=opts.fork)
        ui.warn(
            "This conversation was used in the last 2 minutes, so it may be open in another window. "
            "Continuing it does not type into that window: a second Claude Code process continues the "
            "same conversation, and the two can get mixed up."
        )
        what = ui.choose(
            "What do you want to do",
            [
                "fork: continue in a copy of the conversation (safe)",
                "continue it anyway",
                "pick another conversation",
            ],
        )
        if what == 0:
            return SessionChoice(id=chosen.id, mode=mode, fork=True)
        if what == 1:
            return SessionChoice(id=chosen.id, mode=mode, fork=opts.fork)
        others = [s for s in sessions if s.id != chosen.id]
        if not others:
            ui.info("There is no other conversation. A new one starts.")
            return SessionChoice(id=None, mode=mode)
        sessions = others


def describe_choice(choice: SessionChoice) -> str:
    who = f"conversation {choice.id}" if choice.id else "a new conversation"
    if choice.id and choice.fork:
        who += " (as a copy: fork)"
    how = "with a window (interactive)" if choice.mode == "interactive" else "without a window (headless)"
    return f"{who}, {how}"


# ---- the menu ---------------------------------------------------------------------------------


def run_review(
    ui: UI,
    reason: ApproveReason,
    items: Sequence[EvidenceItem],
    opts: SessionOptions,
    claude_cwd: Path,
) -> ReviewResult:
    proposal = reason.proposal
    prompt = proposal.prompt
    if sha256_hex(prompt) != proposal.prompt_sha256:
        ui.warn("The prompt's hash does not match the hash the agent sent. Read the prompt with care.")
    compact = opts.quick
    if compact:
        show_summary_compact(ui, reason)
    else:
        show_summary(ui, reason)
    seen = False  # FR-APP-01: the full text is shown before any approval

    while True:
        if not compact:
            ui.console.print()
        ui.dim(MENU_COMPACT if compact else MENU)
        try:
            choice = ui.read_line("Your choice: ").strip().lower()
        except Cancelled:
            return ReviewResult("quit")
        edited = prompt != proposal.prompt

        if choice in ("p", "prompt"):
            ui.pager(prompt)
            seen = True
        elif choice in ("e", "edit"):
            new = edit_text(ui, prompt)
            if new is None:
                continue
            seen = True
            if not new.strip():
                ui.warn("The edited prompt is empty. The previous text is kept.")
            elif new == prompt:
                ui.info("No change.")
            else:
                prompt = new
                ui.ok(f"Prompt changed: now {len(prompt.splitlines())} lines. You approve this new text.")
        elif choice in ("s", "sources"):
            show_sources(ui, reason)
        elif choice in ("v", "evidence"):
            show_evidence(ui, reason, items)
        elif choice in ("m", "missing"):
            result = _answer_missing(ui, proposal.missing_info, edited)
            if result is not None:
                return result
        elif choice in ("a", "approve"):
            if not seen:
                ui.info("This is the full prompt you are about to approve:")
                ui.pager(prompt)
                seen = True
            if not compact:
                ui.info(
                    "Approving sends this exact prompt to Claude Code on this laptop. Claude Code still asks "
                    "its own permission questions for anything it wants to do."
                )
            if edited:
                ui.info("You approve your edited text.")
            try:
                label = "Type y or yes to approve" if opts.quick else "Type yes to approve"
                if not ui.typed_yes(f"{label} (anything else goes back):", quick=opts.quick):
                    ui.info("Not approved.")
                    continue
                session = pick_session(ui, opts, claude_cwd)
            except Cancelled:
                ui.info("Not approved.")
                continue
            ui.info(f"Claude Code: {describe_choice(session)}.")
            response = ApproveResponse(
                decision="approve", prompt_sha256=sha256_hex(prompt), edited=edited, session=session
            )
            return ReviewResult("approve", response, prompt)
        elif choice in ("r", "reject"):
            try:
                why = ui.ask("Why do you reject it? (optional)")
            except Cancelled:
                why = ""
            return ReviewResult("reject", ApproveResponse(decision="reject", reason=why[:500] or None))
        elif choice in ("q", "quit"):
            return ReviewResult("quit")
        else:
            ui.info("Please type one of the letters: p, e, s, v, m, a, r, q.")


def _answer_missing(ui: UI, missing: Sequence[str], edited: bool) -> ReviewResult | None:
    if not missing:
        ui.info("Nothing is missing.")
        return None
    if edited:
        ui.warn("The agent writes the prompt again with your answers. Your edits to the prompt are lost.")
        try:
            if not ui.confirm("Go on?", False):
                return None
        except Cancelled:
            return None
    answers: dict[str, str] = {}
    try:
        for item in missing:
            value = ui.ask(f"{item} (empty to skip)")
            if value:
                answers[item] = value
    except Cancelled:
        ui.info("No answers sent.")
        return None
    if not answers:
        ui.info("No answers given.")
        return None
    return ReviewResult("answer", ApproveResponse(decision="answer", answers=answers))
