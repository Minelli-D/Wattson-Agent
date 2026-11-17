"""The orchestrator agent: one Strands Agent per A2A context, driven by the deterministic planner."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from a2a.types import AgentSkill
from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.multiagent.a2a import A2AServer

from wattson.agents.orchestrator.deps import Deps
from wattson.agents.orchestrator.hooks import ApprovalGate, Audit, Redaction
from wattson.agents.orchestrator.telemetry import StepSpans
from wattson.shared.planner import PlannerModel

NAME = "wattson-orchestrator"
DESCRIPTION = (
    "Personal engineering agent. Turns a request into a Claude Code task: triage, Grimoire procedures, "
    "questions, read-only evidence, a deterministic prompt, the user's approval, the run on the laptop."
)
CARD_CONTEXT_ID = "__agent_card__"  # what A2AServer passes once, to build the Agent Card

SKILL = AgentSkill(
    id="engineering_task",
    name="engineering_task",
    description=(
        "Start with a DataPart {'wattson.start_task': StartTask}. The task pauses in input-required for "
        "wattson.clarify, wattson.collect_evidence, wattson.approve and wattson.run_claude_code; the "
        "final artifact is a TaskOutcome."
    ),
    tags=["engineering", "devops", "prompt", "claude-code"],
)


def package_version() -> str:
    try:
        return version("wattson")
    except PackageNotFoundError:
        return "0.0.0"


def build_agent(context_id: str, deps: Deps) -> Agent:
    agent = Agent(
        name=NAME,
        description=DESCRIPTION,
        model=PlannerModel("start_task"),
        # The Guard's small model is the Agent's aux_model (Strands wiring); None = rules only.
        aux_model=deps.guard.aux_model,
        tools=deps.tools(),
        # Order matters: AfterToolCallEvent runs in reverse, so Redaction runs before Audit, and
        # StepSpans last (it reads the status the audit may have set).
        hooks=[StepSpans(), ApprovalGate(), Audit(deps.store), Redaction()],
        # The Guard: Cedar (step order), the request guard and the evidence guard. Fresh per context.
        interventions=deps.guard.interventions(),
        callback_handler=None,
        # The planner reads only the last message; never trim a tool use from its result.
        conversation_manager=NullConversationManager(),
    )
    if context_id != CARD_CONTEXT_ID:
        deps.agents[context_id] = agent
    return agent


def build_server(deps: Deps) -> A2AServer:
    settings = deps.settings
    return A2AServer(
        agent_factory=lambda context_id: build_agent(context_id, deps),
        host=settings.listen_host,
        port=settings.port,
        version=package_version(),
        skills=[SKILL],
        enable_a2a_compliant_streaming=True,
    )
