"""find_procedures against the stub: documents with their sources, related and context documents,
the cache, the read-only allow-list, refresh, a refused refresh, timeouts, TOON and JSON, redaction,
and no token in any text that leaves the agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from grimoire_stub import (
    FAKE_AWS_KEY,
    FAKE_SECRET,
    GATEWAY_MCP_VERSIONS,
    Doc,
    GrimoireStub,
    start_grimoire_stub,
)
from knowledge_helpers import Knowledge, make_settings

from wattson.agents.knowledge.grimoire import ALLOWED_TOOLS, GrimoireError
from wattson.agents.knowledge.oauth import LOGIN_EXPIRED, NOT_CONNECTED, TOKEN_FILE
from wattson.shared.contracts import KnowledgeRequest, KnowledgeResult


def crashloop(**changes: object) -> KnowledgeRequest:
    values: dict[str, object] = {
        "request": "my deployment api is in CrashLoopBackOff",
        "queries": ["crashloop", "kubectl logs previous"],
        "tags": [],
        "limit": 2,
    }
    values.update(changes)
    return KnowledgeRequest.model_validate(values)


async def find(k: Knowledge, request: KnowledgeRequest) -> KnowledgeResult:
    return await k.service.retriever.find(request)


async def test_search_read_and_related(connected: Knowledge, stub: GrimoireStub) -> None:
    result = await find(connected, crashloop())
    assert result.warnings == []
    keys = [d.ref.key for d in result.docs]
    whys = [d.ref.why for d in result.docs]
    assert keys[0] == "infra/k8s/crashloop-runbook"
    assert whys.count("search") == 2 and whys.count("related") == 1
    first = result.docs[0]
    assert first.ref.title == "CrashLoopBackOff: find the cause, then fix it"
    assert first.ref.folder == "infra/k8s"
    assert first.ref.tags == ["k8s", "pods", "crashloop", "kubectl"]
    assert first.ref.freshness == "live"
    assert first.ref.excerpt and "kubectl" in first.ref.excerpt and "<mark>" not in first.ref.excerpt
    assert first.text.startswith("---\ntitle:") and "--previous" in first.text
    assert all(len(d.text) <= 4000 and len(d.ref.excerpt or "") <= 600 for d in result.docs)
    assert keys[:2] == ["infra/k8s/crashloop-runbook", "notes/crashloop-ideas"]  # the draft after
    related = [d.ref.key for d in result.docs if d.ref.why == "related"]
    assert related == ["infra/k8s/pod-logs"]  # kubectl and logs: it passes the softer check
    assert "infra/k8s/oomkilled-pods" not in keys  # two shared tags, but nothing of the queries
    assert set(stub.tool_calls()) <= set(ALLOWED_TOOLS)
    assert stub.tool_calls().count("get_related_documents") == 1


async def test_drafts_come_after_active_documents(data_dir: Path) -> None:
    docs = [
        Doc(
            "notes/rollout-draft", "Rollout draft", ["k8s"], "rollout rollout rollout rollout", status="draft"
        ),
        Doc("infra/k8s/rollout-undo", "Undo a rollout", ["k8s"], "kubectl rollout undo deployment/api"),
    ]
    with start_grimoire_stub(docs=docs) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            result = await find(k, crashloop(queries=["rollout"], limit=1))
        finally:
            await k.close()
    assert [d.ref.key for d in result.docs if d.ref.why == "search"] == ["infra/k8s/rollout-undo"]
    assert "notes/rollout-draft" not in [d.ref.key for d in result.docs]  # nor as a related document


async def test_context_by_tags(connected: Knowledge, stub: GrimoireStub) -> None:
    # a context document must match a query too: "postgres restore" is that query
    request = crashloop(queries=["gitlab runner", "postgres restore"], tags=["postgres", "backup"], limit=1)
    result = await find(connected, request)
    by_why = {d.ref.why: d.ref.key for d in result.docs}
    assert by_why["search"] == "ci/gitlab-runner-troubleshooting"
    assert by_why["context"] == "infra/postgres/backup-restore"
    call = [c for c in stub.tool_calls() if c == "get_context"]
    assert call == ["get_context"]


async def test_cache_marks_documents_cached(connected: Knowledge, stub: GrimoireStub) -> None:
    first = await find(connected, crashloop())
    reads = stub.tool_calls().count("read_document")
    assert reads > len(first.docs)  # the candidates that did not match were read and checked too
    second = await find(connected, crashloop())
    assert [d.ref.key for d in second.docs] == [d.ref.key for d in first.docs]
    assert {d.ref.freshness for d in second.docs} == {"cached"}
    assert [d.ref.retrieved_at for d in second.docs] == [d.ref.retrieved_at for d in first.docs]
    assert stub.tool_calls().count("read_document") == reads  # no new read
    assert stub.tool_calls().count("search_documents") == 4  # searches are always live

    connected.service.retriever.cache.clock = lambda: 1e12  # five minutes and more later
    third = await find(connected, crashloop())
    assert {d.ref.freshness for d in third.docs} == {"live"}


async def test_write_and_delete_are_refused_before_any_request(
    connected: Knowledge, stub: GrimoireStub
) -> None:
    grimoire = connected.service.grimoire
    for name, args in [
        ("write_document", {"title": "x", "type": "note", "body": "y"}),
        ("delete_document", {"key": "infra/k8s/crashloop-runbook", "confirm": True}),
        ("suggest_tags", {"query": "k8s"}),
    ]:
        with pytest.raises(GrimoireError) as caught:
            await grimoire.call(name, args)
        assert caught.value.kind == "refused"
        assert name in caught.value.reason
    assert stub.calls == []  # not even a connection was opened

    async def sneaky(session):  # inside an open session, too
        return await session.call("delete_document", {"key": "infra/k8s/crashloop-runbook", "confirm": True})

    with pytest.raises(GrimoireError) as caught:
        await grimoire.run(sneaky)
    assert caught.value.kind == "refused"
    assert "delete_document" not in stub.tool_calls()
    assert "infra/k8s/crashloop-runbook" in stub.library.docs  # type: ignore[union-attr]


async def test_not_connected(knowledge: Knowledge, stub: GrimoireStub) -> None:
    result = await find(knowledge, crashloop())
    assert result.docs == []
    assert result.warnings == [NOT_CONNECTED]
    assert stub.calls == []


async def test_expired_access_token_is_refreshed(connected: Knowledge, stub: GrimoireStub) -> None:
    stub.expire_access_tokens()  # Grimoire refuses the token the agent still takes for valid
    result = await find(connected, crashloop())
    assert result.warnings == []
    assert result.docs
    assert "POST /auth/v1/oauth/token" in stub.calls
    saved = json.loads((connected.settings.data_dir / TOKEN_FILE).read_text())
    assert stub.access_tokens[saved["tokens"]["access_token"]]["exp"] > 0  # the new token is saved


async def test_short_lived_token_is_refreshed_before_use(data_dir: Path) -> None:
    with start_grimoire_stub(token_ttl_s=1) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            await asyncio.sleep(1.2)
            stub.calls.clear()
            # a new pod: the provider knows the expiry and the token endpoint only from the file
            fresh = Knowledge(make_settings(stub, data_dir))
            try:
                result = await find(fresh, crashloop())
            finally:
                await fresh.close()
            assert result.warnings == [] and result.docs
            assert stub.calls[:2] == ["POST /auth/v1/oauth/token", "POST /mcp"]  # refresh first, no 401
        finally:
            await k.close()


async def test_refused_refresh_means_login_expired(connected: Knowledge, stub: GrimoireStub) -> None:
    stub.expire_access_tokens()
    stub.revoke_refresh_tokens()
    result = await find(connected, crashloop())
    assert result.docs == []
    assert result.warnings == [LOGIN_EXPIRED]
    assert "GET /auth/v1/oauth/authorize" not in stub.calls  # never a browser flow from here
    status = (await connected.http.get("/v1/grimoire/status")).json()
    assert status["connected"] is False
    assert status["last_error"] == LOGIN_EXPIRED
    saved = json.loads((connected.settings.data_dir / TOKEN_FILE).read_text())
    assert "tokens" not in saved and saved["login_expired"] is True
    stub.refuse_refresh = False
    again = await connected.connect(stub)  # a new login fixes it
    assert again["connected"] is True and again["last_error"] is None


async def test_slow_grimoire_gives_a_warning(stub: GrimoireStub, data_dir: Path) -> None:
    k = Knowledge(make_settings(stub, data_dir, timeout_s=0.5))
    try:
        await k.connect(stub)
        stub.delay_s = 1.5
        result = await find(k, crashloop())
        assert result.docs == []
        assert result.warnings == ["Grimoire did not answer within 0.5 s"]
        k.service.grimoire.forget_status()
        status = (await k.http.get("/v1/grimoire/status")).json()
        assert status["connected"] is False and "did not answer" in status["last_error"]
    finally:
        stub.delay_s = 0
        await k.close()


async def test_whole_retrieval_budget_keeps_what_was_found(stub: GrimoireStub, data_dir: Path) -> None:
    k = Knowledge(make_settings(stub, data_dir, timeout_s=2.0, retrieval_budget_s=1.0))
    try:
        await k.connect(stub)
        stub.delay_s = 0.3  # each call is fast enough, all of them together are not
        result = await find(k, crashloop(limit=4))
        assert result.warnings and result.warnings[-1].startswith("Grimoire retrieval stopped after 1 s")
        assert len(result.docs) < 8
    finally:
        stub.delay_s = 0
        await k.close()


async def test_unreachable_grimoire(connected: Knowledge, stub: GrimoireStub) -> None:
    stub.close()
    result = await find(connected, crashloop())
    assert result.docs == []
    assert result.warnings == ["Grimoire cannot be reached (ConnectError)"]


@pytest.mark.parametrize("fmt", ["toon", "json"])
async def test_toon_and_json_answers(fmt: str, data_dir: Path) -> None:
    with start_grimoire_stub(fmt=fmt) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            result = await find(k, crashloop(tags=["k8s"], limit=3))
        finally:
            await k.close()
    assert result.warnings == []
    assert [d.ref.key for d in result.docs][:1] == ["infra/k8s/crashloop-runbook"]
    assert {d.ref.why for d in result.docs} == {"search", "related", "context"}
    for doc in result.docs:
        assert doc.ref.folder and doc.ref.tags and doc.ref.title != doc.ref.key
        assert "<mark>" not in (doc.ref.excerpt or "") and "&amp;" not in (doc.ref.excerpt or "")


async def test_secrets_in_documents_are_redacted(connected: Knowledge) -> None:
    result = await find(connected, crashloop(queries=["backup PGPASSWORD"], limit=1))
    doc = next(d for d in result.docs if d.ref.key == "infra/postgres/backup-restore")
    for text in (doc.text, doc.ref.excerpt or ""):
        assert FAKE_SECRET not in text and FAKE_AWS_KEY not in text
        assert FAKE_SECRET[:12] not in text
    assert "[REDACTED:" in doc.text
    assert "[REDACTED:" in (doc.ref.excerpt or "")
    assert "pg_restore" in doc.text  # the rest of the note is still there


async def test_no_token_in_any_text(connected: Knowledge, stub: GrimoireStub) -> None:
    texts: list[str] = []
    texts += [json.dumps((await find(connected, crashloop())).model_dump())]
    stub.expire_access_tokens()
    texts += [json.dumps((await find(connected, crashloop())).model_dump())]
    stub.expire_access_tokens()
    stub.revoke_refresh_tokens()
    texts += [json.dumps((await find(connected, crashloop())).model_dump())]
    texts += [(await connected.http.get("/v1/grimoire/status")).text]
    started = await connected.http.post(
        "/v1/grimoire/connect", json={"redirect_uri": "http://127.0.0.1:1/cb"}
    )
    texts += [started.text]
    texts += [(await connected.http.post("/v1/grimoire/callback", json={"code": "x", "state": "y"})).text]
    stub.close()
    texts += [json.dumps((await find(connected, crashloop())).model_dump())]
    connected.service.grimoire.forget_status()
    texts += [(await connected.http.get("/v1/grimoire/status")).text]
    joined = "\n".join(texts)
    assert stub.issued
    for secret in stub.issued + list(stub.codes):  # the client_id is public: it is in the URL
        assert secret not in joined
    assert "stub-at-" not in joined and "stub-rt-" not in joined and "stub-code-" not in joined
    assert LOGIN_EXPIRED in joined


async def test_grimoire_gateway_versions(data_dir: Path) -> None:
    """Grimoire's gateway answers HTTP 400 to a protocol version its bridge cannot serve: the
    client probes the newest version, gets that answer, and goes on with the older handshake."""
    with start_grimoire_stub(mcp_versions=GATEWAY_MCP_VERSIONS) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            status = await k.connect(stub)
            result = await find(k, crashloop())
        finally:
            await k.close()
    assert status["connected"] is True and status["tools"] == list(ALLOWED_TOOLS)
    assert result.warnings == [] and result.docs[0].ref.key == "infra/k8s/crashloop-runbook"
    assert any(c.startswith("refused version 2026-") for c in stub.calls)


async def test_one_retry_after_a_network_error(connected: Knowledge, monkeypatch: pytest.MonkeyPatch) -> None:
    grimoire = connected.service.grimoire
    real_open = grimoire._open
    tries: list[int] = []

    def flaky_open(auth, read_timeout_s):  # type: ignore[no-untyped-def]
        tries.append(1)
        if len(tries) == 1:
            raise httpx2.ConnectError("connection reset")
        return real_open(auth, read_timeout_s)

    monkeypatch.setattr(grimoire, "_open", flaky_open)
    result = await find(connected, crashloop())
    assert result.warnings == [] and result.docs
    assert len(tries) == 2

    def always_down(auth, read_timeout_s):  # type: ignore[no-untyped-def]
        tries.append(1)
        raise httpx2.ConnectError("connection refused")

    tries.clear()
    monkeypatch.setattr(grimoire, "_open", always_down)
    result = await find(connected, crashloop())
    assert result.warnings == ["Grimoire cannot be reached (ConnectError)"]
    assert len(tries) == 2  # one retry, not more
