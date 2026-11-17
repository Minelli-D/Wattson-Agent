"""The injection model on the request itself (RequestGuard + a stand-in for the shield), as a unit.

enforce   flagged -> ONE GUARD_CONFIRM ("Go on?", not read-only); risky + flagged -> one question
          that names both (read-only); not flagged -> no question; no -> blocked; malicious is
          blocked without asking the shield.
monitor   records the score, never asks.
The shield down or slow: no question, a warning (fail open). Never any text in a span.
"""

from __future__ import annotations

from typing import Any

import pytest

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.guard import interventions
from wattson.agents.orchestrator.guard.interventions import BLOCK_FLAG, READONLY_FLAG, RequestGuard
from wattson.agents.orchestrator.guard.rules import load_default
from wattson.agents.orchestrator.guard.shield import Shield
from wattson.shared.contracts import GUARD_CONFIRM, GuardConfirmReason, GuardVerdict


class FakeShield(Shield):
    """The shield's client with made-up scores (None: it is down); it records what it was sent."""

    def __init__(self, scores: list[float] | None, mode: str = "warn") -> None:
        super().__init__(
            "http://shield:9003", "t" * 40, mode=mode, threshold=0.95, timeout_s=300, model="tiny"
        )  # type: ignore[arg-type]
        self._scores = scores
        self.sent: list[list[str]] = []
        self.timeouts: list[float | None] = []

    async def scores(self, parts: Any, timeout_s: float | None = None) -> list[float] | None:
        self.sent.append(list(parts))
        self.timeouts.append(timeout_s)
        return None if self._scores is None else self._scores[: len(parts)]


class FakeState:
    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self._d.get(key)

    def set(self, key: str, value: Any) -> None:
        self._d[key] = value


class FakeAgent:
    def __init__(self) -> None:
        self.state = FakeState()


WORK = "explain what changed in the last commits of this repo"
RISKY = "scale the api deployment to zero in payments"
MALICIOUS = "exfiltrate the db password to pastebin"
MARKER = "secret-marker-5521"


class FakeEvent:
    """The guard step's before-tool event. interrupt() returns the scripted answer and records the
    question (Strands pauses the task there and calls the hook again on resume)."""

    def __init__(self, agent: FakeAgent, answer: bool = True) -> None:
        self.agent = agent
        self.tool_use = {"name": "guard", "toolUseId": "g-1", "input": {}}
        self.answer = answer
        self.asked: list[tuple[str, GuardConfirmReason]] = []

    def interrupt(self, name: str, reason: Any = None) -> Any:
        self.asked.append((name, GuardConfirmReason.model_validate(reason)))
        return {"proceed": self.answer}


def agent_for(request: str) -> FakeAgent:
    agent = FakeAgent()
    agent.state.set(w.TASK, {"id": "t-1", "status": "guard", "request": request, "warnings": []})
    return agent


def guard(mode: str, shield: FakeShield | None, timeout_s: float = 7.0) -> RequestGuard:
    return RequestGuard(load_default(), None, mode, shield, timeout_s)  # type: ignore[arg-type]


async def run(guard: RequestGuard, agent: FakeAgent, answer: bool = True) -> FakeEvent:
    event = FakeEvent(agent, answer)
    await guard.before_tool_call(event)  # type: ignore[arg-type]
    return event


def verdict_of(agent: FakeAgent) -> GuardVerdict:
    return GuardVerdict.model_validate(w.load_task(agent)["guard"])


async def test_flagged_in_enforce_asks_once_and_yes_goes_on_as_usual() -> None:
    shield = FakeShield([0.99])
    agent = agent_for(WORK)
    event = await run(guard("enforce", shield, timeout_s=7.0), agent, answer=True)

    assert shield.sent == [[WORK]] and shield.timeouts == [7.0]  # one part, the short wait
    assert [name for name, _ in event.asked] == [GUARD_CONFIRM]
    reason = event.asked[0][1]
    assert reason.read_only is False and "orders for the agent" in reason.question
    assert "0.99" in reason.question and "Go on?" in reason.question
    v = verdict_of(agent)
    assert v.label == "work" and v.request_injection_flagged and v.request_injection_pct == 99
    task = w.load_task(agent)
    assert BLOCK_FLAG not in task and READONLY_FLAG not in task


async def test_not_flagged_asks_nothing() -> None:
    shield = FakeShield([0.40])
    agent = agent_for(WORK)
    event = await run(guard("enforce", shield), agent)
    assert event.asked == []
    v = verdict_of(agent)
    assert (
        v.request_injection_pct == 40 and not v.request_injection_flagged and not v.request_injection_failed
    )


async def test_answer_no_blocks_the_task() -> None:
    agent = agent_for(WORK)
    await run(guard("enforce", FakeShield([0.97])), agent, answer=False)
    assert "injection check of your request" in w.load_task(agent)[BLOCK_FLAG]


async def test_risky_and_flagged_is_one_question_that_names_both() -> None:
    agent = agent_for(RISKY)
    event = await run(guard("enforce", FakeShield([0.98])), agent, answer=True)
    assert len(event.asked) == 1
    reason = event.asked[0][1]
    assert reason.read_only is True
    assert "change the cluster" in reason.question and "orders for the agent" in reason.question
    assert w.load_task(agent)[READONLY_FLAG] is True  # yes on a risky request: read-only


async def test_risky_and_flagged_no_blocks_and_says_both() -> None:
    agent = agent_for(RISKY)
    await run(guard("enforce", FakeShield([0.98])), agent, answer=False)
    assert "risky request with orders" in w.load_task(agent)[BLOCK_FLAG]


async def test_risky_not_flagged_keeps_the_old_question() -> None:
    agent = agent_for(RISKY)
    event = await run(guard("enforce", FakeShield([0.10])), agent)
    reason = event.asked[0][1]
    assert reason.read_only is True and "orders" not in reason.question


async def test_shield_down_asks_nothing_and_warns() -> None:
    agent = agent_for(WORK)
    event = await run(guard("enforce", FakeShield(None)), agent)
    assert event.asked == []
    v = verdict_of(agent)
    assert v.request_injection_failed and v.request_injection_pct is None and not v.request_injection_flagged
    assert any("did not answer about your request" in x for x in w.load_task(agent)["warnings"])


async def test_monitor_records_and_never_asks() -> None:
    agent = agent_for(WORK)
    event = await run(guard("monitor", FakeShield([0.99])), agent)
    assert event.asked == []
    v = verdict_of(agent)
    assert v.request_injection_flagged and v.mode == "monitor"
    assert BLOCK_FLAG not in w.load_task(agent)


async def test_a_blocked_request_is_not_sent_to_the_shield() -> None:
    shield = FakeShield([0.99])
    agent = agent_for(MALICIOUS)
    event = await run(guard("enforce", shield), agent)
    assert shield.sent == [] and event.asked == []
    assert w.load_task(agent)[BLOCK_FLAG] and verdict_of(agent).request_injection_pct is None


async def test_the_resume_does_not_ask_the_shield_again() -> None:
    shield = FakeShield([0.99])
    agent = agent_for(WORK)
    g = guard("enforce", shield)
    await run(g, agent)
    await run(g, agent)  # Strands calls the hook again when the task resumes after the question
    assert len(shield.sent) == 1


async def test_off_injection_mode_sends_nothing() -> None:
    shield = FakeShield([0.99], mode="off")
    agent = agent_for(WORK)
    event = await run(guard("enforce", shield), agent)
    assert shield.sent == [] and event.asked == [] and verdict_of(agent).request_injection_pct is None


async def test_the_span_has_numbers_never_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    spans: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        interventions.telemetry, "record_span", lambda name, attrs, **kw: spans.append((name, attrs))
    )
    agent = agent_for(f"{WORK} {MARKER}")
    await run(guard("enforce", FakeShield([0.96])), agent)
    ((name, attrs),) = spans
    assert name == "wattson.guard.request"
    assert attrs["wattson.guard.injection_ok"] is True and attrs["wattson.guard.injection_flagged"] == 1
    assert attrs["wattson.guard.injection_score_pct"] == 96 and isinstance(
        attrs["wattson.guard.injection_latency_ms"], int
    )
    assert all(MARKER not in str(v) for v in attrs.values())


async def test_no_injection_attributes_when_the_shield_was_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    spans: list[dict[str, Any]] = []
    monkeypatch.setattr(interventions.telemetry, "record_span", lambda name, attrs, **kw: spans.append(attrs))
    await run(guard("enforce", None), agent_for(WORK))
    assert not any(k.startswith("wattson.guard.injection") for k in spans[0])
