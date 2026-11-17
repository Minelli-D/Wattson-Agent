"""The `ask` flow against the fake orchestrator: clarify, evidence, approval, the Claude Code run."""

from __future__ import annotations

import hashlib
import json
import stat

from cli_fakes import PROMPT

from wattson.shared.contracts import APPROVE, CLARIFY, COLLECT_EVIDENCE, RUN_CLAUDE_CODE


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


ASK = ["ask", "why does deployment/api restart?", "--new-session", "--headless"]


def test_happy_path_headless(env, cli):
    r = cli(ASK, ["2", "web", "y", "p", "a", "yes"])
    assert r.code == 0, r.out
    s = env.script
    # What was shown before anything was sent (FR-GIT-05).
    assert "What will be used" in r.out and "demo" in r.out and "main" in r.out
    assert "my-cluster (current, read-only access ready)" in r.out
    start = s.started[0]
    assert start.workspace.repo is not None and start.workspace.repo.name == "demo"
    assert start.workspace.reader_contexts == ["my-cluster"]
    # Clarify.
    assert s.received[CLARIFY][0].answers == {"q1": "devops-investigation", "q2": "web"}
    # Evidence: all three ran.
    items = s.received[COLLECT_EVIDENCE][0].items
    assert [i.request_id for i in items] == ["e1", "e2", "e3"]
    assert all(i.ok for i in items), items
    assert "CrashLoopBackOff" in items[2].output
    # Approval: warnings prominent, sources, the typed yes, the hash of the exact text.
    assert "Warnings" in r.out and "Grimoire answered slowly" in r.out
    assert "CrashLoopBackOff runbook" in r.out and "infra/k8s/crashloop-runbook" in r.out
    approve = s.received[APPROVE][0]
    assert approve.decision == "approve" and approve.edited is False
    assert approve.prompt_sha256 == sha(PROMPT)
    assert approve.session is not None and approve.session.id is None and approve.session.mode == "headless"
    # Claude Code got exactly the approved text.
    calls = env.claude_calls()
    assert len(calls) == 1 and calls[0]["prompt"] == PROMPT and "-p" in calls[0]["args"]
    run = s.received[RUN_CLAUDE_CODE][0]
    assert run.state == "completed" and "512Mi" in (run.summary or "")
    # The outcome.
    assert "Result" in r.out and "completed" in r.out
    # Local state: reported, prompt dropped, private.
    files = env.local_tasks()
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
    data = json.loads(files[0].read_text())
    assert data["reported"] is True and data["prompt"] is None and data["state"] == "completed"


def test_reject(env, cli):
    r = cli(ASK, ["2", "web", "y", "r", "not now"])
    assert r.code == 3, r.out
    approve = env.script.received[APPROVE][0]
    assert approve.decision == "reject" and approve.reason == "not now"
    assert env.claude_calls() == []
    assert "rejected" in r.out
    assert env.local_tasks() == []


def test_cancel_at_clarify(env, cli):
    r = cli(ASK, ["cancel"])
    assert r.code == 3, r.out
    assert env.script.received[CLARIFY][0].cancel is True
    assert COLLECT_EVIDENCE not in env.script.received
    assert env.claude_calls() == []


def test_end_of_input_at_clarify_cancels(env, cli):
    r = cli(ASK, [])
    assert r.code == 3
    assert env.script.received[CLARIFY][0].cancel is True


def test_no_submission_without_typed_yes(env, cli):
    # "y" and "no" are not the word yes; then quit and leave the task open.
    r = cli(ASK, ["2", "web", "y", "a", "y", "a", "no", "q"])
    assert r.code == 3, r.out
    assert r.out.count("Not approved.") == 2
    assert APPROVE not in env.script.received  # nothing was answered
    assert env.claude_calls() == []
    assert "Left open" in r.out
    assert env.local_tasks() == []


def test_full_prompt_shown_before_approval(env, cli):
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "This is the full prompt you are about to approve" in r.out
    assert "## Expected outcome" in r.out


def test_refused_evidence_is_shown_and_not_run(env, cli):
    from wattson.shared.contracts import EvidenceRequest

    env.script.plan += [
        EvidenceRequest(
            id="e4", op="k8s.get", cluster="my-cluster", namespace="web", kind="secrets", reason="x"
        ),
        EvidenceRequest(id="e5", op="k8s.events", cluster="other-ctx", namespace="web", reason="events"),
    ]
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert r.out.count("REFUSED") == 2
    assert "2 item(s) are refused" in r.out
    items = {i.request_id: i for i in env.script.received[COLLECT_EVIDENCE][0].items}
    assert items["e4"].refused and items["e5"].refused and not items["e4"].ok
    assert "wattson k8s add other-ctx" in items["e5"].refused
    kubectl_calls = env.kubectl_log.read_text()
    assert "secrets" not in kubectl_calls and "other-ctx" not in kubectl_calls.replace("get-contexts", "")


def test_declined_evidence(env, cli):
    r = cli(ASK, ["2", "web", "n", "a", "yes"])
    assert r.code == 0, r.out
    answer = env.script.received[COLLECT_EVIDENCE][0]
    assert answer.declined is True and answer.items == []
    assert "Not collected" in r.out


def test_yes_evidence_flag_skips_the_question(env, cli):
    r = cli([*ASK, "--yes-evidence"], ["2", "web", "a", "yes"])
    assert r.code == 0, r.out
    assert "Collect this?" not in r.out
    assert len(env.script.received[COLLECT_EVIDENCE][0].items) == 3


def test_edit_in_editor(env, cli, monkeypatch):
    from cli_fakes import FIXTURES

    monkeypatch.setenv("EDITOR", str(FIXTURES / "append_editor"))
    r = cli(ASK, ["2", "web", "y", "e", "a", "yes"])
    assert r.code == 0, r.out
    edited = PROMPT + "Also check the memory limit of the api container.\n"
    approve = env.script.received[APPROVE][0]
    assert approve.edited is True
    assert approve.prompt_sha256 == sha(edited)
    assert env.claude_calls()[0]["prompt"] == edited
    assert "Prompt changed" in r.out
    # The temp file is gone.
    assert not list(env.tmp.glob("wattson-prompt-*"))


def test_answer_missing_information(env, cli):
    r = cli(ASK, ["2", "web", "y", "m", "since Monday", "a", "yes"])
    assert r.code == 0, r.out
    first, second = env.script.received[APPROVE]
    assert first.decision == "answer" and first.answers == {"When did the restarts start?": "since Monday"}
    assert second.decision == "approve"
    rerendered = PROMPT + "\n- When did the restarts start? since Monday"
    assert second.prompt_sha256 == sha(rerendered)
    assert env.claude_calls()[0]["prompt"] == rerendered


def test_hash_mismatch_is_refused(env, cli):
    env.script.run_hash = "0" * 64
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 1, r.out
    assert env.claude_calls() == []
    run = env.script.received[RUN_CLAUDE_CODE][0]
    assert run.state == "failed" and "hash mismatch" in (run.error or "")
    assert "Nothing was sent to Claude Code" in r.out


def test_late_report_when_the_task_is_gone(env, cli, monkeypatch):
    from wattson.connector import claude as cc

    real = cc.submit

    def submit_then_restart(*args, **kwargs):
        result = real(*args, **kwargs)
        env.orchestrator.drop_tasks()  # the orchestrator pod restarted during the run
        return result

    monkeypatch.setattr(cc, "submit", submit_then_restart)
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert RUN_CLAUDE_CODE not in env.script.received
    ((task_id, report),) = env.script.executions
    assert report.result.state == "completed" and "512Mi" in (report.result.summary or "")
    assert task_id.startswith("task-")
    assert "reported late" in r.out
    data = json.loads(env.local_tasks()[0].read_text())
    assert data["reported"] is True and data["prompt"] is None


def test_claude_failure_is_reported(env, cli, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 1, r.out
    run = env.script.received[RUN_CLAUDE_CODE][0]
    assert run.state == "failed" and "overloaded" in (run.error or "")


def test_interactive_run(env, cli):
    r = cli(["ask", "why does deployment/api restart?", "--new-session"], ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "Claude Code opens now" in r.out
    call = env.claude_calls()[0]
    assert "-p" not in call["args"] and call["args"][-1] == PROMPT and "--session-id" in call["args"]
    assert env.script.received[RUN_CLAUDE_CODE][0].state == "completed"


def _old_session(env, maybe_open: bool) -> str:
    import os
    import re
    import time

    sid = "11111111-2222-3333-4444-555555555555"
    folder = env.claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(env.repo.resolve()))
    folder.mkdir(parents=True)
    path = folder / f"{sid}.jsonl"
    lines = [
        {
            "type": "user",
            "timestamp": "2026-10-20T10:00:00+00:00",
            "message": {"content": "fix the login page"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-20T10:01:00+00:00",
            "message": {"id": "a1", "content": [{"type": "text", "text": "Done."}]},
        },
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    if not maybe_open:
        old = time.time() - 3600
        os.utime(path, (old, old))
    return sid


def test_pick_the_one_session(env, cli):
    sid = _old_session(env, maybe_open=False)
    r = cli(["ask", "why?", "--headless"], ["2", "web", "y", "a", "yes", "y"])
    assert r.code == 0, r.out
    assert "fix the login page" in r.out
    args = env.claude_calls()[0]["args"]
    assert args[args.index("--resume") + 1] == sid and "--fork-session" not in args
    assert env.script.received[APPROVE][0].session.id == sid


def test_session_maybe_open_offers_fork(env, cli):
    sid = _old_session(env, maybe_open=True)
    r = cli(["ask", "why?", "--headless"], ["2", "web", "y", "a", "yes", "y", "1"])
    assert r.code == 0, r.out
    assert "may be open in another window" in r.out
    args = env.claude_calls()[0]["args"]
    assert args[args.index("--resume") + 1] == sid and "--fork-session" in args
    assert env.script.received[APPROVE][0].session.fork is True


def test_session_flag(env, cli):
    sid = _old_session(env, maybe_open=False)
    r = cli(["ask", "why?", "--headless", "--session", sid], ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "Claude Code conversations" not in r.out
    args = env.claude_calls()[0]["args"]
    assert args[args.index("--resume") + 1] == sid


def test_usage_errors(env, cli):
    assert cli(["ask", "  "]).code == 2
    assert cli(["ask", "x", "--cluster", "nope"]).code == 2
    assert cli(["ask", "x", "--workload", "api"]).code == 2
    assert cli(["ask", "x", "--repo", str(env.tmp)]).code == 2  # not a git repository
    assert cli(["ask", "x", "--session", "a", "--new-session"]).code == 2
    assert env.script.started == []


# ---- the Wattson procedure ---------------------------------------------------------------------


def test_procedure_flag_is_sent_as_an_override(env, cli):
    r = cli([*ASK, "--procedure", "K8s-Storage"], ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert env.script.started[0].overrides.procedure == "k8s-storage"
    assert "Procedure (yours)" in r.out
    r = cli([*ASK, "--procedure", "none"], ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert env.script.started[-1].overrides.procedure == "none"


def test_bad_procedure_flag(env, cli):
    assert cli([*ASK, "--procedure", "rm -rf /"]).code == 2
    assert env.script.started == []


def test_the_procedure_question_uses_the_numbered_options(env, cli):
    from wattson.shared.contracts import Question

    env.script.questions = [
        Question(
            id="procedure",
            field="procedure",
            text="The small model chose k8s-tls-ingress, but the words point to k8s-storage. Which fits?",
            options=["k8s-tls-ingress", "k8s-storage", "none"],
        )
    ]
    r = cli(ASK, ["2", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "1. k8s-tls-ingress" in r.out and "3. none" in r.out
    assert env.script.received[CLARIFY][0].answers == {"procedure": "k8s-storage"}


def test_review_shows_the_procedure(env, cli):
    from cli_fakes import SOURCE

    procedure = SOURCE.model_copy(
        update={"key": "wattson/k8s-pod-crashloop.md", "title": "k8s-pod-crashloop", "why": "procedure"}
    )
    env.script.sources = [procedure, SOURCE.model_copy(update={"why": "related"})]
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "Wattson procedure: k8s-pod-crashloop (wattson/k8s-pod-crashloop.md)" in r.out
    assert r.out.index("Wattson procedure:") < r.out.index("Proposed task for Claude Code")


def test_no_procedure_line_without_a_procedure(env, cli):
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "Wattson procedure:" not in r.out


def _set_prompts(env, mode: str) -> None:
    (env.config / "config.json").write_text(
        json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire", "prompts": mode})
    )


def test_quick_accepts_single_y(env, cli):
    _set_prompts(env, "quick")
    r = cli(ASK, ["2", "web", "y", "a", "y"])
    assert r.code == 0, r.out
    assert "Type y or yes to approve" in r.out
    assert env.script.received[APPROVE][0].decision == "approve"
    assert len(env.claude_calls()) == 1


def test_quick_output_is_compact(env, cli):
    _set_prompts(env, "quick")
    r = cli(ASK, ["2", "web", "y", "a", "y"])
    assert r.code == 0, r.out
    assert "[p]rompt [e]dit" in r.out
    assert "Proposed task for Claude Code" not in r.out
    assert "Approving sends this exact prompt" not in r.out


def test_careful_output_is_unchanged(env, cli):
    _set_prompts(env, "careful")
    r = cli(ASK, ["2", "web", "y", "a", "yes"])
    assert r.code == 0, r.out
    assert "Proposed task for Claude Code" in r.out
    assert "Approving sends this exact prompt" in r.out


def test_careful_refuses_single_y(env, cli):
    _set_prompts(env, "careful")
    r = cli(ASK, ["2", "web", "y", "a", "y", "q"])
    assert r.code == 3, r.out
    assert "Type yes to approve (anything else goes back)" in r.out
    assert "Not approved." in r.out
    assert APPROVE not in env.script.received
    assert env.claude_calls() == []


def test_quick_continues_newest_session_without_confirm(env, cli):
    _set_prompts(env, "quick")
    sid = _old_session(env, maybe_open=False)
    r = cli(["ask", "why?", "--headless"], ["2", "web", "y", "a", "y"])
    assert r.code == 0, r.out
    assert "Continue this conversation?" not in r.out
    assert "Claude Code conversations for this folder" not in r.out
    assert "Continuing the newest conversation" in r.out
    args = env.claude_calls()[0]["args"]
    assert args[args.index("--resume") + 1] == sid and "--fork-session" not in args
    assert env.script.received[APPROVE][0].session.id == sid


def test_quick_maybe_open_keeps_warning_flow(env, cli):
    _set_prompts(env, "quick")
    sid = _old_session(env, maybe_open=True)
    r = cli(["ask", "why?", "--headless"], ["2", "web", "y", "a", "y", "y", "1"])
    assert r.code == 0, r.out
    assert "may be open in another window" in r.out
    args = env.claude_calls()[0]["args"]
    assert args[args.index("--resume") + 1] == sid and "--fork-session" in args
