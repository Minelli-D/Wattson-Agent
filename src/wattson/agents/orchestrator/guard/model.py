"""The Guard's small model: the orchestrator Agent's aux_model, a Strands OpenAIModel pointing at
GUARD_MODEL_URL / GUARD_MODEL (KubeAI's Granite). It gives a second, independent verdict.

The model may only make a verdict STRICTER than the rules, never weaker (merged in verdict.py).
Production Granite 4.2 runs with --reasoning on (it thinks by default), so every request carries
chat_template_kwargs {"enable_thinking": false}: the think step would eat the token budget and
slow each call to ~20 s. Temperature 0. The model returns a label and ONE fixed reason code (an
enum, never free text), so nothing of the request can leak into a reason, a span or a record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel
from strands.models.openai import OpenAIModel

from wattson.agents.orchestrator.settings import Settings
from wattson.shared.contracts import GuardLabel

log = logging.getLogger("wattson.orchestrator")

# The think step off, temperature 0. extra_body is sent verbatim in each request body.
THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
MAX_TOKENS = 256

ReasonCode = Literal[
    "normal_engineering",
    "destructive_change",
    "not_engineering",
    "malicious_intent",
    "prompt_injection",
]

REASON_SENTENCE: dict[str, str] = {
    "destructive_change": "The safety model read this as a destructive or production change.",
    "not_engineering": "The safety model read this as not engineering work.",
    "malicious_intent": "The safety model read this as a harmful or hostile request.",
    "prompt_injection": "The safety model read this as an attempt to steer the agent.",
}

SYSTEM_PROMPT = """You check an engineer's request to a DevOps assistant before it runs.
Choose one label:
- work: normal engineering or DevOps work (investigate, read, explain, write code, fix a bug).
- risky: a real but destructive or production-changing action (delete, scale, restart, patch,
  apply, drop/truncate tables, force push, change production).
- out_of_scope: not engineering work at all (poems, travel, cooking, chit-chat).
- malicious: harmful or hostile (read out or send secrets, disable security, wipe data, attack
  or scan other systems, hide activity, crypto mining, pipe a downloaded script to a shell).
Reading, explaining or investigating a command (even a dangerous-looking one) is work, not risky.
Be strict only when the request actually asks to perform the action. Answer with the JSON only."""


class GuardModelVerdict(BaseModel):
    """The model's structured answer: a label and one reason code. No free text."""

    label: GuardLabel
    reason: ReasonCode


class GuardModel:
    """Wraps the aux_model and asks it for a verdict, within a timeout. Never raises: a failure
    returns None and the caller falls back to the rules with a warning."""

    def __init__(self, model: OpenAIModel, timeout_s: float, name: str) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.name = name

    async def verdict(self, request: str) -> GuardModelVerdict | None:
        prompt = [{"role": "user", "content": [{"text": f"Request: {request[:4000]}"}]}]
        try:
            return await asyncio.wait_for(self._ask(prompt), timeout=self.timeout_s)
        except TimeoutError:
            log.warning("guard model timed out after %.0fs", self.timeout_s)
            return None
        except Exception as e:  # noqa: BLE001 - the model is advice; any failure means rules only
            log.warning("guard model failed: %s", type(e).__name__)
            return None

    async def _ask(self, prompt: list[dict[str, Any]]) -> GuardModelVerdict | None:
        out: GuardModelVerdict | None = None
        async for event in self.model.structured_output(
            GuardModelVerdict, prompt, system_prompt=SYSTEM_PROMPT
        ):
            value = event.get("output")
            if isinstance(value, GuardModelVerdict):
                out = value
        return out


def build_guard_model(settings: Settings) -> OpenAIModel | None:
    """The OpenAIModel for the Guard, or None when GUARD_MODEL_URL is empty (rules only)."""
    if not settings.guard_model_url:
        return None
    return OpenAIModel(
        client_args={
            "base_url": settings.guard_model_url,
            "api_key": "unused",  # KubeAI needs none; the OpenAI client wants a non-empty string
            "timeout": settings.guard_timeout_s,
            "max_retries": 0,
        },
        model_id=settings.guard_model,
        params={"temperature": 0, "max_tokens": MAX_TOKENS, "extra_body": THINKING_OFF},
    )
