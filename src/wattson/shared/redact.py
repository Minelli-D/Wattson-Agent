"""Secret redaction (SEC-04, SEC-05), shared by the CLI's connector and the agents.

`redact(text)` replaces likely credentials with `[REDACTED:<kind>]` and says how many it replaced
and of which kinds. `is_secret_path(path)` says whether a file name looks like it holds secrets,
so its contents are never read or sent.

How it works: an ordered list of rules. Each rule finds spans in the current text; the spans are
replaced, then the next rule runs. Rules that ignore case search a lower-cased copy with the same
offsets (much faster than re.IGNORECASE). Every pattern has bounded lengths and no nested
quantifiers, so the time is linear: 1 MB takes well under 300 ms. Standard library only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from functools import lru_cache
from typing import NamedTuple

__all__ = ["MARKER_PREFIX", "Redaction", "is_secret_path", "marker", "redact"]

MARKER_PREFIX = "[REDACTED:"


class Redaction(NamedTuple):
    text: str
    count: int  # how many values were replaced
    kinds: list[str]  # sorted, each kind once


def marker(kind: str) -> str:
    return f"{MARKER_PREFIX}{kind}]"


def _is_marked(value: str) -> bool:
    return value.lstrip(" \t\"'").startswith(MARKER_PREFIX)


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _lower(text: str) -> str:
    return text.translate(_ASCII_LOWER)  # same length and offsets as `text`, unlike str.lower()


Span = tuple[int, int, str]  # start, end, kind
Finder = Callable[[str, str], list[Span]]  # (text, lowered text) -> spans


class _Rule(NamedTuple):
    find: Finder
    needles: tuple[str, ...] = ()  # lower-case; the rule runs only if one is in the text


# ---- secret-like key names ------------------------------------------------------------------

# The key is lower-cased and "-", "_", ".", " " are removed; it is secret-like when it ENDS with
# one of these. So "author", "tokenizer", "secretName", "max_tokens" are not.
_KEY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("password", "password"),
    ("passwd", "password"),
    ("passphrase", "password"),
    ("pwd", "password"),
    ("secret", "secret"),
    ("token", "token"),
    ("apikey", "api-key"),
    ("accesskey", "api-key"),
    ("secretkey", "api-key"),
    ("accountkey", "api-key"),
    ("signingkey", "api-key"),
    ("privatekey", "private-key"),
    ("auth", "auth"),
    ("credential", "credential"),
    ("credentials", "credential"),
)
_KEY_STRIP = str.maketrans("", "", "-_. ")


@lru_cache(maxsize=4096)
def _key_kind(key: str) -> str | None:
    norm = _lower(key).translate(_KEY_STRIP)
    for suffix, kind in _KEY_SUFFIXES:
        if norm.endswith(suffix):
            return kind
    return None


# Values that are not secrets: booleans, type names, placeholders, masks, references.
_PLAIN_VALUES = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "nil",
        "~",
        "yes",
        "no",
        "on",
        "off",
        "undefined",
        "required",
        "optional",
        "str",
        "string",
        "int",
        "bool",
        "bytes",
        "secret",
        "password",
        "token",
        "redacted",
        "hidden",
    }
)
_ENV_REF = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_AUTH_SCHEMES = frozenset({"bearer", "basic", "token", "digest", "negotiate", "bot", "apikey"})


_PLAIN_START = ("${", "{{", "$(", "%(", "#{", "<", MARKER_PREFIX)
_MASK = frozenset("*•")


def _plain_value(value: str) -> bool:
    v = value.strip().strip("\"'").strip()
    if not v or v.startswith(_PLAIN_START):
        return True
    if len(v) <= 9 and v.lower() in _PLAIN_VALUES:
        return True
    if v[0] == "$" and _ENV_REF.fullmatch(v):
        return True
    return v[0] in _MASK and set(v) <= _MASK


def _regex_rule(
    pattern: re.Pattern[str],
    kind: str | Callable[[re.Match[str], str], str | None],
    group: int = 1,
    *,
    on_low: bool = False,
    needles: tuple[str, ...] = (),
    not_after: frozenset[str] = frozenset(),
) -> _Rule:
    """Hide one group of every match. `kind` may be a function of (match, value): None keeps it.
    `not_after`: the match must not follow one of these characters (a look-behind, done here
    because a pattern that starts with a literal is searched many times faster)."""

    def find(text: str, low: str) -> list[Span]:
        spans: list[Span] = []
        for m in pattern.finditer(low if on_low else text):
            if not_after and m.start() > 0 and text[m.start() - 1] in not_after:
                continue
            start, end = m.span(group)
            if start < 0 or start == end:
                continue
            value = text[start:end]
            if _is_marked(value):
                continue
            k = kind(m, value) if callable(kind) else kind
            if k:
                spans.append((start, end, k))
        return spans

    return _Rule(find, needles)


# ---- PEM private keys: the whole block; an unterminated block goes to the end ------------------

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")


def _find_pem(text: str, low: str) -> list[Span]:
    spans: list[Span] = []
    pos = 0
    while m := _PEM_BEGIN.search(text, pos):
        end = _PEM_END.search(text, m.end())
        pos = end.end() if end else len(text)
        spans.append((m.start(), pos, "private-key"))
    return spans


# ---- Kubernetes Secret manifests (best effort): every value under data: / stringData: -----------

_SECRET_KIND_YAML = re.compile(r"(?m)kind:[ \t]*[\"']?Secret[\"']?[ \t]*(?:#[^\n]*)?\r?$")
_DOC_SPLIT = re.compile(r"\n---")
_DATA_LINE = re.compile(r"(?m)^([ \t]*)(?:data|stringData):[ \t]*([^\n]*)$")
_BLOCK_SCALAR = re.compile(r"[|>][-+]?[0-9]?")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_WORD_DASH = _WORD | {"-"}


def _find_k8s_secret_yaml(text: str, low: str) -> list[Span]:
    if not _SECRET_KIND_YAML.search(text):
        return []
    bounds = [0, *(m.start() + 1 for m in _DOC_SPLIT.finditer(text)), len(text)]
    spans: list[Span] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        if _SECRET_KIND_YAML.search(text, a, b):
            spans.extend(_secret_doc_spans(text, a, b))
    return spans


_BLOCKS: dict[int, re.Pattern[str]] = {}
# One line under a data: key: its indent, key and value (the value may be empty).
_CHILD = re.compile(r"(?m)^([ \t]+)([^:#\s][^:\n]{0,200}):(?:[ \t]+([^\n]*))?$")
_BLANK_OR_DEEPER = "(?:[ \t]*\r?\n|[ \t]{%d,}[^\n]*(?:\n|$))*"


def _block_end(text: str, pos: int, b: int, base: int) -> int:
    """End of the lines after `pos` that are blank or indented more than `base`."""
    pattern = _BLOCKS.get(base)
    if pattern is None:
        pattern = _BLOCKS[base] = re.compile(_BLANK_OR_DEEPER % (base + 1))
    m = pattern.match(text, pos, b)
    return m.end() if m else pos


def _secret_doc_spans(text: str, a: int, b: int) -> list[Span]:
    spans: list[Span] = []
    for m in _DATA_LINE.finditer(text, a, b):
        inline = m.group(2).strip()
        if inline and not inline.startswith("#"):
            if inline != "{}" and not _is_marked(inline):
                spans.append((m.start(2), m.start(2) + len(m.group(2).rstrip()), "k8s-secret"))
            continue
        pos = m.end() + 1
        end = _block_end(text, pos, b, len(m.group(1)))
        children = list(_CHILD.finditer(text, pos, end))
        if not children:
            continue
        child = min(len(c.group(1)) for c in children)
        keys = [c for c in children if len(c.group(1)) == child]
        for i, c in enumerate(keys):
            value = (c.group(3) or "").rstrip(" \t\r")
            if _BLOCK_SCALAR.fullmatch(value):
                stop = keys[i + 1].start() if i + 1 < len(keys) else end
                block = text[c.end() : stop]
                body = block.strip()
                if body and not _is_marked(body):
                    first = c.end() + block.index(body[0])
                    spans.append((first, first + len(body), "k8s-secret"))
            elif value and not value.startswith("#") and not _is_marked(value):
                spans.append((c.start(3), c.start(3) + len(value), "k8s-secret"))
    return spans


_SECRET_KIND_JSON = re.compile(r"\"kind\"[ \t]*:[ \t]*\"Secret\"")
_JSON_DATA = re.compile(r"\"(?:data|stringData)\"[ \t\r\n]*:[ \t\r\n]*\{([^{}]{0,200000})\}")
_JSON_PAIR = re.compile(r"\"[^\"\n]{1,200}\"[ \t\r\n]*:[ \t\r\n]*\"((?:[^\"\\\n]|\\.){1,100000})\"")


def _find_k8s_secret_json(text: str, low: str) -> list[Span]:
    if not _SECRET_KIND_JSON.search(text):
        return []
    spans: list[Span] = []
    for m in _JSON_DATA.finditer(text):
        for p in _JSON_PAIR.finditer(text, m.start(1), m.end(1)):
            if not _is_marked(p.group(1)):
                spans.append((*p.span(1), "k8s-secret"))
    return spans


# ---- Kubernetes env lists: "- name: DB_PASSWORD" then "value: ..." ------------------------------

_ENV_PAIR = re.compile(
    r"name:[ \t]*[\"']?([A-Za-z0-9_.-]{1,120})[\"']?[ \t]*\r?\n"
    r"[ \t]*value:[ \t]*([^\n]{1,8192})"
)


_ENV_PREFIX = re.compile(r"[ \t]*(?:-[ \t]*)?")


def _find_env_pairs(text: str, low: str) -> list[Span]:
    spans: list[Span] = []
    for m in _ENV_PAIR.finditer(text):
        kind = _key_kind(m.group(1))
        line_start = text.rfind("\n", max(0, m.start() - 200), m.start()) + 1
        if kind is None or not _ENV_PREFIX.fullmatch(text, line_start, m.start()):
            continue
        vs = m.start(2)
        ve = vs + len(m.group(2).rstrip(" \t\r"))
        if ve - vs >= 2 and text[vs] in "\"'" and text[ve - 1] == text[vs]:
            vs, ve = vs + 1, ve - 1
        if not _plain_value(text[vs:ve]):
            spans.append((vs, ve, kind))
    return spans


# ---- tokens with a known shape ---------------------------------------------------------------

_KUBE_DATA = re.compile(r"client-(key-data|certificate-data)([\"']?[ \t]*:[ \t]*[\"']?)([A-Za-z0-9+/=]{8,})")
_AWS_SECRET = re.compile(  # on the lower-cased text
    r"secret_?access_?key[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"([a-z0-9/+=]{40})(?![a-z0-9/+=])"
)


def _lit(prefix: str, rest: str, not_after: str) -> re.Pattern[str]:
    """`prefix` + `rest`, where `prefix` must not follow a character of `not_after`. The look-behind
    sits after the literal, so the search stays fast and each run of text is scanned once."""
    p = re.escape(prefix)
    return re.compile(f"({p}(?<![{not_after}]{p}){rest})")


_W = "A-Za-z0-9_"  # word characters
_WD = "A-Za-z0-9_-"  # word characters and "-"
_AWS_KEY_ID = _lit("A", r"(?:KIA|SIA|BIA|CCA)[A-Z0-9]{16}(?![A-Za-z0-9])", _W)
_GITHUB = _lit("gh", r"[pousr]_[A-Za-z0-9]{36,255}(?![A-Za-z0-9_])", _W)
_GITHUB_PAT = _lit("github_pat_", r"[A-Za-z0-9_]{22,255}", _W)
_GITLAB = _lit("glpat-", r"[A-Za-z0-9_-]{20,255}", _WD)
_SLACK = _lit("xox", r"[abprs]-[A-Za-z0-9-]{10,255}", _WD)
_STRIPE = _lit("sk_", r"(?:live|test)_[A-Za-z0-9]{16,255}", _W)
_STRIPE_RESTRICTED = _lit("rk_", r"(?:live|test)_[A-Za-z0-9]{16,255}", _W)
_GOOGLE = _lit("AIza", r"[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", _WD)
_ANTHROPIC = _lit("sk-ant-", r"[A-Za-z0-9_-]{20,255}", _WD)
_OPENAI = _lit(
    "sk-", r"(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,255}|[A-Za-z0-9]{20,255})(?![A-Za-z0-9_-])", _WD
)
_NPM = _lit("npm_", r"[A-Za-z0-9]{36}(?![A-Za-z0-9])", _W)
_JWT = _lit("eyJ", r"[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,8192}\.[A-Za-z0-9_-]{0,4096}", _WD)


def _kube_kind(m: re.Match[str], value: str) -> str:
    return "kubeconfig-key" if m.group(1).startswith("key") else "kubeconfig-cert"


def _openai_kind(m: re.Match[str], value: str) -> str | None:
    body = value[3:]
    if not any(c.isdigit() for c in body) or not any(c.isalpha() for c in body):
        return None
    return "openai-key"


# ---- HTTP authorization: keep the scheme, hide the credential ----------------------------------

_AUTH_HEADER = re.compile(  # on the lower-cased text
    r"authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"(?:(bearer|basic|token|digest|bot|apikey)[ \t]+)?([^\s\"',;]{1,4096})"
)
_BEARER = re.compile(r"bearer(?<![a-z0-9_]bearer)[ \t]+([a-z0-9._~+/=-]{16,4096})")  # lower-cased


def _auth_kind(m: re.Match[str], value: str) -> str | None:
    if m.group(1) is None and _lower(value) in _AUTH_SCHEMES:
        return None  # "Authorization: Bearer" with nothing after it
    return None if _plain_value(value) else "authorization"


def _bearer_kind(m: re.Match[str], value: str) -> str | None:
    return "bearer" if any(c.isdigit() for c in value) else None


# ---- URLs and connection strings with user:password@ ------------------------------------------

_URL_CREDS = re.compile(r"://[^\s:/?#@\"'<>]{0,256}:([^\s/?#@\"'<>]{1,256})@")  # lower-cased
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,20}")
_DB_SCHEMES = ("postgres", "mysql", "mariadb", "mongodb", "redis", "amqp", "mssql", "sqlserver", "jdbc")


def _url_kind(m: re.Match[str], value: str) -> str | None:
    if value.isdigit():
        return None  # host:port, not a password
    low = m.string
    start = m.start()
    while start > 0 and start > m.start() - 21 and (low[start - 1].isalnum() or low[start - 1] in "+.-"):
        start -= 1
    scheme = low[start : m.start()]
    if not _SCHEME.fullmatch(scheme):
        return None
    return "connection-string" if scheme.startswith(_DB_SCHEMES) else "url-password"


# ---- command-line flags: --password value, --api-token "value" ---------------------------------

_FLAG_NEEDLES = ("pass", "token", "secret", "key", "auth", "credential")
_FLAG = re.compile(  # on the lower-cased text
    r"--?((?:[a-z0-9]{1,30}-){0,6}(?:password|passwd|passphrase|token|secret|api-?key|access-?key"
    r"|auth|credentials?))[ \t]+(?:\"([^\"\n]{1,4096})\"|'([^'\n]{1,4096})'|([^\s\"'-][^\s\"']{0,4096}))"
)


def _find_flags(text: str, low: str) -> list[Span]:
    spans: list[Span] = []
    for m in _FLAG.finditer(low):
        if m.start() > 0 and text[m.start() - 1] in _WORD_DASH:
            continue
        group = 2 if m.group(2) is not None else 3 if m.group(3) is not None else 4
        start, end = m.span(group)
        kind = _key_kind(m.group(1))
        if kind and not _plain_value(text[start:end]):
            spans.append((start, end, kind))
    return spans


# ---- key=value, key: value, "key": "value" with a secret-like key ------------------------------

_SEP = r"([\"']?)[ \t]*(=(?!=)|:(?![:/]))[ \t]*"
_KEY_ANCHORS = tuple(  # on the lower-cased text; each starts with a literal, so the search is fast
    re.compile(word + _SEP)
    for word in (
        r"pass(?:w(?:or)?d|phrase)",
        "pwd",
        "secret",
        "token",
        "auth",
        "credentials?",
        "key(?:"
        + "|".join(
            f"(?<={w}key)|(?<={w}[-_. ]key)"
            for w in ("api", "access", "secret", "account", "signing", "private")
        )
        + ")",
    )
)
_KEY_WORDS = ("api", "access", "secret", "account", "signing", "private")
_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_KEY_CHARS_QUOTED = _KEY_CHARS | {" "}
_LINE_PREFIX = re.compile(r"[ \t]*(?:-[ \t]+|export[ \t]+|set[ \t]+|#[ \t]*|//[ \t]*)?[\"']?")
_DQ_VALUE = re.compile(r"\"((?:[^\"\\\n]|\\.){0,8192})\"")
_SQ_VALUE = re.compile(r"'([^'\n]{0,8192})'")
_BARE_VALUE = re.compile(r"[^\s\"'`,;&<>(){}\[\]]{1,8192}")
_REST_OF_LINE = re.compile(r"[^\r\n]{1,8192}")
_TO_SPACE = re.compile(r"[^\s\"']{1,8192}")
_CODE_CALL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,200}[(\[]")


def _find_key_values(text: str, low: str) -> list[Span]:
    spans: list[Span] = []
    done = 0
    anchors = sorted((m for p in _KEY_ANCHORS for m in p.finditer(low)), key=lambda m: m.start())
    for m in anchors:
        if m.start() < done:
            continue
        span = _key_value_span(text, m)
        if span:
            spans.append(span)
            done = span[1]
    return spans


def _key_value_span(text: str, m: re.Match[str]) -> Span | None:
    quote = m.group(1)
    key_end = m.start(1)
    start = m.start()
    allowed = _KEY_CHARS_QUOTED if quote else _KEY_CHARS
    while start > 0 and key_end - start < 120 and text[start - 1] in allowed:
        start -= 1
    if start > 0 and text[start - 1] in _KEY_CHARS:
        return None  # longer than any real key
    if quote:
        while start < key_end and text[start] == " ":
            start += 1
        if text[start - 1 : start] != quote:
            return None
    if not quote and key_end - start == 3 and text[start - 1 : start] == " ":
        word = _lower(text[max(0, start - 9) : start - 1]).rsplit(" ", 1)[-1]
        if word in _KEY_WORDS:
            start -= len(word) + 1  # "API key: ..."
    key = text[start:key_end]
    kind = _key_kind(key)
    if kind is None:
        return None
    floor = max(0, start - 512)
    line_start = text.rfind("\n", floor, start) + 1
    found = line_start > 0 or floor == 0
    at_line_start = found and _LINE_PREFIX.fullmatch(text, line_start, start) is not None
    line_start = line_start or floor
    sep = m.group(2)
    if sep == ":" and not quote and not at_line_start:
        before = text[line_start:start].rstrip(" \t")
        compound = " " not in key and (any(c in key for c in "_-.") or any(c.isupper() for c in key[1:]))
        if not compound and not before.endswith(("{", ",", ";")):
            return None  # prose like "invalid token: expected 3 parts"
    pos = m.end()
    if pos >= len(text):
        return None
    ch = text[pos]
    if ch == '"':
        vm = _DQ_VALUE.match(text, pos)
        if not vm:
            return None
        vs, ve = vm.span(1)
    elif ch == "'":
        vm = _SQ_VALUE.match(text, pos)
        if not vm:
            return None
        vs, ve = vm.span(1)
    elif at_line_start and not quote and sep == "=":
        vm = _TO_SPACE.match(text, pos)  # an env file: KEY=value up to the first space
        if not vm or _CODE_CALL.match(vm.group(0)):
            return None  # code: password = get_password()
        vs, ve = vm.span()
    elif at_line_start and not quote:
        vm = _REST_OF_LINE.match(text, pos)  # YAML: key: the rest of the line
        if not vm:
            return None
        vs, ve = pos, pos + len(vm.group(0).rstrip(" \t"))
        if _BLOCK_SCALAR.fullmatch(text[vs:ve]):
            return _yaml_block_span(text, line_start, ve, kind)
    else:
        vm = _BARE_VALUE.match(text, pos)
        if not vm:
            return None
        vs, ve = vm.span()
        if text[ve : ve + 1] in ("(", "["):
            return None  # a function call or an index in code
    value = text[vs:ve]
    if _plain_value(value):
        return None
    if _lower(key).translate(_KEY_STRIP) in ("pwd", "oldpwd") and value.startswith(("/", "~")):
        return None  # the shell's working folder
    return vs, ve, kind


def _yaml_block_span(text: str, line_start: int, ve: int, kind: str) -> Span | None:
    """A YAML block scalar (`password: |`): hide the more indented lines below it."""
    key_indent = _indent(text[line_start:ve])
    nl = text.find("\n", ve)
    if nl < 0:
        return None
    pos = first = nl + 1
    end = first
    while pos < len(text):
        line_end = text.find("\n", pos)
        line_end = len(text) if line_end < 0 else line_end
        line = text[pos:line_end]
        if line.strip() and _indent(line) <= key_indent:
            break
        if line.strip():
            end = pos + len(line.rstrip())
        pos = line_end + 1
    block = text[first:end]
    if not block.strip() or _is_marked(block):
        return None
    return first + _indent(block), end, kind


# ---- a long, random-looking string right after a secret-like word ------------------------------

_AFTER = r"\b(?:[ \t]+(?:is|was|of|for|value|to))?[ \t:=]{0,3}[\"'`]?([a-z0-9][a-z0-9+/_=.~-]{31,4096})"
_AFTER_WORDS = tuple(  # on the lower-cased text
    re.compile(f"{lit}(?<![a-z0-9_]{lit}){rest}{_AFTER}")
    for lit, rest in (
        ("pass", "(?:words?|wd|phrase)"),
        ("pwd", ""),
        ("secret", "s?"),
        ("token", "s?"),
        ("key", "s?"),
        ("credential", "s?"),
    )
)
_AFTER_NEEDLES = ("pass", "pwd", "secret", "token", "key", "credential")
_DNS_NAME = re.compile(r"[a-z0-9]{1,63}(?:[-.][a-z0-9]{1,63}){1,60}")
# A lower-case path of words, like a Grimoire key "infra/k8s/flux/flux-reference-bootstrap.md":
# two or more segments, each a DNS-like name, and real words in it (a run of 4+ letters).
_PATH_SEGMENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_WORD_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})+/?")
_LETTER_RUN = re.compile(r"[a-z]{4,}")
_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(k / n * math.log2(k / n) for k in Counter(s).values())


def _random_kind(m: re.Match[str], value: str) -> str | None:
    if _HEX40.fullmatch(value) or _UUID.fullmatch(value) or _lower(value).startswith("sha"):
        return None  # a git commit, a UUID, a digest
    if _DNS_NAME.fullmatch(value):
        return None  # a Kubernetes or DNS name: my-db-secret-v1, api.example.com
    if _WORD_PATH.fullmatch(value) and _LETTER_RUN.search(value):
        return None  # a path of words (no capitals): a file or a Grimoire key, not a secret
    classes = sum(
        (any(c.islower() for c in value), any(c.isupper() for c in value), any(c.isdigit() for c in value))
    )
    if classes < 2 or _entropy(value) < 3.5:
        return None
    return "high-entropy"


# ---- the rules, in order --------------------------------------------------------------------

_RULES: tuple[_Rule, ...] = (
    _Rule(_find_pem, ("private key",)),
    _Rule(_find_k8s_secret_yaml, ("secret",)),
    _Rule(_find_k8s_secret_json, ('"secret"',)),
    _regex_rule(_KUBE_DATA, _kube_kind, 3, needles=("-data",)),
    _regex_rule(_AWS_KEY_ID, "aws-access-key-id", needles=("akia", "asia", "abia", "acca")),
    _regex_rule(_AWS_SECRET, "aws-secret-key", on_low=True, needles=("access",)),
    _regex_rule(_GITHUB, "github-token", needles=("gh",)),
    _regex_rule(_GITHUB_PAT, "github-token", needles=("github_pat_",)),
    _regex_rule(_GITLAB, "gitlab-token", needles=("glpat-",)),
    _regex_rule(_SLACK, "slack-token", needles=("xox",)),
    _regex_rule(_STRIPE, "stripe-key", needles=("sk_live_", "sk_test_")),
    _regex_rule(_STRIPE_RESTRICTED, "stripe-key", needles=("rk_live_", "rk_test_")),
    _regex_rule(_GOOGLE, "google-api-key", needles=("aiza",)),
    _regex_rule(_ANTHROPIC, "anthropic-key", needles=("sk-ant-",)),
    _regex_rule(_OPENAI, _openai_kind, needles=("sk-",)),
    _regex_rule(_NPM, "npm-token", needles=("npm_",)),
    _regex_rule(_JWT, "jwt", needles=("eyj",)),
    _regex_rule(_AUTH_HEADER, _auth_kind, 2, on_low=True, needles=("authorization",)),
    _regex_rule(_BEARER, _bearer_kind, on_low=True, needles=("bearer",)),
    _regex_rule(_URL_CREDS, _url_kind, on_low=True, needles=("://",)),
    _Rule(_find_env_pairs, ("value:",)),
    _Rule(_find_flags, _FLAG_NEEDLES),
    _Rule(_find_key_values, ("pass", "pwd", "secret", "token", "key", "auth", "credential")),
    *(_regex_rule(p, _random_kind, on_low=True, needles=_AFTER_NEEDLES) for p in _AFTER_WORDS),
)


def _apply(text: str, spans: list[Span], count: list[int], kinds: set[str]) -> str:
    out: list[str] = []
    pos = 0
    for start, end, kind in sorted(spans, key=lambda s: (s[0], -s[1])):
        if start < pos or end <= start or text.startswith(MARKER_PREFIX, start):
            continue
        out.append(text[pos:start])
        out.append(marker(kind))
        count[0] += 1
        kinds.add(kind)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact(text: str) -> Redaction:
    """Hide likely credentials. Idempotent: redacting the result again finds nothing."""
    if not text:
        return Redaction(text, 0, [])
    count = [0]
    kinds: set[str] = set()
    low = first_low = _lower(text)
    seen: dict[str, bool] = {}  # needle -> found in the text
    for rule in _RULES:
        if rule.needles and not any(
            seen.setdefault(n, n in first_low) if n not in seen else seen[n] for n in rule.needles
        ):
            continue
        spans = rule.find(text, low)
        if spans:
            text = _apply(text, spans, count, kinds)
            low = _lower(text)
    return Redaction(text, count[0], sorted(kinds))


# ---- secret-like file names ------------------------------------------------------------------

_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_SECRET_NAMES = frozenset({".env", ".npmrc", ".pypirc", ".netrc", "_netrc", ".pgpass", ".git-credentials"})
_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".p8",
    ".ppk",
    ".jks",
    ".keystore",
    ".kdbx",
    ".tfvars",
    ".tfvars.json",
)
_SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "kubeconfig", "credentials")
_SECRET_MANIFEST = re.compile(r"(?:.*[-_.])?secrets?(?:\.[a-z0-9_-]+)?\.(?:ya?ml|json)")


def is_secret_path(path: str) -> bool:
    """True when the file name says it likely holds secrets: never read or send its contents."""
    parts = [p for p in _lower(path.replace("\\", "/")).split("/") if p and p != "."]
    if not parts:
        return False
    name = parts[-1]
    if name in _ENV_TEMPLATES:
        return False
    if name in _SECRET_NAMES or name.startswith(".env."):
        return True
    if name.endswith(_SECRET_SUFFIXES) or name.startswith(_SECRET_PREFIXES) or ".tfstate" in name:
        return True
    if parts[-2:] in ([".kube", "config"], [".docker", "config.json"]):
        return True
    return _SECRET_MANIFEST.fullmatch(name) is not None
