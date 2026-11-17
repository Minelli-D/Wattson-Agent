"""The knowledge agent: a Strands Agent served by Strands' A2AServer (docs/design.md). Two skills:

  {"wattson.find_procedures": KnowledgeRequest}      -> artifact KnowledgeResult (JSON text)
  {"wattson.list_procedures": ProcedureListRequest}  -> artifact ProcedureList (JSON text)

The message carries one of these DataParts. The planner's first tool, route, reads which one and
calls find_procedures or list_procedures. A Grimoire that is not connected, slow or failing gives
an answer with no documents (no procedures) and a warning (NFR-09).

With COGNEE_RECALL=on, find_procedures is followed by seen_before: up to 3 past lessons from
Cognee memory that match the request are added to the documents (kind "cognee", why
"seen_before"; cognee.py, read-only). Cognee off, slow or failing: the answer as without it.

Admin routes (bearer token, like A2A; the orchestrator proxies them for the CLI):
  GET  /healthz                 open, for probes: {"ok": true}
  GET  /v1/grimoire/status      GrimoireStatus (cached 60 s)
  POST /v1/grimoire/connect     GrimoireConnectRequest -> GrimoireConnectView (the authorization URL)
  POST /v1/grimoire/callback    GrimoireCallback -> GrimoireStatus (the code is exchanged here)
  POST /v1/grimoire/disconnect  -> GrimoireStatus (tokens and client registration forgotten)
Errors: {"error": <code>, "message": <short text>} with 400, 409 or 502.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import uvicorn
from a2a.types import AgentSkill
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer
from strands.types.tools import ToolContext

from wattson import __version__
from wattson.shared import telemetry
from wattson.shared.auth import BearerAuth
from wattson.shared.contracts import (
    FIND_PROCEDURES_KEY,
    LIST_PROCEDURES_KEY,
    GrimoireCallback,
    GrimoireConnectRequest,
    GrimoireConnectView,
    KnowledgeRequest,
    KnowledgeResult,
    ProcedureList,
    ProcedureListRequest,
)
from wattson.shared.planner import PlannerModel, parse_structured, step
from wattson.shared.telemetry import setup_telemetry, traced_asgi

from .cognee import SeenBefore
from .grimoire import GrimoireClient
from .oauth import LoginError, StateMismatch, check_redirect_uri
from .procedures import ProcedureCatalog
from .retrieval import Retriever
from .settings import Settings

log = logging.getLogger(__name__)

AGENT_NAME = "wattson-knowledge"
AGENT_DESCRIPTION = (
    "Finds the user's procedures and runbooks in Grimoire (read-only MCP tools, OAuth): search, "
    "read, related documents and context by tags, each with its source; and, when on, past lessons "
    "from Cognee memory (recall only). Never writes."
)
SKILL = AgentSkill(
    id="find_procedures",
    name="find_procedures",
    description=(
        f"DataPart {{{FIND_PROCEDURES_KEY!r}: KnowledgeRequest}} -> artifact KnowledgeResult (JSON text). "
        "With procedure_key: that Wattson procedure and the runbooks it lists, no search. "
        "With COGNEE_RECALL=on, also up to 3 past lessons from Cognee memory (why 'seen_before')."
    ),
    tags=["grimoire", "knowledge", "procedures", "read-only"],
    input_modes=["application/json"],
    output_modes=["application/json"],
)
LIST_SKILL = AgentSkill(
    id="list_procedures",
    name="list_procedures",
    description=(
        f"DataPart {{{LIST_PROCEDURES_KEY!r}: ProcedureListRequest}} -> artifact ProcedureList (JSON "
        "text): the Wattson procedures of a Grimoire folder, for the triage model to pick from."
    ),
    tags=["grimoire", "knowledge", "procedures", "wattson", "read-only"],
    input_modes=["application/json"],
    output_modes=["application/json"],
)
SKILLS = [SKILL, LIST_SKILL]


class KnowledgeService:
    """What every per-context agent shares: the Grimoire connection and the document cache, and
    the way to Cognee memory (None: COGNEE_RECALL=off)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.grimoire = GrimoireClient(settings)
        self.retriever = Retriever(self.grimoire, budget_s=settings.retrieval_budget_s)
        self.procedures = ProcedureCatalog(
            self.grimoire, self.retriever.cache, budget_s=settings.retrieval_budget_s
        )
        self.seen_before = SeenBefore(settings) if settings.cognee_recall else None

    def forget(self) -> None:
        """After a login or a logout: nothing read with the old login is kept."""
        self.retriever.cache.clear()
        self.procedures.clear()


_default_service: KnowledgeService | None = None


RETRIEVE_SPAN = "wattson.knowledge.retrieve"
FOUND = "found"  # agent.state: find_procedures' KnowledgeResult, for seen_before


async def retrieve(retriever: Retriever, request: KnowledgeRequest) -> KnowledgeResult:
    """retriever.find in the wattson.knowledge.retrieve span: only the counts, never a text."""
    with telemetry.span(RETRIEVE_SPAN) as span:
        result = await retriever.find(request)
        telemetry.set_attributes(
            span,
            {"wattson.knowledge.docs": len(result.docs), "wattson.knowledge.warnings": len(result.warnings)},
        )
    return result


def _message_data(message: str) -> dict[str, Any]:
    data = parse_structured(message)
    if not data:  # also accept the plain JSON text, for clients that send a TextPart
        try:
            data = json.loads(message)
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _request_data(message: str, key: str = FIND_PROCEDURES_KEY) -> Any:
    return _message_data(message).get(key)


def _not_asked(reason: str, answer: type[KnowledgeResult | ProcedureList] = KnowledgeResult) -> str:
    return step(output=answer(warnings=[f"Grimoire was not asked: {reason}"]).model_dump(mode="json"))


def _invalid(exc: ValidationError) -> str:
    return ", ".join(".".join(str(p) for p in e["loc"]) or "(root)" for e in exc.errors()[:5])


@tool(name="route")
def route(message: str) -> str:
    """Send the message to the skill its DataPart asks for: find_procedures or list_procedures.

    Args:
        message: the request message; holds a DataPart with a KnowledgeRequest or a ProcedureListRequest.
    """
    data = _message_data(message)
    for key, skill in ((FIND_PROCEDURES_KEY, "find_procedures"), (LIST_PROCEDURES_KEY, "list_procedures")):
        if key in data:
            return step(next=skill, input={"message": message})
    return _not_asked(f"no {FIND_PROCEDURES_KEY} or {LIST_PROCEDURES_KEY} DataPart in the message")


def make_find_procedures(service: KnowledgeService) -> Any:
    @tool(name="find_procedures", context=True)
    async def find_procedures(message: str, tool_context: ToolContext) -> str:
        """Find the Grimoire documents relevant to a request (read-only).

        Args:
            message: the request message; holds a DataPart with the KnowledgeRequest.
        """
        data = _request_data(message)
        if data is None:
            return _not_asked(f"no {FIND_PROCEDURES_KEY} DataPart in the message")
        try:
            request = KnowledgeRequest.model_validate(data)
        except ValidationError as exc:
            return _not_asked(f"the KnowledgeRequest is not valid: {_invalid(exc)}")
        result = await retrieve(service.retriever, request)
        log.info("find_procedures: %d document(s), %d warning(s)", len(result.docs), len(result.warnings))
        if service.seen_before is None:
            return step(output=result.model_dump(mode="json"))
        tool_context.agent.state.set(FOUND, result.model_dump(mode="json"))
        return step(next="seen_before", input={"message": message})

    return find_procedures


def make_seen_before(service: KnowledgeService) -> Any:
    @tool(name="seen_before", context=True)
    async def seen_before(message: str, tool_context: ToolContext) -> str:
        """Add the past lessons of Cognee memory that match the request to find_procedures'
        answer (read-only: recall only). Cognee slow or failing: the answer as it was.

        Args:
            message: the request message; holds a DataPart with the KnowledgeRequest.
        """
        state = tool_context.agent.state
        result = KnowledgeResult.model_validate(state.get(FOUND) or {})
        state.delete(FOUND)
        try:
            request = KnowledgeRequest.model_validate(_request_data(message))
        except ValidationError:  # find_procedures checked it already
            return step(output=result.model_dump(mode="json"))
        lessons = await service.seen_before.find(request) if service.seen_before is not None else []
        if lessons:
            result = result.model_copy(update={"docs": [*result.docs, *lessons]})
        return step(output=result.model_dump(mode="json"))

    return seen_before


def make_list_procedures(service: KnowledgeService) -> Any:
    @tool(name="list_procedures")
    async def list_procedures(message: str) -> str:
        """List the Wattson procedures of a Grimoire folder (read-only).

        Args:
            message: the request message; holds a DataPart with the ProcedureListRequest.
        """
        data = _message_data(message)
        if LIST_PROCEDURES_KEY not in data:
            return _not_asked(f"no {LIST_PROCEDURES_KEY} DataPart in the message", ProcedureList)
        try:
            request = ProcedureListRequest.model_validate(data[LIST_PROCEDURES_KEY] or {})
        except ValidationError as exc:
            return _not_asked(f"the ProcedureListRequest is not valid: {_invalid(exc)}", ProcedureList)
        result = await service.procedures.list(request)
        log.info(
            "list_procedures: %d procedure(s), %d warning(s)", len(result.procedures), len(result.warnings)
        )
        return step(output=result.model_dump(mode="json"))

    return list_procedures


def build_agent(context_id: str, service: KnowledgeService | None = None) -> Agent:
    """One agent per A2A context (A2AServer's agent_factory). Its planner calls route, then the
    skill's tool once (and seen_before after find_procedures, with COGNEE_RECALL=on)."""
    global _default_service
    if service is None:
        if _default_service is None:
            _default_service = KnowledgeService(Settings.from_env())
        service = _default_service
    return Agent(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        model=PlannerModel("route"),
        tools=[
            route,
            make_find_procedures(service),
            make_seen_before(service),
            make_list_procedures(service),
        ],
        callback_handler=None,
    )


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _body[M: BaseModel](request: Request, model: type[M]) -> M | JSONResponse:
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _error(400, "bad_request", f"the body must be a {model.__name__}")


def create_app(settings: Settings | None = None, service: KnowledgeService | None = None) -> Any:
    """The ASGI app: A2A (JSON-RPC at "/", Agent Card) plus the admin routes, behind BearerAuth."""
    settings = settings or Settings.from_env()
    service = service or KnowledgeService(settings)
    grimoire = service.grimoire
    server = A2AServer(
        agent_factory=lambda context_id: build_agent(context_id, service),
        host=settings.listen_host,
        port=settings.port,
        http_url=settings.public_url,
        version=__version__,
        skills=SKILLS,
        enable_a2a_compliant_streaming=True,
    )
    app = server.to_starlette_app()

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def status(_: Request) -> JSONResponse:
        return JSONResponse((await grimoire.status()).model_dump(mode="json"))

    async def connect(request: Request) -> JSONResponse:
        body = await _body(request, GrimoireConnectRequest)
        if isinstance(body, JSONResponse):
            return body
        try:
            check_redirect_uri(body.redirect_uri)
        except LoginError as e:
            return _error(400, "bad_redirect_uri", str(e))
        try:
            url = await grimoire.start_login(body.redirect_uri)
        except LoginError as e:
            return _error(502, "grimoire_login", str(e))
        return JSONResponse(GrimoireConnectView(authorize_url=url).model_dump(mode="json"))

    async def callback(request: Request) -> JSONResponse:
        body = await _body(request, GrimoireCallback)
        if isinstance(body, JSONResponse):
            return body
        if grimoire.waiting_login() is None:
            return _error(
                409, "no_login", "no Grimoire login is waiting for a code: run wattson grimoire connect"
            )
        try:
            result = await grimoire.finish_login(body.code, body.state, body.iss)
        except StateMismatch as e:
            return _error(400, "state_mismatch", str(e))
        except LoginError as e:
            return _error(502, "grimoire_login", str(e))
        service.forget()
        return JSONResponse(result.model_dump(mode="json"))

    async def disconnect(_: Request) -> JSONResponse:
        result = await grimoire.disconnect()
        service.forget()
        return JSONResponse(result.model_dump(mode="json"))

    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_route("/v1/grimoire/status", status, methods=["GET"])
    app.add_route("/v1/grimoire/connect", connect, methods=["POST"])
    app.add_route("/v1/grimoire/callback", callback, methods=["POST"])
    app.add_route("/v1/grimoire/disconnect", disconnect, methods=["POST"])
    # Outermost: the trace context of the orchestrator's call is read first (when traces are on).
    return traced_asgi(BearerAuth(app, settings.token, open_paths=("/healthz",)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx2 logs every request URL at INFO; the bodies and tokens are never logged, but keep it quiet.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    setup_telemetry(AGENT_NAME)  # on only with OTEL_EXPORTER_OTLP_ENDPOINT
    settings = Settings.from_env()
    if not settings.public_url:
        log.warning(
            "KNOWLEDGE_PUBLIC_URL is not set: the Agent Card says http://%s:%s/",
            settings.listen_host,
            settings.port,
        )
    log.info("knowledge agent: Grimoire at %s, data in %s", settings.mcp_url, settings.data_dir)
    if settings.cognee_recall:
        log.info(
            "seen before: Cognee at %s, dataset %s, at most %d lesson(s), %g s",
            settings.cognee_url,
            settings.cognee_dataset,
            settings.cognee_lessons,
            settings.cognee_timeout_s,
        )
    else:
        log.info("seen before: off (COGNEE_RECALL)")
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
