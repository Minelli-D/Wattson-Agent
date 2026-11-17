"""The engine against any OpenAI-compatible server: the llama.cpp sidecar and Ollama behind KubeAI
(the fake_server fixture runs each test once per flavour)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from wattson.agents.triage import engine as eng
from wattson.agents.triage.engine import HintDraft, TriageEngine
from wattson.shared.contracts import TriageRequest, WorkspaceInfo

LLAMA_CPP_ONLY = {"cache_prompt", "json_schema", "grammar", "n_predict", "n_probs", "id_slot"}


def req(text: str, ws: WorkspaceInfo) -> TriageRequest:
    return TriageRequest(request=text, workspace=ws)


@pytest.fixture
async def any_engine(fake_server: Any) -> Any:
    engine = TriageEngine(fake_server.settings())
    yield engine
    await engine.aclose()


def expected_model(fake: Any) -> str:
    return fake.model if fake.flavour == "ollama" else "fake-model"  # LLM_MODEL, else MODEL_NAME


async def test_classification_by_logprobs(any_engine: TriageEngine, fake_server: Any) -> None:
    verdict = await any_engine.classify_with_model("the pods crash on my-cluster")
    assert verdict.category == "devops-investigation" and verdict.confidence > 0.9
    assert fake_server.paths == [f"POST {fake_server.prefix}/chat/completions"]
    sent = fake_server.requests[0]
    assert sent["model"] == expected_model(fake_server)
    assert (sent["temperature"], sent["max_tokens"], sent["logprobs"], sent["top_logprobs"]) == (
        0,
        1,
        True,
        10,
    )
    assert not LLAMA_CPP_ONLY & set(sent)
    # Granite 4.2 runs with --reasoning on; classify must turn the think step off so the FIRST
    # token is the category letter, not a think token.
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


async def test_every_model_call_turns_thinking_off(
    any_engine: TriageEngine, fake_server: Any, workspace: WorkspaceInfo
) -> None:
    fake_server.pick = {"procedure": "none"}
    await any_engine.classify_with_model("the pods crash on my-cluster")
    await any_engine.hints_with_model(req("pods crash on my-cluster", workspace))
    await any_engine.pick_with_model("the pods crash", [])
    assert fake_server.requests, "no request was sent"
    for sent in fake_server.requests:
        assert sent["chat_template_kwargs"] == {"enable_thinking": False}


async def test_hints_by_response_format(
    any_engine: TriageEngine, fake_server: Any, workspace: WorkspaceInfo
) -> None:
    draft = await any_engine.hints_with_model(req("pods crash on my-cluster -n payments", workspace))
    assert (draft.cluster, draft.namespace, draft.grep) == ("my-cluster", "payments", ["CrashLoopBackOff"])
    sent = fake_server.requests[0]
    assert sent["model"] == expected_model(fake_server)
    assert (sent["temperature"], sent["max_tokens"]) == (0, eng.HINT_MAX_TOKENS)
    assert not LLAMA_CPP_ONLY & set(sent) and "stream" not in sent
    rf = sent["response_format"]
    assert rf["type"] == "json_schema"
    assert (rf["json_schema"]["name"], rf["json_schema"]["strict"]) == ("decision_hints", True)
    schema = rf["json_schema"]["schema"]
    assert set(schema["required"]) == set(HintDraft.model_fields) and schema["additionalProperties"] is False
    assert sent["messages"][0]["role"] == "system"
    assert all(isinstance(m["content"], str) for m in sent["messages"])  # plain strings: any server
    assert sent["messages"][-1]["content"].endswith("Request: pods crash on my-cluster -n payments")


async def test_whole_triage(any_engine: TriageEngine, fake_server: Any, workspace: WorkspaceInfo) -> None:
    result = await any_engine.triage(req("the pods crash on my-cluster -n payments", workspace))
    c = result.classification
    assert (c.category, c.method) == ("devops-investigation", "model")
    assert c.model == "fake-model"  # MODEL_NAME is the name in records, whatever LLM_MODEL says
    assert (result.hints.cluster, result.hints.namespace) == ("my-cluster", "payments")
    assert result.warnings == []
    assert fake_server.classify_calls() == 1 and fake_server.hint_calls() == 1


async def test_health_asks_models(any_engine: TriageEngine, fake_server: Any) -> None:
    health = await any_engine.health()
    assert health.ok, health.detail
    assert fake_server.paths == [f"GET {fake_server.prefix}/models"]
    assert expected_model(fake_server) in (health.detail or "")
    fake_server.status = 503
    health = await any_engine.health()
    assert health.ok is False and "HTTP 503" in (health.detail or "")


async def test_no_api_key_no_header(
    any_engine: TriageEngine, fake_server: Any, workspace: WorkspaceInfo
) -> None:
    await any_engine.triage(req("add a flag to src/cli.py", workspace))
    await any_engine.health()
    assert fake_server.auth and set(fake_server.auth) == {None}


async def test_api_key_is_sent_as_bearer(fake_server: Any, workspace: WorkspaceInfo) -> None:
    fake_server.api_key = "test-key-0123456789"
    engine = TriageEngine(fake_server.settings(llm_api_key="test-key-0123456789"))
    try:
        assert (await engine.health()).ok
        result = await engine.triage(req("the pods crash on my-cluster -n payments", workspace))
    finally:
        await engine.aclose()
    assert result.classification.method == "model" and result.warnings == []
    assert len(fake_server.auth) == 3 and set(fake_server.auth) == {"Bearer test-key-0123456789"}


async def test_wrong_api_key(fake_server: Any, workspace: WorkspaceInfo) -> None:
    fake_server.api_key = "test-key-0123456789"
    engine = TriageEngine(fake_server.settings(llm_api_key="wrong"))
    try:
        health = await engine.health()
        result = await engine.triage(req("pods crash in CrashLoopBackOff on prod-eu -n web", workspace))
    finally:
        await engine.aclose()
    assert health.ok is False and "LLM_API_KEY" in (health.detail or "")
    assert result.classification.method == "rules"
    assert "HTTP 401 (check LLM_API_KEY)" in result.warnings[0]
    assert all("test-key" not in w and "wrong" not in w for w in result.warnings)


async def test_timeout_falls_back_and_says_the_model_may_be_starting(
    fake_server: Any, workspace: WorkspaceInfo
) -> None:
    fake_server.delay_s = 2.0  # like a model scaled to zero (KubeAI minReplicas 0)
    engine = TriageEngine(fake_server.settings(timeout_s=0.5))
    started = time.monotonic()
    try:
        result = await engine.triage(req("add unit tests for src/parser.py", workspace))
    finally:
        await engine.aclose()
    assert time.monotonic() - started < 1.5
    assert (result.classification.category, result.classification.method) == ("engineering-task", "rules")
    assert result.warnings == [
        "decision model unavailable (no answer within 0.5 s; the model may be starting); keyword rules used"
    ]
    assert result.hints.files == ["src/parser.py"]


async def test_hint_timeout_with_a_workflow_override(fake_server: Any, workspace: WorkspaceInfo) -> None:
    fake_server.delay_s = 2.0
    engine = TriageEngine(fake_server.settings(timeout_s=0.5))
    try:
        result = await engine.triage(
            TriageRequest.model_validate(
                {
                    "request": "add unit tests",
                    "workspace": workspace.model_dump(),
                    "overrides": {"workflow": "engineering-task"},
                }
            )
        )
    finally:
        await engine.aclose()
    assert result.classification.method == "override"
    assert len(result.warnings) == 1 and "the model may be starting" in result.warnings[0]
    assert result.warnings[0].startswith("hint model")


async def test_kubeai_unknown_model_is_a_404(fake_ollama: Any, workspace: WorkspaceInfo) -> None:
    engine = TriageEngine(fake_ollama.settings(llm_model="no-such-model"))
    try:
        result = await engine.triage(req("pods crash in CrashLoopBackOff on prod-eu -n web", workspace))
    finally:
        await engine.aclose()
    assert result.classification.method == "rules"
    assert "HTTP 404 (check LLM_BASE_URL and LLM_MODEL)" in result.warnings[0]


async def test_server_without_logprobs(fake_ollama: Any) -> None:
    fake_ollama.top_logprobs = []  # an old Ollama: no log-probs in the answer
    engine = TriageEngine(fake_ollama.settings())
    try:
        with pytest.raises(eng.ModelUnavailable, match="must support logprobs"):
            await engine.classify_with_model("add a flag")
    finally:
        await engine.aclose()


# ---- tolerant parsing ---------------------------------------------------------------------------


def _answer(logprobs: Any) -> dict[str, Any]:
    return {"choices": [{"message": {"content": "B"}, "logprobs": logprobs}]}


def test_top_logprobs_shapes() -> None:
    ollama = {
        "content": [{"token": " B", "logprob": -0.1, "top_logprobs": [{"token": " B", "logprob": -0.1}]}]
    }
    assert eng.top_logprobs_of(_answer(ollama)) == [{"token": " B", "logprob": -0.1}]
    only_chosen = {"content": [{"token": "C", "logprob": -0.2}]}
    assert eng.top_logprobs_of(_answer(only_chosen)) == [{"token": "C", "logprob": -0.2}]
    legacy = {"top_logprobs": [{"A": -0.3, " B": -2.0}]}
    assert eng.top_logprobs_of(_answer(legacy)) == [
        {"token": "A", "logprob": -0.3},
        {"token": " B", "logprob": -2.0},
    ]
    for bad in (None, {}, {"content": []}, {"content": None}, "x"):
        with pytest.raises(eng.ModelUnavailable):
            eng.top_logprobs_of(_answer(bad))
    with pytest.raises(eng.ModelUnavailable):
        eng.top_logprobs_of({"choices": []})


def test_raw_tokenizer_spaces_are_stripped() -> None:
    scores = eng.letter_scores([{"token": "▁C", "logprob": -0.01}, {"token": "ĠA", "logprob": -5.0}])
    assert scores["engineering-task"] > 0.99


def test_hint_answer_parsing() -> None:
    draft = {
        "search_queries": [],
        "tags": [],
        "cluster": None,
        "namespace": "web",
        "workload": None,
        "files": [],
        "grep": [],
        "missing_info": [],
    }

    def answer(content: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    assert eng.hint_draft_of(answer(json.dumps(draft))).namespace == "web"
    assert eng.hint_draft_of(answer("```json\n" + json.dumps(draft) + "\n```")).namespace == "web"
    cases = {
        "not valid JSON": answer('{"search_queries": ['),
        "not a JSON object": answer("[1]"),
        "does not match": answer(json.dumps({**draft, "tags": ["a", "b", "c", "d"]})),  # over the cap
        "is empty": answer(None),
        "has no message": {"choices": []},
    }
    for message, body in cases.items():
        with pytest.raises(eng.ModelUnavailable, match=message):
            eng.hint_draft_of(body)
