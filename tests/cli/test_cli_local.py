"""Config, token file, local task records."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from cli_fakes import TOKEN

from wattson.cli import localstate, migrate
from wattson.cli.config import ConfigError, load_config, read_token
from wattson.shared.contracts import RunResponse, SessionChoice


def test_config_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire"}))
    cfg = load_config(path)
    assert cfg is not None
    assert (cfg.deployment, cfg.remote_port) == ("wattson-orchestrator", 9000)
    assert load_config(tmp_path / "missing.json") is None
    path.write_text("{}")
    with pytest.raises(ConfigError, match="no namespace"):
        load_config(path)


def test_config_prompts_mode(tmp_path, monkeypatch):
    from wattson.cli.config import prompt_mode

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"namespace": "grimoire", "prompts": "quick"}))
    assert load_config(path).prompts == "quick"
    path.write_text(json.dumps({"namespace": "grimoire"}))
    assert load_config(path).prompts == "careful"
    path.write_text(json.dumps({"namespace": "grimoire", "prompts": "loud"}))
    with pytest.raises(ConfigError, match='prompts must be "careful" or "quick"'):
        load_config(path)
    # The helper reads the real location and falls back to careful when there is no usable config.
    monkeypatch.setenv("WATTSON_CONFIG_DIR", str(tmp_path))
    assert prompt_mode() == "careful"  # the file now says "loud"
    path.write_text(json.dumps({"namespace": "grimoire", "prompts": "quick"}))
    assert prompt_mode() == "quick"
    path.unlink()
    assert prompt_mode() == "careful"


def test_token_file_mode_refused(env, cli):
    token = env.config / "token"
    token.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        read_token(token)
    r = cli(["status"])
    assert r.code == 1
    assert "can be read by other users (mode 644)" in r.out
    assert "version 0.1.0" not in r.out  # the agent was not asked
    r = cli(["tasks"])
    assert r.code == 1 and "chmod 600" in r.out


def test_token_file_missing(env, cli):
    (env.config / "token").unlink()
    r = cli(["show", "task-aaa"])
    assert r.code == 1 and "does not exist" in r.out


def test_no_config_and_no_url(env, cli, monkeypatch):
    monkeypatch.delenv("WATTSON_URL")
    r = cli(["status"])
    assert r.code == 1
    assert "Run k8s/deploy.sh first" in r.out


def _task(task_id: str = "task-1") -> localstate.LocalTask:
    return localstate.LocalTask(
        task_id=task_id,
        request="why?",
        prompt="the prompt",
        prompt_sha256="ab" * 32,
        session=SessionChoice(id=None, mode="headless"),
        cwd="/nowhere",
        approved_at=localstate.now_iso(),
    )


def test_localstate_lifecycle(env):
    task = _task()
    path = localstate.save(task)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [t.task_id for t in localstate.list_unfinished()] == ["task-1"]
    localstate.record_result(task, RunResponse(state="completed", summary="done"))
    assert localstate.load("task-1").state == "completed"
    assert localstate.list_unfinished()  # not reported yet
    localstate.mark_reported(task)
    loaded = localstate.load("task-1")
    assert loaded.reported and loaded.prompt is None
    assert localstate.list_unfinished() == []
    assert localstate.load("../etc/passwd") is None


# ---- the one-time copy of the folders of the old name (myagent -> wattson) -------------------


def _old_folders(home: Path) -> Path:
    """The laptop folders as the CLI wrote them before the rename: settings, token, read-only
    kubeconfig, one local task record. Returns the old settings folder."""
    old = home / ".config" / "myagent"
    (old / "kube").mkdir(parents=True)
    files = {
        old / "config.json": json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire"}),
        old / "token": TOKEN + "\n",
        old / "kube" / "my-cluster.yaml": json.dumps({"current-context": "my-cluster"}),
        old / "kube" / "notes.txt": "not a kubeconfig",
        home / ".local" / "state" / "myagent" / "tasks" / "task-old.json": "{}",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
    (home / "elsewhere.yaml").write_text("{}")
    (old / "kube" / "link.yaml").symlink_to(home / "elsewhere.yaml")
    return old


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_old_folders_are_copied_once(tmp_path):
    home = tmp_path / "home"
    old = _old_folders(home)
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    copied, problems = migrate.migrate(home, environ={})
    new, new_state = home / ".config" / "wattson", home / ".local" / "state" / "wattson"
    assert problems == []
    assert copied == [(old, new), (home / ".local" / "state" / "myagent", new_state)]
    assert sorted(p.relative_to(new) for p in new.rglob("*")) == [
        Path("config.json"),
        Path("kube"),
        Path("kube/my-cluster.yaml"),
        Path("token"),
    ]  # no notes.txt, no link
    assert [p.name for p in (new_state / "tasks").iterdir()] == ["task-old.json"]
    assert (new / "token").read_text() == TOKEN + "\n"
    copies = [new / "config.json", new / "token", new / "kube" / "my-cluster.yaml"]
    copies.append(new_state / "tasks" / "task-old.json")
    for path in copies:
        assert _mode(path) == 0o600, path
    for folder in (new, new / "kube", new_state, new_state / "tasks"):
        assert _mode(folder) == 0o700, folder
    # the old folders are not changed, and no temporary folder is left
    after = sorted(p.relative_to(home) for p in home.rglob("*"))
    assert [p for p in after if "wattson" not in str(p)] == before
    assert not [p for p in (home / ".config").iterdir() if p.name.startswith(".")]
    line = migrate.message(copied, home)
    assert line is not None and "\n" not in line
    assert "~/.config/myagent to ~/.config/wattson" in line
    assert "~/.local/state/myagent to ~/.local/state/wattson" in line
    # once: a new folder that is there is never filled again
    (new / "token").write_text("x" * 40)
    assert migrate.migrate(home, environ={}) == ([], [])
    assert (new / "token").read_text() == "x" * 40


def test_old_folders_not_copied_into_a_folder_of_its_own(tmp_path):
    home = tmp_path / "home"
    _old_folders(home)
    copied, _ = migrate.migrate(home, environ={"WATTSON_CONFIG_DIR": str(tmp_path / "mine")})
    assert [new for _, new in copied] == [home / ".local" / "state" / "wattson"]
    assert not (home / ".config" / "wattson").exists() and not (tmp_path / "mine").exists()
    empty = tmp_path / "empty-home"
    (empty / ".config" / "myagent" / "kube").mkdir(parents=True)  # nothing to copy in it
    assert migrate.migrate(empty, environ={}) == ([], [])
    assert not (empty / ".config" / "wattson").exists()


def test_old_folders_an_empty_new_folder_counts_as_missing(tmp_path):
    home = tmp_path / "home"
    _old_folders(home)
    empty_state = home / ".local" / "state" / "wattson"
    (empty_state / "tasks").mkdir(parents=True)  # made by something else, with no file in it
    (home / ".config" / "wattson").mkdir(parents=True)
    (home / ".config" / "wattson" / "token").write_text("a new token")  # a file: it is there
    copied, problems = migrate.migrate(home, environ={})
    assert problems == [] and [new for _, new in copied] == [empty_state]
    assert [p.name for p in (empty_state / "tasks").iterdir()] == ["task-old.json"]
    assert _mode(empty_state) == 0o700 and _mode(empty_state / "tasks") == 0o700
    assert sorted(p.name for p in (home / ".config" / "wattson").iterdir()) == ["token"]


def test_old_folders_a_failed_copy_leaves_no_new_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _old_folders(home)
    real = migrate._copy_private

    def failing(source, target):
        if source.name == "token":
            raise PermissionError(13, "Permission denied")
        real(source, target)

    monkeypatch.setattr(migrate, "_copy_private", failing)
    copied, problems = migrate.migrate(home, environ={"WATTSON_STATE_DIR": str(tmp_path / "state")})
    assert copied == []
    assert len(problems) == 1 and "could not copy" in problems[0] and "Permission denied" in problems[0]
    assert sorted(p.name for p in (home / ".config").iterdir()) == ["myagent"]  # no new, no temp folder


def test_the_cli_copies_the_old_folders_and_uses_them(env, cli, monkeypatch, tmp_path):
    home = tmp_path / "home"
    _old_folders(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WATTSON_CONFIG_DIR")
    monkeypatch.delenv("WATTSON_STATE_DIR")
    r = cli(["k8s", "list"])
    assert r.code == 0, r.out
    assert r.out.count("Copied your settings of the old name myagent") == 1
    assert "my-cluster" in r.out and "ready" in r.out  # the copied read-only kubeconfig is used
    assert (home / ".config" / "wattson" / "kube" / "my-cluster.yaml").exists()
    r = cli(["k8s", "list"])
    assert r.code == 0 and "Copied" not in r.out
