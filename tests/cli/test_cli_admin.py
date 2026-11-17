"""The commands around the admin routes: tasks, show, recover, status, grimoire, sessions, k8s."""

from __future__ import annotations

import json
import re

from wattson.cli import localstate
from wattson.cli.main import main
from wattson.shared.contracts import RunResponse, SessionChoice


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "Wattson Agent 0.1.0" in capsys.readouterr().out


def test_help_shows_the_wattson_command(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: wattson ")
    assert 'wattson ask "' in out and "old command" not in out


def test_no_command(capsys):
    assert main([]) == 2


def test_status(env, cli):
    r = cli(["status"])
    assert r.code == 0, r.out
    assert "mode 600" in r.out and "version 0.1.0" in r.out
    assert "not connected: run wattson grimoire connect" in r.out


def test_tasks_marks_local_unfinished(env, cli):
    task = localstate.LocalTask(
        task_id="task-aaa",
        request="why?",
        prompt="p",
        prompt_sha256="ab" * 32,
        session=SessionChoice(),
        cwd=str(env.repo),
        state="completed",
        result=RunResponse(state="completed"),
    )
    localstate.save(task)
    other = task.model_copy(update={"task_id": "task-zzz"})
    localstate.save(other)
    r = cli(["tasks", "--limit", "5"])
    assert r.code == 0, r.out
    assert "task-aaa" in r.out and "task-bbb" in r.out
    assert "wattson recover task-aaa" in r.out
    assert "Open on this laptop" in r.out and "task-zzz" in r.out


def test_show(env, cli):
    r = cli(["show", "task-aaa"])
    assert r.code == 0, r.out
    assert "devops-investigation" in r.out and "Claude Code finished." in r.out
    r = cli(["show", "missing"])
    assert r.code == 1 and "has no task missing" in r.out


def _session_with_reply(env, sid: str) -> None:
    folder = env.claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(env.repo.resolve()))
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": "2026-10-22T09:59:00+00:00", "message": {"content": "the prompt"}},
        {
            "type": "assistant",
            "timestamp": "2099-01-01T00:00:00+00:00",
            "message": {"id": "a1", "content": [{"type": "text", "text": "Fixed: the limit is now 512Mi."}]},
        },
    ]
    (folder / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_recover_reads_the_session_and_reports(env, cli):
    sid = "11111111-2222-3333-4444-555555555555"
    _session_with_reply(env, sid)
    task = localstate.LocalTask(
        task_id="task-r1",
        request="why?",
        prompt="the prompt",
        prompt_sha256="ab" * 32,
        session=SessionChoice(id=None, mode="headless"),
        cwd=str(env.repo.resolve()),
        submitted_at="2026-10-22T10:00:00+00:00",
        state="interrupted",
        result=RunResponse(state="interrupted", session_id=sid, error="stopped with Ctrl-C"),
        reported=True,
    )
    localstate.save(task)
    r = cli(["recover", "task-r1"])
    assert r.code == 0, r.out
    ((task_id, report),) = env.script.executions
    assert task_id == "task-r1" and report.result.state == "completed"
    assert "512Mi" in (report.result.summary or "")
    loaded = localstate.load("task-r1")
    assert loaded.reported and loaded.state == "completed" and loaded.prompt is None
    assert cli(["recover", "task-r1"]).code == 0  # already reported: nothing to do
    assert len(env.script.executions) == 1


def test_recover_known_result(env, cli):
    task = localstate.LocalTask(
        task_id="task-r2",
        request="why?",
        prompt="p",
        prompt_sha256="ab" * 32,
        session=SessionChoice(),
        cwd=str(env.repo),
        state="failed",
        result=RunResponse(state="failed", error="exit code 1"),
    )
    localstate.save(task)
    r = cli(["recover", "task-r2"])
    assert r.code == 0, r.out
    assert env.script.executions[0][1].result.error == "exit code 1"


def test_recover_unknown(env, cli):
    assert cli(["recover", "task-none"]).code == 1


def test_grimoire_connect(env, cli):
    r = cli(["grimoire", "connect", "--port", "0", "--no-browser", "--wait", "10"])
    assert r.code == 0, r.out
    assert "https://grimoire.example.test/authorize" in r.out
    assert env.script.redirects[0].startswith("http://127.0.0.1:") and env.script.redirects[0].endswith(
        "/callback"
    )
    assert env.script.callbacks[0].code == "c0de" and env.script.callbacks[0].state == "st4te"
    assert "You can close this tab" in env.script.browser_pages[0]
    assert "Grimoire is connected" in r.out
    r = cli(["grimoire", "status"])
    assert r.code == 0 and "search_documents" in r.out
    r = cli(["grimoire", "disconnect"])
    assert r.code == 0 and "forgot" in r.out


def test_sessions(env, cli):
    _session_with_reply(env, "11111111-2222-3333-4444-555555555555")
    r = cli(["sessions", "--repo", str(env.repo)])
    assert r.code == 0, r.out
    assert "11111111-2222-3333-4444-555555555555" in r.out and "the prompt" in r.out


def test_k8s_list(env, cli):
    r = cli(["k8s", "list"])
    assert r.code == 0, r.out
    assert "my-cluster" in r.out and "ready" in r.out and "wattson k8s add other-ctx" in r.out


def test_k8s_add_says_no(env, cli):
    r = cli(["k8s", "add", "other-ctx", "--namespace", "team"], ["n"])
    assert r.code == 3, r.out
    assert "ClusterRoleBinding" in r.out or "wattson-reader" in r.out
    assert "namespace 'team'" in r.out
    assert "apply" not in env.kubectl_log.read_text()


def test_k8s_add_uses_wattson_namespace_on_its_own_cluster(env, cli):
    (env.config / "config.json").write_text(
        json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire"})
    )
    r = cli(["k8s", "add", "my-cluster"], ["n"])
    assert r.code == 3, r.out
    assert "namespace 'grimoire'" in r.out


def test_k8s_add_needs_a_namespace_on_another_cluster(env, cli):
    (env.config / "config.json").write_text(
        json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire"})
    )
    r = cli(["k8s", "add", "other-ctx"], [])
    assert r.code == 2, r.out
    assert "--namespace" in r.out
    assert not env.kubectl_log.exists()  # refused before any kubectl call


def test_wrong_token_is_refused(env, cli):
    (env.config / "token").write_text("x" * 40)
    r = cli(["status"])
    assert r.code == 1 and "refused the token" in r.out


def test_agent_unreachable(env, cli, monkeypatch):
    monkeypatch.setenv("WATTSON_URL", "http://127.0.0.1:9")
    r = cli(["tasks"])
    assert r.code == 1 and "cannot reach the agent" in r.out
