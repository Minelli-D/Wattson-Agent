"""Wattson procedures against the stub: list_procedures (the documents of folder "wattson" only, their
fields, the cache, the warnings) and find_procedures with procedure_key (the procedure, then the
runbooks it lists, no search; a procedure that cannot be read means a free search)."""

from __future__ import annotations

from pathlib import Path

from grimoire_stub import FAKE_SECRET, Doc, GrimoireStub, default_docs, procedure_body, start_grimoire_stub
from knowledge_helpers import Knowledge, make_settings

from wattson.agents.knowledge.answers import first_paragraph, listed_keys, section
from wattson.agents.knowledge.oauth import NOT_CONNECTED
from wattson.shared.contracts import KnowledgeRequest, ProcedureList, ProcedureListRequest

TLS = "wattson/k8s-tls-ingress.md"


def procedure(key: str, queries: list[str] | None = None) -> KnowledgeRequest:
    return KnowledgeRequest(
        request="https on my ingress fails", queries=queries or ["tls certificate"], procedure_key=key
    )


async def listed(k: Knowledge, folder: str = "wattson") -> ProcedureList:
    return await k.service.procedures.list(ProcedureListRequest(folder=folder))


# ---- the sections of a body -------------------------------------------------------------------


def test_sections_of_a_procedure() -> None:
    body = (
        "## When to use\n\nA pod starts and then dies\nagain and again.\n\nSecond paragraph.\n\n"
        "## Steps for Claude Code\n\n```sh\n# not a heading\nkubectl get pods\n```\n\n### Detail\n\nx\n\n"
        "## Related runbooks\n\n"
        "- infra/k8s/flux/example-k8s-manifest-set-for-a-flux-managed-service.md\n"
        "- `go-live/grimoire-https.md` — the hosts file\n"
        "- [Kubectl](infra/k8s/kubectl-commands-i-actually-use.md)\n"
        "- infra/k8s/flux/example-k8s-manifest-set-for-a-flux-managed-service.md\n"
        "- not a key: words\n"
    )
    assert (
        first_paragraph(section(body, "when to use") or "") == "A pod starts and then dies again and again."
    )
    steps = section(body, "Steps for Claude Code") or ""
    assert "# not a heading" in steps and "### Detail" in steps and "Related" not in steps
    assert listed_keys(section(body, "Related runbooks") or "") == [
        "infra/k8s/flux/example-k8s-manifest-set-for-a-flux-managed-service.md",
        "go-live/grimoire-https.md",
        "infra/k8s/kubectl-commands-i-actually-use.md",
    ]
    assert listed_keys("- none yet") == [] and listed_keys("- None yet.") == []
    assert section(body, "Done when") is None


# ---- list_procedures --------------------------------------------------------------------------


async def test_list_procedures(connected: Knowledge, stub: GrimoireStub) -> None:
    result = await listed(connected)
    assert result.warnings == []
    assert [p.name for p in result.procedures] == ["code-bug-fix", "k8s-tls-ingress", "prompt-rewrite"]
    by_name = {p.name: p for p in result.procedures}
    tls = by_name["k8s-tls-ingress"]
    assert tls.key == TLS
    assert tls.workflow == "devops-investigation"
    assert tls.signals == ["tls", "certificate", "ingress", "https"]  # no "wattson", no workflow
    assert tls.when_to_use.startswith("An app on the cluster cannot be reached at its address")
    assert "\n" not in tls.when_to_use and "##" not in tls.when_to_use
    bug = by_name["code-bug-fix"]
    assert (bug.key, bug.workflow) == ("wattson/code-bug-fix.md", "engineering-task")
    assert bug.signals == ["bug", "regression", "traceback", "failing-test"]
    # Grimoire's summary comes first; the body is read only for the documents without one
    assert (
        by_name["prompt-rewrite"].when_to_use
        == "Rewrite a rough request into a clear prompt for Claude Code."
    )
    assert by_name["prompt-rewrite"].workflow == "prompt-optimization"
    assert sorted(stub.tool_calls()) == ["get_context", "read_document", "read_document"]


async def test_the_folder_and_its_subfolders_active_documents_only(data_dir: Path) -> None:
    # Subfolders count ("wattson/k8s-procedures"); a note elsewhere tagged "wattson", drafts and
    # archived documents do not. The subfolder's own tag is not a signal word.
    extra = [
        Doc(
            "wattson/k8s-procedures/k8s-node-notready.md",
            "k8s-node-notready",
            ["devops-investigation", "notready", "kubelet", "wattson", "k8s-procedures"],
            "## When to use\n\nA node is NotReady.",
        ),
        Doc("notes/wattson-ideas", "Wattson ideas", ["notes", "wattson"], "## When to use\n\nnever"),
        Doc("wattson/half-done.md", "half-done", ["engineering-task", "wattson"], "draft", status="draft"),
        Doc("wattson/gone.md", "gone", ["engineering-task", "wattson"], "archived", status="archived"),
    ]
    with start_grimoire_stub(docs=default_docs() + extra) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            result = await listed(k)
        finally:
            await k.close()
    assert result.warnings == []
    assert [p.key for p in result.procedures] == [
        "wattson/code-bug-fix.md",
        "wattson/k8s-procedures/k8s-node-notready.md",  # sorted by name: k8s-node… before k8s-tls…
        TLS,
        "wattson/prompt-rewrite.md",
    ]
    node = next(p for p in result.procedures if p.name == "k8s-node-notready")
    assert node.signals == ["notready", "kubelet"] and node.workflow == "devops-investigation"


async def test_a_name_used_twice_is_offered_once(data_dir: Path) -> None:
    extra = [
        Doc(
            "wattson/k8s-procedures/code-bug-fix.md",
            "code-bug-fix",
            ["engineering-task", "bug", "wattson", "k8s-procedures"],
            "## When to use\n\nA copy.",
        )
    ]
    with start_grimoire_stub(docs=default_docs() + extra) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            result = await listed(k)
        finally:
            await k.close()
    assert [p.name for p in result.procedures].count("code-bug-fix") == 1
    assert any("two Wattson procedures named code-bug-fix" in w for w in result.warnings)


async def test_when_to_use_is_redacted_short_and_one_line(data_dir: Path) -> None:
    long_text = "A very long first paragraph that goes on\nand on. " * 20
    docs = [
        Doc(
            "wattson/secret-proc.md",
            "secret-proc",
            ["devops-investigation", "wattson"],
            procedure_body(f"Use it when the token {FAKE_SECRET} leaks.", "-", "1.", "x", "-", "- none yet"),
        ),
        Doc(
            "wattson/long-proc.md",
            "long-proc",
            ["engineering-task", "wattson"],
            procedure_body(long_text, "-", "1.", "x", "-", "- none yet"),
        ),
        Doc("wattson/no-heading.md", "no-heading", ["wattson"], "Just a note."),
    ]
    with start_grimoire_stub(docs=docs) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            result = await listed(k)
        finally:
            await k.close()
    by_name = {p.name: p for p in result.procedures}
    assert FAKE_SECRET not in by_name["secret-proc"].when_to_use
    assert "[REDACTED:" in by_name["secret-proc"].when_to_use
    long = by_name["long-proc"].when_to_use
    assert len(long) <= 300 and long.endswith("…") and "\n" not in long
    assert by_name["no-heading"].when_to_use == "" and by_name["no-heading"].workflow is None


async def test_the_list_is_cached_five_minutes(connected: Knowledge, stub: GrimoireStub) -> None:
    first = await listed(connected)
    calls = len(stub.tool_calls())
    second = await listed(connected)
    assert second == first
    assert len(stub.tool_calls()) == calls  # no call at all
    # the procedure picked next was read for the list: it comes from memory
    found = await connected.service.retriever.find(procedure(TLS))
    assert found.docs[0].ref.why == "procedure" and found.docs[0].ref.freshness == "cached"

    connected.service.procedures.clock = lambda: 1e12  # five minutes and more later
    stub.calls.clear()
    third = await listed(connected)
    assert third.procedures == first.procedures
    assert "get_context" in stub.tool_calls()


async def test_the_cache_is_forgotten_after_a_disconnect(connected: Knowledge, stub: GrimoireStub) -> None:
    assert (await listed(connected)).procedures
    await connected.http.post("/v1/grimoire/disconnect")
    result = await listed(connected)
    assert result.procedures == [] and result.warnings == [NOT_CONNECTED]


async def test_list_when_not_connected(knowledge: Knowledge, stub: GrimoireStub) -> None:
    result = await listed(knowledge)
    assert result.procedures == []
    assert result.warnings == [NOT_CONNECTED]
    assert stub.calls == []
    await knowledge.connect(stub)  # the warning was not kept: connected, the list is there
    assert len((await listed(knowledge)).procedures) == 3


async def test_list_when_grimoire_cannot_be_reached(connected: Knowledge, stub: GrimoireStub) -> None:
    stub.close()
    result = await listed(connected)
    assert result.procedures == []
    assert result.warnings == ["Grimoire cannot be reached (ConnectError)"]


async def test_an_empty_folder_says_so(connected: Knowledge) -> None:
    result = await listed(connected, folder="nothing-here")
    assert result.procedures == []
    assert result.warnings == ["Grimoire has no active procedure in folder nothing-here"]


# ---- find_procedures with a procedure ----------------------------------------------------------


async def test_the_procedure_then_its_runbooks_and_no_search(
    connected: Knowledge, stub: GrimoireStub
) -> None:
    result = await connected.service.retriever.find(procedure(TLS))
    assert result.warnings == []
    assert [(d.ref.key, d.ref.why) for d in result.docs] == [
        (TLS, "procedure"),
        ("infra/k8s/cert-manager-renewal", "related"),
        ("infra/k8s/networkpolicy-basics", "related"),
    ]
    first = result.docs[0]
    assert first.ref.title == "k8s-tls-ingress" and first.ref.folder == "wattson"
    assert first.ref.excerpt and first.ref.excerpt.startswith("An app on the cluster cannot be reached")
    assert "## Steps for Claude Code" in first.text and "## Related runbooks" in first.text
    assert result.docs[1].ref.title == "cert-manager: renew a stuck certificate"
    calls = stub.tool_calls()
    assert calls == ["read_document", "read_document", "read_document"]  # no search, related, context


async def test_none_yet_and_a_runbook_that_is_missing(connected: Knowledge, stub: GrimoireStub) -> None:
    bug = await connected.service.retriever.find(procedure("wattson/code-bug-fix.md"))
    assert bug.warnings == []
    assert [(d.ref.key, d.ref.why) for d in bug.docs] == [("wattson/code-bug-fix.md", "procedure")]

    prompt = await connected.service.retriever.find(procedure("wattson/prompt-rewrite.md"))
    assert [(d.ref.key, d.ref.why) for d in prompt.docs] == [
        ("wattson/prompt-rewrite.md", "procedure"),
        ("ai/prompt-writing-guide", "related"),
    ]
    assert prompt.warnings == [
        'Grimoire could not read ai/prompt-examples: No document found for key "ai/prompt-examples".'
    ]
    assert "search_documents" not in stub.tool_calls()


async def test_a_missing_procedure_means_a_free_search(connected: Knowledge, stub: GrimoireStub) -> None:
    result = await connected.service.retriever.find(procedure("wattson/does-not-exist.md", ["crashloop"]))
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("Grimoire could not read wattson/does-not-exist.md: No document")
    assert result.warnings[0].endswith('"; a free search was done instead')
    assert result.docs and result.docs[0].ref.key == "infra/k8s/crashloop-runbook"
    assert "procedure" not in {d.ref.why for d in result.docs}
    assert "search_documents" in stub.tool_calls()


async def test_a_long_procedure_is_cut_and_redacted(data_dir: Path) -> None:
    steps = "\n".join(f"{i}. Check the thing number {i} with care and write it down." for i in range(200))
    docs = [
        Doc(
            "wattson/long-proc.md",
            "long-proc",
            ["devops-investigation", "wattson"],
            procedure_body(
                f"When it is long. The key is {FAKE_SECRET}.", "-", steps, "x", "-", "- infra/a-runbook"
            ),
        ),
        Doc("infra/a-runbook", "A runbook", ["infra"], "# A runbook\n\nDo this."),
    ]
    with start_grimoire_stub(docs=docs) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            result = await k.service.retriever.find(procedure("wattson/long-proc.md"))
        finally:
            await k.close()
    assert result.warnings == []
    proc = result.docs[0]
    assert 4000 < len(proc.text) <= 6000 and proc.text.endswith("[… truncated: the rest is in Grimoire]")
    assert FAKE_SECRET not in proc.text and FAKE_SECRET not in (proc.ref.excerpt or "")
    assert "[REDACTED:" in proc.text
    # the runbooks are listed after the cut: they are read from the whole body
    assert [(d.ref.key, d.ref.why) for d in result.docs[1:]] == [("infra/a-runbook", "related")]


async def test_free_search_never_returns_a_wattson_procedure(connected: Knowledge) -> None:
    # The pick said "no procedure": the free search must not bring one in by the side door,
    # even when the query matches its words.
    result = await connected.service.retriever.find(
        KnowledgeRequest(request="https certificate on my ingress", queries=["tls certificate ingress"])
    )
    assert not any(d.ref.key.startswith("wattson/") for d in result.docs)
