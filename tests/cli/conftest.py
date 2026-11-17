"""Fixtures for the CLI tests: the fake orchestrator, a private config/state/claude home, a small git
repository, and a helper that runs `wattson` with scripted input. Nothing here touches a real
cluster, the real Grimoire or the real `claude`."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from cli_fakes import FIXTURES, TOKEN, FakeOrchestrator, Script

from wattson.cli.main import main
from wattson.cli.ui import UI


@pytest.fixture(scope="session")
def orchestrator() -> Iterator[FakeOrchestrator]:
    fake = FakeOrchestrator().start()
    yield fake
    fake.stop()


@dataclass
class Env:
    tmp: Path
    config: Path
    state: Path
    claude_home: Path
    repo: Path
    claude_log: Path
    kubectl_log: Path
    script: Script
    orchestrator: FakeOrchestrator

    def claude_calls(self) -> list[dict]:
        if not self.claude_log.exists():
            return []
        return [json.loads(line) for line in self.claude_log.read_text().splitlines() if line.strip()]

    def local_tasks(self) -> list[Path]:
        folder = self.state / "tasks"
        return sorted(folder.glob("*.json")) if folder.exists() else []


def _git(repo: Path, *args: str) -> None:
    git = ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run([*git, *args], check=True, capture_output=True)  # noqa: S603 - a temp repository


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: FakeOrchestrator) -> Env:
    config = tmp_path / "config"
    (config / "kube").mkdir(parents=True)
    token = config / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    # A read-only kubeconfig for my-cluster, as `wattson k8s add my-cluster` writes it (none for other-ctx).
    reader = config / "kube" / "my-cluster.yaml"
    reader.write_text(json.dumps({"current-context": "my-cluster"}))
    reader.chmod(0o600)

    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# demo\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "first commit")

    e = Env(
        tmp=tmp_path,
        config=config,
        state=tmp_path / "state",
        claude_home=tmp_path / "claude-home",
        repo=repo,
        claude_log=tmp_path / "claude.log",
        kubectl_log=tmp_path / "kubectl.log",
        script=orchestrator.reset(),
        orchestrator=orchestrator,
    )
    for name, value in {
        "WATTSON_URL": orchestrator.url,
        "WATTSON_CONFIG_DIR": str(config),
        "WATTSON_STATE_DIR": str(e.state),
        "WATTSON_CLAUDE_HOME": str(e.claude_home),
        "WATTSON_CLAUDE_BIN": str(FIXTURES / "fake_claude"),
        "WATTSON_KUBECTL_BIN": str(FIXTURES / "fake_kubectl"),
        "FAKE_CLAUDE_LOG": str(e.claude_log),
        "FAKE_KUBECTL_LOG": str(e.kubectl_log),
        "NO_COLOR": "1",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("WATTSON_TOKEN_FILE", "EDITOR", "VISUAL", "CLAUDE_CONFIG_DIR", "FAKE_CLAUDE_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)
    return e


@dataclass
class Result:
    code: int
    out: str


def run_cli(argv: list[str], answers: list[str] | None = None) -> Result:
    """Run `wattson argv` with the answers typed one per line."""
    stdin = io.StringIO("".join(f"{a}\n" for a in answers or []))
    out = io.StringIO()
    code = main(argv, ui=UI(stdin=stdin, stdout=out, width=200))
    return Result(code, out.getvalue())


@pytest.fixture
def cli():
    return run_cli
