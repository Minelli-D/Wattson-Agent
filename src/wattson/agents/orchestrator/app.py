"""The orchestrator process: A2A and the admin routes behind the bearer token, one access-log line
per request, and the history jobs (sweep at start and daily, expire open tasks every minute)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount

from wattson.agents.orchestrator.admin import admin_routes
from wattson.agents.orchestrator.agent import NAME, build_server, package_version
from wattson.agents.orchestrator.deps import Deps
from wattson.agents.orchestrator.flow import now_iso
from wattson.agents.orchestrator.remote import Remotes
from wattson.agents.orchestrator.settings import Settings
from wattson.agents.orchestrator.store import Store
from wattson.agents.orchestrator.tools import RemoteAgents
from wattson.shared.auth import BearerAuth
from wattson.shared.telemetry import server_middleware, setup_telemetry

OPEN_PATHS = frozenset({"/healthz"})
SWEEP_EVERY_S = 24 * 60 * 60
EXPIRE_EVERY_S = 60

access_log = logging.getLogger("wattson.orchestrator.access")
log = logging.getLogger("wattson.orchestrator")

ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]
]


class AccessLog:
    """One JSON line per request: time, method, path, status, duration. No bodies, headers or query
    strings (they can hold codes and tokens)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_and_note(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_and_note)
        finally:
            line = {
                "t": now_iso(),
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": status,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            }
            access_log.info(json.dumps(line))


async def _every(seconds: float, job: Callable[[], Any], name: str) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            job()
        except Exception:  # noqa: BLE001 - a failed run must not stop the next one
            log.exception(json.dumps({"t": now_iso(), "event": f"{name}_failed"}))


def create_app(
    settings: Settings,
    *,
    remotes: RemoteAgents | None = None,
    store: Store | None = None,
    background: bool = True,
) -> Starlette:
    """The whole HTTP app. `remotes` and `store` are for tests; `background` runs the history jobs."""
    own_store = store is None
    deps = Deps(
        settings=settings,
        store=store or Store(settings.data_dir),
        remotes=remotes
        or Remotes(
            triage_url=settings.triage_url,
            triage_token=settings.triage_token,
            knowledge_url=settings.knowledge_url,
            knowledge_token=settings.knowledge_token,
            triage_timeout_s=settings.triage_timeout_s,
            knowledge_timeout_s=settings.knowledge_timeout_s,
        ),
    )
    a2a_app = build_server(deps).to_starlette_app()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        jobs: list[asyncio.Task[None]] = []
        if background:
            swept, expired = deps.sweep(), deps.expire()
            log.info(
                json.dumps({"t": now_iso(), "event": "history", "swept": swept, "expired": len(expired)})
            )
            jobs = [
                asyncio.create_task(_every(SWEEP_EVERY_S, deps.sweep, "sweep")),
                asyncio.create_task(_every(EXPIRE_EVERY_S, deps.expire, "expire")),
            ]
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            if own_store:
                deps.store.close()

    app = Starlette(
        routes=[*admin_routes(deps), Mount("/", app=a2a_app)],
        middleware=[
            *server_middleware(),  # traces (when on): reads the CLI's traceparent; not /healthz
            Middleware(AccessLog),  # 401s are logged too
            Middleware(BearerAuth, token=settings.token, open_paths=OPEN_PATHS),
        ],
        lifespan=lifespan,
    )
    app.state.deps = deps
    return app


class _Server(uvicorn.Server):
    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            settings: Settings = self.config.app.state.deps.settings
            ready = {
                "t": now_iso(),
                "event": "ready",
                "agent": NAME,
                "version": package_version(),
                "listen": f"http://{settings.listen_host}:{settings.port}",
            }
            print(json.dumps(ready), flush=True)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)
    logging.getLogger("wattson").setLevel(logging.INFO)


def main() -> None:
    _setup_logging()
    setup_telemetry(NAME)  # on only with OTEL_EXPORTER_OTLP_ENDPOINT
    settings = Settings.from_env()
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.listen_host,
        port=settings.port,
        log_level="warning",
        access_log=False,  # our own line has no query string
        server_header=False,
        proxy_headers=False,
    )
    _Server(config).run()


if __name__ == "__main__":
    main()
