"""The Guard in the CLI: its question (go on read-only, or stop), its verdict at the review, and a
task it stopped ("blocked") in `ask`, `tasks` and `show`."""

from __future__ import annotations

import json

from cli_fakes import BLOCKED_ID, BLOCKED_SUMMARY, GUARD_QUESTION

from wattson.shared.contracts import APPROVE, CLARIFY, COLLECT_EVIDENCE, GUARD_CONFIRM, GuardVerdict

ASK = ["ask", "delete the crashing pods of deployment/api", "--new-session", "--headless"]
QUESTION = "Go on with a read-only investigation? [y/N]"


def risky(**update) -> GuardVerdict:
    verdict = GuardVerdict(
        label="risky",
        reasons=["It asks to delete pods."],
        rule_hits=["k8s-delete"],
        method="rules",
        mode="enforce",
    )
    return verdict.model_copy(update=update)


def _set_prompts(env, mode: str) -> None:
    (env.config / "config.json").write_text(
        json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire", "prompts": mode})
    )


# ---- the Guard's question -------------------------------------------------------------------


def test_confirm_yes_goes_on(env, cli):
    env.script.guard_confirm = risky()
    r = cli(ASK, ["y", "2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "The Guard has a question" in r.out
    assert "RISKY (it asks to change or delete something)" in r.out
    assert "decided by fixed rules" in r.out and "enforce: it can stop tasks" in r.out
    assert "It asks to delete pods." in r.out and GUARD_QUESTION in r.out
    assert QUESTION in r.out and "Going on, read-only only." in r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is True
    # The question comes first; the task goes on as usual.
    assert r.out.index("The Guard has a question") < r.out.index("The agent needs some answers")
    assert env.script.received[CLARIFY][0].answers == {"q1": "devops-investigation", "q2": "web"}
    assert env.script.received[APPROVE][0].decision == "approve"


def injection_only() -> GuardVerdict:
    return GuardVerdict(
        label="work",
        reasons=["Nothing in the request matched a safety rule."],
        method="rules",
        mode="enforce",
        request_injection_pct=99,
        request_injection_flagged=True,
    )


INJECTION_QUESTION = (
    "The injection model (tiny) reads orders for the agent in your request (score 0.99): "
    "text pasted from a ticket, a log or a chat? Go on?"
)


def test_injection_question_yes_goes_on_as_usual(env, cli):
    env.script.guard_confirm = injection_only()
    env.script.guard_question = INJECTION_QUESTION
    env.script.guard_read_only = False
    r = cli(ASK, ["y", "2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "orders for the agent in your request (score 0.99)" in r.out
    assert "Go on? [y/N]" in r.out and "Going on." in r.out
    assert "Go on with a read-only investigation?" not in r.out and "Going on, read-only only." not in r.out
    assert "Nothing in the request matched" not in r.out  # no "Why" panel for a plain work label
    assert env.script.received[GUARD_CONFIRM][0].proceed is True


def test_injection_question_no_blocks(env, cli):
    env.script.guard_confirm = injection_only()
    env.script.guard_question = INJECTION_QUESTION
    env.script.guard_read_only = False
    r = cli(ASK, ["n"])
    assert r.code == 3, r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is False
    assert "Stopped. Nothing ran. The task is marked as blocked." in r.out


def test_confirm_no_blocks(env, cli):
    env.script.guard_confirm = risky()
    r = cli(ASK, ["n"])
    assert r.code == 3, r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is False
    assert "Stopped. Nothing ran. The task is marked as blocked." in r.out
    assert "Stopped by the Guard: nothing ran" in r.out
    assert "Stopped by the Guard: You said no to the Guard's question." in r.out
    assert CLARIFY not in env.script.received and COLLECT_EVIDENCE not in env.script.received
    assert env.claude_calls() == [] and env.local_tasks() == []


def test_confirm_empty_answer_is_no(env, cli):
    env.script.guard_confirm = risky()
    r = cli(ASK, [""])
    assert r.code == 3, r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is False


def test_confirm_end_of_input_is_no(env, cli):
    env.script.guard_confirm = risky()
    r = cli(ASK, [])
    assert r.code == 3, r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is False
    assert CLARIFY not in env.script.received


def test_confirm_other_answer_asks_again(env, cli):
    env.script.guard_confirm = risky()
    r = cli(ASK, ["a", "yes please", "n"])
    assert r.code == 3, r.out
    assert r.out.count("Please answer y (yes) or n (no).") == 2
    assert env.script.received[GUARD_CONFIRM][0].proceed is False


def test_quick_preset_still_asks(env, cli):
    _set_prompts(env, "quick")
    env.script.guard_confirm = risky()
    r = cli(ASK, [""])
    assert r.code == 3, r.out
    assert QUESTION in r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is False
    r = cli(ASK, ["y", "2", "web", "y", "a", "y"])
    assert r.code == 0, r.out
    assert env.script.received[GUARD_CONFIRM][-1].proceed is True


def test_yes_evidence_does_not_skip_it(env, cli):
    env.script.guard_confirm = risky()
    r = cli([*ASK, "--yes-evidence"], [""])
    assert r.code == 3, r.out
    assert QUESTION in r.out
    assert env.script.received[GUARD_CONFIRM][0].proceed is False


def test_malicious_reasons_shown(env, cli):
    env.script.guard_confirm = risky(label="malicious", reasons=["It asks for the tokens of the cluster."])
    r = cli(ASK, ["n"])
    assert r.code == 3, r.out
    assert "MALICIOUS (it looks like an attack or abuse)" in r.out
    assert "It asks for the tokens of the cluster." in r.out


# ---- a task the Guard stopped at once --------------------------------------------------------


def test_guard_block(env, cli):
    env.script.guard_block = risky(label="malicious", reasons=["It asks to read every Secret."])
    r = cli(ASK, [])
    assert r.code == 3, r.out
    assert "Stopped by the Guard: It asks to read every Secret." in r.out
    assert "Stopped by the Guard: nothing ran" in r.out
    assert GUARD_CONFIRM not in env.script.received and CLARIFY not in env.script.received
    assert env.claude_calls() == []


# ---- the verdict at the review ---------------------------------------------------------------


def test_verdict_at_review(env, cli):
    env.script.guard = GuardVerdict(
        label="work", method="rules+model", model="granite-4-2-3b", mode="monitor", evidence_removed=2
    )
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "work (a normal engineering task)" in r.out
    assert "decided by fixed rules and the small model, model granite-4-2-3b" in r.out
    assert "mode monitor: it only records, it blocks nothing" in r.out
    assert "it blocks nothing; 2 evidence part(s) removed" in r.out  # the Guard row (it wraps)
    assert "2 evidence part(s) were removed as possible prompt injection." in r.out
    assert "The Guard says" not in r.out  # work: no alarm


def test_injection_model_at_review():
    from wattson.cli import review

    base = {"label": "work", "method": "rules", "mode": "enforce"}
    assert "injection model" not in review.guard_text(GuardVerdict(**base))
    flagged = GuardVerdict(**base, injection_model="horizon-small", injection_flagged=2)
    assert review.guard_text(flagged).endswith("; injection model: 2 part(s) flagged")
    clean = GuardVerdict(**base, injection_model="horizon-small")
    assert review.guard_text(clean).endswith("; injection model: nothing flagged")
    failed = GuardVerdict(**base, injection_model="horizon-small", injection_failed=True)
    assert review.guard_text(failed).endswith("; the injection model did not answer (rules only)")


def test_risky_verdict_is_prominent(env, cli):
    env.script.guard = risky(mode="monitor")
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "The Guard says: RISKY (it asks to change or delete something)." in r.out
    assert "- It asks to delete pods." in r.out
    assert r.out.index("The Guard says") < r.out.index("Classified as")


def test_no_verdict_no_guard_line(env, cli):
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "The Guard" not in r.out and "Guard:" not in r.out


def test_quick_review_shows_the_verdict(env, cli):
    _set_prompts(env, "quick")
    env.script.guard = risky(mode="monitor", evidence_removed=1)
    r = cli(ASK, ["2", "web", "y", "a", "y"])
    assert r.code == 0, r.out
    assert "Guard: risky (decided by fixed rules, monitor)" in r.out
    assert "The Guard says: RISKY" in r.out
    assert "1 evidence part(s) were removed as possible prompt injection." in r.out


# ---- tasks and show ---------------------------------------------------------------------------


def test_tasks_shows_blocked(env, cli):
    env.script.list_blocked = True
    r = cli(["tasks"])
    assert r.code == 0, r.out
    assert BLOCKED_ID in r.out and "blocked (stopped by the Guard)" in r.out


def test_show_blocked(env, cli):
    r = cli(["show", BLOCKED_ID])
    assert r.code == 0, r.out
    assert f"Stopped by the Guard: {BLOCKED_SUMMARY}" in r.out
    assert "blocked: stopped by the Guard, nothing ran" in r.out
