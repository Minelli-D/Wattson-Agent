"""The A2A client of the orchestrator: JSON-RPC `message/send` over httpx with the bearer token,
plus the admin routes (design: "Admin routes").

Wire protocol (proven by the spike):
  send     DataPart {START_TASK_KEY: StartTask}
  receive  state "input-required", status DataPart {"interrupts": [{interruptId, name, reason}]}
  answer   DataPart {"interruptResponse": {"interruptId", "response"}}, same taskId and contextId
  receive  state "completed" with an artifact holding a TaskOutcome
Every reason is checked against its contract model before the CLI shows it.

Traces: every request of one task carries a W3C `traceparent` with the same trace id (made at
start), so the agents put the whole task, waits included, in one trace. The CLI exports nothing.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from wattson.cli.portforward import Endpoint, PortForwardError
from wattson.shared.contracts import (
    APPROVE,
    CLARIFY,
    COLLECT_EVIDENCE,
    GUARD_CONFIRM,
    RUN_CLAUDE_CODE,
    START_TASK_KEY,
    ApproveReason,
    ClarifyReason,
    CollectEvidenceReason,
    ExecutionReport,
    GrimoireCallback,
    GrimoireConnectRequest,
    GrimoireConnectView,
    GrimoireStatus,
    GuardConfirmReason,
    HealthView,
    RunReason,
    RunResponse,
    StartTask,
    TaskEvent,
    TaskOutcome,
    TaskRecord,
)

MESSAGE_TIMEOUT_S = 900.0  # the model, Grimoire and the shield (INJECTION_TIMEOUT_S, up to 300 s) can be slow
ADMIN_TIMEOUT_S = 15.0
TASK_NOT_FOUND = -32001

REASON_MODELS: dict[str, type[BaseModel]] = {
    CLARIFY: ClarifyReason,
    COLLECT_EVIDENCE: CollectEvidenceReason,
    APPROVE: ApproveReason,
    RUN_CLAUDE_CODE: RunReason,
    GUARD_CONFIRM: GuardConfirmReason,
}

M = TypeVar("M", bound=BaseModel)


def new_trace_id() -> str:
    """32 hex characters, never all zeros (W3C trace context)."""
    while True:
        trace_id = secrets.token_hex(16)
        if trace_id.strip("0"):
            return trace_id


def traceparent(trace_id: str) -> str:
    """The header for one request: the task's trace id, a fresh parent span id, sampled."""
    while True:
        span_id = secrets.token_hex(8)
        if span_id.strip("0"):
            return f"00-{trace_id}-{span_id}-01"


class AgentError(Exception):
    """Something went wrong talking to the agent. The message is for people."""


class AgentUnreachable(AgentError):
    pass


class TaskGone(AgentError):
    """The A2A task is not there any more (pod restart, expiry): report with the admin route."""


class NotFound(AgentError):
    pass


class ProtocolError(AgentError):
    """The agent sent something this CLI does not understand (a contract mismatch)."""


@dataclass
class PendingInterrupt:
    id: str
    name: str
    reason: Any  # the validated contract model: ClarifyReason, CollectEvidenceReason, ...


@dataclass
class TaskView:
    id: str
    context_id: str
    state: str  # A2A state: input-required, completed, failed, canceled, rejected, ...
    interrupts: list[PendingInterrupt] = field(default_factory=list)
    outcome: TaskOutcome | None = None
    error: str | None = None
    message: str | None = None


def _message(parts: list[dict[str, Any]], task: TaskView | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "user", "kind": "message", "messageId": uuid.uuid4().hex, "parts": parts}
    if task is not None:
        msg["taskId"] = task.id
        msg["contextId"] = task.context_id
    return msg


def _texts(parts: list[dict[str, Any]]) -> list[str]:
    return [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]


def _datas(parts: list[dict[str, Any]]) -> list[Any]:
    return [p.get("data") for p in parts if isinstance(p, dict) and p.get("kind") == "data"]


def parse_interrupt(raw: dict[str, Any]) -> PendingInterrupt:
    name = raw.get("name")
    interrupt_id = raw.get("interruptId")
    if not isinstance(interrupt_id, str) or not interrupt_id:
        raise ProtocolError("the agent asked a question without an id")
    model = REASON_MODELS.get(str(name))
    if model is None:
        raise ProtocolError(f"the agent asks something this CLI does not know: {name!r}. Update the CLI.")
    reason = raw.get("reason")
    if isinstance(reason, str):  # a reason the server could not encode arrives as text
        try:
            reason = json.loads(reason)
        except ValueError:
            pass
    try:
        validated = model.model_validate(reason)
    except ValidationError as exc:
        raise ProtocolError(
            f"the agent's question {name} does not match this CLI's contract: {_first_errors(exc)}"
        ) from exc
    return PendingInterrupt(id=interrupt_id, name=str(name), reason=validated)


def _outcome_from(value: Any) -> tuple[TaskOutcome | None, str | None]:
    """A TaskOutcome, or an error text, from an artifact value."""
    if isinstance(value, dict):
        if "task_id" in value and "status" in value:
            try:
                return TaskOutcome.model_validate(value), None
            except ValidationError as exc:
                raise ProtocolError(
                    f"the agent's result does not match this CLI's contract: {_first_errors(exc)}"
                ) from exc
        if set(value) == {"error"}:
            return None, str(value["error"])
        if len(value) == 1:
            return _outcome_from(next(iter(value.values())))
    return None, None


def parse_task(result: dict[str, Any]) -> TaskView:
    if not isinstance(result, dict) or result.get("kind", "task") != "task":
        raise ProtocolError("the agent answered with a message instead of a task")
    status = result.get("status") or {}
    state = str(status.get("state") or "unknown")
    status_parts = (status.get("message") or {}).get("parts") or []
    view = TaskView(id=str(result.get("id")), context_id=str(result.get("contextId")), state=state)
    view.message = "\n".join(t for t in _texts(status_parts) if t).strip() or None

    if state == "input-required":
        for data in _datas(status_parts):
            if isinstance(data, dict):
                view.interrupts += [parse_interrupt(i) for i in data.get("interrupts") or []]
        if not view.interrupts:
            raise ProtocolError("the agent waits for input but did not say what it needs")
    elif state == "completed":
        texts: list[str] = []
        for artifact in result.get("artifacts") or []:
            parts = artifact.get("parts") or []
            values: list[Any] = list(_datas(parts))
            # A streamed artifact arrives in chunks: parse the joined text first, then each part.
            pieces = _texts(parts)
            joined = "".join(pieces)
            texts.append(joined)
            for text in [joined, *pieces] if len(pieces) > 1 else pieces:
                try:
                    values.append(json.loads(text))
                except ValueError:
                    continue
            for value in values:
                outcome, error = _outcome_from(value)
                view.outcome = view.outcome or outcome
                view.error = view.error or error
        if view.outcome is None and view.error is None:
            view.message = "\n".join(texts).strip() or view.message
    elif state in ("failed", "canceled", "rejected"):
        view.error = view.message or f"the task ended in the state {state}"
    return view


def _first_errors(exc: ValidationError, limit: int = 3) -> str:
    out = []
    for err in exc.errors()[:limit]:
        where = ".".join(str(x) for x in err.get("loc", ()))
        out.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return "; ".join(out)


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


class AgentClient:
    def __init__(
        self,
        endpoint: Endpoint,
        token: str,
        message_timeout: float = MESSAGE_TIMEOUT_S,
        admin_timeout: float = ADMIN_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ):
        self.endpoint = endpoint
        self._headers = {"Authorization": f"Bearer {token}"}
        self.message_timeout = message_timeout
        self.admin_timeout = admin_timeout
        self._transport = transport
        self.trace_id: str | None = None  # the current task's trace; in memory for this run only

    # ---- transport ------------------------------------------------------------------------

    def _trace_headers(self) -> dict[str, str]:
        return {"traceparent": traceparent(self.trace_id)} if self.trace_id else {}

    def _request(self, method: str, path: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
        for attempt in (1, 2):
            try:
                base = self.endpoint.base_url()
            except PortForwardError as exc:
                raise AgentUnreachable(f"cannot reach the agent: {exc}") from exc
            try:
                with httpx.Client(
                    base_url=base, headers=self._headers, timeout=timeout, transport=self._transport
                ) as client:
                    resp = client.request(method, path, **kwargs)
            except httpx.ConnectError as exc:
                # Nothing reached the agent: safe to open the forward again and retry once.
                if attempt == 1 and self._reopen():
                    continue
                raise AgentUnreachable(f"cannot reach the agent at {base}: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise AgentError(f"the agent did not answer within {timeout:.0f} seconds") from exc
            except httpx.HTTPError as exc:
                raise AgentUnreachable(f"cannot reach the agent at {base}: {exc}") from exc
            if resp.status_code == 401:
                raise AgentError(
                    "the agent refused the token (HTTP 401). Check the token file, or run k8s/deploy.sh."
                )
            return resp
        raise AssertionError("unreachable")

    def _reopen(self) -> bool:
        try:
            return self.endpoint.reopen()
        except PortForwardError:
            return False

    def _admin(
        self,
        method: str,
        path: str,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body.model_dump(mode="json")
        if params:
            kwargs["params"] = params
        if headers:
            kwargs["headers"] = headers
        resp = self._request(method, path, timeout=self.admin_timeout, **kwargs)
        if resp.status_code == 404:
            raise NotFound(f"not found: {_detail(resp)}")
        if resp.status_code >= 400:
            raise AgentError(f"the agent answered HTTP {resp.status_code} on {path}: {_detail(resp)}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProtocolError(f"the agent answered no JSON on {path}") from exc

    def _admin_model(
        self,
        model: type[M],
        method: str,
        path: str,
        body: BaseModel | None = None,
        headers: dict[str, str] | None = None,
    ) -> M:
        data = self._admin(method, path, body, headers=headers)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ProtocolError(
                f"the answer of {path} does not match this CLI's contract: {_first_errors(exc)}"
            ) from exc

    def _admin_list(self, model: type[M], path: str, params: dict[str, Any] | None = None) -> list[M]:
        data = self._admin("GET", path, params=params)
        try:
            return TypeAdapter(list[model]).validate_python(data)  # type: ignore[valid-type]
        except ValidationError as exc:
            raise ProtocolError(
                f"the answer of {path} does not match this CLI's contract: {_first_errors(exc)}"
            ) from exc

    def _send(self, message: dict[str, Any]) -> TaskView:
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {"message": message},
        }
        resp = self._request(
            "POST", "/", timeout=self.message_timeout, json=body, headers=self._trace_headers()
        )
        if resp.status_code >= 400:
            raise AgentError(f"the agent answered HTTP {resp.status_code}: {_detail(resp)}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProtocolError("the agent answered no JSON") from exc
        if data.get("error"):
            err = data["error"]
            text = str(err.get("message") or err)
            if err.get("code") == TASK_NOT_FOUND or "terminal state" in text:
                raise TaskGone(f"the task is not open on the agent any more ({text})")
            raise AgentError(f"the agent refused the message: {text}")
        return parse_task(data.get("result"))

    # ---- A2A ------------------------------------------------------------------------------

    def card(self) -> dict[str, Any]:
        return self._admin("GET", "/.well-known/agent-card.json")

    def start(self, start: StartTask) -> TaskView:
        self.trace_id = new_trace_id()  # one trace per task: reused by every answer
        part = {"kind": "data", "data": {START_TASK_KEY: start.model_dump(mode="json")}}
        return self._send(_message([part]))

    def answer(self, task: TaskView, answers: Sequence[tuple[str, BaseModel]]) -> TaskView:
        parts = [
            {
                "kind": "data",
                "data": {"interruptResponse": {"interruptId": iid, "response": resp.model_dump(mode="json")}},
            }
            for iid, resp in answers
        ]
        return self._send(_message(parts, task))

    # ---- admin routes ---------------------------------------------------------------------

    def health(self) -> HealthView:
        return self._admin_model(HealthView, "GET", "/v1/health")

    def tasks(self, limit: int = 20) -> list[TaskRecord]:
        return self._admin_list(TaskRecord, "/v1/tasks", params={"limit": limit})

    def task(self, task_id: str) -> TaskRecord:
        return self._admin_model(TaskRecord, "GET", f"/v1/tasks/{task_id}")

    def events(self, task_id: str) -> list[TaskEvent]:
        return self._admin_list(TaskEvent, f"/v1/tasks/{task_id}/events")

    def report_execution(self, task_id: str, result: RunResponse) -> TaskRecord:
        """A late result. In the task's trace when the task started in this run."""
        return self._admin_model(
            TaskRecord,
            "POST",
            f"/v1/tasks/{task_id}/execution",
            ExecutionReport(result=result),
            headers=self._trace_headers(),
        )

    def grimoire_status(self) -> GrimoireStatus:
        return self._admin_model(GrimoireStatus, "GET", "/v1/grimoire/status")

    def grimoire_connect(self, redirect_uri: str) -> GrimoireConnectView:
        return self._admin_model(
            GrimoireConnectView,
            "POST",
            "/v1/grimoire/connect",
            GrimoireConnectRequest(redirect_uri=redirect_uri),
        )

    def grimoire_callback(self, code: str, state: str, iss: str | None = None) -> GrimoireStatus:
        return self._admin_model(
            GrimoireStatus, "POST", "/v1/grimoire/callback", GrimoireCallback(code=code, state=state, iss=iss)
        )

    def grimoire_disconnect(self) -> GrimoireStatus:
        return self._admin_model(GrimoireStatus, "POST", "/v1/grimoire/disconnect")
