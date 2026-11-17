"""The orchestrator's spans (Grafana reads them with TraceQL; see wattson.shared.telemetry).

wattson.step      one per run of a workflow tool: task id, step, workflow, the status after it.
                  A run resumed after an interrupt also has wattson.interrupt (clarify,
                  collect_evidence, approve, run_claude_code) and wattson.wait_s (float) and
                  wattson.wait_ms (int): how long the task waited for the user, from the moment
                  the interrupt was raised.
wattson.task.end  once per task: the outcome, the workflow, the triage decision (confidence as a
                  float and as wattson.triage.confidence_pct, an int 0..100), the duration.

Only ids, names, numbers and statuses: never the request, the prompt, the evidence or an answer.
The interrupt's raise time and the task's trace are kept in agent.state["telemetry"], so a wait
that spans several A2A requests is measured, and expiry can end the task in its own trace."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from wattson.agents.orchestrator import working as w
from wattson.shared import telemetry

STATE_KEY = "telemetry"
STEP_SPAN = "wattson.step"
END_SPAN = "wattson.task.end"
INTERRUPT_PREFIX = "wattson."
# The interrupt each waiting step raises; used when the agent result does not name it.
INTERRUPT_OF_STEP = {
    "clarify": "clarify",
    "collect_evidence": "collect_evidence",
    "request_approval": "approve",
    "run_on_laptop": "run_claude_code",
}


def _state(agent: Any) -> dict[str, Any]:
    return dict(agent.state.get(STATE_KEY) or {})


def saved_trace(agent: Any) -> dict[str, str] | None:
    return _state(agent).get("trace")


class StepSpans(HookProvider):
    """One wattson.step span per tool run. Register it first: its AfterToolCallEvent callback then
    runs last (reverse order), after the audit has set the status."""

    def __init__(self) -> None:
        # toolUseId -> (start in ns, step name, attributes of a resumed run)
        self._open: dict[str, tuple[int, str, dict[str, Any]]] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)
        # An interrupt ends the invocation without an AfterToolCallEvent.
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def before(self, event: BeforeToolCallEvent) -> None:
        if not telemetry.enabled():
            return
        name = str(event.tool_use.get("name"))
        state = {} if name == "start_task" else _state(event.agent)
        resumed: dict[str, Any] = {}
        waiting = state.pop("waiting", None)
        if waiting and waiting.get("step") == name:
            wait = max(0.0, time.time() - float(waiting.get("since", 0)))
            resumed = {
                "wattson.interrupt": waiting.get("interrupt"),
                "wattson.wait_s": round(wait, 3),
                "wattson.wait_ms": round(wait * 1000),  # an int: Tempo's quantiles need one
            }
        if not state.get("trace"):
            state["trace"] = telemetry.current_trace()
        event.agent.state.set(STATE_KEY, state)
        self._open[str(event.tool_use.get("toolUseId"))] = (time.time_ns(), name, resumed)

    def after(self, event: AfterToolCallEvent) -> None:
        run = self._open.pop(str(event.tool_use.get("toolUseId")), None)
        if run is None:
            return
        failed = (event.result or {}).get("status") == "error"
        self._emit(event.agent, run, failed)

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        if not self._open:
            return
        result = event.result
        interrupted = result is not None and result.stop_reason == "interrupt"
        names = [str(i.name) for i in (result.interrupts or [])] if interrupted and result else []
        state = _state(event.agent)
        for run in self._open.values():
            self._emit(event.agent, run, failed=False)
            _start, step, _resumed = run
            interrupt = names[0].removeprefix(INTERRUPT_PREFIX) if names else INTERRUPT_OF_STEP.get(step)
            if interrupted and interrupt:
                state["waiting"] = {"step": step, "interrupt": interrupt, "since": time.time()}
        self._open.clear()
        event.agent.state.set(STATE_KEY, state)

    @staticmethod
    def _emit(agent: Any, run: tuple[int, str, dict[str, Any]], failed: bool) -> None:
        start_ns, step, resumed = run
        task = w.load_task(agent)
        attributes = {
            "wattson.task_id": task.get("id"),
            "wattson.step": step,
            "wattson.workflow": task.get("workflow"),
            "wattson.status": task.get("status"),
            **resumed,
        }
        telemetry.record_span(STEP_SPAN, attributes, start_ns=start_ns, error=failed)


def _seconds_since(created_at: str | None) -> float | None:
    if not created_at:
        return None
    try:
        start = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return round(max(0.0, (datetime.now(UTC) - start).total_seconds()), 3)


def task_end(
    *,
    task_id: str,
    outcome: str,
    workflow: str | None,
    classification: Any,
    created_at: str | None,
    parent: dict[str, str] | None = None,
) -> None:
    """The wattson.task.end span. `parent`: a saved trace (expiry, outside any request)."""
    if not telemetry.enabled():
        return
    now = time.time_ns()
    attributes = {
        "wattson.task_id": task_id,
        "wattson.outcome": outcome,
        "wattson.workflow": workflow,
        **telemetry.triage_attributes(classification),
        "wattson.task_duration_s": _seconds_since(created_at),
    }
    telemetry.record_span(
        END_SPAN,
        attributes,
        start_ns=now,
        end_ns=now,
        error=outcome == "failed",
        parent=telemetry.parent_context(parent),
    )


def task_end_of(task: dict[str, Any]) -> None:
    """The end span from the working state (the finish step)."""
    task_end(
        task_id=str(task.get("id")),
        outcome=str(task.get("status")),
        workflow=task.get("workflow"),
        classification=task.get("classification") or {},
        created_at=task.get("created_at"),
    )


def task_end_of_record(record: Any, parent: dict[str, str] | None = None) -> None:
    """The end span from a TaskRecord (expiry, a late result)."""
    task_end(
        task_id=record.id,
        outcome=record.status,
        workflow=record.workflow,
        classification=record,  # TaskRecord has category, confidence and method
        created_at=record.created_at,
        parent=parent,
    )
