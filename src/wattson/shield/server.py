"""wattson-shield: a small prompt-injection classifier behind one HTTP route.

  POST /v1/classify  {"parts": ["...", ...]}  ->  {"scores": [0.01, 0.97, ...]}   (bearer token)
  GET  /v1/health    ->  {"ok": true, "label": ..., "window": ..., "threads": ...}  (bearer token)
  GET  /healthz      ->  {"ok": true}                                             (no token: probes)

A score is the model's probability that a part holds orders to an AI agent: the highest over
the overlapping windows of a long part. The shield returns numbers only: the text of a part
never reaches a log or an answer. The Guard in the orchestrator decides what a score means
(INJECTION_THRESHOLD), and goes on with its rules alone when the shield does not answer.

The model files (an ONNX file, its tokenizer.json and config.json) are downloaded and checked
(SHA-256) by the init container of the pod; this program only reads them. It imports nothing
of wattson: Dockerfile.shield copies this one file. The windows are the ones measured by
eval/injection_model.py (docs/study/guardrails.md).

Settings (environment):
  SHIELD_TOKEN or SHIELD_TOKEN_FILE   the bearer token, at least 32 signs
  SHIELD_LISTEN_HOST, SHIELD_PORT     127.0.0.1 and 9003 when not set
  SHIELD_MODEL_PATH                   /models/model.onnx
  SHIELD_TOKENIZER_PATH               /models/tokenizer.json
  SHIELD_CONFIG_PATH                  /models/config.json
  INJECTION_LABEL                     the label of config.json's id2label that means injection (any case)
  INJECTION_WINDOW                    tokens per window (2048)
  SHIELD_THREADS                      CPU threads of one inference (2)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("wattson.shield")

OVERLAP = 64  # tokens shared by two windows of a long text (as measured)
MIN_TOKEN_LENGTH = 32
MAX_BODY = 4 * 1024 * 1024  # bytes of one request
MAX_PARTS = 256  # parts in one request
MAX_CHARS = 2_000_000  # characters of all the parts of one request


class ShieldError(SystemExit):
    """A setting or a model file is wrong: the program stops with this message."""


class Classifier:
    """The ONNX model and its tokenizer. Imports onnxruntime, tokenizers and numpy only here, so
    the HTTP part can be tested without them."""

    def __init__(
        self, model_path: Path, tokenizer_path: Path, config_path: Path, label: str, window: int, threads: int
    ) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        for path in (model_path, tokenizer_path, config_path):
            if not path.is_file():
                raise ShieldError(f"wattson-shield: the model file {path} is missing")
        self.tok = Tokenizer.from_file(str(tokenizer_path))
        self.tok.no_truncation()
        self.tok.no_padding()
        # The special tokens the tokenizer puts around a text ([CLS] ... [SEP], <s> ... </s>):
        # each window gets them, as a whole text would.
        with_sp = self.tok.encode("hello", add_special_tokens=True).ids
        without = self.tok.encode("hello", add_special_tokens=False).ids
        start = next((i for i in range(len(with_sp)) if with_sp[i : i + len(without)] == without), None)
        if start is None:
            raise ShieldError("wattson-shield: the tokenizer's special tokens could not be found")
        self.prefix, self.suffix = with_sp[:start], with_sp[start + len(without) :]
        self.window = window
        self.size = window - len(self.prefix) - len(self.suffix)
        if self.size <= OVERLAP:
            raise ShieldError(f"wattson-shield: INJECTION_WINDOW must be more than {OVERLAP + 2} tokens")
        labels = json.loads(config_path.read_text(encoding="utf-8")).get("id2label") or {}
        found = [int(k) for k, v in labels.items() if str(v).lower() == label.lower()]
        if len(found) != 1:
            names = ", ".join(sorted(str(v) for v in labels.values())) or "none"
            message = f"INJECTION_LABEL {label!r} is not a label of config.json: {names}"
            raise ShieldError(f"wattson-shield: {message}")
        self.label = label
        self.index = found[0]
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.threads = threads
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self._lock = threading.Lock()  # one inference at a time: the threads are the CPU limit

    def windows(self, ids: list[int]) -> list[list[int]]:
        if len(ids) <= self.size:
            return [ids]
        step = self.size - OVERLAP
        return [ids[i : i + self.size] for i in range(0, max(1, len(ids) - OVERLAP), step)]

    def _window(self, ids: list[int]) -> float:
        np = self._np
        full = np.array([self.prefix + ids + self.suffix], dtype=np.int64)
        feed = {"input_ids": full, "attention_mask": np.ones_like(full)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(full)
        logits = self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0][0]
        e = np.exp(logits - logits.max())
        return float(e[self.index] / e.sum())

    def score(self, text: str) -> float:
        if not text.strip():
            return 0.0
        ids = self.tok.encode(text, add_special_tokens=False).ids
        return max(self._window(w) for w in self.windows(ids)) if ids else 0.0

    def scores(self, parts: Sequence[str]) -> list[float]:
        with self._lock:
            return [self.score(p) for p in parts]

    def describe(self) -> dict[str, Any]:
        return {"label": self.label, "window": self.window, "threads": self.threads}


Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


class BearerOnly:
    """Every request needs "Authorization: Bearer <token>", except /healthz. Constant time."""

    def __init__(self, app: Callable[..., Awaitable[None]], token: str) -> None:
        self.app = app
        self.expected = _digest(f"Bearer {token}".encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        sent = next((v for k, v in scope.get("headers", []) if k.lower() == b"authorization"), b"")
        if hmac.compare_digest(_digest(sent), self.expected):
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized","message":"a valid bearer token is required"}'
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def create_app(scorer: Any, token: str) -> Any:
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, **scorer.describe()})

    async def classify(request: Request) -> JSONResponse:
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                return error(413, "too_large", f"the body is larger than {MAX_BODY} bytes")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return error(400, "invalid", "the body is not JSON")
        parts = data.get("parts") if isinstance(data, dict) else None
        if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
            return error(422, "invalid", 'the body must be {"parts": ["...", ...]}')
        if len(parts) > MAX_PARTS or sum(len(p) for p in parts) > MAX_CHARS:
            return error(413, "too_large", f"at most {MAX_PARTS} parts and {MAX_CHARS} characters at once")
        started = time.perf_counter()
        scores = await run_in_threadpool(scorer.scores, parts)
        # Numbers only: never the text of a part.
        log.info("classified %d part(s) in %d ms", len(parts), int((time.perf_counter() - started) * 1000))
        return JSONResponse({"scores": [round(float(s), 6) for s in scores]})

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/classify", classify, methods=["POST"]),
        ]
    )
    return BearerOnly(app, token)


def read_token(env: Mapping[str, str]) -> str:
    token = env.get("SHIELD_TOKEN", "")
    if not token and env.get("SHIELD_TOKEN_FILE"):
        token = Path(env["SHIELD_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise ShieldError(f"wattson-shield: SHIELD_TOKEN is missing or shorter than {MIN_TOKEN_LENGTH} signs")
    return token


def _whole(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip() or str(default)
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise ShieldError(f"wattson-shield: {name} must be a whole number from {low} to {high}")
    return int(raw)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = os.environ
    token = read_token(env)
    host = env.get("SHIELD_LISTEN_HOST", "").strip() or "127.0.0.1"
    port = _whole(env, "SHIELD_PORT", 9003, 1, 65535)
    label = env.get("INJECTION_LABEL", "").strip()
    if not label:
        raise ShieldError("wattson-shield: INJECTION_LABEL is missing (the injection label of config.json)")
    started = time.perf_counter()
    classifier = Classifier(
        Path(env.get("SHIELD_MODEL_PATH", "").strip() or "/models/model.onnx"),
        Path(env.get("SHIELD_TOKENIZER_PATH", "").strip() or "/models/tokenizer.json"),
        Path(env.get("SHIELD_CONFIG_PATH", "").strip() or "/models/config.json"),
        label,
        _whole(env, "INJECTION_WINDOW", 2048, 128, 32768),
        _whole(env, "SHIELD_THREADS", 2, 1, 64),
    )
    log.info(
        "model loaded in %d ms: label %s, window %d tokens, %d thread(s)",
        int((time.perf_counter() - started) * 1000),
        classifier.label,
        classifier.window,
        classifier.threads,
    )
    import uvicorn

    uvicorn.run(create_app(classifier, token), host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()  # a ShieldError is a SystemExit: its message, and exit code 1
