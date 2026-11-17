"""Combine the fixed rules and the small model into one GuardVerdict.

The rules decide first; the model may only make the verdict STRICTER (work -> risky -> out of
scope / malicious), never weaker. Reasons are plain sentences from the rules and a fixed sentence
from the model; the request text is never copied into a reason.
"""

from __future__ import annotations

from wattson.agents.orchestrator.guard.model import REASON_SENTENCE, GuardModelVerdict
from wattson.agents.orchestrator.guard.rules import RequestHits
from wattson.shared.contracts import GuardLabel, GuardMode, GuardVerdict

# Higher = stricter outcome. work: proceed; risky: confirm; out_of_scope/malicious: block.
SEVERITY: dict[GuardLabel, int] = {"work": 0, "risky": 1, "out_of_scope": 2, "malicious": 3}


def rule_label(hits: RequestHits) -> GuardLabel:
    """The strictest label among the matched rules. 'injection' on a request counts as risky."""
    best: GuardLabel = "work"
    for r in hits.rules:
        label: GuardLabel = "risky" if r.label == "injection" else r.label
        if SEVERITY[label] > SEVERITY[best]:
            best = label
    return best


def combine(
    hits: RequestHits,
    model: GuardModelVerdict | None,
    *,
    mode: GuardMode,
    model_name: str | None,
    model_configured: bool,
    model_failed: bool,
    latency_ms: int,
) -> GuardVerdict:
    base = rule_label(hits)
    label = base
    reasons = list(hits.reasons)

    if model is not None and SEVERITY[model.label] > SEVERITY[base]:
        label = model.label
        sentence = REASON_SENTENCE.get(model.reason)
        if sentence and sentence not in reasons:
            reasons.append(sentence)

    if not reasons:
        reasons = ["Nothing in the request matched a safety rule."]

    if not model_configured:
        method = "rules"
    elif model_failed or model is None:
        method = "rules"
    else:
        method = "rules+model"

    return GuardVerdict(
        label=label,
        reasons=reasons,
        rule_hits=hits.names,
        method=method,
        model=model_name if method == "rules+model" else None,
        latency_ms=latency_ms,
        mode=mode,
    )
