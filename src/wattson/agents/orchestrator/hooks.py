"""Safety and audit live in hooks, not in the planner (docs/design.md).

ApprovalGate  cancels a step the transition table does not allow from the current status, and
              cancels run_on_laptop unless the user approved a prompt hash (FR-DEC-06, SEC-10).
Audit         writes status changes and step errors to the store (MEM-01..03, NFR-05). It is the
              only writer of task records; tools queue changes in the working state.
Redaction     redacts every tool result before it enters the conversation (SEC-05).

AfterToolCallEvent callbacks run in reverse order of registration, so build_agent registers
[ApprovalGate, Audit, Redaction]: results are redacted before the audit sees them."""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.flow import TERMINAL, allowed, is_sha256, now_iso
from wattson.agents.orchestrator.store import Store, short
from wattson.shared.contracts import TaskEvent
from wattson.shared.redact import redact

log = logging.getLogger("wattson.orchestrator")

GATE_NO_APPROVAL = "run_on_laptop needs the user's approval of this exact prompt hash"


class ApprovalGate(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check)

    def check(self, event: BeforeToolCallEvent) -> None:
        name = str(event.tool_use.get("name"))
        task = w.load_task(event.agent)
        status = task.get("status")
        if not allowed(name, status):
            event.cancel_tool = (
                f"the step {name!r} is not allowed while the task is {status or 'not started'}"
            )
            return
        if name == "run_on_laptop":
            approval = w.get(event.agent, w.APPROVAL, {})
            approved = approval.get("prompt_sha256")
            if (
                approval.get("decision") != "approve"
                or not is_sha256(approved)
                or task.get("approved_sha256") != approved
            ):
                event.cancel_tool = GATE_NO_APPROVAL


class Audit(HookProvider):
    def __init__(self, store: Store) -> None:
        self.store = store

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        # An interrupt ends the invocation without an AfterToolCallEvent: flush here too.
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        result = event.result or {}
        if result.get("status") == "error":
            self._step_failed(event.agent, str(event.tool_use.get("name")), result, event.cancel_message)
        self.flush(event.agent)

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        self.flush(event.agent)

    def _step_failed(self, agent: Any, name: str, result: dict[str, Any], cancelled: str | None) -> None:
        task = w.load_task(agent)
        if not task.get("id"):
            return
        text = " ".join(b.get("text", "") for b in result.get("content", []) if isinstance(b, dict))
        what = "refused" if cancelled else "failed"
        detail = f"step {name} {what}: {text}"
        log.warning(json.dumps({"t": now_iso(), "event": f"step_{what}", "task": task["id"], "tool": name}))
        if task.get("status") in TERMINAL:
            w.mark(task, task["status"], detail, note=True)
        else:
            task["error_summary"] = short(detail)
            w.mark(task, "failed", detail)
        w.save_task(agent, task)

    def flush(self, agent: Any) -> None:
        """Write queued status changes and the current record. Safe to call any time."""
        task = w.load_task(agent)
        if not task.get("id") or not task.get("status"):
            return
        pending = task.get("pending_events") or []
        record = w.record_of(task)
        snapshot = {k: v for k, v in record.model_dump(mode="json").items() if k != "updated_at"}
        if not pending and task.get("saved") == snapshot:
            return
        self.store.save_task(record)
        for ev in pending:
            self.store.add_event(
                TaskEvent(
                    task_id=task["id"],
                    at=ev["at"],
                    from_status=ev["from"],
                    to_status=ev["to"],
                    detail=ev["detail"],
                )
            )
        task["pending_events"] = []
        task["saved"] = snapshot
        w.save_task(agent, task)


class Redaction(HookProvider):
    """Tool results are small summaries, but they enter the conversation: redact them anyway."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.clean)

    def clean(self, event: AfterToolCallEvent) -> None:
        result = event.result
        if not result:
            return
        changed = False
        content = []
        for block in result.get("content", []):
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text = _redact_text(block["text"])
                if text != block["text"]:
                    changed = True
                    block = {**block, "text": text}
            content.append(block)
        if changed:
            event.result = {**result, "content": content}


def _redact_text(text: str) -> str:
    # A step() result is JSON: redact its string values, so the planner can still read it.
    try:
        data = json.loads(text)
    except ValueError:
        return redact(text).text
    cleaned = _redact_value(data, key=None)
    return text if cleaned == data else json.dumps(cleaned)


def _redact_value(value: Any, key: str | None) -> Any:
    if isinstance(value, str):
        return value if key == "next" else redact(value).text
    if isinstance(value, list):
        return [_redact_value(v, None) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v, k) for k, v in value.items()}
    return value
