"""Keyword rules: a deterministic classifier and hint extractor.

They decide when the decision model is down, slow, or unsure (docs/design.md 10.2), they fill the
hints the model left empty, and they are the baseline in eval/. Fixed confidences, method "rules".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from wattson.shared.contracts import WorkspaceInfo

Strength = Literal["strong", "medium", "weak"]
# Fixed confidences by strength (method "rules"). Not probabilities: a strong rule is a hint word
# that rarely lies ("rewrite this prompt", "CrashLoopBackOff"), a weak one is a guess.
CONFIDENCE: dict[Strength, float] = {"strong": 0.85, "medium": 0.65, "weak": 0.4}
UNCLEAR_CONFIDENCE = 0.5

# ---- classification ---------------------------------------------------------------------

_I = re.IGNORECASE

_PROMPT_WORD = re.compile(r"\bprompts?\b|\bsystem message\b|\bistruzioni\b", _I)
_REWRITE = re.compile(
    r"\b(re-?write|rephrase|reword|re-?formulate|polish|improve|optimi[sz]e|tighten|shorten|"
    r"make (?:it|this|the (?:text|task|prompt))? ?(?:more )?(?:clear|clearer|shorter|better|precise|"
    r"structured)|clean ?up the (?:text|wording)|riscrivi|migliora|translate)\w*",
    _I,
)
_FOR_AGENT = re.compile(
    r"\bfor (?:the )?(?:claude(?: code)?|coding agent|agent|codex|copilot|cursor|llm|ai|model)\b"
    r"|\bto (?:give|send|pass) (?:to )?(?:claude|the agent)\b",
    _I,
)
_TASK_TEXT = re.compile(r"\b(task (?:text|description)|instructions?|wording|this text)\b", _I)

# (pattern, weight) pairs. Infra nouns and symptoms point to an investigation.
_DEVOPS: list[tuple[re.Pattern[str], float]] = [
    (
        re.compile(
            r"\b(crash ?loop\w*|imagepullbackoff|errimagepull|oomkill\w*|evicted|"
            r"createcontainer\w*error|backofflimitexceeded|failedscheduling|failedmount|"
            r"nodenotready|deadlineexceeded)\b",
            _I,
        ),
        2.0,
    ),
    (
        re.compile(
            r"\b(pods?|kubectl|namespaces?|nodes?|pvcs?|persistent ?volume\w*|replicas?|"
            r"rollouts?|hpa|ingress\w*|cert-manager|clusterissuer|certificates?|x509|tls|"
            r"statefulsets?|daemonsets?|kubelet|coredns|dns|endpoints?|services?|"
            r"load ?balancer|eks|aks|gke|rds|iam|vpc|route53|alb|nlb|terraform|tfstate|drift|"
            r"runners?|pipelines?|helm|releases?|argo ?cd|flux|prometheus|grafana|alerts?|"
            r"latency|throttl\w*|quota|disk pressure|memory pressure|volume)\b",
            _I,
        ),
        1.0,
    ),
    (
        re.compile(
            r"\b(5\d\d|bad gateway|gateway timeout|timeouts?|timed out|connection refused|"
            r"unreachable|not reachable|is down|went down|outage|restart(?:s|ing|ed)?|"
            r"stuck|pending|not ready|not starting|failing|failed|fails|expired|broken|"
            r"slow|leak\w*|spik\w*|error|errors)\b",
            _I,
        ),
        1.0,
    ),
    (
        re.compile(
            r"\b(check|investigate|why|debug|diagnose|look (?:into|at)|find out|"
            r"troubleshoot|what happened|what is wrong|what's wrong|figure out|verify)\b",
            _I,
        ),
        0.75,
    ),
    (re.compile(r"(?:^|\s)(?:-n|--namespace)[\s=]\S+|--context[\s=]\S+", _I), 1.0),
]
# Code verbs and code nouns point to a change in a repository.
_ENGINEERING: list[tuple[re.Pattern[str], float]] = [
    (
        re.compile(
            r"\b(add|implement|refactor|rename|extract|create|write|build|introduce|"
            r"migrate|port|upgrade|bump|remove|delete|replace|split|move|convert|support|"
            r"generate|document|cover|aggiungi|scrivi)\b",
            _I,
        ),
        1.25,
    ),
    (
        re.compile(
            r"\b(fix (?:the |a |this )?(?:bug|typo|test|lint|type)\w*|unit tests?|tests?|"
            r"pytest|jest|vitest|playwright|e2e|coverage|lint\w*|eslint|mypy|ruff|types?|"
            r"typing|readme|docs|docstrings?|changelog|comments?)\b",
            _I,
        ),
        1.25,
    ),
    (
        re.compile(
            r"\b(functions?|class(?:es)?|methods?|modules?|endpoints?|routes?|handlers?|"
            r"components?|hooks?|pages?|schemas?|models?|migrations?|apis?|cli|flag|"
            r"parser|validator|middleware|repo(?:sitory)?|codebase|code|script|"
            r"pull request|pr|commit|branch|feature|dependenc\w+|package|library|"
            r"chart templates?|template|module|makefile|dockerfile|workflow file)\b",
            _I,
        ),
        1.0,
    ),
    (
        re.compile(
            r"\b(next\.?js|react|node(?:\.js)?|typescript|javascript|python|fastapi|django|"
            r"flask|express|prisma|sqlalchemy|pydantic|go(?:lang)?|rust|java)\b",
            _I,
        ),
        1.0,
    ),
]
_FILE_HINT = re.compile(r"[\w./-]+\.(?:py|tsx?|jsx?|go|rs|java|md|tf|ya?ml|json|toml|sql)\b", _I)
_VAGUE = re.compile(
    r"^\W*(help|hi|hello|ciao|check it|look at (?:it|this)|it'?s broken|again|the thing|any ideas?|"
    r"same as (?:before|yesterday)|do (?:it|the thing)|continue|go on|fix it|improve it|\?+)\W*$",
    _I,
)


@dataclass
class RulesVerdict:
    category: str  # one of CATEGORIES
    confidence: float
    strength: Strength
    scores: dict[str, float] = field(default_factory=dict)  # raw keyword scores per category


def _score(text: str, table: list[tuple[re.Pattern[str], float]]) -> float:
    total = 0.0
    for pattern, weight in table:
        hits = len(pattern.findall(text))
        total += weight * min(hits, 3)  # one topic word repeated is still one topic
    return total


def classify(text: str) -> RulesVerdict:
    """The keyword verdict. A weak verdict says "unclear"; its best guess stays in scores."""
    words = re.findall(r"\w+", text)
    prompt_word = bool(_PROMPT_WORD.search(text))
    rewrite = bool(_REWRITE.search(text))
    for_agent = bool(_FOR_AGENT.search(text))
    p = (2.0 if prompt_word else 0) + (1.5 if rewrite else 0) + (1.5 if for_agent else 0)
    p += 0.5 if _TASK_TEXT.search(text) else 0
    d = _score(text, _DEVOPS)
    e = _score(text, _ENGINEERING) + (1.0 if _FILE_HINT.search(text) else 0)
    scores = {"prompt-optimization": p, "devops-investigation": d, "engineering-task": e, "unclear": 0.0}

    # "Rewrite this prompt" wins over whatever the prompt itself talks about.
    if (prompt_word and (rewrite or for_agent)) or (rewrite and for_agent):
        return RulesVerdict("prompt-optimization", CONFIDENCE["strong"], "strong", scores)

    if _VAGUE.match(text.strip()) or len(words) <= 2:
        scores["unclear"] = 1.0
        return RulesVerdict("unclear", UNCLEAR_CONFIDENCE, "medium", scores)

    best = max(("devops-investigation", "engineering-task", "prompt-optimization"), key=lambda c: scores[c])
    ranked = sorted((scores[c] for c in scores if c != "unclear"), reverse=True)
    top, margin = ranked[0], ranked[0] - ranked[1]
    if top >= 3.0 and margin >= 2.0:
        return RulesVerdict(best, CONFIDENCE["strong"], "strong", scores)
    if top >= 2.0 and margin >= 1.0:
        return RulesVerdict(best, CONFIDENCE["medium"], "medium", scores)
    scores["unclear"] = 1.0
    return RulesVerdict("unclear", CONFIDENCE["weak"], "weak", scores)


# ---- hints --------------------------------------------------------------------------------

RFC1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
WORKLOAD_KINDS = {
    "deployment": "deployment",
    "deployments": "deployment",
    "deploy": "deployment",
    "statefulset": "statefulset",
    "statefulsets": "statefulset",
    "sts": "statefulset",
    "daemonset": "daemonset",
    "daemonsets": "daemonset",
    "ds": "daemonset",
    "pod": "pod",
    "pods": "pod",
    "po": "pod",
    "job": "job",
    "jobs": "job",
    "cronjob": "cronjob",
    "cronjobs": "cronjob",
    "cj": "cronjob",
    "replicaset": "replicaset",
    "rs": "replicaset",
}
# Words that follow "pod", "deployment", "namespace" in plain sentences but are not names.
NOT_NAMES = frozenset(
    """a an the is are was were be been it its this that these those of in on at to for from with
    and or but not no keeps keep kept goes going went gets got has have had does do did can could
    will would should must may might restarts restarting restarted status logs log events spec
    template templates yaml manifest manifests rollout rolled name names called named which what
    who where when why how all any some every each other another same new old last latest current
    whole main broken failing failed stuck pending crashing again there here now still also only
    just more less after before since into via per my our your their his her us we you they i me
    file files config configuration env variable variables probe probes resource resources limits
    requests image images container containers selector label labels annotation annotations
    because so if then than too very please pls thanks ok yes yeah creation create update delete
    level wide side by""".split()
)
# Also not workload names ("the prod deployment", "a k8s cronjob"); fine as namespaces.
NOT_WORKLOAD_NAMES = NOT_NAMES | frozenset(
    "k8s kubernetes kube helm docker cluster prod production staging dev test new".split()
)
_NS_PATTERNS = [
    re.compile(r"(?:^|[\s(])(?:-n|--namespace)(?:\s+|=)['\"`]?([A-Za-z0-9][-A-Za-z0-9]*)", _I),
    re.compile(r"\bnamespaces?(?:\s*[:=]\s*|\s+)['\"`]?([A-Za-z0-9][-A-Za-z0-9]*)", _I),
    re.compile(r"\bns(?:\s*[:=]\s*|\s+)['\"`]?([A-Za-z0-9][-A-Za-z0-9]*)", _I),
    re.compile(
        r"\b(?:in|on|from|of|inside)\s+(?:the\s+)?['\"`]?([A-Za-z0-9][-A-Za-z0-9]*)['\"`]?\s+(?:namespace|ns)\b",
        _I,
    ),
]
_WL_SLASH = re.compile(
    r"\b(deployments?|deploy|statefulsets?|sts|daemonsets?|ds|pods?|po|jobs?|cronjobs?|cj|replicaset|rs)"
    r"/([a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9])",
    _I,
)
_WL_KIND_NAME = re.compile(
    r"\b(deployment|deploy|statefulset|daemonset|pod|cronjob|job)\s+['\"`]?([a-z0-9][a-z0-9.-]*[a-z0-9])",
    _I,
)
_WL_NAME_KIND = re.compile(
    r"\b(?:the|a|our|my|its)\s+['\"`]?([a-z0-9][a-z0-9-]*[a-z0-9])['\"`]?\s+"
    r"(deployment|statefulset|daemonset|cronjob|job|pod)\b",
    _I,
)
_CONTEXT_FLAG = re.compile(r"--context[\s=]['\"`]?([\w.@:-]+)", _I)

_QUOTED = [
    re.compile(r"\"([^\"\n]{3,200})\""),
    re.compile(r"“([^”\n]{3,200})”"),
    re.compile(r"`([^`\n]{2,200})`"),
    re.compile(r"(?:(?<=\s)|^)'([^'\n]{3,200})'(?=[\s.,;:!?)]|$)"),
]
_ERRORS = [
    re.compile(
        r"\b(CrashLoopBackOff|ImagePullBackOff|ErrImagePull|OOMKilled|CreateContainerConfigError|"
        r"CreateContainerError|RunContainerError|BackoffLimitExceeded|FailedScheduling|FailedMount|"
        r"NodeNotReady|DeadlineExceeded|Evicted)\b",
        _I,
    ),
    re.compile(r"\bx509: [^\n\"'`]{3,100}"),
    re.compile(r"\b(?:Error|ERROR|FATAL|panic): [^\n\"'`]{3,120}"),
    re.compile(r"\b[A-Z][A-Za-z]+(?:Error|Exception)\b(?::[^\n\"'`]{1,100})?"),
    re.compile(
        r"\b(?:connection refused|permission denied|no such host|i/o timeout|"
        r"context deadline exceeded|certificate has expired|bad gateway|another operation "
        r"\(install/upgrade/rollback\) is in progress)\b",
        _I,
    ),
]
_FILE_EXT = (
    r"py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|rb|php|sh|bash|ya?ml|json|toml|ini|cfg|conf|md|mdx|"
    r"txt|tf|tfvars|hcl|sql|css|scss|html|vue|svelte|lock|tpl|proto|graphql|env\.example"
)
_FILE = re.compile(
    rf"(?<![\w/.@:-])((?:\./)?(?:[\w.@-]+/)*[\w.@-]*[\w-]\.(?:{_FILE_EXT}))(?![\w/-])"
    r"|(?<![\w/.@:-])((?:\./)?(?:[\w.@-]+/)*(?:Dockerfile|Makefile|Jenkinsfile|Procfile|Containerfile)"
    r"(?:\.[\w-]+)?)(?![\w/-])",
)
_NOT_FILES = frozenset(
    {
        "next.js",
        "node.js",
        "vue.js",
        "nuxt.js",
        "express.js",
        "react.js",
        "three.js",
        "d3.js",
        "chart.js",
        "nest.js",
        "deno.js",
    }
)

TAG_WORDS: list[tuple[str, re.Pattern[str]]] = [
    (
        "kubernetes",
        re.compile(
            r"\b(kubernetes|k8s|kubectl|kube|pods?|crash ?loop\w*|namespaces?|"
            r"statefulsets?|daemonsets?|deployments?|pvcs?|nodes?|kubelet|hpa)\b",
            _I,
        ),
    ),
    ("helm", re.compile(r"\b(helm|charts?|values\.ya?ml)\b", _I)),
    ("cert-manager", re.compile(r"\b(cert-manager|certmanager|clusterissuer|issuer|letsencrypt|acme)\b", _I)),
    ("tls", re.compile(r"\b(tls|x509|ssl|certificates?)\b", _I)),
    ("ingress", re.compile(r"\b(ingress\w*|traefik|nginx)\b", _I)),
    ("postgres", re.compile(r"\b(postgres\w*|psql|cnpg|pgbouncer)\b", _I)),
    ("redis", re.compile(r"\bredis\b", _I)),
    ("terraform", re.compile(r"\b(terraform|tfstate|tfvars)\b|\.tf\b", _I)),
    ("aws", re.compile(r"\b(aws|eks|s3|iam|rds|ec2|route53|cloudfront|alb)\b", _I)),
    ("ci", re.compile(r"\b(ci|ci/cd|pipelines?|runners?|github actions|\.gitlab-ci\.ya?ml)\b", _I)),
    ("gitlab", re.compile(r"\bgitlab\b", _I)),
    ("github-actions", re.compile(r"\b(github actions|\.github/workflows)\b", _I)),
    ("docker", re.compile(r"\b(docker\w*|containerfile|image|images|registry)\b", _I)),
    ("dns", re.compile(r"\b(dns|coredns|nslookup)\b", _I)),
    ("monitoring", re.compile(r"\b(prometheus|grafana|alertmanager|alerts?|metrics)\b", _I)),
    ("argocd", re.compile(r"\b(argo ?cd)\b", _I)),
    ("nextjs", re.compile(r"\b(next\.?js|nextjs)\b|page\.tsx|app router", _I)),
    ("nodejs", re.compile(r"\b(node\.?js|npm|pnpm|yarn)\b", _I)),
    ("typescript", re.compile(r"\b(typescript)\b|\.tsx?\b", _I)),
    ("react", re.compile(r"\breact\b", _I)),
    ("python", re.compile(r"\b(python|pytest|fastapi|django|flask|pydantic|poetry)\b|\.py\b", _I)),
    ("claude-code", re.compile(r"\bclaude\b", _I)),
    ("prompt", re.compile(r"\bprompts?\b", _I)),
]

_STOP = NOT_NAMES | frozenset(
    """can you could please pls me i want would like need help some something thing things look
    check why what how about make sure tell give us since today yesterday morning again really
    think know see let lets let's get go going""".split()
)


def _first_valid(patterns: list[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        for m in pattern.finditer(text):
            name = m.group(1).lower()
            if name not in NOT_NAMES and RFC1123_LABEL.match(name):
                return name
    return None


def find_namespace(text: str) -> str | None:
    return _first_valid(_NS_PATTERNS, text)


def find_workload(text: str) -> str | None:
    for m in _WL_SLASH.finditer(text):
        kind, name = WORKLOAD_KINDS[m.group(1).lower()], m.group(2).lower()
        if name not in NOT_WORKLOAD_NAMES:
            return f"{kind}/{name}"
    for m in _WL_KIND_NAME.finditer(text):
        kind, name = WORKLOAD_KINDS[m.group(1).lower()], m.group(2).lower()
        if name not in NOT_WORKLOAD_NAMES and not name.isdigit():
            return f"{kind}/{name}"
    for m in _WL_NAME_KIND.finditer(text):
        name, kind = m.group(1).lower(), WORKLOAD_KINDS[m.group(2).lower()]
        if name not in NOT_WORKLOAD_NAMES and name not in WORKLOAD_KINDS:
            return f"{kind}/{name}"
    return None


def find_cluster(text: str, contexts: list[str]) -> str | None:
    flag = _CONTEXT_FLAG.search(text)
    if flag:
        for ctx in contexts:
            if ctx.lower() == flag.group(1).lower():
                return ctx
    for ctx in sorted(contexts, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(ctx)}(?![\w-])", text, _I):
            return ctx
    return None


def find_grep(text: str, limit: int = 3) -> list[str]:
    found: list[str] = []
    for pattern in _QUOTED + _ERRORS:
        for m in pattern.finditer(text):
            value = (m.group(1) if m.groups() else m.group(0)).strip().rstrip(".,;:")
            if 2 <= len(value) <= 120 and value.lower() not in (f.lower() for f in found):
                found.append(value)
    # A quoted error and the bare error inside it: keep the longer one only.
    found = [f for f in found if not any(f != g and f.lower() in g.lower() for g in found)]
    return found[:limit]


def find_files(text: str, limit: int = 8) -> list[str]:
    files: list[str] = []
    for m in _FILE.finditer(text):
        path = (m.group(1) or m.group(2) or "").removeprefix("./")
        if not path or path.lower() in _NOT_FILES or ".." in path.split("/"):
            continue
        if path not in files:
            files.append(path)
    return files[:limit]


def find_tags(text: str, limit: int = 6) -> list[str]:
    return [tag for tag, pattern in TAG_WORDS if pattern.search(text)][:limit]


def keywords(text: str, limit: int = 6) -> list[str]:
    out: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*[A-Za-z0-9]", text):
        w = word.lower()
        if w in _STOP or len(w) < 3 or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def search_queries(text: str, tags: list[str], grep: list[str]) -> list[str]:
    queries: list[str] = []
    kw = keywords(text)
    if kw:
        queries.append(" ".join(kw))
    if grep and len(grep[0]) <= 60:
        queries.append(f"{grep[0]} {tags[0]}".strip() if tags else grep[0])
    if tags:
        queries.append(" ".join(tags[:3]) + " runbook")
    unique: list[str] = []
    for q in queries:
        if q and q.lower() not in (u.lower() for u in unique):
            unique.append(q[:80])
    return unique[:3]


@dataclass
class RulesHints:
    cluster: str | None = None
    namespace: str | None = None
    workload: str | None = None
    files: list[str] = field(default_factory=list)
    grep: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)


def extract_hints(text: str, workspace: WorkspaceInfo) -> RulesHints:
    tags = find_tags(text)
    grep = find_grep(text)
    return RulesHints(
        cluster=find_cluster(text, workspace.kube_contexts),
        namespace=find_namespace(text),
        workload=find_workload(text),
        files=find_files(text),
        grep=grep,
        tags=tags,
        search_queries=search_queries(text, tags, grep),
    )
