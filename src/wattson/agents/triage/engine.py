"""The triage engine: classification by log-probs, hints by JSON-schema output, keyword rules as
the fallback (docs/design.md 10.2), and the pick of one Wattson procedure (procedures.py). Advice
only (FR-DEC-06): nothing here grants anything.

The model server is any OpenAI-compatible one (the llama.cpp sidecar, Ollama behind KubeAI,
vLLM): plain POST {LLM_BASE_URL}/chat/completions calls (two, three with procedures) and GET
{LLM_BASE_URL}/models for the health. No server-specific parameter is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from wattson.shared import telemetry
from wattson.shared.contracts import (
    Classification,
    ComponentHealth,
    DecisionHints,
    Overrides,
    ProcedureOption,
    ProcedurePick,
    TriageRequest,
    TriageResult,
    WorkspaceInfo,
)

from . import procedures as proc
from . import rules
from .settings import Settings

log = logging.getLogger(__name__)

LETTERS = {"A": "prompt-optimization", "B": "devops-investigation", "C": "engineering-task", "D": "unclear"}
MISSING_LOGPROB = -30.0  # a letter not in the top log-probs: practically zero
SPACE_MARKS = "\u2581\u0120"  # SentencePiece and byte-level BPE spaces, if a server sends raw tokens
MAX_REQUEST_CHARS = 4000
# A hard stop for the hint JSON: about 7 s at the 1.5B model's 2-CPU speed. A cut JSON = rules.
HINT_MAX_TOKENS = 120
# Production Granite 4.2 runs with --reasoning on (it thinks by default). Every triage request
# turns the think step off per request: classify needs the FIRST token to be the letter (not a
# think token), and the hint/pick JSON must not be eaten by a think preamble. Sent in the body as
# chat_template_kwargs; llama.cpp and vLLM both read it, other servers ignore an unknown key.
THINKING_OFF: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}

CLASSIFY_SYSTEM = """You sort the requests an engineer types into a command-line assistant.
Choose exactly one category:
A = prompt-optimization: rewrite or improve a prompt or a task text for a coding agent such as \
Claude Code. The request gives the text to improve. If the text talks about Kubernetes or code, \
the wish is still to improve the text, so the answer is A.
B = devops-investigation: investigate or check a problem in Kubernetes, a deploy, CI runners or \
pipelines, or infrastructure (pods, Helm releases, ingress, certificates, DNS, Terraform, AWS).
C = engineering-task: change a code repository: a feature, a bug fix in code, a refactor, tests, \
docs, or writing Dockerfiles, Helm charts or Terraform modules as code.
D = unclear: no concrete task can be read from the request: one or two words, "fix it", a \
reference to something said before, a wish without a subject. Choose D when you would have to guess.
Answer with one letter: A, B, C or D."""

# Few-shot turns: they sit in the cached prompt prefix, so they cost almost nothing per request.
# Written for the prompt, not taken from eval/ (interleaved so no letter comes last by habit).
CLASSIFY_EXAMPLES = [
    (
        "Make this prompt clearer for Claude Code: 'check the api, it is slow, maybe the db, fix it'",
        "A",
    ),
    ("pods of the search service are in CrashLoopBackOff on prod since the last deploy", "B"),
    ("add a /healthz endpoint to the FastAPI app and a test for it", "C"),
    ("help me", "D"),
    ("helm upgrade of the billing release fails with a timeout, can you check?", "B"),
    ("write a helm chart template for a CronJob that cleans old reports", "C"),
    ("rewrite better this task for the agent: investigate why the worker pods restart in ns batch", "A"),
    ("terraform plan shows drift on the S3 bucket policy, find out what changed", "B"),
    ("fix the TypeError in src/utils/format.ts when the price is null", "C"),
    ("any idea?", "D"),
    ("fix it", "D"),
    (
        "Improve the wording of these instructions for the coding agent: refactor the auth module",
        "A",
    ),
    ("the CI runner on the cluster does not pick up jobs, pipelines stay pending", "B"),
    ("update the README with the new setup steps", "C"),
    ("please handle it", "D"),
    ("ok now the other one", "D"),
]

HINTS_SYSTEM = """You read a request an engineer typed into a command-line assistant and fill a \
JSON object with hints. Use only what the request says. Never invent names: use null or [] when \
the request does not say. Answer with compact JSON on one line.
search_queries: short queries to find runbooks or notes about the request. tags: topic words. \
cluster: a kube context named in the request, only from the given list. namespace: the Kubernetes \
namespace named. workload: the workload named, as kind/name. files: repository paths named. \
grep: error messages or identifiers quoted in the request. missing_info: what the engineer must \
still say before work can start."""

HINT_EXAMPLES = [
    (
        "Kube contexts: alpha, beta-eu\nRequest: the gateway pods in namespace edge on beta-eu fail with "
        '"connection refused" after the helm upgrade',
        '{"search_queries":["gateway connection refused after helm upgrade"],"tags":["kubernetes","helm"],'
        '"cluster":"beta-eu","namespace":"edge","workload":null,"files":[],"grep":["connection refused"],'
        '"missing_info":[]}',
    ),
    (
        "Kube contexts: alpha, beta-eu\nRequest: add retries to the HTTP client in pkg/http/client.go and "
        "update docs/http.md",
        '{"search_queries":["http client retries"],"tags":["go"],"cluster":null,"namespace":null,'
        '"workload":null,"files":["pkg/http/client.go","docs/http.md"],"grep":[],"missing_info":[]}',
    ),
    (
        "Kube contexts: alpha, beta-eu\nRequest: statefulset/kafka is not ready, check it",
        '{"search_queries":["kafka statefulset not ready"],"tags":["kubernetes","kafka"],"cluster":null,'
        '"namespace":null,"workload":"statefulset/kafka","files":[],"grep":[],'
        '"missing_info":["the cluster","the namespace"]}',
    ),
]

_Query = Annotated[str, StringConstraints(max_length=50)]
_Tag = Annotated[str, StringConstraints(max_length=16)]
_Path = Annotated[str, StringConstraints(max_length=100)]
_Text = Annotated[str, StringConstraints(max_length=80)]
_Info = Annotated[str, StringConstraints(max_length=40)]
_Name = Annotated[str, StringConstraints(max_length=63)]
_Workload = Annotated[str, StringConstraints(max_length=80)]


class HintDraft(BaseModel):
    """What the model fills: DecisionHints without recommended_next. Every field is required in the
    schema so the grammar makes the model write each one (null or [] when the request says nothing);
    the caps keep the generation short."""

    model_config = ConfigDict(extra="ignore")

    search_queries: list[_Query] = Field(max_length=2)
    tags: list[_Tag] = Field(max_length=3)
    cluster: _Name | None
    namespace: _Name | None
    workload: _Workload | None
    files: list[_Path] = Field(max_length=3)
    grep: list[_Text] = Field(max_length=2)
    missing_info: list[_Info] = Field(max_length=2)


class ModelUnavailable(Exception):
    """The model server could not answer: down, timed out, or answered something unusable."""


HINT_SCHEMA_NAME = "decision_hints"


def hint_response_format() -> dict[str, Any]:
    """The OpenAI "response_format" for the hints: llama.cpp server and Ollama both turn the
    schema into a grammar; vLLM into guided decoding. No extra key: every field is required."""
    schema = HintDraft.model_json_schema()
    schema["additionalProperties"] = False
    return {
        "type": "json_schema",
        "json_schema": {"name": HINT_SCHEMA_NAME, "schema": schema, "strict": True},
    }


def no_answer(seconds: float) -> str:
    """A timeout. A model server scaled to zero (KubeAI minReplicas 0) takes long on the first call."""
    return f"no answer within {seconds:g} s; the model may be starting"


_STATUS_HINTS = {
    401: "check LLM_API_KEY",
    403: "check LLM_API_KEY",
    404: "check LLM_BASE_URL and LLM_MODEL",
    503: "the model may be loading or starting",
}


CLASSIFY_SPAN = "wattson.model.classify"  # spans: the model name, ok, the decision; no text
HINTS_SPAN = "wattson.model.hints"


def http_problem(status: int) -> str:
    hint = _STATUS_HINTS.get(status)
    return f"model server answered HTTP {status}" + (f" ({hint})" if hint else "")


@dataclass
class ModelVerdict:
    category: str
    confidence: float
    scores: dict[str, float]
    latency_ms: int


@dataclass
class Trace:
    """What happened inside one triage: for eval/ and for tests. Not part of the contract."""

    model_verdict: ModelVerdict | None = None
    rules_verdict: rules.RulesVerdict | None = None
    model_hints: HintDraft | None = None
    hints_source: str = "rules"  # "model+rules" or "rules"
    model_pick: str | None = None  # the procedure name the model answered, or "none"
    pick_scores: dict[str, int] = field(default_factory=dict)  # signal words per procedure
    timings_ms: dict[str, int] = field(default_factory=dict)


# ---- pure helpers ---------------------------------------------------------------------------


def letter_scores(top_logprobs: list[dict[str, Any]], temperature: float = 1.0) -> dict[str, float]:
    """Softmax over the four category letters, from the top log-probs of the first answer token.

    Tokens are matched after stripping spaces ("A" and " A" are the same answer; their
    probabilities add up; so is a raw tokenizer space mark). A letter that is not in the list gets
    MISSING_LOGPROB. `temperature` > 1 flattens, < 1 sharpens: the calibration knob (FR-DEC-05)."""
    mass: dict[str, float] = {}
    for item in top_logprobs:
        token = str(item.get("token", "")).strip().lstrip(SPACE_MARKS).strip().upper()
        lp = item.get("logprob")
        if token in LETTERS and isinstance(lp, (int, float)) and math.isfinite(lp):
            mass[token] = mass.get(token, 0.0) + math.exp(lp)
    logits = {
        k: (math.log(mass[k]) if mass.get(k, 0.0) > 0 else MISSING_LOGPROB) / temperature for k in LETTERS
    }
    top = max(logits.values())
    exp = {k: math.exp(v - top) for k, v in logits.items()}
    total = sum(exp.values())
    return {LETTERS[k]: exp[k] / total for k in LETTERS}


def top_logprobs_of(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The top log-probs of the first generated token, from an OpenAI-style answer (llama.cpp,
    Ollama and vLLM all send it): choices[0].logprobs.content[0].top_logprobs = [{"token",
    "logprob", ...}, ...]. Also read: the legacy shape logprobs.top_logprobs[0] = {token: logprob}."""
    try:
        logprobs = body["choices"][0]["logprobs"]
    except (KeyError, IndexError, TypeError):
        logprobs = None
    if isinstance(logprobs, dict):
        content = logprobs.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            first = content[0]
            tops = first.get("top_logprobs") or []
            if not tops and "token" in first:
                tops = [first]
            return [t for t in tops if isinstance(t, dict)]
        legacy = logprobs.get("top_logprobs")
        if isinstance(legacy, list) and legacy and isinstance(legacy[0], dict):
            return [{"token": token, "logprob": lp} for token, lp in legacy[0].items()]
    raise ModelUnavailable("the answer has no log-probs; the server must support logprobs")


def hint_draft_of(body: dict[str, Any]) -> HintDraft:
    """The HintDraft in choices[0].message.content, validated with its caps. A JSON fence around
    it is tolerated. Anything else is ModelUnavailable (the rules give the hints)."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ModelUnavailable("the hint answer has no message") from None
    if not isinstance(content, str) or not content.strip():
        raise ModelUnavailable("the hint answer is empty")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip().removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise ModelUnavailable("the hint answer is not valid JSON") from None
    if not isinstance(data, dict):
        raise ModelUnavailable("the hint answer is not a JSON object")
    try:
        return HintDraft.model_validate(data)
    except ValidationError:
        raise ModelUnavailable("the hint answer does not match the schema") from None


_TARGET_WORDS = re.compile(r"\b(clusters?|namespaces?|kube|context|workloads?|deployments?|pods?)\b", re.I)
_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,30}$")


def _grounded(value: str, text: str) -> bool:
    return value.lower() in text.lower()


def sanitize_cluster(value: str | None, workspace: WorkspaceInfo, text: str | None = None) -> str | None:
    """A kube context from the workspace (case-insensitive, returned as the workspace writes it),
    named in the request when `text` is given. Anything else is dropped."""
    if not value:
        return None
    for ctx in workspace.kube_contexts:
        if ctx.lower() == value.strip().lower():
            return ctx if text is None or _grounded(ctx, text) else None
    return None


def sanitize_namespace(value: str | None, text: str | None = None) -> str | None:
    if not value:
        return None
    ns = value.strip().lower()
    if not rules.RFC1123_LABEL.match(ns) or ns in rules.NOT_NAMES:
        return None
    return ns if text is None or _grounded(ns, text) else None


def sanitize_workload(value: str | None, text: str | None = None) -> str | None:
    """kind/name with a known kind (aliases like deploy, sts are mapped) and a Kubernetes name."""
    if not value or "/" not in value:
        return None
    kind, _, name = value.strip().lower().partition("/")
    kind = rules.WORKLOAD_KINDS.get(kind, "")
    if not kind or not _K8S_NAME.match(name) or name in rules.NOT_WORKLOAD_NAMES:
        return None
    if text is not None and not _grounded(name, text):
        return None
    return f"{kind}/{name}"


def sanitize_files(values: list[str], text: str | None = None, limit: int = 8) -> list[str]:
    """Relative repository paths only: no absolute paths, no "~", no "..", no URLs, no backslashes."""
    out: list[str] = []
    for raw in values:
        path = raw.strip().strip("'\"`").removeprefix("./")
        if (
            not path
            or len(path) > 200
            or path.startswith(("/", "~", "-"))
            or "\\" in path
            or "://" in path
            or ".." in path.split("/")
            or any(c.isspace() for c in path)
        ):
            continue
        if text is not None and not _grounded(path, text):
            continue
        if path not in out:
            out.append(path)
    return out[:limit]


def _clean_list(values: list[str], max_len: int, limit: int, text: str | None = None) -> list[str]:
    out: list[str] = []
    for raw in values:
        value = " ".join(str(raw).split())[:max_len]
        if len(value) < 2:
            continue
        if text is not None and not _grounded(value, text):
            continue
        if value.lower() not in (o.lower() for o in out):
            out.append(value)
    return out[:limit]


def sanitize_tags(values: list[str], limit: int = 8) -> list[str]:
    out: list[str] = []
    for raw in values:
        tag = raw.strip().lower().replace(" ", "-")
        if _TAG.match(tag) and tag not in out:
            out.append(tag)
    return out[:limit]


def _union(first: list[str], second: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for value in [*first, *second]:
        if value.lower() not in (o.lower() for o in out):
            out.append(value)
    return out[:limit]


def merge_hints(draft: HintDraft | None, found: rules.RulesHints, req: TriageRequest) -> DecisionHints:
    """Sanitized model hints first; the rules fill what the model left empty. Every name must be
    in the request text (and a cluster in the workspace): a hint is never invented."""
    text, ws = req.request, req.workspace
    if draft is None:
        draft = HintDraft(
            search_queries=[],
            tags=[],
            cluster=None,
            namespace=None,
            workload=None,
            files=[],
            grep=[],
            missing_info=[],
        )
    return DecisionHints(
        search_queries=_union(_clean_list(draft.search_queries, 100, 3), found.search_queries, 3),
        tags=_union(found.tags, sanitize_tags(draft.tags), 8),
        cluster=sanitize_cluster(draft.cluster, ws, text) or sanitize_cluster(found.cluster, ws),
        namespace=sanitize_namespace(draft.namespace, text) or sanitize_namespace(found.namespace),
        workload=sanitize_workload(draft.workload, text) or sanitize_workload(found.workload),
        files=_union(sanitize_files(draft.files, text), sanitize_files(found.files), 8),
        grep=_union(_clean_list(draft.grep, 200, 5, text), _clean_list(found.grep, 200, 5), 5),
        missing_info=_clean_list(draft.missing_info, 160, 5),
    )


def apply_overrides(hints: DecisionHints, overrides: Overrides, workspace: WorkspaceInfo) -> list[str]:
    """The user's explicit choices win over any hint. Returns warnings."""
    warnings: list[str] = []
    if overrides.cluster:
        cluster = sanitize_cluster(overrides.cluster, workspace)
        if cluster:
            hints.cluster = cluster
        else:
            warnings.append(f"cluster {overrides.cluster!r} is not a kube context of this workspace; ignored")
    if overrides.namespace:
        ns = sanitize_namespace(overrides.namespace)
        if ns:
            hints.namespace = ns
        else:
            warnings.append(f"namespace {overrides.namespace!r} is not a valid namespace name; ignored")
    if overrides.workload:
        wl = sanitize_workload(overrides.workload)
        if wl:
            hints.workload = wl
        else:
            warnings.append(f"workload {overrides.workload!r} is not like deployment/name; ignored")
    return warnings


def finish_hints(
    category: str, hints: DecisionHints, workspace: WorkspaceInfo, overrides: Overrides | None = None
) -> DecisionHints:
    """The standard missing information and the recommended next step (FR-DEC-03, FR-DEC-04).
    The model's own items about cluster, namespace or workload are dropped: the standard ones below
    say exactly what is still missing, after the rules and the overrides (several namespaces, or
    all of them, count as known)."""
    missing = [m for m in hints.missing_info if not _TARGET_WORDS.search(m)]
    namespace_known = bool(
        hints.namespace or (overrides is not None and (overrides.namespaces or overrides.all_namespaces))
    )

    def need(item: str) -> None:
        if item.lower() not in (m.lower() for m in missing):
            missing.insert(0, item)

    if category == "unclear":
        need("what you want: improve a prompt, investigate a problem, or change code")
        nxt = "ask"
    elif category == "devops-investigation":
        if not namespace_known:
            need("the namespace")
        if not hints.cluster:
            need("the cluster (kube context)")
        nxt = "ask" if not (hints.cluster and namespace_known) else "collect"
    elif category == "engineering-task":
        nxt = "collect" if (workspace.repo or hints.files or hints.grep) else "propose"
    else:  # prompt-optimization: the text is all there, unless it points at files of this repo
        nxt = "collect" if (hints.files and workspace.repo) else "propose"
    hints.missing_info = missing[:5]
    hints.recommended_next = nxt  # type: ignore[assignment]
    return hints


# ---- the engine -----------------------------------------------------------------------------


class TriageEngine:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        # Paths are relative ("chat/completions"): httpx puts them after the base path ("/v1/").
        self._client = client or httpx.AsyncClient(
            base_url=f"{settings.llm_base_url}/", timeout=settings.timeout_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- model calls --

    def _headers(self) -> dict[str, str]:
        key = self.settings.llm_api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _post(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        for attempt in (1, 2):
            try:
                response = await self._client.post(path, json=body, headers=self._headers(), timeout=timeout)
                break
            except httpx.ConnectError as exc:  # connection refused: the server may be restarting
                if attempt == 2:
                    raise ModelUnavailable(f"cannot connect to {self.settings.llm_base_url}: {exc}") from exc
                await asyncio.sleep(0.5)
            except httpx.TimeoutException as exc:
                raise ModelUnavailable(no_answer(timeout)) from exc
            except httpx.HTTPError as exc:
                raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise ModelUnavailable(http_problem(response.status_code))
        try:
            body = response.json()
        except ValueError:
            raise ModelUnavailable("the model server answered no JSON") from None
        if not isinstance(body, dict):
            raise ModelUnavailable("the model server answered no JSON object")
        return body

    async def classify_with_model(self, text: str, timeout: float | None = None) -> ModelVerdict:
        """One constrained choice: max_tokens 1, top log-probs of the first token, softmax."""
        started = time.perf_counter()
        body = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": CLASSIFY_SYSTEM},
                *[
                    m
                    for example, letter in CLASSIFY_EXAMPLES
                    for m in (
                        {"role": "user", "content": f"Request: {example}\nCategory letter:"},
                        {"role": "assistant", "content": letter},
                    )
                ],
                {"role": "user", "content": f"Request: {text[:MAX_REQUEST_CHARS]}\nCategory letter:"},
            ],
            "temperature": 0,
            "max_tokens": 1,
            "logprobs": True,
            "top_logprobs": 10,
            **THINKING_OFF,
        }
        answer = await self._post("chat/completions", body, timeout or self.settings.timeout_s)
        scores = letter_scores(top_logprobs_of(answer), self.settings.confidence_temperature)
        category = max(scores, key=lambda c: scores[c])
        return ModelVerdict(
            category=category,
            confidence=scores[category],
            scores={k: round(v, 4) for k, v in scores.items()},
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def hints_with_model(self, req: TriageRequest, timeout: float | None = None) -> HintDraft:
        """JSON-schema output through the OpenAI "response_format": the server constrains the
        answer to the schema; the answer is validated again here (caps included)."""
        contexts = ", ".join(req.workspace.kube_contexts) or "none"
        request = f"Kube contexts: {contexts}\nRequest: {req.request[:MAX_REQUEST_CHARS]}"
        body = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": HINTS_SYSTEM},
                *[
                    m
                    for question, answer in HINT_EXAMPLES
                    for m in (
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": answer},
                    )
                ],
                {"role": "user", "content": request},
            ],
            "temperature": 0,
            "max_tokens": HINT_MAX_TOKENS,
            "response_format": hint_response_format(),
            **THINKING_OFF,
        }
        answer = await self._post("chat/completions", body, timeout or self.settings.timeout_s)
        return hint_draft_of(answer)

    async def pick_with_model(
        self, request: str, options: list[ProcedureOption], timeout: float | None = None
    ) -> tuple[str, int]:
        """One Wattson procedure NAME (or "none") through a JSON-schema enum, and the latency in ms.
        The names, never letters: 18/20 against 3/20 on production Granite (procedures.py)."""
        started = time.perf_counter()
        body = {"model": self.settings.llm_model, **proc.pick_body(request, options)}
        answer = await self._post("chat/completions", body, timeout or self.settings.timeout_s)
        try:
            name = proc.parse_pick(answer, options)
        except ValueError as exc:
            raise ModelUnavailable(str(exc)) from None
        return name, int((time.perf_counter() - started) * 1000)

    async def health(self) -> ComponentHealth:
        """GET {LLM_BASE_URL}/models: llama.cpp, Ollama (KubeAI) and vLLM all have it. Up = 200.
        KubeAI answers it also while the model is scaled to zero: the first triage may be slow."""
        base, model = self.settings.llm_base_url, self.settings.llm_model
        try:
            response = await self._client.get("models", headers=self._headers(), timeout=5.0)
        except httpx.HTTPError as exc:
            return ComponentHealth(
                ok=False, detail=f"model server at {base} not reachable: {type(exc).__name__}"
            )
        if response.status_code != 200:
            problem = http_problem(response.status_code)
            return ComponentHealth(ok=False, detail=f"{problem} on {base}/models (model {model})")
        return ComponentHealth(ok=True, detail=f"model server up at {base}, model {model}")

    async def warm_up(self, wait_s: float = 600.0, every_s: float = 2.0) -> bool:
        """Fill the server's prompt cache (the system prompts and few-shot turns) once the server is
        up, so the first real triage after a restart is not the slow one. Never raises."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if (await self.health()).ok:
                started = time.perf_counter()
                try:
                    request = TriageRequest(
                        request="warm-up: list the pods", workspace=WorkspaceInfo(cwd_name="-")
                    )
                    result = await self.triage(request)
                except Exception as exc:  # noqa: BLE001 - a warm-up failure is only logged
                    log.warning("warm-up failed: %s", type(exc).__name__)
                    return False
                ok = result.classification.method == "model"
                log.info(
                    "warm-up %s in %d ms",
                    "done" if ok else "fell back to rules",
                    (time.perf_counter() - started) * 1000,
                )
                return ok
            await asyncio.sleep(every_s)
        log.warning("warm-up gave up: the model server is not ready after %.0f s", wait_s)
        return False

    # -- the whole triage --

    async def triage(self, req: TriageRequest, trace: Trace | None = None) -> TriageResult:
        """Classification, hints and the procedure within settings.timeout_s; the rules and the
        signal words cover every failure.

        The model calls run at the same time: the llama.cpp sidecar has one slot for each of the
        first two (-np 2), keeps each prompt prefix cached, and batches them. A triage costs about
        the hint call. The procedure call is made only when the request lists procedures; it offers
        only the procedures of the workflow (D42), so it starts when the classification is known
        (at once when the user gave the workflow), while the hint call still runs."""
        trace = trace if trace is not None else Trace()
        limit = self.settings.timeout_s
        started = time.perf_counter()
        warnings: list[str] = []
        text = req.request
        verdict = rules.classify(text)
        trace.rules_verdict = verdict
        found = rules.extract_hints(text, req.workspace)

        async def timed(coro: Any, key: str) -> Any:
            begin = time.perf_counter()
            try:
                return await asyncio.wait_for(coro, limit)
            finally:
                trace.timings_ms[key] = int((time.perf_counter() - begin) * 1000)

        model = {"wattson.model.name": self.settings.model_name}
        hint_task = asyncio.create_task(
            telemetry.run_in_span(HINTS_SPAN, timed(self.hints_with_model(req, limit), "hints"), **model)
        )
        options = proc.usable(req.procedures)
        user_pick: ProcedurePick | None = None
        pick_task: asyncio.Task[Any] | None = None
        offered = options
        if options:
            user_pick, problem = proc.resolve_override(req.overrides.procedure, options)
            if problem:
                warnings.append(problem)
            if user_pick is None and req.overrides.workflow:
                offered = proc.for_workflow(options, req.overrides.workflow)
                pick_task = asyncio.create_task(timed(self.pick_with_model(text, offered, limit), "pick"))
        classify_start = time.time_ns()
        model_down: str | None = None
        if req.overrides.workflow:
            classification = Classification(
                category=req.overrides.workflow, confidence=1.0, method="override"
            )
        else:
            try:
                mv = await timed(self.classify_with_model(text, limit), "classify")
                trace.model_verdict = mv
                classification = self._decide(mv, verdict, warnings)
            except (ModelUnavailable, TimeoutError) as exc:
                model_down = str(exc) or no_answer(limit)
                warnings.append(f"decision model unavailable ({model_down}); keyword rules used")
                classification = Classification(
                    category=verdict.category,  # type: ignore[arg-type]
                    confidence=verdict.confidence,
                    method="rules",
                    scores={k: round(v, 3) for k, v in verdict.scores.items()},
                )
        telemetry.record_span(
            CLASSIFY_SPAN,
            {
                **model,
                # None (absent) when the model was not asked: an override.
                "wattson.model.ok": None if req.overrides.workflow else model_down is None,
                **telemetry.triage_attributes(classification),
            },
            start_ns=classify_start,
        )
        if options and user_pick is None and pick_task is None:
            offered = proc.for_workflow(options, classification.category)
            if model_down is None:
                pick_task = asyncio.create_task(timed(self.pick_with_model(text, offered, limit), "pick"))

        draft: HintDraft | None = None
        if model_down is not None:  # no second wait on a model that is known to be down or slow
            hint_task.cancel()
            await asyncio.gather(hint_task, return_exceptions=True)
        else:
            try:
                draft = await hint_task
                trace.model_hints = draft
                trace.hints_source = "model+rules"
            except TimeoutError:
                warnings.append(f"hint model unavailable ({no_answer(limit)}); keyword rules used for hints")
            except ModelUnavailable as exc:
                warnings.append(f"hint model failed ({exc}); keyword rules used for hints")
            except Exception as exc:  # noqa: BLE001 - any failure here falls back to the rules
                log.warning("hint model failed: %s", type(exc).__name__)
                warnings.append(f"hint model failed ({type(exc).__name__}); keyword rules used for hints")

        procedure: ProcedurePick | None = None
        if options:
            procedure = user_pick or await self._pick(text, offered, pick_task, model_down, warnings, trace)

        hints = merge_hints(draft, found, req)
        warnings += apply_overrides(hints, req.overrides, req.workspace)
        hints = finish_hints(classification.category, hints, req.workspace, req.overrides)
        classification.latency_ms = int((time.perf_counter() - started) * 1000)
        trace.timings_ms["total"] = classification.latency_ms
        return TriageResult(
            classification=classification, hints=hints, warnings=warnings, procedure=procedure
        )

    async def _pick(
        self,
        text: str,
        options: list[ProcedureOption],
        task: asyncio.Task[Any] | None,
        model_down: str | None,
        warnings: list[str],
        trace: Trace,
    ) -> ProcedurePick:
        """The model's answer, checked by the signal words (procedures.decide)."""
        answer: str | None = None
        error: str | None = None
        latency = 0
        if model_down is not None:  # no second wait on a model that is known to be down or slow
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            error = model_down
        elif task is None:
            error = "not asked"
        else:
            try:
                answer, latency = await task
            except TimeoutError:
                error = no_answer(self.settings.timeout_s)
            except ModelUnavailable as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - any failure here falls back to the signal words
                log.warning("procedure model failed: %s", type(exc).__name__)
                error = type(exc).__name__
        latency = latency or trace.timings_ms.get("pick", 0)
        trace.model_pick = answer
        decision = proc.decide(text, options, answer, error, latency)
        trace.pick_scores = decision.scores
        warnings += decision.warnings
        return decision.pick

    def _decide(self, mv: ModelVerdict, verdict: rules.RulesVerdict, warnings: list[str]) -> Classification:
        """Model at or above min_confidence: the model decides. Under it: strong rules decide, else
        "unclear" (the orchestrator then asks)."""
        name, minimum = self.settings.model_name, self.settings.min_confidence
        if mv.confidence >= minimum:
            category, confidence, method = mv.category, round(mv.confidence, 4), "model"
        elif verdict.strength == "strong":
            warnings.append(
                f"decision model unsure ({mv.confidence:.2f} < {minimum:.2f}); keyword rules used"
            )
            category, confidence, method = verdict.category, verdict.confidence, "rules"
        else:
            warnings.append(
                f"decision model unsure ({mv.confidence:.2f} < {minimum:.2f}, best guess {mv.category}); "
                "the workflow must be asked"
            )
            category, confidence, method = "unclear", round(mv.confidence, 4), "model"
        return Classification.model_validate(
            {
                "category": category,
                "confidence": confidence,
                "method": method,
                "model": name,
                "scores": mv.scores,
            }
        )
