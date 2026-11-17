"""The triage agent: a Strands Agent with one tool, served by Strands' A2AServer (docs/design.md).

The orchestrator sends a message with a DataPart {"wattson.triage": TriageRequest}; the A2A
artifact of the completed task is a TriageResult as JSON text, or {"error": "..."} when the request
does not parse. Admin: GET /healthz (open), GET /v1/health -> ComponentHealth. Bearer token on all
else.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import uvicorn
from a2a.types import AgentSkill
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer

from wattson import __version__
from wattson.shared.auth import BearerAuth
from wattson.shared.contracts import TRIAGE_KEY, TriageRequest
from wattson.shared.planner import PlannerModel, parse_structured, step
from wattson.shared.telemetry import quiet, setup_telemetry, traced_asgi

from .engine import TriageEngine
from .settings import Settings

log = logging.getLogger(__name__)

AGENT_NAME = "wattson-triage"
AGENT_DESCRIPTION = (
    "Classifies an engineering request into a workflow and suggests hints (cluster, namespace, "
    "workload, files, search queries) with a small CPU model and keyword rules. Advice only."
)
SKILL = AgentSkill(
    id="triage",
    name="triage",
    description=f"DataPart {{{TRIAGE_KEY!r}: TriageRequest}} -> artifact TriageResult (JSON text).",
    tags=["triage", "classification", "hints"],
    input_modes=["application/json"],
    output_modes=["application/json"],
)

_default_engine: TriageEngine | None = None


def _request_data(message: str) -> Any:
    data = parse_structured(message)
    if not data:  # also accept the plain JSON text, for clients that send a TextPart
        try:
            data = json.loads(message)
        except ValueError:
            data = {}
    return data.get(TRIAGE_KEY) if isinstance(data, dict) else None


def make_triage_tool(engine: TriageEngine) -> Any:
    @tool(name="triage")
    async def triage(message: str) -> str:
        """Classify one engineering request and suggest hints.

        Args:
            message: the request message; holds a DataPart with the TriageRequest.
        """
        data = _request_data(message)
        if data is None:
            return step(output={"error": f"no {TRIAGE_KEY} DataPart in the message"})
        try:
            request = TriageRequest.model_validate(data)
        except ValidationError as exc:
            fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors()[:5])
            return step(output={"error": f"the TriageRequest is not valid: {fields}"})
        result = await engine.triage(request)
        return step(output=result.model_dump(mode="json"))

    return triage


async def warm_up_quietly(engine: TriageEngine, **kwargs: Any) -> bool:
    """The warm-up is not a user's triage: no span, and its model calls carry no trace context."""
    with quiet():
        return await engine.warm_up(**kwargs)


def build_agent(context_id: str, engine: TriageEngine | None = None) -> Agent:
    """One agent per A2A context (A2AServer's agent_factory). Its planner calls the tool once."""
    global _default_engine
    if engine is None:
        if _default_engine is None:
            _default_engine = TriageEngine(Settings.from_env())
        engine = _default_engine
    return Agent(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        model=PlannerModel("triage"),
        tools=[make_triage_tool(engine)],
        callback_handler=None,
    )


def create_app(settings: Settings | None = None, engine: TriageEngine | None = None) -> Any:
    """The ASGI app: A2A (JSON-RPC at "/", Agent Card) plus the admin routes, behind BearerAuth."""
    settings = settings or Settings.from_env()
    engine = engine or TriageEngine(settings)
    server = A2AServer(
        agent_factory=lambda context_id: build_agent(context_id, engine),
        host=settings.listen_host,
        port=settings.port,
        http_url=settings.public_url,
        version=__version__,
        skills=[SKILL],
        enable_a2a_compliant_streaming=True,
    )
    app = server.to_starlette_app()

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def health(_: Request) -> JSONResponse:
        return JSONResponse((await engine.health()).model_dump())

    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(the_app: Any) -> Any:
        warm = asyncio.create_task(warm_up_quietly(engine))  # in the background: the pod starts at once
        try:
            async with original_lifespan(the_app) as state:
                yield state
        finally:
            warm.cancel()
            await engine.aclose()

    app.router.lifespan_context = lifespan
    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_route("/v1/health", health, methods=["GET"])
    # Outermost: the trace context of the orchestrator's call is read first (when traces are on).
    return traced_asgi(BearerAuth(app, settings.token, open_paths=("/healthz",)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    setup_telemetry(AGENT_NAME)  # on only with OTEL_EXPORTER_OTLP_ENDPOINT
    settings = Settings.from_env()
    if not settings.public_url:
        log.warning(
            "TRIAGE_PUBLIC_URL is not set: the Agent Card says http://%s:%s/",
            settings.listen_host,
            settings.port,
        )
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
