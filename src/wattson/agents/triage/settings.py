"""Settings of the triage agent, read from the environment (docs/design.md 10.2).

The decision model is any OpenAI-compatible server: the llama.cpp sidecar (the default), Ollama
behind KubeAI, or vLLM. LLM_BASE_URL is its OpenAI base ("/chat/completions" and "/models" are
added); without it, LLAMA_URL + "/v1" (the sidecar). LLM_MODEL is the "model" field of each
request (KubeAI: the Model resource name); without it, MODEL_NAME. LLM_API_KEY is an optional
bearer. MODEL_NAME stays the name shown in records, health and the Agent Card.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from wattson.shared.auth import read_token

# Chosen by measurement: docs/decision-model.md and eval/results/. MODEL_NAME is a label (Agent Card,
# health, Classification.model); the file itself is passed to llama-server with -m.
DEFAULT_LLAMA_URL = "http://127.0.0.1:8081"
DEFAULT_MODEL_NAME = "granite-4.0-h-1b-Q4_K_M"
DEFAULT_MIN_CONFIDENCE = 0.8  # 91.9 % accuracy at 86.1 % coverage on eval/requests.jsonl


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    token: str = field(default="", repr=False)
    listen_host: str = "0.0.0.0"  # noqa: S104 - the pod's interface; a NetworkPolicy limits who comes in
    port: int = 9001
    llama_url: str = DEFAULT_LLAMA_URL  # the llama.cpp sidecar; only the default of llm_base_url
    model_name: str = DEFAULT_MODEL_NAME
    timeout_s: float = 50.0
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    # Softmax temperature on the letter log-probs; 1.0 = raw. A calibration knob (FR-DEC-05).
    confidence_temperature: float = 1.0
    public_url: str | None = None  # the URL in the Agent Card, e.g. http://wattson-triage:9001/
    # The OpenAI-compatible model server. Empty: llama_url + "/v1", and model_name.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)  # sent as "Authorization: Bearer" when set

    def __post_init__(self) -> None:
        base = (self.llm_base_url or f"{self.llama_url.rstrip('/')}/v1").rstrip("/")
        object.__setattr__(self, "llm_base_url", base)
        object.__setattr__(self, "llm_model", self.llm_model or self.model_name)

    @classmethod
    def from_env(cls) -> Settings:
        min_conf = _float("MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
        if not 0.0 <= min_conf <= 1.0:
            raise SystemExit("MIN_CONFIDENCE must be between 0 and 1")
        temperature = _float("CONFIDENCE_TEMPERATURE", 1.0)
        if temperature <= 0:
            raise SystemExit("CONFIDENCE_TEMPERATURE must be above 0")
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise SystemExit("LLM_BASE_URL must start with http:// or https://")
        return cls(
            token=read_token("TRIAGE_TOKEN", "TRIAGE_TOKEN_FILE"),
            listen_host=os.environ.get("TRIAGE_LISTEN_HOST", "0.0.0.0"),  # noqa: S104
            port=_int("TRIAGE_PORT", 9001),
            llama_url=(os.environ.get("LLAMA_URL", "").strip() or DEFAULT_LLAMA_URL).rstrip("/"),
            model_name=os.environ.get("MODEL_NAME", "").strip() or DEFAULT_MODEL_NAME,
            timeout_s=_float("TRIAGE_TIMEOUT_S", 50.0),
            min_confidence=min_conf,
            confidence_temperature=temperature,
            public_url=os.environ.get("TRIAGE_PUBLIC_URL") or None,
            llm_base_url=base_url,
            llm_model=os.environ.get("LLM_MODEL", "").strip(),
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        )
