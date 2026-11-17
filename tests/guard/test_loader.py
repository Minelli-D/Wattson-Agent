"""Loading the rules and policy files: a good file is used, a bad file falls back to the packaged
default and reports the problem (for /v1/health), and the packaged files are shipped and valid."""

from __future__ import annotations

from pathlib import Path

from wattson.agents.orchestrator.guard import cedar, rules


def test_default_rules_are_packaged_and_valid() -> None:
    rs = rules.load_default()
    assert rs.count > 0
    assert rs.source == "the packaged default rules"


def test_k8s_rules_file_matches_the_packaged_default() -> None:
    k8s = Path(__file__).resolve().parents[2] / "k8s" / "guard" / "rules.yaml"
    packaged = rules.default_rules_text()
    assert k8s.read_text(encoding="utf-8") == packaged


def test_bad_yaml_falls_back_with_a_problem(tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text("rules: [this is: not: valid", encoding="utf-8")
    rs, problem = rules.load_rules(bad)
    assert problem is not None and "default" in problem
    assert rs.source == "the packaged default rules"


def test_invalid_rule_names_the_field(tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        "version: 1\nrules:\n  - name: no-label\n    match: [x]\n    reason: a reason here\n",
        encoding="utf-8",
    )
    rs, problem = rules.load_rules(bad)
    assert problem is not None and "rule 1 (no-label)" in problem
    assert rs.source == "the packaged default rules"


def test_good_file_is_used(tmp_path: Path) -> None:
    good = tmp_path / "rules.yaml"
    good.write_text(
        "version: 1\nrules:\n"
        "  - name: my-rule\n    label: risky\n    applies_to: request\n"
        "    match: ['do the risky thing']\n    reason: It is my own risky rule.\n",
        encoding="utf-8",
    )
    rs, problem = rules.load_rules(good)
    assert problem is None
    assert rs.source == str(good)
    assert rs.check_request("please do the risky thing now").names == ["my-rule"]


def test_missing_file_falls_back(tmp_path: Path) -> None:
    rs, problem = rules.load_rules(tmp_path / "nope.yaml")
    assert problem is not None and "cannot read" in problem
    assert rs.source == "the packaged default rules"


def test_default_policies_are_packaged_and_valid() -> None:
    text, problem = cedar.load_policies(None)
    assert problem is None and "run_on_laptop" in text


def test_k8s_policies_file_matches_the_packaged_default() -> None:
    k8s = Path(__file__).resolve().parents[2] / "k8s" / "guard" / "policies.cedar"
    assert k8s.read_text(encoding="utf-8") == cedar.default_policies_text()


def test_bad_policy_falls_back_with_a_problem(tmp_path: Path) -> None:
    bad = tmp_path / "policies.cedar"
    bad.write_text("this is not a cedar policy", encoding="utf-8")
    text, problem = cedar.load_policies(bad)
    assert problem is not None and "default" in problem
    assert "run_on_laptop" in text  # the packaged default
