import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wattson.connector import claude
from wattson.shared.contracts import SessionChoice

OLD = "2026-01-01T10:00:00.000Z"


def line(kind: str, content, ts: str = OLD, **extra) -> dict:
    role = "user" if kind == "user" else "assistant"
    msg = {"role": role, "content": content}
    if kind == "assistant":
        msg["id"] = extra.pop("mid", "msg_" + uuid.uuid4().hex[:8])
    return {"type": kind, "isSidechain": False, "timestamp": ts, "sessionId": "x", "message": msg, **extra}


def write_session(cwd: Path, sid: str, lines: list[dict], mtime: float | None = None) -> Path:
    folder = claude.project_dir(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def a_session(cwd: Path, mtime: float | None = None, prompt: str = "Fix the login bug") -> str:
    sid = str(uuid.uuid4())
    write_session(
        cwd,
        sid,
        [
            {"type": "permission-mode", "permissionMode": "auto", "sessionId": sid},
            {"type": "ai-title", "aiTitle": "Login bug", "sessionId": sid},
            line("user", "<command-name>/clear</command-name>"),
            line("user", "hidden", isMeta=True),
            line("user", prompt),
            line("assistant", [{"type": "text", "text": "OLD REPLY"}], mid="m1"),
            line("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], mid="m1"),
            line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            line("assistant", [{"type": "text", "text": "side"}], isSidechain=True),
        ],
        mtime,
    )
    return sid


def test_project_dir_name_matches_claude_code():
    assert claude.project_dir_name("/home/dm/Desktop/n/improve_eng") == "-home-dm-Desktop-n-improve-eng"
    assert claude.project_dir_name("/x/.claude/worktrees/a.b") == "-x--claude-worktrees-a-b"


def test_claude_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("WATTSON_CLAUDE_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert claude.claude_home() == tmp_path / "cfg"
    monkeypatch.setenv("WATTSON_CLAUDE_HOME", str(tmp_path / "mine"))
    assert claude.claude_home() == tmp_path / "mine"


def test_list_sessions(claude_calls, repo: Path):
    now = time.time()
    old = a_session(repo, now - 3600, prompt="Old work " + "x" * 200)
    new = a_session(
        repo, now - 10, prompt="Use token " + "gh" + "p_FAKE0123456789abcdefFAKE0123456789ab" + " now"
    )
    write_session(repo, str(uuid.uuid4()), [{"type": "file-history-snapshot", "snapshot": {}}], now)  # empty
    write_session(repo, "agent-abc", [line("user", "subagent")], now)  # not a session id
    sessions = claude.list_sessions(repo)
    assert [s.id for s in sessions] == [new, old]
    first, second = sessions
    assert first.maybe_open and not second.maybe_open
    assert "ghp_FAKE" not in first.first_prompt and "[REDACTED:" in first.first_prompt
    assert second.first_prompt.startswith("Old work") and len(second.first_prompt) == 100
    assert first.messages == 2  # one prompt typed + one assistant message
    datetime.fromisoformat(first.last_activity)
    assert claude.list_sessions(repo, limit=1)[0].id == new
    assert claude.list_sessions(repo / "src") == []
    assert claude_calls() == []  # listing never runs claude


def test_new_interactive_session(claude_calls, repo: Path):
    res = claude.submit("Investigate the crash", repo, SessionChoice())
    [call] = claude_calls()
    assert call["args"][0] == "--session-id" and call["args"][2:] == ["Investigate the crash"]
    uuid.UUID(call["args"][1])
    assert call["cwd"] == str(repo.resolve())
    assert res.state == "completed" and res.exit_code == 0
    assert res.session_id == call["args"][1]
    assert res.summary == "Fake reply to: Investigate the crash"
    assert res.duration_ms is not None


def test_resume_reads_only_the_new_reply(claude_calls, repo: Path):
    sid = a_session(repo)
    res = claude.submit("Next step", repo, SessionChoice(id=sid))
    [call] = claude_calls()
    assert call["args"] == ["--resume", sid, "Next step"]
    assert res.state == "completed" and res.session_id == sid
    assert res.summary == "Fake reply to: Next step"


def test_fork(claude_calls, repo: Path):
    sid = a_session(repo)
    res = claude.submit("Try another way", repo, SessionChoice(id=sid, fork=True))
    [call] = claude_calls()
    assert call["args"] == ["--resume", sid, "--fork-session", "Try another way"]
    assert res.state == "completed" and res.session_id not in (None, sid)
    assert res.summary == "Fake reply to: Try another way"


def test_prompt_that_starts_with_a_dash(claude_calls, repo: Path):
    res = claude.submit("- check the logs", repo, SessionChoice())
    [call] = claude_calls()
    assert call["args"][2:] == ["--", "- check the logs"]
    assert res.state == "completed"


@pytest.mark.parametrize(
    ("mode", "state", "error"),
    [
        ("fail", "failed", "exited with code 2"),
        ("noreply", "interrupted", "without a reply"),
        ("signal", "interrupted", "signal 15"),
    ],
)
def test_interactive_outcomes(claude_calls, repo: Path, monkeypatch: pytest.MonkeyPatch, mode, state, error):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    res = claude.submit("Do it", repo, SessionChoice())
    assert res.state == state and error in res.error and res.summary is None


def test_resume_of_a_missing_session_fails(claude_calls, repo: Path):
    res = claude.submit("Do it", repo, SessionChoice(id=str(uuid.uuid4())))
    assert res.state == "failed" and res.exit_code == 1


def test_headless_new(claude_calls, repo: Path):
    res = claude.submit("Summarise the repo", repo, SessionChoice(mode="headless"))
    [call] = claude_calls()
    assert call["args"][:3] == ["-p", "--output-format", "json"]
    assert call["args"][3] == "--session-id"
    assert call["stdin"] == "Summarise the repo"
    assert "Summarise the repo" not in call["args"]  # the prompt goes on stdin only
    assert res.state == "completed" and res.session_id == call["args"][4]
    assert res.summary == "Fake reply to: Summarise the repo"


def test_headless_resume_and_fork(claude_calls, repo: Path):
    sid = a_session(repo)
    a = claude.submit("One", repo, SessionChoice(id=sid, mode="headless"))
    b = claude.submit("Two", repo, SessionChoice(id=sid, mode="headless", fork=True))
    first, second = claude_calls()
    assert first["args"] == ["-p", "--output-format", "json", "--resume", sid]
    assert second["args"] == ["-p", "--output-format", "json", "--resume", sid, "--fork-session"]
    assert a.session_id == sid and b.session_id not in (None, sid)


def test_headless_permission_denials_are_in_the_summary(claude_calls, repo: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "denials")
    res = claude.submit("Deploy", repo, SessionChoice(mode="headless"))
    assert res.state == "completed"
    assert "refused 3 tool uses" in res.summary and "Bash (2), Edit" in res.summary


def test_headless_error_result(claude_calls, repo: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error_result")
    res = claude.submit("Deploy", repo, SessionChoice(mode="headless"))
    assert res.state == "failed" and "overloaded" in res.error


def test_headless_failure_without_json(claude_calls, repo: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    res = claude.submit("Deploy", repo, SessionChoice(mode="headless"))
    assert res.state == "failed" and "something broke" in res.error and res.exit_code == 2


@pytest.mark.parametrize("mode", ["sleep", "stubborn"])
def test_headless_timeout(claude_calls, repo: Path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setattr(claude, "STOP_GRACE_S", 0.5)
    started = time.monotonic()
    res = claude.submit("Slow", repo, SessionChoice(mode="headless"), timeout_s=1)
    assert time.monotonic() - started < 10
    assert res.state == "interrupted" and "timed out after 1 s" in res.error


def test_summary_is_redacted(claude_calls, repo: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "secret")
    for mode in ("interactive", "headless"):
        res = claude.submit("Use the token", repo, SessionChoice(mode=mode))
        assert res.state == "completed"
        assert "ghp_FAKE" not in res.summary and "[REDACTED:github-token]" in res.summary


def test_summary_length():
    note = claude.denials_note([{"tool_name": "Bash"}])
    text = claude._summary("y" * 5000, note)
    assert len(text) <= claude.SUMMARY_MAX and text.endswith(note)


def test_missing_binary(claude_calls, repo: Path, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WATTSON_CLAUDE_BIN", "/nonexistent/claude")
    res = claude.submit("x", repo, SessionChoice())
    assert res.state == "failed" and res.error == "Claude Code CLI not found"
    monkeypatch.delenv("WATTSON_CLAUDE_BIN")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    res = claude.submit("x", repo, SessionChoice(mode="headless"))
    assert res.state == "failed" and res.error == "Claude Code CLI not found"


def test_never_a_bypass_flag(claude_calls, repo: Path):
    sid = a_session(repo)
    for choice in (
        SessionChoice(),
        SessionChoice(id=sid),
        SessionChoice(id=sid, fork=True),
        SessionChoice(mode="headless"),
        SessionChoice(id=sid, mode="headless", fork=True),
    ):
        claude.submit("go", repo, choice)
    calls = claude_calls()
    assert len(calls) == 5
    for call in calls:
        for arg in call["args"]:
            assert "dangerously" not in arg and "bypass" not in arg.lower()
            assert not arg.startswith("--permission-mode")
    for flag in claude.FORBIDDEN_FLAGS:
        with pytest.raises(RuntimeError, match="FR-CC-05"):
            claude.check_no_bypass(["claude", flag, "x"])
    with pytest.raises(RuntimeError):
        claude.check_no_bypass(["claude", "--permission-mode=bypassPermissions"])


def test_invalid_session_id(claude_calls, repo: Path):
    res = claude.submit("x", repo, SessionChoice(id="../../etc/passwd"))
    assert res.state == "failed" and claude_calls() == []


def test_recover(claude_calls, repo: Path):
    since = datetime.now(UTC).isoformat()
    res = claude.submit("Late work", repo, SessionChoice(mode="headless"))
    got = claude.recover(repo, res.session_id, since)
    assert got is not None and got.state == "completed" and got.session_id == res.session_id
    assert got.summary == "Fake reply to: Late work"
    later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    assert claude.recover(repo, res.session_id, later) is None
    assert claude.recover(repo, str(uuid.uuid4()), since) is None
    assert claude.recover(repo, "../x", since) is None


def test_recover_ignores_old_replies(claude_calls, repo: Path):
    sid = a_session(repo)
    assert claude.recover(repo, sid, "2026-01-01T09:00:00Z").summary == "OLD REPLY"
    assert claude.recover(repo, sid, "2026-01-01T11:00:00") is None  # no zone: UTC


def test_recover_without_a_start_time(claude_calls, repo: Path):
    sid = a_session(repo)
    assert claude.recover(repo, sid, "").summary == "OLD REPLY"
    with pytest.raises(ValueError):
        claude.recover(repo, sid, "yesterday")


def test_new_conversation_uses_the_id_chosen_by_the_caller(claude_calls, repo: Path):
    # The CLI saves this id before the run, so `wattson recover` finds the conversation later.
    chosen = "3f0c2a8e-1b7d-4c55-9a61-0d2e8f4b7c19"
    res = claude.submit("Summarise", repo, SessionChoice(mode="headless"), new_session_id=chosen)
    [call] = claude_calls()
    assert call["args"][3:5] == ["--session-id", chosen]
    assert res.session_id == chosen


def test_a_bad_chosen_id_is_refused_before_anything_runs(claude_calls, repo: Path):
    res = claude.submit("Summarise", repo, SessionChoice(mode="headless"), new_session_id="not-a-uuid")
    assert res.state == "failed" and claude_calls() == []
