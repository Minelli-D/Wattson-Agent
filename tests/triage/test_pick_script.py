"""The procedure-pick measurement script runs the triage's own pick code; here against the fake
OpenAI-compatible server (no model runs)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "eval" / "pick.py"
CASES = ROOT / "eval" / "procedures.jsonl"
# The evaluation data is not in the public repository (see README): skip these tests without it.
pytestmark = pytest.mark.skipif(
    not CASES.exists(), reason="eval/procedures.jsonl is not in the public release"
)
NAMES = [
    "k8s-pod-crashloop",
    "k8s-rollout-stuck",
    "k8s-tls-ingress",
    "k8s-storage",
    "k8s-gitops-flux",
    "k8s-cluster-access",
    "k8s-node-health",
    "k8s-helm-release",
    "k8s-pod-termination",
    "k8s-admission-webhook-timeout",
    "k8s-app-errors-in-logs",
    "k8s-workload-health-check",
    "k8s-cluster-inventory",
    "ci-pipeline-failure",
    "code-feature-with-tests",
    "code-bug-fix",
    "docs-from-data",
    "prompt-rewrite",
    "k8s-image-removed-upstream",
    "k8s-clone-app-to-new-namespace",
]


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wattson_pick_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cases_name_known_procedures() -> None:
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    assert len(cases) == 68
    assert {c["procedure"] for c in cases} == {*NAMES, "none"}
    # 30 cases come from past sessions: 24 opening requests, 6 situations met in them.
    assert [c.get("source") for c in cases].count("real") == 24
    assert [c.get("source") for c in cases].count("session") == 6


def test_a_draft_becomes_an_option(tmp_path: Path) -> None:
    draft = tmp_path / "k8s-storage.md"
    draft.write_text(
        '---\ntitle: "k8s-storage"\ntype: runbook\ntags: ["devops-investigation", "pvc", "volume"]\n---\n\n'
        "## When to use\n\nA volume or disk problem:\na PVC stays Pending.\n\n"
        "## Steps for Claude Code\n\n1. x\n"
    )
    option = script().option_from_draft(draft)
    assert (option.key, option.name, option.workflow) == (
        "wattson/k8s-storage.md",
        "k8s-storage",
        "devops-investigation",
    )
    assert option.signals == ["pvc", "volume"]
    assert option.when_to_use == "A volume or disk problem: a PVC stays Pending."


def test_the_script_against_a_fake_server(fake_ollama: Any, tmp_path: Path, capsys: Any) -> None:
    options = [
        {"key": f"wattson/{n}.md", "name": n, "when_to_use": f"when {n}", "signals": n.split("-")}
        for n in NAMES
    ]
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"procedures": options}))
    out = tmp_path / "result.json"
    fake_ollama.pick = {"procedure": "k8s-storage"}
    code = script().main(
        [
            "--url",
            fake_ollama.base_url,
            "--model",
            fake_ollama.model,
            "--options",
            str(path),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "model granite-4-h-1b: 5/68 right" in printed
    summary = json.loads(out.read_text())
    assert summary["total"] == 68 and summary["model_right"] == 5 and summary["errors"] == 0
    calls = fake_ollama.pick_calls()
    assert len(calls) == 68
    enum = calls[0]["response_format"]["json_schema"]["schema"]["properties"]["procedure"]["enum"]
    assert enum == [*NAMES, "none"]
