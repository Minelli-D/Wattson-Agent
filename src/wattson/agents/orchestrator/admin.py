"""The admin routes next to A2A (docs/design.md, "Admin routes"). Same bearer token as A2A,
except /healthz. The Grimoire routes are passed to the knowledge agent, which owns the OAuth."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wattson.agents.orchestrator.agent import package_version
from wattson.agents.orchestrator.deps import Deps
from wattson.agents.orchestrator.flow import REPORTABLE, describe_errors, is_session_id
from wattson.agents.orchestrator.remote import RemoteError
from wattson.agents.orchestrator.store import short
from wattson.agents.orchestrator.telemetry import task_end_of_record
from wattson.agents.orchestrator.tools import MAX_ERROR
from wattson.shared.contracts import (
    ComponentHealth,
    ExecutionReport,
    GrimoireCallback,
    GrimoireConnectRequest,
    GrimoireStatus,
    HealthView,
)

MAX_BODY = 64 * 1024


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _body[M: BaseModel](request: Request, model: type[M]) -> M | JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _error(413, "too_large", "the body is too large")
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError as e:
        return _error(422, "invalid", f"the body is not a valid {model.__name__}: {describe_errors(e)}")


def admin_routes(deps: Deps) -> list[Route]:
    store = deps.store
    remotes: Any = deps.remotes

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def health(request: Request) -> JSONResponse:
        triage, grimoire = await asyncio.gather(
            remotes.triage_health(), _grimoire_status(), return_exceptions=False
        )
        if isinstance(grimoire, GrimoireStatus):
            if grimoire.connected:
                knowledge = ComponentHealth(ok=True, detail="Grimoire is connected")
            else:
                knowledge = ComponentHealth(
                    ok=False, detail="Grimoire is not connected: run `wattson grimoire connect`"
                )
            grimoire_view: GrimoireStatus | None = grimoire
        else:
            knowledge = ComponentHealth(ok=False, detail=f"knowledge: {grimoire}")
            grimoire_view = None
        shield = deps.guard.shield
        view = HealthView(
            ok=triage.ok and knowledge.ok,
            version=package_version(),
            triage=triage,
            knowledge=knowledge,
            grimoire=grimoire_view,
            history={"tasks": store.count_tasks(), "retention_days": deps.settings.history_retention_days},
            shield=await shield.health() if shield is not None else None,
        )
        return JSONResponse(view.model_dump(mode="json"))

    async def _grimoire_status() -> GrimoireStatus | str:
        try:
            return await remotes.grimoire_status()
        except RemoteError as e:
            return str(e)

    async def list_tasks(request: Request) -> JSONResponse:
        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            return _error(400, "invalid", "limit must be a whole number")
        if not 1 <= limit <= 500:
            return _error(400, "invalid", "limit must be between 1 and 500")
        return JSONResponse([r.model_dump(mode="json") for r in store.list_tasks(limit)])

    async def get_task(request: Request) -> JSONResponse:
        record = store.get_task(request.path_params["task_id"])
        if record is None:
            return _error(404, "not_found", "no such task")
        return JSONResponse(record.model_dump(mode="json"))

    async def task_events(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        if store.get_task(task_id) is None:
            return _error(404, "not_found", "no such task")
        return JSONResponse([e.model_dump(mode="json") for e in store.list_events(task_id)])

    async def report_execution(request: Request) -> JSONResponse:
        """A late result, when the A2A task is gone (pod restart, TTL): `wattson recover`."""
        task_id = request.path_params["task_id"]
        body = await _body(request, ExecutionReport)
        if isinstance(body, JSONResponse):
            return body
        current = store.get_task(task_id)
        if current is None:
            return _error(404, "not_found", "no such task")
        if current.status not in REPORTABLE:
            return _error(
                409, "conflict", f"the task is {current.status}; a result can only be reported after approval"
            )
        run = body.result
        changes: dict[str, Any] = {"error_summary": short(run.error, MAX_ERROR)}
        if is_session_id(run.session_id):
            changes["claude_session_id"] = run.session_id
        bits = [f"late report: Claude Code {run.state}"]
        if run.exit_code is not None:
            bits.append(f"exit {run.exit_code}")
        record = store.transition(task_id, run.state, "; ".join(bits), **changes)
        if record is None:
            return _error(404, "not_found", "no such task")
        task_end_of_record(record)  # in this request's trace: the CLI sends the task's traceparent
        deps.apply_record(record)
        return JSONResponse(record.model_dump(mode="json"))

    def grimoire_proxy(method: str, path: str, model: type[BaseModel] | None) -> Any:
        async def handler(request: Request) -> JSONResponse:
            body: Any = None
            if model is not None:
                parsed = await _body(request, model)
                if isinstance(parsed, JSONResponse):
                    return parsed
                body = parsed.model_dump(mode="json", exclude_none=True)  # no empty optional fields
            try:
                status, data = await remotes.grimoire(method, path, body)
            except RemoteError as e:
                return _error(502, "bad_gateway", f"the knowledge agent: {e}")
            return JSONResponse(data, status_code=status)

        return handler

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/tasks", list_tasks, methods=["GET"]),
        Route("/v1/tasks/{task_id}", get_task, methods=["GET"]),
        Route("/v1/tasks/{task_id}/events", task_events, methods=["GET"]),
        Route("/v1/tasks/{task_id}/execution", report_execution, methods=["POST"]),
        Route("/v1/grimoire/status", grimoire_proxy("GET", "/v1/grimoire/status", None), methods=["GET"]),
        Route(
            "/v1/grimoire/connect",
            grimoire_proxy("POST", "/v1/grimoire/connect", GrimoireConnectRequest),
            methods=["POST"],
        ),
        Route(
            "/v1/grimoire/callback",
            grimoire_proxy("POST", "/v1/grimoire/callback", GrimoireCallback),
            methods=["POST"],
        ),
        Route(
            "/v1/grimoire/disconnect",
            grimoire_proxy("POST", "/v1/grimoire/disconnect", None),
            methods=["POST"],
        ),
    ]
