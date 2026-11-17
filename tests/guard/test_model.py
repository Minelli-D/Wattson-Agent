"""The small model wrapper: it returns the structured verdict, and any failure (timeout, bad
answer, server error) becomes None so the caller falls back to the rules."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from wattson.agents.orchestrator.guard.model import GuardModel, GuardModelVerdict


class FakeModel:
    """Stands in for a Strands OpenAIModel: its structured_output yields what we tell it."""

    def __init__(
        self, *, output: GuardModelVerdict | None = None, raises: Exception | None = None, sleep: float = 0.0
    ) -> None:
        self._output = output
        self._raises = raises
        self._sleep = sleep

    async def structured_output(
        self, output_model: type, prompt: Any, system_prompt: str | None = None
    ) -> AsyncGenerator[dict[str, Any]]:
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raises is not None:
            raise self._raises
        if self._output is not None:
            yield {"output": self._output}


def model(**kw: Any) -> GuardModel:
    return GuardModel(FakeModel(**kw), timeout_s=0.2, name="granite")  # type: ignore[arg-type]


async def test_returns_the_structured_verdict() -> None:
    out = GuardModelVerdict(label="risky", reason="destructive_change")
    assert await model(output=out).verdict("scale the deployment") == out


async def test_timeout_returns_none() -> None:
    slow = model(sleep=1.0, output=GuardModelVerdict(label="work", reason="normal_engineering"))
    assert await slow.verdict("x") is None


async def test_server_error_returns_none() -> None:
    assert await model(raises=RuntimeError("500")).verdict("x") is None


async def test_no_output_returns_none() -> None:
    assert await model().verdict("x") is None
