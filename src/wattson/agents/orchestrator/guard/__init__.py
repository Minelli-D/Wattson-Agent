"""The Guard: the orchestrator's safety check, built with Strands' own features.

A request is checked before Wattson goes on with it; evidence and Grimoire documents are checked
for prompt injection before they reach the prompt. Fixed rules first (rules.yaml, the user's file),
then a small model (the Agent's aux_model) that may only make the verdict stricter. Cedar policies
(policies.cedar) are a declarative second lock on the step order. With INJECTION_MODE warn or cut,
the small injection model of the pod wattson-shield (shield.py) reads the evidence and the
documents after the rules, and the request itself after its verdict (for at most
min(INJECTION_TIMEOUT_S, GUARD_TIMEOUT_S): the start of a task never waits long).

build_guard() reads the settings and files once (at start). Its `aux_model` and
`interventions()` give build_agent what each per-context Agent needs; fresh intervention handlers
are made per context so nothing leaks between tasks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from strands.interventions import InterventionHandler
from strands.models.openai import OpenAIModel
from strands.vended_interventions.cedar import CedarAuthorization

from wattson.agents.orchestrator.guard.cedar import build_cedar_from_text, load_policies
from wattson.agents.orchestrator.guard.interventions import EvidenceGuard, RequestGuard
from wattson.agents.orchestrator.guard.model import GuardModel, build_guard_model
from wattson.agents.orchestrator.guard.rules import RuleSet, load_rules
from wattson.agents.orchestrator.guard.shield import Shield
from wattson.agents.orchestrator.settings import Settings

__all__ = ["Guard", "build_guard"]

log = logging.getLogger("wattson.orchestrator")


@dataclass
class Guard:
    """The Guard loaded once. `interventions()` builds fresh handlers for one Agent."""

    mode: str
    ruleset: RuleSet
    aux_model: OpenAIModel | None
    policies: str
    problems: list[str] = field(default_factory=list)  # rules/policy load problems, for health
    shield: Shield | None = None  # the injection model; None with INJECTION_MODE=off

    @property
    def ok(self) -> bool:
        return not self.problems

    def detail(self) -> str:
        if self.mode == "off":
            return "guard off (GUARD_MODE=off)"
        parts = [
            f"mode {self.mode}",
            f"{self.ruleset.count} rule(s) from {self.ruleset.source}",
            "rules+model" if self.aux_model is not None else "rules only",
        ]
        if self.shield is not None:
            parts.append(f"injection model {self.shield.model} ({self.shield.mode})")
        return "; ".join(parts) + ("; " + " ".join(self.problems) if self.problems else "")

    def interventions(self) -> list[InterventionHandler]:
        if self.mode == "off":
            return []
        guard_model = (
            GuardModel(self.aux_model, timeout_s=self._timeout, name=self._model_name)
            if self.aux_model is not None
            else None
        )
        cedar: CedarAuthorization = build_cedar_from_text(self.policies)
        return [
            cedar,
            RequestGuard(
                self.ruleset,
                guard_model,
                self.mode,  # type: ignore[arg-type]
                self.shield,
                min(self.shield.timeout_s, self._timeout) if self.shield is not None else self._timeout,
            ),
            EvidenceGuard(self.ruleset, self.mode, self.shield),  # type: ignore[arg-type]
        ]

    # Filled by build_guard.
    _timeout: float = 20.0
    _model_name: str = ""


def build_guard(settings: Settings) -> Guard:
    mode = settings.guard_mode
    ruleset, rules_problem = load_rules(settings.guard_rules_file)
    problems = [p for p in (rules_problem,) if p]
    if rules_problem:
        log.warning("guard rules: %s", rules_problem)

    if mode == "off":
        return Guard(mode=mode, ruleset=ruleset, aux_model=None, policies="", problems=problems)

    shield = None
    if settings.injection_mode != "off":
        shield = Shield(
            settings.shield_url,
            settings.shield_token,
            mode=settings.injection_mode,  # type: ignore[arg-type]
            threshold=settings.injection_threshold,
            timeout_s=settings.injection_timeout_s,
            model=settings.injection_model,
        )

    aux_model = build_guard_model(settings)
    policies, cedar_problem = load_policies(settings.guard_policies_file)
    if cedar_problem:
        log.warning("guard cedar: %s", cedar_problem)
        problems.append(cedar_problem)

    return Guard(
        mode=mode,
        ruleset=ruleset,
        aux_model=aux_model,
        policies=policies,
        problems=problems,
        shield=shield,
        _timeout=settings.guard_timeout_s,
        _model_name=settings.guard_model,
    )
