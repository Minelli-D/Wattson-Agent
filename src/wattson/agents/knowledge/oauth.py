"""The OAuth side of the Grimoire connection: where the client and its tokens live, and the login.

Grimoire's gateway is an OAuth 2.1 server (Supabase Auth behind it, see grimoire/ARCHITECTURE.md):
protected-resource metadata, authorization-server metadata, dynamic client registration, PKCE.
mcp's OAuthClientProvider does that dance; this module gives it
- a storage: one JSON file in KNOWLEDGE_DATA_DIR (file 600, folder 700), written only here;
- what the provider forgets between two runs: when the access token runs out, and the two
  metadata documents (without them a refresh after a restart would go to the wrong address);
- a login in two halves. POST /v1/grimoire/connect starts it and returns the authorization URL;
  the user opens it in the browser of the laptop (the only enrolled device), the CLI receives the
  one-time code on 127.0.0.1 and posts it to /v1/grimoire/callback. The code is exchanged here,
  with the PKCE verifier that never left this process.

Outside a login the provider must never start a browser flow: its redirect handler raises
LoginRequired, before anything is sent to /authorize.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

log = logging.getLogger(__name__)

TOKEN_FILE = "grimoire-oauth.json"  # noqa: S105 - a file name
CLIENT_NAME = "wattson"
NOT_CONNECTED = "Grimoire is not connected: run wattson grimoire connect"
LOGIN_EXPIRED = "Grimoire login expired: run wattson grimoire connect"
# Refresh this long before the access token runs out (when it lives longer than 2 minutes).
EXPIRY_MARGIN_S = 30

# A login that waits for its code has this redirect URI in the client registration.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LoginRequired(Exception):  # noqa: N818 - a signal, not a failure of this code
    """Grimoire needs a browser login (no tokens, or the refresh was refused)."""


class LoginError(Exception):
    """The login did not work. The message is short and safe to show (no token, no server body)."""


class StateMismatch(LoginError):
    """The state from the laptop is not the state of the waiting login. The login keeps waiting."""


def check_redirect_uri(uri: str) -> str:
    """Only a loopback address of the laptop: the code must not go anywhere else (RFC 8252)."""
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        raise LoginError("redirect_uri must be an http://127.0.0.1:<port>/... address")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise LoginError("redirect_uri must be a loopback address (127.0.0.1 or localhost)")
    if parsed.username or parsed.password or parsed.fragment:
        raise LoginError("redirect_uri must not hold a user, a password or a fragment")
    return uri


def client_metadata(redirect_uri: str) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[redirect_uri],  # type: ignore[list-item]
        token_endpoint_auth_method="none",  # noqa: S106 - a public client: PKCE, no secret
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=CLIENT_NAME,
    )


class OAuthStore:
    """mcp's TokenStorage, plus what the provider forgets. With a path: the JSON file, written at
    once on every change. Without one: memory only, for a login that is not finished yet."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, Any] = self._load()

    # ---- the file --------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("the Grimoire token file cannot be read; starting without a login")
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        if self.path is None:
            return
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(folder, 0o700)
        except OSError:  # a volume root owned by someone else: the file itself is still 600
            log.warning("cannot make the knowledge data folder private (chmod 700)")
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ---- mcp's TokenStorage ----------------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        raw = self.data.get("tokens")
        try:
            return OAuthToken.model_validate(raw) if raw else None
        except ValueError:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        if tokens.expires_in:
            margin = EXPIRY_MARGIN_S if tokens.expires_in > 4 * EXPIRY_MARGIN_S else 0
            self.data["expires_at"] = time.time() + tokens.expires_in - margin
        else:
            self.data.pop("expires_at", None)
        self.data.pop("login_expired", None)
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.data.get("client")
        try:
            return OAuthClientInformationFull.model_validate(raw) if raw else None
        except ValueError:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save()

    # ---- what the provider forgets between two runs ----------------------------------------

    def expires_at(self) -> float | None:
        value = self.data.get("expires_at")
        return float(value) if isinstance(value, int | float) else None

    def set_discovery(
        self,
        metadata: OAuthMetadata | None,
        resource: ProtectedResourceMetadata | None,
        auth_server: str | None,
    ) -> None:
        self.data["discovery"] = {
            "metadata": metadata.model_dump(mode="json", exclude_none=True) if metadata else None,
            "resource": resource.model_dump(mode="json", exclude_none=True) if resource else None,
            "auth_server": auth_server,
        }
        self._save()

    def discovery(self) -> tuple[OAuthMetadata | None, ProtectedResourceMetadata | None, str | None]:
        raw = self.data.get("discovery") or {}
        try:
            metadata = OAuthMetadata.model_validate(raw["metadata"]) if raw.get("metadata") else None
            resource = (
                ProtectedResourceMetadata.model_validate(raw["resource"]) if raw.get("resource") else None
            )
        except ValueError:
            return None, None, None
        server = raw.get("auth_server")
        return metadata, resource, server if isinstance(server, str) else None

    # ---- the state of the login ------------------------------------------------------------

    def has_login(self) -> bool:
        tokens = self.data.get("tokens") or {}
        return bool(tokens.get("access_token")) and bool(self.data.get("client"))

    def has_refresh_token(self) -> bool:
        return bool((self.data.get("tokens") or {}).get("refresh_token"))

    def login_expired(self) -> bool:
        return bool(self.data.get("login_expired"))

    def redirect_uris(self) -> list[str]:
        return [str(u) for u in (self.data.get("client") or {}).get("redirect_uris") or []]

    def mark_login_expired(self) -> None:
        """The tokens are of no use any more: forget them, keep the client registration."""
        self.data.pop("tokens", None)
        self.data.pop("expires_at", None)
        self.data["login_expired"] = True
        self._save()

    def force_refresh(self) -> None:
        """Grimoire refused an access token we took for valid: refresh it at the next request."""
        if self.data.get("tokens"):
            self.data["expires_at"] = 1.0  # long past; not 0, which the provider reads as "no expiry"
            self._save()

    def adopt(self, other: OAuthStore) -> None:
        """A finished login: its client, tokens and metadata replace ours."""
        self.data = {
            k: v for k, v in other.data.items() if k in ("client", "tokens", "expires_at", "discovery")
        }
        self._save()

    def forget(self) -> None:
        self.data = {}
        if self.path is not None:
            self.path.unlink(missing_ok=True)


def token_file(data_dir: Path) -> Path:
    return data_dir / TOKEN_FILE


class GrimoireOAuth(OAuthClientProvider):
    """mcp's provider (an httpx2.Auth), with the expiry and the metadata kept in the OAuthStore."""

    def __init__(
        self,
        server_url: str,
        store: OAuthStore,
        redirect_uri: str,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
    ) -> None:
        super().__init__(server_url, client_metadata(redirect_uri), store, redirect_handler, callback_handler)
        self.store = store
        self.refresh_refused = False

    async def _initialize(self) -> None:
        await super()._initialize()
        # The provider takes a loaded token for valid forever, and knows no token endpoint
        # until it gets a 401: give it both from the file.
        self.context.token_expiry_time = self.store.expires_at()
        metadata, resource, auth_server = self.store.discovery()
        if metadata is not None:
            self.context.oauth_metadata = metadata
        if resource is not None:
            self.context.protected_resource_metadata = resource
        if auth_server is not None:
            self.context.auth_server_url = auth_server

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        await super()._handle_token_response(response)
        self.store.set_discovery(
            self.context.oauth_metadata,
            self.context.protected_resource_metadata,
            self.context.auth_server_url,
        )

    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        ok = await super()._handle_refresh_response(response)
        if not ok:
            self.refresh_refused = True
        return ok


async def _no_browser(_url: str) -> None:
    raise LoginRequired


async def _no_code() -> AuthorizationCodeResult:
    raise LoginRequired


def runtime_provider(server_url: str, store: OAuthStore) -> GrimoireOAuth:
    """The provider for normal calls: refreshes on its own, never starts a browser login."""
    uris = store.redirect_uris()
    return GrimoireOAuth(
        server_url, store, uris[0] if uris else "http://127.0.0.1/callback", _no_browser, _no_code
    )


@dataclass
class PendingLogin:
    """A login between /connect and /callback."""

    redirect_uri: str
    store: OAuthStore  # memory only until the login works
    provider: GrimoireOAuth
    authorize_url: asyncio.Future[str]
    code: asyncio.Future[AuthorizationCodeResult]
    started: float = field(default_factory=time.monotonic)
    task: asyncio.Task[Any] | None = None

    @property
    def state(self) -> str | None:
        if not self.authorize_url.done() or self.authorize_url.cancelled() or self.authorize_url.exception():
            return None
        values = parse_qs(urlparse(self.authorize_url.result()).query).get("state")
        return values[0] if values else None

    def accept(self, code: str, state: str, iss: str | None = None) -> None:
        """The code and state that the laptop received. The state is compared in constant time."""
        expected = self.state
        if expected is None or not hmac.compare_digest(state.encode(), expected.encode()):
            raise StateMismatch("the state does not match the waiting Grimoire login")
        if self.code.done():
            raise StateMismatch("this Grimoire login has already received its code")
        # The MCP client checks iss against the server's issuer itself (RFC 9207).
        self.code.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))

    def cancel(self) -> None:
        for future in (self.authorize_url, self.code):
            if not future.done():
                future.cancel()
        if self.task is not None and not self.task.done():
            self.task.cancel()


def new_login(server_url: str, saved: OAuthStore, redirect_uri: str, wait_s: float) -> PendingLogin:
    """A login that reuses the saved client registration when its redirect URI is the same, and
    registers a new client when it changed."""
    loop = asyncio.get_running_loop()
    staging = OAuthStore(None)
    if redirect_uri in saved.redirect_uris():
        staging.data["client"] = saved.data.get("client")
        if saved.data.get("discovery"):
            staging.data["discovery"] = saved.data["discovery"]
    authorize_url: asyncio.Future[str] = loop.create_future()
    code: asyncio.Future[AuthorizationCodeResult] = loop.create_future()

    async def redirect_handler(url: str) -> None:
        if not authorize_url.done():
            authorize_url.set_result(url)

    async def callback_handler() -> AuthorizationCodeResult:
        try:
            return await asyncio.wait_for(asyncio.shield(code), wait_s)
        except TimeoutError:
            raise LoginError(
                f"no code arrived within {wait_s:g} s: run wattson grimoire connect again"
            ) from None

    provider = GrimoireOAuth(server_url, staging, redirect_uri, redirect_handler, callback_handler)
    return PendingLogin(redirect_uri, staging, provider, authorize_url, code)
