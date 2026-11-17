"""Bearer tokens between the parts: CLI -> orchestrator, orchestrator -> triage and knowledge.

The network rules are the first lock (port-forward only for the orchestrator, NetworkPolicies
between the agents); the token is the second. Compared in constant time; never logged."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

MIN_TOKEN_LENGTH = 32

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def read_token(env: str, file_env: str | None = None) -> str:
    """The token from $env, or from the file named by $file_env. Refuses short tokens."""
    token = os.environ.get(env, "")
    if not token and file_env and os.environ.get(file_env):
        token = Path(os.environ[file_env]).read_text(encoding="utf-8").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"{env} is missing or shorter than {MIN_TOKEN_LENGTH} characters")
    return token


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


class BearerAuth:
    """ASGI middleware: every request needs "Authorization: Bearer <token>", except open_paths."""

    def __init__(
        self, app: Callable[..., Awaitable[None]], token: str, open_paths: Iterable[str] = ("/healthz",)
    ):
        self.app = app
        self.expected = _digest(f"Bearer {token}".encode())
        self.open_paths = frozenset(open_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        sent = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                sent = value
                break
        if not hmac.compare_digest(_digest(sent), self.expected):
            body = json.dumps(
                {"error": "unauthorized", "message": "a valid bearer token is required"}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def bearer_client(token: str, timeout: float = 120.0, base_url: str = "") -> httpx.AsyncClient:
    """An httpx client that sends the token: for A2AAgent(client_config=ClientConfig(httpx_client=...))
    and for the admin routes."""
    return httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
