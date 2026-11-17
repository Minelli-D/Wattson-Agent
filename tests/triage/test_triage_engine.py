"""The engine against a fake llama.cpp server: confidence, threshold, fallbacks, hints, overrides.
The same engine against an Ollama/KubeAI-like server: test_triage_openai.py."""

from __future__ import annotations

import math
import time
from typing import Any

import httpx
import pytest

from wattson.agents.triage import engine as eng
from wattson.agents.triage.engine import HintDraft, TriageEngine, letter_scores, merge_hints
from wattson.agents.triage.rules import RulesHints
from wattson.shared.contracts import DecisionHints, Overrides, RepoInfo, TriageRequest, WorkspaceInfo


def top(**letters: float) -> list[dict[str, Any]]:
    return [{"token": k.replace("_", " "), "logprob": v} for k, v in letters.items()]


def req(text: str, ws: WorkspaceInfo, **overrides: Any) -> TriageRequest:
    return TriageRequest(request=text, workspace=ws, overrides=Overrides(**overrides))


# ---- softmax and confidence ------------------------------------------------------------------


def test_softmax_over_the_four_letters() -> None:
    scores = letter_scores(top(B=math.log(0.6), C=math.log(0.2), A=math.log(0.1), D=math.log(0.05), x=-0.1))
    assert set(scores) == {"prompt-optimization", "devops-investigation", "engineering-task", "unclear"}
    assert sum(scores.values()) == pytest.approx(1.0)
    assert scores["devops-investigation"] == pytest.approx(0.6 / 0.95)


def test_spaced_tokens_add_up_and_missing_letters_are_tiny() -> None:
    scores = letter_scores(top(A=math.log(0.3), _A=math.log(0.3), C=math.log(0.4)))
    assert scores["prompt-optimization"] == pytest.approx(0.6)
    assert scores["unclear"] < 1e-9


def test_temperature_flattens() -> None:
    sharp = letter_scores(top(A=-0.1, B=-2.5, C=-4.0, D=-5.0))
    flat = letter_scores(top(A=-0.1, B=-2.5, C=-4.0, D=-5.0), temperature=2.0)
    assert flat["prompt-optimization"] < sharp["prompt-optimization"]


def test_answer_without_logprobs_is_unusable() -> None:
    with pytest.raises(eng.ModelUnavailable):
        eng.top_logprobs_of({"choices": [{"message": {"content": "B"}}]})


# ---- the whole triage ---------------------------------------------------------------------------


async def test_model_decides_above_threshold(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    result = await engine.triage(req("the pods crash on my-cluster -n payments", workspace))
    c = result.classification
    assert (c.category, c.method, c.model) == ("devops-investigation", "model", "fake-model")
    assert c.confidence > 0.9 and c.scores["devops-investigation"] == pytest.approx(c.confidence, abs=1e-3)
    assert result.hints.cluster == "my-cluster" and result.hints.namespace == "payments"
    assert result.hints.workload is None  # the model said deployment/checkout, but the text does not name it
    assert result.hints.recommended_next == "collect"
    assert result.warnings == []
    sent = next(r for r in fake_llama.requests if r.get("logprobs"))
    assert (sent["max_tokens"], sent["temperature"], sent["logprobs"], sent["top_logprobs"]) == (
        1,
        0,
        True,
        10,
    )
    assert len(sent["messages"]) == 2 + 2 * len(eng.CLASSIFY_EXAMPLES)  # system, few-shot turns, request
    assert sent["messages"][-1]["content"].startswith("Request: the pods crash")
    hint = next(r for r in fake_llama.requests if "response_format" in r)
    schema = hint["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["search_queries"]["maxItems"] == 2
    assert set(schema["required"]) == set(HintDraft.model_fields)
    assert hint["max_tokens"] == eng.HINT_MAX_TOKENS and hint["temperature"] == 0
    assert fake_llama.paths.count("POST /v1/chat/completions") == 2  # LLAMA_URL + /v1


async def test_under_threshold_strong_rules_decide(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.top_logprobs = top(B=-1.2, C=-1.3, A=-1.5, D=-1.6)  # flat: about 0.3
    result = await engine.triage(req("Rewrite this prompt for Claude: check the pods", workspace))
    c = result.classification
    assert (c.category, c.method) == ("prompt-optimization", "rules")
    assert c.scores["devops-investigation"] < 0.6  # the model's scores stay visible
    assert any("decision model unsure" in w for w in result.warnings)


async def test_under_threshold_weak_rules_mean_unclear(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.top_logprobs = top(B=-1.2, C=-1.3, A=-1.5, D=-1.6)
    result = await engine.triage(req("the usual thing from yesterday", workspace))
    assert (result.classification.category, result.classification.method) == ("unclear", "model")
    assert result.hints.recommended_next == "ask"
    assert any("must be asked" in w for w in result.warnings)


@pytest.fixture
def closed_url() -> str:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"  # nothing listens there any more


async def test_model_down(make_settings: Any, closed_url: str, workspace: WorkspaceInfo) -> None:
    engine = TriageEngine(make_settings(closed_url))
    try:
        result = await engine.triage(
            req("pods crash in CrashLoopBackOff on prod-eu -n web, check why", workspace)
        )
    finally:
        await engine.aclose()
    c = result.classification
    assert (c.category, c.method) == ("devops-investigation", "rules")
    assert c.confidence == pytest.approx(0.85)
    assert result.warnings[0].startswith("decision model unavailable (cannot connect")
    assert result.warnings[0].endswith("keyword rules used")
    assert (result.hints.cluster, result.hints.namespace) == ("prod-eu", "web")
    assert "CrashLoopBackOff" in result.hints.grep


async def test_one_retry_on_connection_refused(make_settings: Any, workspace: WorkspaceInfo) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "logprobs": {
                            "content": [{"token": "C", "logprob": -0.01, "top_logprobs": top(C=-0.01)}]
                        }
                    }
                ]
            },
        )

    settings = make_settings("http://llama.invalid")
    client = httpx.AsyncClient(base_url=settings.llm_base_url, transport=httpx.MockTransport(handler))
    engine = TriageEngine(settings, client=client)
    try:
        verdict = await engine.classify_with_model("add a flag")
    finally:
        await engine.aclose()
    assert calls["n"] == 2 and verdict.category == "engineering-task"


async def test_timeout_falls_back_to_rules(
    fake_llama: Any, make_settings: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.delay_s = 2.0
    engine = TriageEngine(make_settings(fake_llama.url, timeout_s=0.5))
    started = time.monotonic()
    try:
        result = await engine.triage(req("add unit tests for src/parser.py", workspace))
    finally:
        await engine.aclose()
    assert time.monotonic() - started < 1.5
    assert (result.classification.category, result.classification.method) == ("engineering-task", "rules")
    assert "decision model unavailable" in result.warnings[0]
    assert "the model may be starting" in result.warnings[0]
    assert len(result.warnings) == 1  # the hint call was cancelled: no second wait on a slow model
    assert result.hints.files == ["src/parser.py"]


async def test_model_loading_503(engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo) -> None:
    fake_llama.status = 503
    result = await engine.triage(req("help", workspace))
    assert result.classification.method == "rules"
    assert "HTTP 503" in result.warnings[0]
    health = await engine.health()
    assert health.ok is False and "HTTP 503" in (health.detail or "")


async def test_bad_hint_json_falls_back_to_rules(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.hints = '{"search_queries": ["x"'  # cut off
    result = await engine.triage(req("pods crash on kind-dev -n data, statefulset postgres", workspace))
    assert result.classification.method == "model"
    assert any(w.startswith("hint model failed (the hint answer is not valid JSON)") for w in result.warnings)
    assert (result.hints.cluster, result.hints.namespace, result.hints.workload) == (
        "kind-dev",
        "data",
        "statefulset/postgres",
    )


async def test_override_wins_and_skips_the_classification(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    result = await engine.triage(
        req("pods crash", workspace, workflow="engineering-task", cluster="kind-dev", namespace="Web")
    )
    c = result.classification
    assert (c.category, c.method, c.confidence) == ("engineering-task", "override", 1.0)
    assert fake_llama.classify_calls() == 0
    assert (result.hints.cluster, result.hints.namespace) == ("kind-dev", "web")


async def test_override_with_unknown_cluster_is_ignored(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    result = await engine.triage(req("pods crash -n web", workspace, cluster="prod-us"))
    assert result.hints.cluster is None
    assert any("prod-us" in w for w in result.warnings)
    assert result.hints.recommended_next == "ask"


async def test_warm_up_fills_the_cache(engine: TriageEngine, fake_llama: Any) -> None:
    assert await engine.warm_up(wait_s=5) is True
    assert fake_llama.classify_calls() == 1 and fake_llama.hint_calls() == 1


async def test_warm_up_gives_up_quietly(make_settings: Any, closed_url: str) -> None:
    engine = TriageEngine(make_settings(closed_url))
    try:
        assert await engine.warm_up(wait_s=0.3, every_s=0.1) is False
    finally:
        await engine.aclose()


async def test_health(engine: TriageEngine) -> None:
    health = await engine.health()
    assert health.ok and "fake-model" in (health.detail or "")


# ---- hint sanitizing and the next step ---------------------------------------------------------


def _draft(**kw: Any) -> HintDraft:
    base: dict[str, Any] = {
        "search_queries": [],
        "tags": [],
        "cluster": None,
        "namespace": None,
        "workload": None,
        "files": [],
        "grep": [],
        "missing_info": [],
    }
    base.update(kw)
    return HintDraft.model_construct(**base)  # no caps: the sanitizer must cope with any list


def test_sanitize_drops_what_is_not_grounded_or_not_valid(workspace: WorkspaceInfo) -> None:
    text = "on Prod-EU the api deploy in ns Web_1 fails, see /etc/passwd ../x.py src/a.py 'boom happened'"
    draft = _draft(
        cluster="prod-eu",
        namespace="Web_1",
        workload="deploy/api",
        files=["/etc/passwd", "../x.py", "src/a.py", "src/invented.py", "https://x.io/a.py"],
        grep=["boom happened", "not in the text"],
        tags=["Kubernetes", "bad tag!", "aws"],
        search_queries=["  api   deploy fails "],
    )
    hints = merge_hints(draft, RulesHints(), req(text, workspace))
    assert hints.cluster == "prod-eu"  # as the workspace writes it, and named in the text
    assert hints.namespace is None  # not RFC 1123
    assert hints.workload == "deployment/api"
    assert hints.files == ["src/a.py"]
    assert hints.grep == ["boom happened"]
    assert hints.tags == ["kubernetes", "aws"]
    assert hints.search_queries == ["api deploy fails"]


def test_sanitize_cluster_must_be_in_the_workspace(workspace: WorkspaceInfo) -> None:
    hints = merge_hints(_draft(cluster="prod-us"), RulesHints(), req("pods on prod-us", workspace))
    assert hints.cluster is None
    hints = merge_hints(_draft(cluster="my-cluster"), RulesHints(), req("pods on my cluster", workspace))
    assert hints.cluster is None  # in the workspace, but the request does not name it


def test_rules_fill_what_the_model_left_empty(workspace: WorkspaceInfo) -> None:
    found = RulesHints(namespace="web", workload="deployment/api", files=["a.py"], tags=["helm"])
    hints = merge_hints(
        _draft(namespace=None, tags=["kubernetes"]), found, req("x -n web deployment/api a.py", workspace)
    )
    assert (hints.namespace, hints.workload, hints.files) == ("web", "deployment/api", ["a.py"])
    assert hints.tags == ["helm", "kubernetes"]  # the rules' known words first


@pytest.mark.parametrize(
    ("category", "cluster", "namespace", "repo", "files", "expected"),
    [
        ("unclear", None, None, False, [], "ask"),
        ("devops-investigation", "my-cluster", None, False, [], "ask"),
        ("devops-investigation", "my-cluster", "web", False, [], "collect"),
        ("engineering-task", None, None, True, [], "collect"),
        ("engineering-task", None, None, False, [], "propose"),
        ("prompt-optimization", None, None, True, [], "propose"),
        ("prompt-optimization", None, None, True, ["src/a.py"], "collect"),
    ],
)
def test_recommended_next(
    category: str, cluster: str | None, namespace: str | None, repo: bool, files: list[str], expected: str
) -> None:
    ws = WorkspaceInfo(
        cwd_name="x",
        kube_contexts=["my-cluster"],
        repo=RepoInfo(name="x", root_display="~/x") if repo else None,
    )
    hints = DecisionHints(cluster=cluster, namespace=namespace, files=files)
    out = eng.finish_hints(category, hints, ws)
    assert out.recommended_next == expected
    if category == "devops-investigation" and not namespace:
        assert "the namespace" in out.missing_info


@pytest.mark.parametrize("overrides", [Overrides(namespaces=["web", "shop"]), Overrides(all_namespaces=True)])
def test_several_or_all_namespaces_count_as_known(workspace: WorkspaceInfo, overrides: Overrides) -> None:
    hints = DecisionHints(cluster="my-cluster", missing_info=["the namespace"])
    out = eng.finish_hints("devops-investigation", hints, workspace, overrides)
    assert "the namespace" not in out.missing_info and out.recommended_next == "collect"
    assert (
        "the namespace" in eng.finish_hints("devops-investigation", DecisionHints(), workspace).missing_info
    )


def test_model_missing_info_about_the_target_is_replaced(workspace: WorkspaceInfo) -> None:
    hints = DecisionHints(
        cluster="my-cluster", namespace="web", missing_info=["the cluster", "which version"]
    )
    out = eng.finish_hints("devops-investigation", hints, workspace)
    assert out.missing_info == ["which version"]
    out = eng.finish_hints("engineering-task", DecisionHints(missing_info=["the namespace"]), workspace)
    assert out.missing_info == []
