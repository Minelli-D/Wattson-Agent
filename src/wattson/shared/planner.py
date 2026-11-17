"""The deterministic planner: a Strands model provider that runs no LLM.

Each agent of wattson is a Strands Agent whose tools are the steps of its workflow. This "model"
only reads the last tool result and calls the tool it names in "next". So the order of the steps
is fixed in code (FR-PROMPT-01, FR-DEC-06), while the agent still gets everything Strands gives:
tools, hooks, interrupts (which A2A turns into input-required), the A2A server, traces.
A real LLM provider can take its place later (FR-PROMPT-07): the tools and hooks stay the same.

Tool result convention: every step returns step(...), a JSON object with
  next   the name of the next tool, or None to finish
  input  the input for that tool (usually {}: the tools keep their data in agent.state)
  output the final answer when next is None (the A2A artifact)
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from strands.models.model import Model

STRUCTURED_MARK = "[Structured Data]"
MAX_STEPS = 60


def step(next: str | None = None, input: dict[str, Any] | None = None, output: Any = None) -> str:
    """The result of a workflow tool, read by the planner."""
    return json.dumps({"next": next, "input": input or {}, "output": output})


def parse_structured(text: str) -> dict[str, Any]:
    """The JSON of an A2A DataPart, as the Strands A2A executor hands it over (a text block that
    starts with "[Structured Data]"). Returns {} when the text holds no such block."""
    at = text.find(STRUCTURED_MARK)
    if at < 0:
        return {}
    body = text[at + len(STRUCTURED_MARK) :].strip()
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _text_of(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)


class PlannerModel(Model):
    """Calls entry_tool with {"message": <the user's text>} first, then follows "next"."""

    def __init__(self, entry_tool: str, max_steps: int = MAX_STEPS) -> None:
        self.config: dict[str, Any] = {"entry_tool": entry_tool, "max_steps": max_steps}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError("the planner runs no LLM; use a model provider for structured output")

    async def stream(  # type: ignore[override]
        self, messages: list[dict[str, Any]], tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any]]:
        steps = sum(1 for m in messages if m.get("role") == "assistant")
        last = messages[-1] if messages else {"role": "user", "content": []}
        results = [
            b["toolResult"] for b in last.get("content", []) if isinstance(b, dict) and "toolResult" in b
        ]

        if not results:
            call: tuple[str, dict[str, Any]] | None = (
                self.config["entry_tool"],
                {"message": _text_of(last.get("content", []))},
            )
            final: Any = None
        else:
            result = results[-1]
            text = _text_of(result.get("content", []))
            if result.get("status") == "error":
                # A tool failed, or a hook cancelled it: stop, and say why. Never guess a next step.
                call, final = None, {"error": text[:2000] or "a step failed"}
            else:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {"next": None, "output": {"error": f"a step answered no JSON: {text[:200]}"}}
                call = (data["next"], data.get("input") or {}) if data.get("next") else None
                final = data.get("output")

        if call is not None and steps >= self.config["max_steps"]:
            call, final = None, {"error": f"stopped after {steps} steps"}

        yield {"messageStart": {"role": "assistant"}}
        if call is not None:
            name, tool_input = call
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": f"p-{uuid.uuid4().hex[:12]}", "name": name}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": json.dumps(final)}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
