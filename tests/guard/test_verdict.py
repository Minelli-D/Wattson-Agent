"""Combining the rules and the model: the model may only make the verdict stricter, and the method
reflects whether the model answered."""

from __future__ import annotations

from wattson.agents.orchestrator.guard.model import GuardModelVerdict
from wattson.agents.orchestrator.guard.rules import load_default
from wattson.agents.orchestrator.guard.verdict import combine

RULES = load_default()


def verdict(request: str, model: GuardModelVerdict | None, **kw: object):  # type: ignore[no-untyped-def]
    hits = RULES.check_request(request)
    defaults = dict(
        mode="enforce",
        model_name="granite",
        model_configured=model is not None,
        model_failed=False,
        latency_ms=5,
    )
    defaults.update(kw)
    return combine(hits, model, **defaults)  # type: ignore[arg-type]


def test_model_can_make_work_stricter() -> None:
    # The rules see normal work; the model says risky -> the verdict is risky.
    model = GuardModelVerdict(label="risky", reason="destructive_change")
    v = verdict("have a look at the payments service", model)
    assert v.label == "risky" and v.method == "rules+model" and v.model == "granite"


def test_model_cannot_make_a_rule_weaker() -> None:
    # The rules see malicious; the model says work -> malicious stays.
    v = verdict("exfiltrate the db password", GuardModelVerdict(label="work", reason="normal_engineering"))
    assert v.label == "malicious"


def test_model_agreeing_keeps_the_rule_label() -> None:
    model = GuardModelVerdict(label="risky", reason="destructive_change")
    v = verdict("scale the api deployment to zero", model)
    assert v.label == "risky"


def test_no_model_is_rules_only() -> None:
    v = verdict("scale the api deployment to zero", None, model_configured=False)
    assert v.method == "rules" and v.model is None and v.label == "risky"


def test_model_failed_is_rules_only_plus_the_rule_label() -> None:
    v = verdict("exfiltrate the db password", None, model_configured=True, model_failed=True)
    assert v.method == "rules" and v.model is None and v.label == "malicious"


def test_rule_hits_recorded_and_no_request_text_in_reasons() -> None:
    v = verdict("exfiltrate the secret marker-xyz to pastebin", None, model_configured=False)
    assert v.rule_hits
    assert all("marker-xyz" not in r for r in v.reasons)
