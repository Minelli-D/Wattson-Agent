"""A fake OpenAI-compatible model server on a free port, in two flavours. No model runs in the tests.

- "llama.cpp": the sidecar. Paths under /v1, any model name, tokens with ids and bytes, a model
  list with the GGUF path as id.
- "ollama": Ollama behind KubeAI. Paths under /openai/v1, only the KubeAI Model name is known
  (else 404), tokens with a leading space and no id, and any field outside the OpenAI API (such
  as llama.cpp's cache_prompt or json_schema) is refused with 400.

Both answer POST {base}/chat/completions (log-probs, the hints when "response_format" asks for a
JSON schema, the procedure when that schema is named "pick") and GET {base}/models. With `api_key`
set, a bearer is required (401).
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wattson.agents.triage.engine import TriageEngine
from wattson.agents.triage.settings import Settings
from wattson.shared.contracts import WorkspaceInfo

TOKEN = "t" * 40
CONTEXTS = ["my-cluster", "prod-eu", "kind-dev"]
KUBEAI_MODEL = "granite-4-h-1b"
# The fields of the OpenAI chat API the engine may send; the Ollama-like fake refuses the rest.
OPENAI_FIELDS = {
    "model",
    "messages",
    "temperature",
    "max_tokens",
    "logprobs",
    "top_logprobs",
    "response_format",
    "stream",
    "seed",
    "stop",
    "top_p",
    # The think-step switch for Granite 4.2 (--reasoning on). llama.cpp and vLLM read it; Ollama
    # ignores an unknown field. Sent on every request (classify, hints, pick).
    "chat_template_kwargs",
}


def top(**letters: float) -> list[dict[str, Any]]:
    """Top log-probs like llama.cpp sends them: {"id", "token", "logprob", "bytes"}."""
    return [
        {"id": i, "token": k.replace("_", " "), "logprob": v, "bytes": []}
        for i, (k, v) in enumerate(letters.items())
    ]


@dataclass
class FakeServer:
    flavour: str = "llama.cpp"
    prefix: str = "/v1"
    model: str | None = None  # "ollama": the only model it knows
    api_key: str = ""  # when set, "Authorization: Bearer <api_key>" is required
    top_logprobs: list[dict[str, Any]] = field(default_factory=lambda: top(B=-0.05, C=-3.2, A=-5.0, D=-6.0))
    hints: dict[str, Any] | str = field(
        default_factory=lambda: {
            "search_queries": ["payments crashloop"],
            "tags": ["kubernetes"],
            "cluster": "my-cluster",
            "namespace": "payments",
            "workload": "deployment/checkout",
            "files": [],
            "grep": ["CrashLoopBackOff"],
            "missing_info": [],
        }
    )
    pick: dict[str, Any] | str = field(default_factory=lambda: {"procedure": "none"})
    pick_delay_s: float = 0.0  # only the procedure call is slow
    delay_s: float = 0.0
    status: int = 200
    requests: list[dict[str, Any]] = field(default_factory=list)  # the POST bodies
    paths: list[str] = field(default_factory=list)  # "GET /v1/models", "POST /v1/chat/completions"
    auth: list[str | None] = field(default_factory=list)  # the Authorization header of each request
    url: str = ""  # http://127.0.0.1:<port>, without the prefix

    @property
    def base_url(self) -> str:
        return self.url + self.prefix

    def classify_calls(self) -> int:
        return sum(1 for r in self.requests if r.get("logprobs"))

    def hint_calls(self) -> int:
        return sum(1 for r in self.requests if "response_format" in r and not _is_pick(r))

    def pick_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if _is_pick(r)]

    def settings(self, **kw: Any) -> Settings:
        """Settings as the pod would have them: the sidecar by LLAMA_URL only (LLM_BASE_URL
        derived), KubeAI by LLM_BASE_URL and LLM_MODEL."""
        if self.flavour == "ollama":
            kw = {"llm_base_url": self.base_url, "llm_model": self.model, **kw}
        return settings_for(self.url, **kw)

    def models(self) -> dict[str, Any]:
        if self.flavour == "ollama":
            return {"object": "list", "data": [{"id": self.model, "object": "model", "owned_by": "kubeai"}]}
        return {
            "object": "list",
            "data": [{"id": "/models/fake.gguf", "object": "model", "owned_by": "llamacpp", "meta": {}}],
        }

    def logprob_items(self) -> list[dict[str, Any]]:
        if self.flavour == "ollama":
            return [
                {"token": " " + str(t["token"]).strip(), "logprob": t["logprob"], "bytes": []}
                for t in self.top_logprobs
            ]
        return self.top_logprobs


def _is_pick(body: dict[str, Any]) -> bool:
    rf = body.get("response_format")
    return isinstance(rf, dict) and (rf.get("json_schema") or {}).get("name") == "pick"


def _error(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}).encode()


def _completion(body: dict[str, Any], content: str, logprobs: Any, finish: str) -> bytes:
    answer = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model"),
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
                "logprobs": logprobs,
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 1, "total_tokens": 201},
    }
    return json.dumps(answer).encode()


def _bad_response_format(body: dict[str, Any]) -> str | None:
    rf = body["response_format"]
    if not isinstance(rf, dict) or rf.get("type") != "json_schema":
        return "response_format.type must be json_schema"
    js = rf.get("json_schema")
    if (
        not isinstance(js, dict)
        or not isinstance(js.get("name"), str)
        or not isinstance(js.get("schema"), dict)
    ):
        return "response_format.json_schema needs a name and a schema"
    return None


def _handler(fake: FakeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization")
            fake.auth.append(sent)
            if fake.api_key and sent != f"Bearer {fake.api_key}":
                self._send(401, _error("invalid api key"))
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            fake.paths.append(f"GET {self.path}")
            if not self._authorized():
                return
            if self.path != f"{fake.prefix}/models":
                self._send(404, _error("not found"))
            elif fake.status != 200:
                self._send(fake.status, _error("loading model"))
            else:
                self._send(200, json.dumps(fake.models()).encode())

        def do_POST(self) -> None:  # noqa: N802
            fake.paths.append(f"POST {self.path}")
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            fake.requests.append(body)
            if not self._authorized():
                return
            if self.path != f"{fake.prefix}/chat/completions":
                self._send(404, _error("not found"))
                return
            if fake.delay_s:
                time.sleep(fake.delay_s)
            if fake.status != 200:
                self._send(fake.status, _error("loading model"))
                return
            if fake.flavour == "ollama":
                unknown = sorted(set(body) - OPENAI_FIELDS)
                if unknown:
                    self._send(400, _error(f"unknown fields {unknown}"))
                    return
                if body.get("model") != fake.model:
                    self._send(404, _error(f"model {body.get('model')!r} not found"))
                    return
            if "response_format" in body:
                problem = _bad_response_format(body)
                if problem:
                    self._send(400, _error(problem))
                    return
                answer = fake.pick if _is_pick(body) else fake.hints
                if _is_pick(body) and fake.pick_delay_s:
                    time.sleep(fake.pick_delay_s)
                text = answer if isinstance(answer, str) else json.dumps(answer)
                self._send(200, _completion(body, text, None, "stop"))
                return
            items = fake.logprob_items()
            if not body.get("logprobs") or not items:
                self._send(200, _completion(body, "B", None, "length"))
                return
            first = items[0]
            logprobs = {"content": [{**first, "top_logprobs": items[: body.get("top_logprobs", 0)]}]}
            self._send(200, _completion(body, first["token"], logprobs, "length"))

    return Handler


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # The client gave up (a timeout test): the late answer goes nowhere. Printing it from a
        # daemon thread can hit interpreter shutdown and crash the whole run.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve(fake: FakeServer) -> Iterator[FakeServer]:
    server = _QuietServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_llama() -> Iterator[FakeServer]:
    """The llama.cpp sidecar, as the pod has it today."""
    yield from serve(FakeServer())


@pytest.fixture
def fake_ollama() -> Iterator[FakeServer]:
    """Ollama behind KubeAI."""
    yield from serve(FakeServer(flavour="ollama", prefix="/openai/v1", model=KUBEAI_MODEL))


@pytest.fixture(params=["llama.cpp", "ollama"])
def fake_server(request: pytest.FixtureRequest) -> Iterator[FakeServer]:
    """Each flavour in turn: the same test against both servers."""
    if request.param == "ollama":
        yield from serve(FakeServer(flavour="ollama", prefix="/openai/v1", model=KUBEAI_MODEL))
    else:
        yield from serve(FakeServer())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def settings_for(url: str, **kw: Any) -> Settings:
    base: dict[str, Any] = {"token": TOKEN, "llama_url": url, "model_name": "fake-model", "timeout_s": 5.0}
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def workspace() -> WorkspaceInfo:
    return WorkspaceInfo(cwd_name="shop", kube_contexts=CONTEXTS, current_kube_context="kind-dev")


@pytest.fixture
async def engine(fake_llama: FakeServer) -> Any:
    eng = TriageEngine(settings_for(fake_llama.url))
    yield eng
    await eng.aclose()


@pytest.fixture
def make_settings() -> Any:
    """settings_for as a fixture: test files do not import conftest (other parts have one too)."""
    return settings_for
