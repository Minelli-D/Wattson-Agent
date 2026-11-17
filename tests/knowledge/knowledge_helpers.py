"""Helpers for the knowledge agent tests: the Grimoire stub (tests/fixtures/grimoire_stub.py), the
app in-process (httpx ASGITransport), and the login as the CLI and the browser would do it."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from wattson.agents.knowledge.app import KnowledgeService, create_app
from wattson.agents.knowledge.settings import Settings

FIXTURES = str(Path(__file__).resolve().parents[1] / "fixtures")
if FIXTURES not in sys.path:
    sys.path.insert(0, FIXTURES)

from grimoire_stub import GrimoireStub  # noqa: E402

TOKEN = "knowledge-test-token-0123456789abcdef"  # noqa: S105 - a test token
REDIRECT = "http://127.0.0.1:47615/callback"


def make_settings(stub: GrimoireStub, data_dir: Path, **changes: object) -> Settings:
    values: dict[str, object] = {
        "token": TOKEN,
        "mcp_url": stub.mcp_url,
        "data_dir": data_dir,
        "timeout_s": 5.0,
        "retrieval_budget_s": 20.0,
        "connect_wait_s": 30.0,
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


class Knowledge:
    """The knowledge app with its service, and an httpx client that speaks to it in-process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = KnowledgeService(settings)
        self.app = create_app(settings, self.service)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://knowledge",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=60,
        )

    async def connect(self, stub: GrimoireStub, redirect_uri: str = REDIRECT) -> dict:
        """POST /connect, the browser at /authorize, the code to /callback: the GrimoireStatus."""
        started = await self.http.post("/v1/grimoire/connect", json={"redirect_uri": redirect_uri})
        assert started.status_code == 200, started.text
        code, state = await browser(started.json()["authorize_url"])
        done = await self.http.post("/v1/grimoire/callback", json={"code": code, "state": state})
        assert done.status_code == 200, done.text
        return done.json()

    async def close(self) -> None:
        await self.http.aclose()
        await self.service.grimoire.close()


async def browser(authorize_url: str) -> tuple[str, str]:
    """What the user's browser does: open the authorization URL, end at the redirect URI."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        answer = await client.get(authorize_url)
    assert answer.status_code == 302, answer.text
    query = parse_qs(urlparse(answer.headers["location"]).query)
    return query["code"][0], query["state"][0]
