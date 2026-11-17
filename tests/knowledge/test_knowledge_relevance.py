"""The relevance check of find_procedures: Grimoire's search returns a document for one common word,
so the knowledge agent reads each candidate and keeps only those that hold enough of a query's
words. The rule itself, then the retrieval against the stub (its invented documents)."""

from __future__ import annotations

from pathlib import Path

import pytest
from grimoire_stub import Doc, GrimoireStub, start_grimoire_stub
from knowledge_helpers import Knowledge, make_settings

from wattson.agents.knowledge.answers import Record
from wattson.agents.knowledge.retrieval import CachedDoc, _Work, passes, words_of
from wattson.shared.contracts import KnowledgeRequest, KnowledgeResult


def request(
    queries: list[str], tags: list[str] | None = None, limit: int = 4, text: str = "x"
) -> KnowledgeRequest:
    return KnowledgeRequest(request=text, queries=queries, tags=tags or [], limit=limit)


def no_match(*queries: str) -> str:
    return (
        "Grimoire has no document that matches this request well (searched: "
        + "; ".join(queries)
        + "); none was added"
    )


async def find_with(
    docs: list[Doc], data_dir: Path, req: KnowledgeRequest
) -> tuple[KnowledgeResult, list[str]]:
    with start_grimoire_stub(docs=docs) as stub:
        k = Knowledge(make_settings(stub, data_dir))
        try:
            await k.connect(stub)
            stub.calls.clear()
            result = await k.service.retriever.find(req)
        finally:
            await k.close()
    return result, stub.tool_calls()


# ---- the words and the rule -----------------------------------------------------------------


def test_words_of() -> None:
    assert words_of("Granite-4.0-H-1B") == {"granite", "4", "0", "1b"}  # "h": too short, no digit
    assert words_of("k8s pods/logs_view.md") == {"k8s", "pod", "log", "view"}
    assert words_of("Decisions about MODELS") == {"decision", "model"}
    assert words_of("policies status class addresses") == {"policy", "status", "class", "address"}
    assert words_of("export PGPASSWORD=[REDACTED:github-token]") == {"export", "pgpassword"}


def test_generic_words_are_dropped() -> None:
    request_text = (
        "In ~/Desktop/wattson write docs/decision-model.md from eval/results/*.json: the three "
        "models measured, why Granite-4.0-H-1B was chosen, and the numbers"
    )
    assert words_of(request_text) == {
        "wattson", "decision", "model", "eval", "measured", "granite", "4", "0", "1b", "chosen"
    }  # fmt: skip
    for generic in ("write the docs", "update the code files", "please add a new task", "run the json yaml"):
        assert words_of(generic) == frozenset(), generic


@pytest.mark.parametrize(
    ("n", "strict", "soft"),
    [(1, 1, 1), (2, 2, 1), (3, 3, 2), (4, 3, 2), (5, 4, 3), (6, 5, 3), (7, 5, 4), (10, 7, 5)],
)
def test_how_many_words_must_be_present(n: int, strict: int, soft: int) -> None:
    query = frozenset(f"word{i}" for i in range(n))
    for need, is_soft in ((strict, False), (soft, True)):
        some = frozenset(sorted(query)[:need])
        fewer = frozenset(sorted(query)[: need - 1])
        assert passes(query, some, soft=is_soft)  # 7 of 10 is enough: no 0.7 * 10 > 7 surprise
        assert not passes(query, fewer, soft=is_soft)


def test_a_query_without_words_passes_nothing() -> None:
    assert not passes(frozenset(), frozenset({"anything"}))
    assert not passes(frozenset(), frozenset({"anything"}), soft=True)


def test_plural_and_singular_are_the_same_word() -> None:
    doc = words_of("A decision about the model, with the pod logs")
    assert passes(words_of("decisions models"), doc)
    assert passes(words_of("pods log"), doc)


def _work(queries: list[str]) -> _Work:
    return _Work(request(queries), queries)


def _cached(body: str) -> CachedDoc:
    return CachedDoc(None, [], body, body, words_of(body), "2026-10-23T00:00:00+00:00", 0.0)


def test_the_title_tags_folder_and_key_count() -> None:
    work = _work(["hpa autoscale replicas"])
    record = Record(
        key="infra/k8s/hpa", title="Autoscale a deployment", folder="infra/k8s", tags=["replicas"]
    )
    assert work.relevant(record, _cached("nothing here"))
    assert not work.relevant(Record(key="a/b", title="b", folder="a"), _cached("hpa autoscale"))


def test_marks_are_stripped_from_the_excerpt() -> None:
    work = _work(["kubectl logs"])
    record = Record(key="a/b", title="b", folder="a", snippet="run <mark>kubectl</mark> <mark>logs</mark>")
    work.add(record, _cached("kubectl logs"), "live", "search")
    assert work.docs[0].ref.excerpt == "run kubectl logs"


# ---- against the stub -----------------------------------------------------------------------


async def test_one_word_of_three_is_not_enough(data_dir: Path) -> None:
    docs = [
        Doc(
            "infra/k8s/hpa-autoscaling",
            "HPA: autoscale a deployment",
            ["k8s", "hpa"],
            "`kubectl autoscale deployment api --min 2 --max 10`: the HPA adds replicas when the CPU "
            "is high.",
        ),
        Doc(
            "notes/deployment-checklist",
            "Deployment checklist",
            ["notes"],
            "Before each deployment: the tests are green and the changelog is written.",
            type="note",
        ),
    ]
    # Grimoire finds the checklist for the second query by its one word "deployment"
    req = request(["autoscale deployment replicas", "deployment rollback steps"])
    result, calls = await find_with(docs, data_dir, req)
    assert result.warnings == []
    assert [d.ref.key for d in result.docs] == ["infra/k8s/hpa-autoscaling"]  # all three words
    assert calls.count("read_document") == 2  # the checklist was read, checked and dropped


async def test_a_singular_query_finds_a_plural_document(connected: Knowledge, stub: GrimoireStub) -> None:
    result = await connected.service.retriever.find(request(["pod log"], limit=8))
    keys = [d.ref.key for d in result.docs if d.ref.why == "search"]
    assert "infra/k8s/pod-logs" in keys and "infra/k8s/crashloop-runbook" in keys
    assert "infra/k8s/oomkilled-pods" not in keys  # pods, but no log


async def test_unrelated_documents_are_not_added(connected: Knowledge, stub: GrimoireStub) -> None:
    """The run that started this: Grimoire has no document about the model choice, and its search
    returns two architecture notes for the word "model". The right answer is no document."""
    queries = ["Granite-4.0-H-1B model decision", "eval results models", "decision model docs"]
    req = request(
        queries,
        tags=["models", "eval"],
        text="In ~/Desktop/wattson write docs/decision-model.md from eval/results/*.json: the three "
        "models measured, why Granite-4.0-H-1B was chosen, and the numbers",
    )
    result = await connected.service.retriever.find(req)
    assert result.docs == []
    assert result.warnings == [no_match(*queries)]
    calls = stub.tool_calls()
    assert calls.count("search_documents") == 3 and calls.count("read_document") == 2  # the two notes
    assert "get_related_documents" not in calls and "get_context" not in calls


async def test_generic_words_only_find_nothing(connected: Knowledge, stub: GrimoireStub) -> None:
    queries = ["write the docs", "update the code files"]
    result = await connected.service.retriever.find(request(queries, tags=["k8s"]))
    assert result.docs == []
    assert result.warnings == [no_match(*queries)]
    assert stub.tool_calls() == ["search_documents", "search_documents"]  # nothing read: nothing can pass


async def test_an_empty_search_keeps_its_own_warning(connected: Knowledge) -> None:
    result = await connected.service.retriever.find(request(["zeppelin hangar"]))
    assert result.docs == []
    assert result.warnings == ["Grimoire found no document for: zeppelin hangar"]


async def test_related_documents_come_from_a_document_that_passed(data_dir: Path) -> None:
    docs = [
        Doc("notes/kubectl", "Notes", ["notes"], "kubectl kubectl kubectl", type="note"),
        Doc("notes/kubectl-tips", "Tips", ["notes"], "kubectl rollout tips", type="note"),
        Doc(
            "infra/k8s/rollout-undo",
            "Undo a rollout",
            ["k8s", "rollout"],
            "kubectl rollout undo deployment/api, then kubectl rollout status",
        ),
        Doc(
            "infra/k8s/rollout-history",
            "Rollout history",
            ["k8s", "rollout"],
            "kubectl rollout history deployment/api lists the revisions",
        ),
        Doc(
            "infra/k8s/rollout-canary", "Canary rollout", ["k8s", "rollout"], "Argo: canary steps and weights"
        ),
    ]
    # Grimoire's first answer is the weak notes/kubectl (three times "kubectl" for the first query)
    req = request(["kubectl cheatsheet shortcuts", "kubectl rollout undo"], limit=2)
    result, calls = await find_with(docs, data_dir, req)
    assert result.warnings == []
    assert [(d.ref.key, d.ref.why) for d in result.docs] == [
        ("infra/k8s/rollout-undo", "search"),
        ("infra/k8s/rollout-history", "related"),  # kubectl and rollout: 2 of 3, enough for the softer check
    ]
    # notes/kubectl-tips is related to the weak note only; rollout-canary shares two tags but
    # holds one query word: neither is added
    assert calls.count("get_related_documents") == 1


async def test_context_documents_must_match_and_share_two_tags(connected: Knowledge) -> None:
    retriever = connected.service.retriever
    one_tag = await retriever.find(request(["kubectl logs previous"], tags=["k8s"], limit=2))
    by_why = [(d.ref.key, d.ref.why) for d in one_tag.docs]
    assert by_why == [
        ("infra/k8s/crashloop-runbook", "search"),
        ("infra/k8s/pod-logs", "related"),
        ("infra/k8s/deployment-restart", "context"),  # kubectl and logs; the other k8s documents fail
    ]
    two_tags = await retriever.find(request(["kubectl logs previous"], tags=["postgres", "k8s"], limit=2))
    assert [d.ref.why for d in two_tags.docs] == ["search", "related"]  # each shares one of the 2 tags
    assert two_tags.warnings == []
