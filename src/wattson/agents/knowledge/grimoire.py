"""The one place that talks to Grimoire over MCP (streamable HTTP, OAuth).

READ-ONLY: only the tools in ALLOWED_TOOLS are ever called. check_allowed() runs before anything is
sent, in Session.call, the only function here that calls a tool. Tools are called by this code, in
a fixed order; no model picks them. (Strands' MCPClient has tool_filters, but its call_tool does not
check them, and its background thread does not fit a login that waits minutes for a code: so this
module uses the mcp client session directly.)

Errors become GrimoireError, whose reason is short and safe to show: never a token, never a body
from the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
from mcp.client import Client
from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import REQUEST_TIMEOUT

from wattson.shared import telemetry
from wattson.shared.contracts import GrimoireStatus
from wattson.shared.redact import redact

from .oauth import (
    LOGIN_EXPIRED,
    NOT_CONNECTED,
    GrimoireOAuth,
    LoginError,
    LoginRequired,
    OAuthStore,
    PendingLogin,
    check_redirect_uri,
    new_login,
    runtime_provider,
    token_file,
)
from .settings import Settings

log = logging.getLogger(__name__)
# mcp logs every failed OAuth flow with a traceback; a refused refresh is a normal event here and
# is reported by this module, in its own words.
logging.getLogger("mcp.client.auth.oauth2").setLevel(logging.CRITICAL)

# The read-only tools of Grimoire (grimoire/mcp/server.ts). write_document, delete_document and
# suggest_tags are never called.
ALLOWED_TOOLS: tuple[str, ...] = (
    "search_documents",
    "get_context",
    "read_document",
    "get_related_documents",
    "list_tags",
    "list_folders",
)
STATUS_TTL_S = 60.0
MAX_TOOL_PAGES = 5
GRIMOIRE_SPAN = "wattson.grimoire.call"  # one per MCP call (list_tools too)


class ToolRefused(Exception):  # noqa: N818 - a refusal, by design
    """A tool outside the read-only allow-list was asked for. Nothing was sent."""


class GrimoireError(Exception):
    """Grimoire could not be used. reason is short and safe to show."""

    def __init__(self, reason: str, kind: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = (
            kind  # not-connected, login-expired, login, timeout, network, refused, auth, protocol, error
        )


def check_allowed(name: str) -> None:
    if name not in ALLOWED_TOOLS:
        raise ToolRefused(f"Grimoire tool {name!r} is not in the read-only allow-list: it is never called")


@dataclass(frozen=True)
class ToolAnswer:
    text: str
    is_error: bool


class Session:
    """One open MCP connection. call() is the only way to a tool."""

    def __init__(self, client: Client, timeout_s: float, memo: dict[tuple[str, str], ToolAnswer]) -> None:
        self._client = client
        self.timeout_s = timeout_s
        self._memo = memo  # answers already received in this piece of work (kept over a retry)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolAnswer:
        check_allowed(name)
        key = (name, json.dumps(arguments, sort_keys=True))
        if key in self._memo:
            return self._memo[key]
        # The span has the tool name and ok: never the arguments (a query) or the answer.
        attributes = {"wattson.grimoire.tool": name, "wattson.grimoire.ok": False}
        with telemetry.span(GRIMOIRE_SPAN, **attributes) as s, anyio.fail_after(self.timeout_s):
            result = await self._client.call_tool(name, arguments, read_timeout_seconds=self.timeout_s)
            telemetry.set_attributes(s, {"wattson.grimoire.ok": not result.is_error})
        text = "\n".join(block.text for block in result.content if getattr(block, "type", None) == "text")
        answer = ToolAnswer(text, bool(result.is_error))
        self._memo[key] = answer
        return answer

    async def tool_names(self) -> list[str]:
        names: list[str] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            attributes = {"wattson.grimoire.tool": "list_tools", "wattson.grimoire.ok": False}
            with telemetry.span(GRIMOIRE_SPAN, **attributes) as s, anyio.fail_after(self.timeout_s):
                page = await self._client.list_tools(cursor=cursor)
                telemetry.set_attributes(s, {"wattson.grimoire.ok": True})
            names += [tool.name for tool in page.tools]
            cursor = page.next_cursor
            if not cursor:
                break
        return names


def _settle(task: asyncio.Task[Any]) -> None:
    """A login nobody waits for any more (no code came): read its error, so it is not reported."""
    if not task.cancelled() and task.exception() is not None:
        log.info("grimoire: a login ended without a connection")


def allowed_offered(names: list[str]) -> list[str]:
    offered = set(names)
    return [name for name in ALLOWED_TOOLS if name in offered]


# ---- errors ----------------------------------------------------------------------------------

_OPAQUE = re.compile(r"[A-Za-z0-9._~+/=-]{24,}")
_HTTP_STATUS = re.compile(r"\((\d{3})\)")


def safe_text(text: str, limit: int = 160) -> str:
    """Text from a server or a library, made safe to show: secrets and long opaque strings out."""
    cleaned = _OPAQUE.sub("…", redact(text).text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _leaf(exc: BaseException) -> BaseException:
    leaves = _leaves(exc)
    for wanted in (ToolRefused, LoginRequired, LoginError, GrimoireError):
        for leaf in leaves:
            if isinstance(leaf, wanted):
                return leaf
    for leaf in leaves:
        if not isinstance(leaf, asyncio.CancelledError):
            return leaf
    return leaves[0]


def _is_network(leaf: BaseException) -> bool:
    if isinstance(leaf, httpx2.TimeoutException):
        return False
    return isinstance(leaf, httpx2.TransportError | ConnectionError)


def classify(exc: BaseException, timeout_s: float) -> GrimoireError:
    leaf = _leaf(exc)
    if isinstance(leaf, GrimoireError):
        return leaf
    if isinstance(leaf, ToolRefused):
        return GrimoireError(str(leaf), "refused")
    if isinstance(leaf, LoginRequired):
        return GrimoireError(LOGIN_EXPIRED, "login-expired")
    if isinstance(leaf, LoginError):
        return GrimoireError(str(leaf), "login")
    if isinstance(leaf, TimeoutError | httpx2.TimeoutException) or (
        isinstance(leaf, MCPError) and leaf.code == REQUEST_TIMEOUT
    ):
        return GrimoireError(f"Grimoire did not answer within {timeout_s:g} s", "timeout")
    if _is_network(leaf) or isinstance(leaf, OSError):
        return GrimoireError(f"Grimoire cannot be reached ({type(leaf).__name__})", "network")
    if isinstance(leaf, OAuthTokenError):
        found = _HTTP_STATUS.search(str(leaf))
        status = f"HTTP {found.group(1)}" if found else "no token"
        return GrimoireError(f"Grimoire's login server refused the code ({status})", "auth")
    if isinstance(leaf, OAuthRegistrationError | OAuthFlowError):
        return GrimoireError(f"Grimoire login failed ({type(leaf).__name__})", "auth")
    if isinstance(leaf, MCPError):
        return GrimoireError(
            f"Grimoire answered an MCP error {leaf.code}: {safe_text(leaf.error.message, 80)}", "protocol"
        )
    return GrimoireError(f"Grimoire call failed ({type(leaf).__name__})", "error")


# ---- the client --------------------------------------------------------------------------------


class GrimoireClient:
    """The Grimoire connection of this agent: its login, its status, and sessions for retrieval."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = OAuthStore(token_file(settings.data_dir))
        self._runtime: GrimoireOAuth | None = None
        self._login: PendingLogin | None = None
        self._status: tuple[float, GrimoireStatus] | None = None

    @property
    def mcp_url(self) -> str:
        return self.settings.mcp_url

    @asynccontextmanager
    async def _open(self, auth: httpx2.Auth, read_timeout_s: float | None) -> AsyncIterator[Client]:
        timeout = httpx2.Timeout(self.settings.timeout_s)
        async with httpx2.AsyncClient(auth=auth, timeout=timeout, follow_redirects=False) as http:
            transport = streamable_http_client(self.settings.mcp_url, http_client=http)
            async with Client(transport, read_timeout_seconds=read_timeout_s, cache=None) as client:
                yield client

    def _runtime_provider(self) -> GrimoireOAuth:
        if self._runtime is None:
            self._runtime = runtime_provider(self.settings.mcp_url, self.store)
        return self._runtime

    def _forget_runtime(self) -> None:
        self._runtime = None
        self._status = None

    async def run[T](self, work: Callable[[Session], Awaitable[T]]) -> T:
        """work in one MCP session with the saved login. Retries once after a network error, and
        once with a refresh when Grimoire refused an access token that looked valid.
        Raises GrimoireError; never asks for a browser login."""
        if not self.store.has_login():
            if self.store.login_expired():
                raise GrimoireError(LOGIN_EXPIRED, "login-expired")
            raise GrimoireError(NOT_CONNECTED, "not-connected")
        memo: dict[tuple[str, str], ToolAnswer] = {}
        network_retry, refresh_retry = True, True
        while True:
            provider = self._runtime_provider()
            try:
                async with self._open(provider, self.settings.timeout_s) as client:
                    return await work(Session(client, self.settings.timeout_s, memo))
            except Exception as exc:  # an ExceptionGroup from the transport, or the error itself
                leaf = _leaf(exc)
                if isinstance(leaf, LoginRequired):
                    self._forget_runtime()
                    if refresh_retry and not provider.refresh_refused and self.store.has_refresh_token():
                        refresh_retry = False
                        self.store.force_refresh()
                        continue
                    self.store.mark_login_expired()
                    log.warning("grimoire: the login expired (refresh refused or no refresh token)")
                    raise GrimoireError(LOGIN_EXPIRED, "login-expired") from None
                if network_retry and _is_network(leaf):
                    network_retry = False
                    log.info("grimoire: network error (%s), one retry", type(leaf).__name__)
                    continue
                error = classify(leaf, self.settings.timeout_s)
                if error.kind != "refused":
                    log.warning("grimoire: %s", error.reason)
                raise error from None

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolAnswer:
        """One tool call in its own session. A tool outside the allow-list sends nothing at all."""
        try:
            check_allowed(name)
        except ToolRefused as e:
            raise GrimoireError(str(e), "refused") from None
        return await self.run(lambda session: session.call(name, arguments))

    # ---- status -----------------------------------------------------------------------------

    async def status(self, fresh: bool = False) -> GrimoireStatus:
        """connected = a saved login and list_tools works. Cached for STATUS_TTL_S."""
        now = time.monotonic()
        if not fresh and self._status is not None and now - self._status[0] < STATUS_TTL_S:
            return self._status[1]
        if not self.store.has_login():
            reason = LOGIN_EXPIRED if self.store.login_expired() else NOT_CONNECTED
            status = GrimoireStatus(connected=False, mcp_url=self.mcp_url, last_error=reason)
        else:
            try:
                names = await self.run(lambda session: session.tool_names())
                status = GrimoireStatus(connected=True, mcp_url=self.mcp_url, tools=allowed_offered(names))
            except GrimoireError as e:
                status = GrimoireStatus(connected=False, mcp_url=self.mcp_url, last_error=e.reason)
        self._status = (time.monotonic(), status)
        return status

    def forget_status(self) -> None:
        self._status = None

    # ---- login --------------------------------------------------------------------------------

    async def start_login(self, redirect_uri: str) -> str:
        """Start a login; returns the authorization URL for the browser. Raises LoginError."""
        check_redirect_uri(redirect_uri)
        if self._login is not None:
            self._login.cancel()
            self._login = None
        login = new_login(self.settings.mcp_url, self.store, redirect_uri, self.settings.connect_wait_s)
        login.task = asyncio.create_task(self._login_task(login), name="grimoire-login")
        login.task.add_done_callback(_settle)
        wait_s = self.settings.timeout_s * 2
        done, _ = await asyncio.wait(
            {login.authorize_url, login.task}, timeout=wait_s, return_when=asyncio.FIRST_COMPLETED
        )
        if login.authorize_url.done() and not login.authorize_url.cancelled():
            self._login = login
            log.info("grimoire: login started, waiting for the code")
            return login.authorize_url.result()
        login.cancel()
        if login.task in done and not login.task.cancelled() and login.task.exception() is not None:
            raise LoginError(classify(login.task.exception() or Exception(), self.settings.timeout_s).reason)
        if login.task in done:
            raise LoginError("Grimoire did not ask for a login: check GRIMOIRE_MCP_URL")
        raise LoginError(f"Grimoire did not start the login within {wait_s:g} s")

    async def _login_task(self, login: PendingLogin) -> list[str]:
        # No read timeout on this session: its first request waits until the code arrives (the
        # callback handler has its own limit). The calls after it have the usual one.
        async with self._open(login.provider, None) as client:
            return await Session(client, self.settings.timeout_s, {}).tool_names()

    def waiting_login(self) -> PendingLogin | None:
        """The login that waits for its code, if any."""
        login = self._login
        if login is None or login.task is None or login.task.done():
            return None
        return login

    async def finish_login(self, code: str, state: str, iss: str | None = None) -> GrimoireStatus:
        """The code from the laptop: exchange it here (with the PKCE verifier), then check the tools."""
        login = self.waiting_login()
        if login is None:
            raise LoginError("no Grimoire login is waiting for a code: run wattson grimoire connect")
        login.accept(code, state, iss)
        wait_s = self.settings.timeout_s * 3
        try:
            names = await asyncio.wait_for(asyncio.shield(login.task), timeout=wait_s)
        except TimeoutError:
            login.cancel()
            raise LoginError(f"Grimoire did not finish the login within {wait_s:g} s") from None
        except Exception as exc:
            raise LoginError(classify(exc, self.settings.timeout_s).reason) from None
        finally:
            if self._login is login and (login.task.done() or login.task.cancelled()):
                self._login = None
        self.store.adopt(login.store)
        self._forget_runtime()
        status = GrimoireStatus(connected=True, mcp_url=self.mcp_url, tools=allowed_offered(names))
        self._status = (time.monotonic(), status)
        log.info("grimoire: connected, %d read-only tools", len(status.tools))
        return status

    async def disconnect(self) -> GrimoireStatus:
        if self._login is not None:
            self._login.cancel()
            self._login = None
        self.store.forget()
        self._forget_runtime()
        status = GrimoireStatus(connected=False, mcp_url=self.mcp_url, last_error=NOT_CONNECTED)
        self._status = (time.monotonic(), status)
        return status

    async def close(self) -> None:
        if self._login is not None:
            self._login.cancel()
            self._login = None
