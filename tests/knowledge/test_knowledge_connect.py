"""The Grimoire login through the admin routes, against the stub: registration, PKCE, the code
from the laptop, the token file, status, disconnect, and the bearer token on every route."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from grimoire_stub import GrimoireStub
from knowledge_helpers import REDIRECT, TOKEN, Knowledge, browser, make_settings

from wattson.agents.knowledge.grimoire import ALLOWED_TOOLS
from wattson.agents.knowledge.oauth import NOT_CONNECTED, TOKEN_FILE
from wattson.agents.knowledge.settings import Settings
from wattson.shared.contracts import GrimoireConnectView, GrimoireStatus


async def test_full_connect_flow(knowledge: Knowledge, stub: GrimoireStub, data_dir: Path) -> None:
    started = await knowledge.http.post("/v1/grimoire/connect", json={"redirect_uri": REDIRECT})
    assert started.status_code == 200
    view = GrimoireConnectView.model_validate(started.json())
    url = urlparse(view.authorize_url)
    query = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{stub.issuer}/oauth/authorize"
    assert query["redirect_uri"] == [REDIRECT]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert len(query["state"][0]) >= 32 and len(query["code_challenge"][0]) >= 43
    assert "code_verifier" not in query  # the verifier never leaves the knowledge agent

    registration = next(iter(stub.clients.values()))
    assert registration["client_name"] == "wattson"
    assert registration["redirect_uris"] == [REDIRECT]
    assert registration["token_endpoint_auth_method"] == "none"  # noqa: S105
    assert set(registration["grant_types"]) == {"authorization_code", "refresh_token"}

    code, state = await browser(view.authorize_url)
    done = await knowledge.http.post("/v1/grimoire/callback", json={"code": code, "state": state})
    assert done.status_code == 200, done.text
    status = GrimoireStatus.model_validate(done.json())
    assert status.connected is True
    assert status.mcp_url == stub.mcp_url
    assert status.tools == list(ALLOWED_TOOLS)  # write_document, delete_document, suggest_tags left out
    assert status.last_error is None
    assert "POST /auth/v1/oauth/token" in stub.calls

    token_file = data_dir / TOKEN_FILE
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    saved = json.loads(token_file.read_text())
    assert saved["tokens"]["access_token"] in stub.access_tokens
    assert saved["client"]["client_id"] in stub.clients


async def test_status_is_cached_and_survives_a_restart(connected: Knowledge, stub: GrimoireStub) -> None:
    first = (await connected.http.get("/v1/grimoire/status")).json()
    assert first["connected"] is True
    await connected.http.get("/v1/grimoire/status")
    assert stub.calls.count("tools/list") == 0  # the callback's own check is reused for 60 s

    again = Knowledge(make_settings(stub, connected.settings.data_dir))  # a new pod, same volume
    try:
        status = (await again.http.get("/v1/grimoire/status")).json()
        assert status["connected"] is True
        assert status["tools"] == list(ALLOWED_TOOLS)
        assert "tools/list" in stub.calls
    finally:
        await again.close()


async def test_status_before_any_login(knowledge: Knowledge, stub: GrimoireStub) -> None:
    status = GrimoireStatus.model_validate((await knowledge.http.get("/v1/grimoire/status")).json())
    assert status.connected is False
    assert status.last_error == NOT_CONNECTED
    assert stub.calls == []  # nothing was sent to Grimoire


async def test_state_must_match(knowledge: Knowledge, stub: GrimoireStub) -> None:
    started = await knowledge.http.post("/v1/grimoire/connect", json={"redirect_uri": REDIRECT})
    code, state = await browser(started.json()["authorize_url"])
    wrong = await knowledge.http.post("/v1/grimoire/callback", json={"code": code, "state": state + "x"})
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "state_mismatch"
    assert "POST /auth/v1/oauth/token" not in stub.calls  # no exchange with a wrong state
    right = await knowledge.http.post("/v1/grimoire/callback", json={"code": code, "state": state})
    assert right.status_code == 200
    assert right.json()["connected"] is True


async def test_callback_without_a_login(knowledge: Knowledge) -> None:
    answer = await knowledge.http.post("/v1/grimoire/callback", json={"code": "c", "state": "s"})
    assert answer.status_code == 409
    assert answer.json()["error"] == "no_login"


async def test_bad_bodies_and_redirect_uris(knowledge: Knowledge, stub: GrimoireStub) -> None:
    for uri in ("https://evil.example/callback", "http://10.0.0.5:47615/callback", "ftp://127.0.0.1/x"):
        answer = await knowledge.http.post("/v1/grimoire/connect", json={"redirect_uri": uri})
        assert answer.status_code == 400, uri
        assert answer.json()["error"] == "bad_redirect_uri"
    answer = await knowledge.http.post("/v1/grimoire/connect", json={"redirect": REDIRECT})
    assert answer.status_code == 400
    assert answer.json()["error"] == "bad_request"
    assert stub.calls == []


async def test_reconnect_reuses_or_renews_the_client(connected: Knowledge, stub: GrimoireStub) -> None:
    assert len(stub.clients) == 1
    await connected.connect(stub)  # same redirect URI: the same registration
    assert len(stub.clients) == 1
    other = "http://127.0.0.1:50123/callback"
    status = await connected.connect(stub, redirect_uri=other)  # changed: a new registration
    assert status["connected"] is True
    assert len(stub.clients) == 2
    saved = json.loads((connected.settings.data_dir / TOKEN_FILE).read_text())
    assert saved["client"]["redirect_uris"] == [other]


async def test_disconnect_forgets_everything(connected: Knowledge, stub: GrimoireStub) -> None:
    answer = await connected.http.post("/v1/grimoire/disconnect")
    assert answer.status_code == 200
    assert answer.json()["connected"] is False
    assert not (connected.settings.data_dir / TOKEN_FILE).exists()
    status = (await connected.http.get("/v1/grimoire/status")).json()
    assert status == {"connected": False, "mcp_url": stub.mcp_url, "tools": [], "last_error": NOT_CONNECTED}


async def test_bearer_token_on_every_route_but_healthz(knowledge: Knowledge) -> None:
    async with knowledge.http as client:
        client.headers.pop("Authorization")
        assert (await client.get("/healthz")).json() == {"ok": True}
        for method, path in [
            ("GET", "/v1/grimoire/status"),
            ("POST", "/v1/grimoire/connect"),
            ("POST", "/v1/grimoire/callback"),
            ("POST", "/v1/grimoire/disconnect"),
            ("GET", "/.well-known/agent-card.json"),
            ("POST", "/"),
        ]:
            answer = await client.request(method, path, json={})
            assert answer.status_code == 401, path
        wrong = await client.get("/v1/grimoire/status", headers={"Authorization": f"Bearer {TOKEN}x"})
        assert wrong.status_code == 401


async def test_a_login_without_a_code_ends(stub: GrimoireStub, data_dir: Path) -> None:
    k = Knowledge(make_settings(stub, data_dir, connect_wait_s=0.3))
    try:
        started = await k.http.post("/v1/grimoire/connect", json={"redirect_uri": REDIRECT})
        code, state = await browser(started.json()["authorize_url"])
        await asyncio.sleep(0.6)  # the user took too long
        late = await k.http.post("/v1/grimoire/callback", json={"code": code, "state": state})
        assert late.status_code == 409
        status = (await k.http.get("/v1/grimoire/status")).json()
        assert status["connected"] is False
        assert "POST /auth/v1/oauth/token" not in stub.calls
    finally:
        await k.close()


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("KNOWLEDGE_LISTEN_HOST", "KNOWLEDGE_PORT", "GRIMOIRE_MCP_URL", "KNOWLEDGE_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KNOWLEDGE_TOKEN", TOKEN)
    defaults = Settings.from_env()
    assert (defaults.listen_host, defaults.port) == ("0.0.0.0", 9002)  # noqa: S104
    assert defaults.mcp_url == "https://grimoire.example.com/mcp"
    assert defaults.data_dir == Path("./.data/knowledge") and defaults.timeout_s == 15.0
    assert TOKEN not in repr(defaults)
    monkeypatch.setenv("GRIMOIRE_TIMEOUT_S", "2.5")
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KNOWLEDGE_PUBLIC_URL", "http://wattson-knowledge:9002/")
    custom = Settings.from_env()
    assert (custom.timeout_s, custom.data_dir, custom.public_url) == (
        2.5,
        tmp_path,
        "http://wattson-knowledge:9002/",
    )
    monkeypatch.setenv("KNOWLEDGE_TOKEN", "short")
    with pytest.raises(SystemExit):
        Settings.from_env()
