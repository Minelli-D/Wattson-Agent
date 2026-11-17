"""The Guard's fixed rules: a YAML file the user writes (k8s/guard/rules.yaml), loaded at start.

The file is data, not code: adding, changing or turning off a rule needs no code change. Each rule:

  name        a short id, unique: "secrets-out"
  label       malicious | risky | out_of_scope | injection
  applies_to  request | evidence | both   (evidence: the kubectl/git output and Grimoire documents)
  match       plain phrases, case-insensitive; spaces match any whitespace; whole words only
  regex       Python regular expressions (case-insensitive), for what phrases cannot say
  unless      plain phrases: when one is in the text, the rule does not apply
  reason      one plain sentence, shown to the user. Never the text that matched.
  enabled     true | false
  examples    texts the rule must match (the tests check every one)
  not_examples texts the rule must not match (the tests check these too)

A request is checked against the rules for "request" and "both"; the strictest label wins
(an "injection" rule on a request counts as risky). Evidence is checked against the rules for
"evidence" and "both": each line a rule matches is cut out and replaced by REMOVED_MARK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

RuleLabel = Literal["malicious", "risky", "out_of_scope", "injection"]
AppliesTo = Literal["request", "evidence", "both"]

REMOVED_MARK = "[removed: looked like instructions to the agent]"
DEFAULT_RULES = "rules.yaml"  # packaged next to this module: a copy of k8s/guard/rules.yaml
MAX_RULES = 500
MAX_PATTERNS = 200


class RulesError(ValueError):
    """The rules file cannot be used. The message says where, never the file's text."""


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    label: RuleLabel
    applies_to: AppliesTo = "request"
    match: list[str] = Field(default_factory=list, max_length=MAX_PATTERNS)
    regex: list[str] = Field(default_factory=list, max_length=MAX_PATTERNS)
    unless: list[str] = Field(default_factory=list, max_length=MAX_PATTERNS)
    reason: str = Field(min_length=3, max_length=300)
    # The standard risks this rule covers, e.g. OWASP-ASI02, OWASP-LLM02, ATTACK-T1552 (docs only).
    refs: list[str] = Field(default_factory=list, max_length=8)
    enabled: bool = True
    examples: list[str] = Field(default_factory=list)
    not_examples: list[str] = Field(default_factory=list)

    @field_validator("match", "unless")
    @classmethod
    def _phrases(cls, values: list[str]) -> list[str]:
        out = [" ".join(str(v).split()) for v in values]
        if any(not v for v in out):
            raise ValueError("an empty phrase")
        return out

    @field_validator("regex")
    @classmethod
    def _regexes(cls, values: list[str]) -> list[str]:
        for i, value in enumerate(values):
            try:
                re.compile(value, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"regex {i + 1} is not valid: {e.msg}") from None
        return values

    @model_validator(mode="after")
    def _something_to_match(self) -> Rule:
        if not self.match and not self.regex:
            raise ValueError("a rule needs at least one 'match' phrase or 'regex'")
        return self


class RulesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rules: list[Rule] = Field(max_length=MAX_RULES)

    @model_validator(mode="after")
    def _unique(self) -> RulesFile:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.name in seen:
                raise ValueError(f"the rule name {rule.name!r} is used twice")
            seen.add(rule.name)
        return self


def _phrase_regex(phrase: str) -> str:
    words = [re.escape(w) for w in phrase.split()]
    # Whole words: "rm" does not match "format". Edges that are not word characters need no check.
    head = r"(?<!\w)" if re.match(r"\w", phrase) else ""
    tail = r"(?!\w)" if re.search(r"\w$", phrase) else ""
    return head + r"\s+".join(words) + tail


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    pattern: re.Pattern[str]
    unless: re.Pattern[str] | None

    def matches(self, text: str) -> bool:
        if self.unless is not None and self.unless.search(text):
            return False
        return self.pattern.search(text) is not None


def compile_rule(rule: Rule) -> CompiledRule:
    parts = [_phrase_regex(p) for p in rule.match] + [f"(?:{r})" for r in rule.regex]
    pattern = re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE)
    unless = (
        re.compile("|".join(_phrase_regex(p) for p in rule.unless), re.IGNORECASE) if rule.unless else None
    )
    return CompiledRule(rule=rule, pattern=pattern, unless=unless)


@dataclass(frozen=True)
class RequestHits:
    rules: tuple[Rule, ...]  # the enabled rules that matched, in file order

    @property
    def names(self) -> list[str]:
        return [r.name for r in self.rules]

    @property
    def reasons(self) -> list[str]:
        out: list[str] = []
        for r in self.rules:
            if r.reason not in out:
                out.append(r.reason)
        return out


@dataclass(frozen=True)
class Scan:
    text: str  # with the flagged lines replaced by REMOVED_MARK
    removed: int  # how many parts were cut
    rule_names: tuple[str, ...]


class RuleSet:
    """The enabled rules of one file, compiled. `source` says where they came from."""

    def __init__(self, data: RulesFile, source: str) -> None:
        self.file = data
        self.source = source
        enabled = [compile_rule(r) for r in data.rules if r.enabled]
        self.request_rules = [c for c in enabled if c.rule.applies_to in ("request", "both")]
        self.evidence_rules = [c for c in enabled if c.rule.applies_to in ("evidence", "both")]

    @property
    def count(self) -> int:
        return len(self.request_rules) + len([c for c in self.evidence_rules if c not in self.request_rules])

    def check_request(self, text: str) -> RequestHits:
        return RequestHits(tuple(c.rule for c in self.request_rules if c.matches(text)))

    def scan_evidence(self, text: str) -> Scan:
        """Cut every line an evidence rule matches. Lines in a row become one cut."""
        if not text:
            return Scan(text, 0, ())
        lines = text.split("\n")
        flagged: set[int] = set()
        names: list[str] = []
        starts = _line_starts(lines)
        for c in self.evidence_rules:
            if c.unless is not None and c.unless.search(text):
                continue
            for m in c.pattern.finditer(text):
                first = _line_of(starts, m.start())
                last = _line_of(starts, max(m.start(), m.end() - 1))
                rows = [i for i in range(first, last + 1) if lines[i].strip() != REMOVED_MARK]
                if rows:
                    flagged.update(rows)
                    if c.rule.name not in names:
                        names.append(c.rule.name)
        if not flagged:
            return Scan(text, 0, ())
        out: list[str] = []
        removed = 0
        for i, line in enumerate(lines):
            if i not in flagged:
                out.append(line)
            elif i - 1 not in flagged:
                out.append(REMOVED_MARK)
                removed += 1
        return Scan("\n".join(out), removed, tuple(names))


def _line_starts(lines: list[str]) -> list[int]:
    starts, at = [], 0
    for line in lines:
        starts.append(at)
        at += len(line) + 1
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def parse_rules(text: str, source: str) -> RuleSet:
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise RulesError(f"{source} is not valid YAML{where}") from None
    if not isinstance(raw, dict):
        raise RulesError(f"{source} must be a mapping with a 'rules' list")
    try:
        data = RulesFile.model_validate(raw)
    except ValidationError as e:
        raise RulesError(f"{source}: {_describe(e, raw)}") from None
    return RuleSet(data, source)


def _describe(e: ValidationError, raw: dict[str, Any]) -> str:
    """Where the file is wrong (rule number and name, field), without echoing values."""
    parts = []
    rules = raw.get("rules") if isinstance(raw.get("rules"), list) else []
    for err in e.errors(include_input=False, include_url=False)[:5]:
        loc = list(err.get("loc", ()))
        where = ".".join(str(p) for p in loc) or "(root)"
        if len(loc) >= 2 and loc[0] == "rules" and isinstance(loc[1], int):
            item = rules[loc[1]] if loc[1] < len(rules) else None
            name = item.get("name") if isinstance(item, dict) else None
            label = f"rule {loc[1] + 1}" + (f" ({name})" if isinstance(name, str) else "")
            where = ".".join([label, *(str(p) for p in loc[2:])])
        parts.append(f"{where}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def default_rules_text() -> str:
    return resources.files(__package__).joinpath(DEFAULT_RULES).read_text(encoding="utf-8")


def load_default() -> RuleSet:
    return parse_rules(default_rules_text(), "the packaged default rules")


def load_rules(path: Path | None) -> tuple[RuleSet, str | None]:
    """The rules of `path` (None: the packaged default). A file that cannot be used gives the
    packaged default and the problem, to log and to show in /v1/health."""
    if path is None:
        return load_default(), None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        problem = f"cannot read {path}: {e.strerror or type(e).__name__}"
    else:
        try:
            return parse_rules(text, str(path)), None
        except RulesError as e:
            problem = str(e)
    return load_default(), f"{problem}; using the packaged default rules"
