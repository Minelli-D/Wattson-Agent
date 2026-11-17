"""Fixtures of the connector tests: a temporary git repository, and the fake kubectl / claude of
tests/fixtures/bin. Nothing here touches a real cluster, the real ~/.claude, or the real claude."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from connector_helpers import BIN, git, read_log


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    def make(name: str = "repo", commit: bool = True) -> Path:
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        if not commit:
            return root
        (root / "README.md").write_text("# Demo\nThe api reads DATABASE_URL at start.\n")
        (root / "src" / "util").mkdir(parents=True)
        (root / "src" / "app.py").write_text(
            "import os\nURL = os.environ['DATABASE_URL']\nprint('-v flag')\n"
        )
        (root / "src" / "util" / "helpers.py").write_text("def helper():\n    return 'DATABASE_URL'\n")
        (root / "docs").mkdir()
        (root / "docs" / "guide.md").write_text("Guide\n")
        (root / ".env").write_text("DATABASE_URL=postgres://app:hunter2@db/app\n")
        (root / "config").mkdir()
        (root / "config" / "secrets.yaml").write_text("password: hunter2\n")
        (root / "k8s").mkdir()
        (root / "k8s" / "all.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\ndata:\n  LEVEL: info\n---\n"
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: aHVudGVyMg==\n"
        )
        (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        (root / "big.txt").write_text("".join(f"line {i:05d} of a long file\n" for i in range(1000)))
        os.symlink("/etc/hostname", root / "outside")
        os.symlink(".env", root / "env-link")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "first commit")
        (root / "docs" / "guide.md").write_text("Guide\nmore\n")
        git(root, "commit", "-q", "-am", "second commit")
        return root

    return make


@pytest.fixture
def repo(make_repo: Callable[..., Path]) -> Path:
    return make_repo()


@pytest.fixture
def kubectl_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[dict]]:
    """The fake kubectl, a temporary config dir; returns a function that lists the calls."""
    log = tmp_path / "kubectl-calls.jsonl"
    monkeypatch.setenv("WATTSON_KUBECTL_BIN", str(BIN / "kubectl"))
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(log))
    monkeypatch.setenv("WATTSON_CONFIG_DIR", str(tmp_path / "config"))
    for var in ("FAKE_KUBE_CONTEXTS", "FAKE_KUBE_CURRENT", "FAKE_KUBE_CAN_I_SECRETS", "FAKE_KUBE_APPLY_FAIL"):
        monkeypatch.delenv(var, raising=False)
    return lambda: read_log(log)


@pytest.fixture
def claude_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[dict]]:
    """The fake claude and a temporary Claude home; returns a function that lists the calls."""
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("WATTSON_CLAUDE_BIN", str(BIN / "claude"))
    monkeypatch.setenv("WATTSON_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return lambda: read_log(log)
