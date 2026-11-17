"""The Guard's Strands interventions on the orchestrator Agent.

RequestGuard   before_tool_call on the "guard" step (once per task, never on an interrupt resume):
               computes the verdict (rules + model); then, with INJECTION_MODE warn or cut and a
               request that is not blocked anyway, the injection model reads the request too (text
               pasted from a ticket, a log or a chat may give orders to the agent). It records a
               span, and in enforce mode raises ONE GUARD_CONFIRM interrupt for a risky request
               and/or a flagged one, or marks the task to be blocked. The shield down or slow: no
               question, a warning (fail open, like the evidence). The guard step tool (tools.py)
               reads the flags it leaves and routes to finish or on.
EvidenceGuard  after_tool_call on find_procedures and collect_evidence: a Transform that cuts any
               part of a document or evidence item that looks like an instruction to the agent,
               counts them, warns, and records a span. Then, with INJECTION_MODE warn or cut, the
               small injection model (guard/shield.py) reads each evidence item and each document
               section: warn names the parts it flags at the review, cut replaces them. It can
               add a warning or a cut, never undo a rule's cut, never block a task.

Monitor mode records the verdict and the spans but changes nothing in the task flow; the evidence
injection cut is a data-sanitisation safety net and runs whenever the Guard is not off. Off does
nothing. No content ever reaches a span or a record: only labels, names and counts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from strands.interventions import InterventionHandler, Proceed, Transform

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.guard.model import GuardModel
from wattson.agents.orchestrator.guard.rules import RuleSet
from wattson.agents.orchestrator.guard.shield import Shield
from wattson.agents.orchestrator.guard.verdict import combine
from wattson.shared import telemetry
from wattson.shared.contracts import (
    GUARD_CONFIRM,
    GuardConfirmReason,
    GuardConfirmResponse,
    GuardMode,
    GuardVerdict,
)

log = logging.getLogger("wattson.orchestrator")

GUARD_STEP = "guard"
EVIDENCE_TOOLS = {"find_procedures": w.DOCS, "collect_evidence": w.EVIDENCE}
REQUEST_SPAN = "wattson.guard.request"
EVIDENCE_SPAN = "wattson.guard.evidence"

# The injection model (INJECTION_MODE=cut) puts this in place of a part it flags.
INJECTION_MARK = "[removed: the injection model flagged this part]"
MIN_SECTION_CHARS = 200  # a shorter document section joins the next one (as measured)
MAX_NAMED = 5  # flagged parts named in the warning; the others are counted

# Flags the RequestGuard leaves in the working state; the guard step tool reads them.
BLOCK_FLAG = "guard_block"  # a plain reason sentence: the task is blocked
READONLY_FLAG = "guard_readonly"  # the user confirmed a risky request: go on read-only


BLOCKING_LABELS = ("malicious", "out_of_scope")


class RequestGuard(InterventionHandler):
    name = "wattson-request-guard"

    def __init__(
        self,
        ruleset: RuleSet,
        model: GuardModel | None,
        mode: GuardMode,
        shield: Shield | None = None,
        shield_timeout_s: float = 20.0,
    ) -> None:
        self._ruleset = ruleset
        self._model = model
        self._mode = mode
        self._shield = shield if shield is not None and shield.mode != "off" else None
        self._shield_timeout_s = shield_timeout_s  # min(INJECTION_TIMEOUT_S, GUARD_TIMEOUT_S)

    async def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any) -> Any:
        if event.tool_use.get("name") != GUARD_STEP or self._mode == "off":
            return Proceed()
        agent = event.agent
        task = w.load_task(agent)
        request = task.get("request") or ""

        verdict = self._cached(task)
        if verdict is None:
            verdict = await self._compute(request)
            latency = None
            blocked = self._mode == "enforce" and verdict.label in BLOCKING_LABELS
            if self._shield is not None and not blocked and request.strip():
                verdict, latency = await self._check_injection(request, verdict)
                if verdict.request_injection_failed:
                    w.warn(
                        task,
                        f"The injection model ({self._shield.model}) did not answer about your request; "
                        "it was checked without it.",
                    )
            task["guard"] = verdict.model_dump(mode="json")
            w.save_task(agent, task)
            self._span(task, verdict, latency)

        if self._mode == "monitor":
            return Proceed()  # record only, change nothing

        label = verdict.label
        if label in BLOCKING_LABELS:
            task[BLOCK_FLAG] = self._block_reason(label)
            w.save_task(agent, task)
            return Proceed()
        if label == "risky" or verdict.request_injection_flagged:
            self._confirm(event, task, verdict)  # one question for both reasons
        return Proceed()

    async def _check_injection(self, request: str, verdict: GuardVerdict) -> tuple[GuardVerdict, int]:
        """The injection model's score of the request (one part). Never raises: None is a failure."""
        shield = self._shield
        if shield is None:
            return verdict, 0
        started = time.perf_counter()
        scores = await shield.scores([request], timeout_s=self._shield_timeout_s)
        latency = int((time.perf_counter() - started) * 1000)
        if scores is None or len(scores) != 1:
            return verdict.model_copy(update={"request_injection_failed": True}), latency
        score = float(scores[0])
        update = {
            "request_injection_pct": max(0, min(100, round(score * 100))),
            "request_injection_flagged": score >= shield.threshold,
        }
        return verdict.model_copy(update=update), latency

    def _confirm(self, event: BeforeToolCallEvent, task: dict[str, Any], verdict: GuardVerdict) -> None:
        risky = verdict.label == "risky"
        flagged = verdict.request_injection_flagged
        model = self._shield.model if self._shield is not None else "the injection model"
        orders = (
            f"the injection model ({model}) reads orders for the agent in your request "
            f"(score {(verdict.request_injection_pct or 0) / 100:.2f}): "
            "text pasted from a ticket, a log or a chat?"
        )
        change = "This request looks like it would change the cluster or production"
        read_only = "Go on with a read-only investigation (no apply, delete, scale, restart or patch)?"
        if risky and flagged:
            question = f"{change}, and {orders} {read_only}"
        elif risky:
            question = f"{change}. {read_only}"
        else:
            question = f"{orders[0].upper()}{orders[1:]} Go on?"
        reason = GuardConfirmReason(task_id=task["id"], verdict=verdict, question=question, read_only=risky)
        response = event.interrupt(GUARD_CONFIRM, reason=reason.model_dump(mode="json"))
        answer = GuardConfirmResponse.model_validate(response)
        task = w.load_task(event.agent)
        if answer.proceed:
            if risky:
                task[READONLY_FLAG] = True
        elif risky and flagged:
            task[BLOCK_FLAG] = (
                "You stopped the task at the Guard's question (a risky request with orders for the agent "
                "in it); nothing ran."
            )
        elif risky:
            task[BLOCK_FLAG] = "You stopped the task at the risky-request check; nothing ran."
        else:
            task[BLOCK_FLAG] = "You stopped the task at the injection check of your request; nothing ran."
        w.save_task(event.agent, task)

    async def _compute(self, request: str) -> GuardVerdict:
        started = time.perf_counter()
        hits = self._ruleset.check_request(request)
        model_result = await self._model.verdict(request) if self._model is not None else None
        return combine(
            hits,
            model_result,
            mode=self._mode,
            model_name=self._model.name if self._model is not None else None,
            model_configured=self._model is not None,
            model_failed=self._model is not None and model_result is None,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _cached(task: dict[str, Any]) -> GuardVerdict | None:
        data = task.get("guard")
        return GuardVerdict.model_validate(data) if isinstance(data, dict) else None

    @staticmethod
    def _block_reason(label: str) -> str:
        if label == "malicious":
            return "The Guard stopped this: it looks harmful or hostile, not normal engineering work."
        return "The Guard stopped this: it does not look like engineering work for this agent."

    def _span(self, task: dict[str, Any], verdict: GuardVerdict, injection_latency_ms: int | None) -> None:
        now = time.time_ns()
        attributes: dict[str, Any] = {
            "wattson.task_id": task.get("id"),
            "wattson.guard.label": verdict.label,
            "wattson.guard.method": verdict.method,
            "wattson.guard.mode": verdict.mode,
            "wattson.guard.rule_hits": len(verdict.rule_hits),
            "wattson.guard.latency_ms": verdict.latency_ms,
        }
        if injection_latency_ms is not None and self._shield is not None:
            # Numbers and the model's name only: never the request.
            attributes |= {
                "wattson.guard.injection_model": self._shield.model,
                "wattson.guard.injection_ok": not verdict.request_injection_failed,
                "wattson.guard.injection_flagged": int(verdict.request_injection_flagged),
                "wattson.guard.injection_score_pct": verdict.request_injection_pct,
                "wattson.guard.injection_latency_ms": injection_latency_ms,
            }
        telemetry.record_span(REQUEST_SPAN, attributes, start_ns=now, end_ns=now)


class EvidenceGuard(InterventionHandler):
    name = "wattson-evidence-guard"

    def __init__(self, ruleset: RuleSet, mode: GuardMode, shield: Shield | None = None) -> None:
        self._ruleset = ruleset
        self._mode = mode
        self._shield = shield if shield is not None and shield.mode != "off" else None

    async def after_tool_call(self, event: AfterToolCallEvent, **kwargs: Any) -> Any:
        name = str(event.tool_use.get("name"))
        if self._mode == "off" or name not in EVIDENCE_TOOLS:
            return Proceed()
        key = EVIDENCE_TOOLS[name]
        stored = w.get(event.agent, key, [])
        # The rules first: they cut the lines they match. Then the model, on what is left.
        removed, names = (self._scan_docs if key == w.DOCS else self._scan_evidence)(stored)
        check = await self._model_check(key, stored) if self._shield is not None else None
        if removed == 0 and check is None:
            return Proceed()
        return Transform(apply=lambda e: self._commit(e, key, stored, removed, names, check))

    def _commit(
        self,
        event: AfterToolCallEvent,
        key: str,
        stored: list[Any],
        removed: int,
        names: list[str],
        check: _ModelCheck | None,
    ) -> None:
        agent = event.agent
        agent.state.set(key, stored)
        task = w.load_task(agent)
        guard = dict(task.get("guard") or {})
        if removed:
            guard["evidence_removed"] = int(guard.get("evidence_removed", 0)) + removed
            w.warn(
                task,
                f"{removed} part(s) of the evidence or documents looked like instructions to the agent "
                "and were removed before the prompt.",
            )
        if check is not None and self._shield is not None:
            guard["injection_model"] = self._shield.model
            if not check.ok:
                guard["injection_failed"] = True
                w.warn(
                    task,
                    f"The injection model ({self._shield.model}) did not answer: the evidence and the "
                    "documents were checked by the rules only.",
                )
            elif check.flagged:
                guard["injection_flagged"] = int(guard.get("injection_flagged", 0)) + len(check.flagged)
                w.warn(task, self._flag_warning(check.flagged))
        task["guard"] = guard
        w.save_task(agent, task)
        self._span(task, removed, names, check)

    def _scan_docs(self, docs: list[dict[str, Any]]) -> tuple[int, list[str]]:
        removed, names = 0, []
        for doc in docs:
            scan = self._ruleset.scan_evidence(doc.get("text") or "")
            if scan.removed:
                doc["text"] = scan.text
                removed += scan.removed
                names.extend(scan.rule_names)
            ref = doc.get("ref")
            if isinstance(ref, dict) and ref.get("excerpt"):
                scan_x = self._ruleset.scan_evidence(ref["excerpt"])
                if scan_x.removed:
                    ref["excerpt"] = scan_x.text
                    removed += scan_x.removed
                    names.extend(scan_x.rule_names)
        return removed, names

    def _scan_evidence(self, pairs: list[Any]) -> tuple[int, list[str]]:
        removed, names = 0, []
        for item in _items(pairs):
            scan = self._ruleset.scan_evidence(item.get("output") or "")
            if scan.removed:
                item["output"] = scan.text
                removed += scan.removed
                names.extend(scan.rule_names)
        return removed, names

    # ---- the injection model (the shield) ------------------------------------------------

    async def _model_check(self, key: str, stored: list[Any]) -> _ModelCheck | None:
        shield = self._shield
        assert shield is not None
        parts = _doc_parts(stored) if key == w.DOCS else _evidence_parts(stored)
        if not parts:
            return None
        started = time.perf_counter()
        scores = await shield.scores([p.text for p in parts])
        latency_ms = int((time.perf_counter() - started) * 1000)
        if scores is None:
            return _ModelCheck(parts=len(parts), flagged=[], ok=False, latency_ms=latency_ms)
        hits = [part for part, score in zip(parts, scores, strict=True) if score >= shield.threshold]
        if shield.mode == "cut":
            for part in hits:
                part.cut()
        return _ModelCheck(parts=len(parts), flagged=[p.where for p in hits], ok=True, latency_ms=latency_ms)

    def _flag_warning(self, flagged: list[str]) -> str:
        assert self._shield is not None
        n = len(flagged)
        named = "; ".join(flagged[:MAX_NAMED]) + (f"; and {n - MAX_NAMED} more" if n > MAX_NAMED else "")
        head = f"Injection model ({self._shield.model}): {n} part{'' if n == 1 else 's'} "
        head += f"look{'s' if n == 1 else ''} like orders to the agent"
        if self._shield.mode == "cut":
            return f"{head} and {'was' if n == 1 else 'were'} removed before the prompt: {named}."
        it = "it" if n == 1 else "them"
        return f"{head}: {named}. Still in the prompt (INJECTION_MODE=warn): read {it} before you approve."

    def _span(self, task: dict[str, Any], removed: int, names: list[str], check: _ModelCheck | None) -> None:
        attributes: dict[str, Any] = {
            "wattson.task_id": task.get("id"),
            "wattson.guard.evidence_removed": removed,
            # The names of the rules that cut, never what they cut.
            "wattson.guard.evidence_rules": ",".join(sorted(set(names))),
        }
        if check is not None and self._shield is not None:
            # Counts and the model's name: never a part, never a score of a part.
            attributes |= {
                "wattson.guard.injection_model": self._shield.model,
                "wattson.guard.injection_mode": self._shield.mode,
                "wattson.guard.injection_ok": check.ok,
                "wattson.guard.injection_parts": check.parts,
                "wattson.guard.injection_flagged": len(check.flagged),
                "wattson.guard.injection_latency_ms": check.latency_ms,
            }
        now = time.time_ns()
        telemetry.record_span(EVIDENCE_SPAN, attributes, start_ns=now, end_ns=now)


@dataclass
class _ModelCheck:
    parts: int  # parts sent to the model
    flagged: list[str]  # where each flagged part is, for the review ("evidence e3 (kubectl ...)")
    ok: bool  # False: the shield did not answer; the rules alone checked
    latency_ms: int


@dataclass
class _Part:
    text: str
    where: str
    cut: Callable[[], None]  # puts INJECTION_MARK in its place (INJECTION_MODE=cut)


def _items(pairs: list[Any]) -> list[dict[str, Any]]:
    """The evidence items of the stored [request, item] pairs."""
    return [p[1] for p in pairs if isinstance(p, list) and len(p) == 2 and isinstance(p[1], dict)]


def _evidence_parts(pairs: list[Any]) -> list[_Part]:
    """One part per evidence item: the output of one command, after the rules' cut."""
    parts = []
    for item in _items(pairs):
        if not (item.get("output") or "").strip():
            continue
        where = f"evidence {item.get('request_id') or '?'}"
        source = " ".join(str(item.get("source") or "").split())
        if source:
            where += f" ({source[:57] + '...' if len(source) > 60 else source})"

        def cut(item: dict[str, Any] = item) -> None:
            item["output"] = INJECTION_MARK

        parts.append(_Part(item["output"], where, cut))
    return parts


def _doc_parts(docs: list[dict[str, Any]]) -> list[_Part]:
    """The sections of each Grimoire document, and its excerpt, after the rules' cut."""
    parts = []
    for doc in docs:
        ref = doc.get("ref") if isinstance(doc.get("ref"), dict) else {}
        label = f"document {ref.get('key') or '?'}"
        secs = sections(doc.get("text") or "")
        for j, sec in enumerate(secs):
            if not sec.strip():
                continue

            def cut(doc: dict[str, Any] = doc, secs: list[str] = secs, j: int = j) -> None:
                secs[j] = INJECTION_MARK
                doc["text"] = "\n".join(secs)

            parts.append(_Part(sec, f"{label}, section {j + 1}" if len(secs) > 1 else label, cut))
        if ref.get("excerpt"):

            def cut_excerpt(ref: dict[str, Any] = ref) -> None:
                ref["excerpt"] = INJECTION_MARK

            parts.append(_Part(ref["excerpt"], f"{label}, excerpt", cut_excerpt))
    return parts


def sections(text: str) -> list[str]:
    """A document in parts for the injection model, as measured (eval/injection_model.py): one
    per Markdown heading, never cut inside a code fence; a part shorter than MIN_SECTION_CHARS
    joins the next one. Joined with "\\n" they are the text again."""
    out: list[str] = []
    cur: list[str] = []
    fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("#") and cur:
            out.append("\n".join(cur))
            cur = []
        cur.append(line)
    out.append("\n".join(cur))
    merged: list[str] = []
    carry: str | None = None
    for part in out:
        part = part if carry is None else carry + "\n" + part
        if len(part.strip()) < MIN_SECTION_CHARS:
            carry = part
            continue
        merged.append(part)
        carry = None
    if carry is not None:
        merged.append(carry)
    return merged
