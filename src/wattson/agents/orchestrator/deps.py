"""What the orchestrator's parts share: settings, the store, the remote agents, and the live agents.

The live agents are kept by A2A context id (weakly: the A2A executor owns them and may evict
them). Expiry and late results use this to update a task that is still in memory."""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.flow import TERMINAL
from wattson.agents.orchestrator.guard import Guard, build_guard
from wattson.agents.orchestrator.settings import Settings
from wattson.agents.orchestrator.store import Store
from wattson.agents.orchestrator.telemetry import saved_trace, task_end_of_record
from wattson.agents.orchestrator.tools import RemoteAgents, make_tools
from wattson.shared.contracts import TaskRecord


@dataclass
class Deps:
    settings: Settings
    store: Store
    remotes: RemoteAgents
    agents: weakref.WeakValueDictionary[str, Any] = field(default_factory=weakref.WeakValueDictionary)
    _tools: list[Any] | None = None
    _guard: Guard | None = None

    @property
    def guard(self) -> Guard:
        if self._guard is None:
            self._guard = build_guard(self.settings)
        return self._guard

    def tools(self) -> list[Any]:
        if self._tools is None:
            self._tools = make_tools(self.remotes)
        return self._tools

    def live_agents(self, task_id: str) -> list[Any]:
        return [a for a in list(self.agents.values()) if w.load_task(a).get("id") == task_id]

    def apply_record(self, record: TaskRecord) -> None:
        """A change made outside the agent (expiry, late result): make the agent agree, so the
        gate refuses a late answer to a task that is over."""
        for agent in self.live_agents(record.id):
            task = w.load_task(agent)
            task["status"] = record.status
            task["claude_session_id"] = record.claude_session_id
            task["error_summary"] = record.error_summary
            task["pending_events"] = []
            task["saved"] = {
                k: v for k, v in w.record_of(task).model_dump(mode="json").items() if k != "updated_at"
            }
            w.save_task(agent, task)
            if record.status in TERMINAL:
                w.drop_bulky(agent)

    def expire(self) -> list[str]:
        ids = self.store.expire_open(self.settings.task_ttl_minutes)
        for task_id in ids:
            record = self.store.get_task(task_id)
            if record is not None:
                # End the task in its own trace when its agent is still in memory.
                traces = [t for t in (saved_trace(a) for a in self.live_agents(task_id)) if t]
                task_end_of_record(record, parent=traces[0] if traces else None)
                self.apply_record(record)
        return ids

    def sweep(self) -> int:
        return self.store.sweep(self.settings.history_retention_days)
