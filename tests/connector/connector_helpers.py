"""Helpers shared by the connector tests (a unique module name: other parts have a conftest too)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "fixtures" / "bin"
GIT_ID = [
    "-c",
    "user.email=test@example.test",
    "-c",
    "user.name=Test",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
]


def git(root: Path, *args: str) -> str:
    cmd = ["git", "-C", str(root), *GIT_ID, *args]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603, S607 - test helper
    return res.stdout


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
