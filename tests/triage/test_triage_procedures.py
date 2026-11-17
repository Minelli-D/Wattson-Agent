"""The pick of one Wattson procedure: the model chooses among NAMES (a JSON-schema enum), the signal
words of the request check it, the user's procedure wins. Against the fake OpenAI-compatible
servers; no model runs."""

from __future__ import annotations

import time
from typing import Any

import pytest

from wattson.agents.triage import procedures as proc
from wattson.agents.triage.engine import TriageEngine
from wattson.shared.contracts import Overrides, ProcedureOption, TriageRequest, WorkspaceInfo


def option(name: str, when: str, workflow: str | None, *signals: str) -> ProcedureOption:
    return ProcedureOption(
        key=f"wattson/{name}.md", name=name, when_to_use=when, workflow=workflow, signals=list(signals)
    )


OPTIONS = [
    option(
        "k8s-pod-crashloop",
        "A pod starts and then dies again and again: CrashLoopBackOff, OOMKilled or Error.",
        "devops-investigation",
        "crashloopbackoff",
        "oomkilled",
        "oom",
        "crash",
        "restart",
        "restarts",
        "liveness",
    ),
    option(
        "k8s-rollout-stuck",
        "A new version was deployed but its pods never become ready.",
        "devops-investigation",
        "rollout",
        "pending",
        "imagepullbackoff",
        "readiness",
    ),
    option(
        "k8s-tls-ingress",
        "An app cannot be reached at its address: certificate errors, or the Ingress answers 502.",
        "devops-investigation",
        "tls",
        "certificate",
        "cert-manager",
        "ingress",
        "x509",
    ),
    option(
        "k8s-storage",
        "A volume or disk problem: a PersistentVolumeClaim stays Pending, a disk is full.",
        "devops-investigation",
        "pvc",
        "persistentvolumeclaim",
        "volume",
        "disk",
        "diskpressure",
        "evicted",
    ),
    option(
        "code-bug-fix",
        "The code does something wrong: find the cause, fix it, add a test.",
        "engineering-task",
        "bug",
        "fix",
        "error",
        "failing",
    ),
    option(
        "docs-from-data",
        "Write or update documentation from real facts.",
        "engineering-task",
        "docs",
        "readme",
        "documentation",
    ),
]
NAMES = [o.name for o in OPTIONS]
STORAGE_REQUEST = "the pvc of postgres is stuck and the volume is full"


def req(
    text: str, ws: WorkspaceInfo, procedures: list[ProcedureOption] = OPTIONS, **overrides: Any
) -> TriageRequest:
    return TriageRequest(request=text, workspace=ws, overrides=Overrides(**overrides), procedures=procedures)


@pytest.fixture
async def any_engine(fake_server: Any) -> Any:
    engine = TriageEngine(fake_server.settings())
    yield engine
    await engine.aclose()


# ---- the call: names in an enum, never letters ----------------------------------------------------


async def test_the_pick_call_sends_the_names_as_an_enum(any_engine: TriageEngine, fake_server: Any) -> None:
    fake_server.pick = {"procedure": "k8s-storage"}
    name, latency = await any_engine.pick_with_model(STORAGE_REQUEST, OPTIONS)
    assert name == "k8s-storage" and latency >= 0
    (sent,) = fake_server.pick_calls()
    assert sent["model"] == (fake_server.model if fake_server.flavour == "ollama" else "fake-model")
    assert (sent["temperature"], sent["max_tokens"]) == (0, 40)
    rf = sent["response_format"]
    assert rf["type"] == "json_schema"
    assert (rf["json_schema"]["name"], rf["json_schema"]["strict"]) == ("pick", True)
    schema = rf["json_schema"]["schema"]
    assert schema["required"] == ["procedure"] and schema["additionalProperties"] is False
    enum = schema["properties"]["procedure"]["enum"]
    assert enum == [*NAMES, "none"]  # names, not letters
    assert not {"A", "B", "C", "D"} & set(enum)
    system, user = sent["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    assert system["content"].startswith(
        "You choose the one procedure that fits an engineering request.\n\nProcedures:\n"
    )
    assert (
        "\n- k8s-storage: A volume or disk problem: a PersistentVolumeClaim stays Pending"
        in system["content"]
    )
    assert system["content"].endswith("\n- none: none of the above fits")
    assert user["content"] == f"Request: {STORAGE_REQUEST}"


async def test_whole_triage_with_procedures_runs_three_calls(
    any_engine: TriageEngine, fake_server: Any, workspace: WorkspaceInfo
) -> None:
    fake_server.pick = {"procedure": "k8s-pod-crashloop"}
    result = await any_engine.triage(req("the pods in payments crash with CrashLoopBackOff", workspace))
    pick = result.procedure
    assert pick is not None
    assert (pick.key, pick.name, pick.method, pick.agreed) == (
        "wattson/k8s-pod-crashloop.md",
        "k8s-pod-crashloop",
        "model",
        True,
    )
    assert result.warnings == []
    assert fake_server.classify_calls() == 1 and fake_server.hint_calls() == 1
    assert len(fake_server.pick_calls()) == 1
    assert proc.ask_candidates(pick, "the pods in payments crash with CrashLoopBackOff", OPTIONS) == []


async def test_no_procedures_no_pick(
    any_engine: TriageEngine, fake_server: Any, workspace: WorkspaceInfo
) -> None:
    result = await any_engine.triage(req("the pods crash", workspace, procedures=[]))
    assert result.procedure is None and fake_server.pick_calls() == []


# ---- the decision ----------------------------------------------------------------------------


async def test_model_none_but_clear_signals_keep_a_name_to_ask(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "none"}
    result = await engine.triage(req(STORAGE_REQUEST, workspace))
    pick = result.procedure
    assert pick is not None
    assert (pick.key, pick.name, pick.method, pick.agreed) == (None, "k8s-storage", "none", False)
    assert proc.ask_candidates(pick, STORAGE_REQUEST, OPTIONS) == ["k8s-storage"]


async def test_model_none_and_weak_signals_mean_no_procedure(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "none"}
    result = await engine.triage(req("what time is it in london", workspace))
    pick = result.procedure
    assert pick is not None and (pick.key, pick.name, pick.method) == (None, None, "none")
    assert proc.ask_candidates(pick, "what time is it in london", OPTIONS) == []


async def test_owned_tags_win_over_a_model_pick_without_them(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "k8s-tls-ingress"}  # no tag of it; k8s-storage owns "pvc", "volume"
    result = await engine.triage(req(STORAGE_REQUEST, workspace))
    pick = result.procedure
    assert pick is not None
    assert (pick.key, pick.method, pick.agreed) == ("wattson/k8s-storage.md", "signals", False)
    assert result.warnings == [
        "procedure k8s-storage chosen by its own tags in the request; the model chose k8s-tls-ingress"
    ]
    assert proc.ask_candidates(pick, STORAGE_REQUEST, OPTIONS) == []


async def test_model_and_signals_disagree_on_a_tie_asks(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    text = "the ingress certificate is wrong and the pvc volume is full"  # two owned tags each
    fake_llama.pick = {"procedure": "k8s-pod-crashloop"}
    result = await engine.triage(req(text, workspace))
    pick = result.procedure
    assert pick is not None
    assert (pick.key, pick.method, pick.agreed) == ("wattson/k8s-pod-crashloop.md", "model", False)
    assert proc.ask_candidates(pick, text, OPTIONS) == ["k8s-pod-crashloop", "k8s-tls-ingress", "k8s-storage"]


async def test_the_pick_offers_only_the_procedures_of_the_workflow(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    def enum() -> list[str]:
        sent = fake_llama.pick_calls()[-1]
        return sent["response_format"]["json_schema"]["schema"]["properties"]["procedure"]["enum"]

    fake_llama.pick = {"procedure": "k8s-storage"}
    await engine.triage(req(STORAGE_REQUEST, workspace))  # the model says devops-investigation
    assert enum() == ["k8s-pod-crashloop", "k8s-rollout-stuck", "k8s-tls-ingress", "k8s-storage", "none"]
    fake_llama.pick = {"procedure": "code-bug-fix"}
    result = await engine.triage(req("fix the failing login test", workspace, workflow="engineering-task"))
    assert enum() == ["code-bug-fix", "docs-from-data", "none"]
    assert result.procedure is not None and result.procedure.name == "code-bug-fix"
    unclear = [{"id": 0, "token": "D", "logprob": -0.01, "bytes": []}]
    fake_llama.top_logprobs = unclear  # every procedure is offered
    await engine.triage(req("hello", workspace))
    assert enum() == [*NAMES, "none"]


async def test_model_pick_without_any_signal_is_agreed(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "k8s-pod-crashloop"}
    text = "the api deployment in web keeps going down on my-cluster"  # no signal word of any option
    result = await engine.triage(req(text, workspace))
    assert result.procedure is not None and result.procedure.agreed is True


async def test_user_procedure_wins_and_skips_the_model(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "k8s-tls-ingress"}
    result = await engine.triage(req(STORAGE_REQUEST, workspace, procedure="K8S-Storage"))
    pick = result.procedure
    assert pick is not None and (pick.key, pick.name, pick.method) == (
        "wattson/k8s-storage.md",
        "k8s-storage",
        "user",
    )
    assert fake_llama.pick_calls() == [] and result.warnings == []
    result = await engine.triage(req(STORAGE_REQUEST, workspace, procedure="none"))
    assert result.procedure is not None and (result.procedure.key, result.procedure.method) == (None, "user")
    assert proc.ask_candidates(result.procedure, STORAGE_REQUEST, OPTIONS) == []


async def test_unknown_user_procedure_is_ignored(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "k8s-storage"}
    result = await engine.triage(req(STORAGE_REQUEST, workspace, procedure="k8s-everything"))
    assert result.procedure is not None and result.procedure.method == "model"
    assert any("'k8s-everything' is not one of the Wattson procedures" in w for w in result.warnings)
    assert len(fake_llama.pick_calls()) == 1


async def test_timeout_falls_back_to_the_signal_words(fake_server: Any, workspace: WorkspaceInfo) -> None:
    fake_server.delay_s = 2.0  # every call: the classification fails, the pick call is cancelled
    engine = TriageEngine(fake_server.settings(timeout_s=0.5))
    text = "pods in CrashLoopBackOff, OOMKilled again"
    started = time.monotonic()
    try:
        result = await engine.triage(req(text, workspace))
    finally:
        await engine.aclose()
    assert time.monotonic() - started < 1.5
    pick = result.procedure
    assert pick is not None and (pick.key, pick.method, pick.agreed) == (
        "wattson/k8s-pod-crashloop.md",
        "signals",
        False,
    )
    assert result.classification.method == "rules"
    assert any(
        w.startswith("procedure model unavailable (no answer within 0.5 s") and "chosen by signal words" in w
        for w in result.warnings
    )


async def test_slow_pick_alone_falls_back(
    fake_llama: Any, make_settings: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick_delay_s = 2.0
    engine = TriageEngine(make_settings(fake_llama.url, timeout_s=0.5))
    try:
        result = await engine.triage(req("what time is it in london", workspace))
    finally:
        await engine.aclose()
    assert result.classification.method == "model"  # the other calls were fine
    pick = result.procedure
    assert pick is not None and (pick.key, pick.method) == (None, "none")
    assert result.warnings == [
        "procedure model unavailable (no answer within 0.5 s; the model may be starting); no procedure chosen"
    ]


async def test_an_answer_outside_the_names_is_refused(
    engine: TriageEngine, fake_llama: Any, workspace: WorkspaceInfo
) -> None:
    fake_llama.pick = {"procedure": "B"}  # a letter, as the old prompt asked
    result = await engine.triage(req(STORAGE_REQUEST, workspace))
    pick = result.procedure
    assert pick is not None and (pick.key, pick.method) == ("wattson/k8s-storage.md", "signals")
    assert any("not one of the procedure names" in w for w in result.warnings)


# ---- signal words and parsing ------------------------------------------------------------------------


def test_signal_words_are_the_tags_only() -> None:
    assert proc.words("Pods restarts ingresses Certificates status") >= {
        "pod",
        "restart",
        "ingress",
        "certificate",
        "status",
    }
    assert {"k8s", "lab"} <= proc.words("on k8s-lab")
    crash = proc.signal_words(OPTIONS[0])
    assert {"restart", "crashloopbackoff", "crash"} <= crash
    assert not {"pod", "crashloop", "k8s"} & crash  # the words of the name do not count
    assert proc.signal_words(OPTIONS[5]) == {"doc", "readme", "documentation"}
    scores = proc.signal_scores("the pods keep restarts, CrashLoopBackOff", OPTIONS)
    assert scores["k8s-pod-crashloop"] == 2 and list(scores) == NAMES


def test_owned_scores_count_only_the_tags_of_one_option() -> None:
    shared = [*OPTIONS[:2], option("k8s-node-health", "x", "devops-investigation", "restart", "node")]
    owned = proc.owned_scores("the node keeps restarts, CrashLoopBackOff", shared)
    assert owned == {"k8s-pod-crashloop": 1, "k8s-rollout-stuck": 0, "k8s-node-health": 1}
    assert proc.clear_best(owned, least=1) is None  # a tie: no owner


def test_for_workflow() -> None:
    def names(options: list[ProcedureOption]) -> list[str]:
        return [o.name for o in options]

    assert names(proc.for_workflow(OPTIONS, "devops-investigation")) == NAMES[:4]
    assert names(proc.for_workflow(OPTIONS, "engineering-task")) == NAMES[4:]
    for unknown in ("unclear", None, "prompt-optimization"):  # none of its own: all of them
        assert proc.for_workflow(OPTIONS, unknown) == OPTIONS
    loose = option("k8s-loose", "x", None, "loose")
    assert names(proc.for_workflow([*OPTIONS, loose], "engineering-task")) == [*NAMES[4:], "k8s-loose"]


def test_clear_best_needs_two_and_no_tie() -> None:
    assert proc.clear_best({"a": 2, "b": 1}) == "a"
    assert proc.clear_best({"a": 1, "b": 0}) is None
    assert proc.clear_best({"a": 2, "b": 2}) is None
    assert proc.clear_best({}) is None


def test_parse_pick() -> None:
    def answer(content: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    assert proc.parse_pick(answer('{"procedure": "k8s-storage"}'), OPTIONS) == "k8s-storage"
    assert proc.parse_pick(answer('```json\n{"procedure": "None"}\n```'), OPTIONS) == "none"
    for bad, message in (
        (answer('{"procedure": "C"}'), "not one of"),
        (answer('{"procedure": 3}'), "no procedure"),
        (answer("k8s-storage"), "not valid JSON"),
        (answer(None), "empty"),
        ({"choices": []}, "no message"),
    ):
        with pytest.raises(ValueError, match=message):
            proc.parse_pick(bad, OPTIONS)


def test_override_by_key_and_none_name_options() -> None:
    pick, problem = proc.resolve_override("wattson/k8s-storage.md", OPTIONS)
    assert pick is not None and pick.name == "k8s-storage" and problem is None
    odd = [*OPTIONS, option("none", "x", "engineering-task"), OPTIONS[0]]
    assert [o.name for o in proc.usable(odd)] == NAMES  # no "none" option, no duplicate
