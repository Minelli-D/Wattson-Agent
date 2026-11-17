"""A stand-in for the user's Grimoire server, for tests and for a local run of the CLI.

It plays both parts of the real one (see /home/dm/Desktop/grimoire: gateway/server.mjs, mcp/server.ts):
- a small OAuth 2.1 server at /auth/v1, laid out like Grimoire's (Supabase Auth behind the gateway):
  the protected-resource document, the authorization-server document, dynamic client registration,
  /oauth/authorize (it says yes at once and redirects to the redirect_uri with code and state), and
  /oauth/token (authorization_code with a real PKCE S256 check, refresh_token with rotation);
- the MCP endpoint /mcp over streamable HTTP (the mcp SDK's server classes), open only with a token
  from that server. It offers Grimoire's tool names and input schemas, write_document and
  delete_document included, so tests can prove that the client never calls them. The list answers
  are TOON like Grimoire's (mcp/output.ts) or indented JSON (fmt="json").

The documents are invented. Nothing here talks to the real Grimoire.

    uv run python tests/fixtures/grimoire_stub.py --port 18090 [--format json] [--delay 0] [--token-ttl 3600]
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import anyio
import mcp_types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

# A fake credential inside one note: the knowledge agent must redact it (SEC-04, SEC-05).
FAKE_SECRET = "ghp_" + "Zx9" * 12  # a GitHub-token-shaped string, 40 characters
FAKE_AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"
# The protocol versions Grimoire's gateway lets through (gateway/server.mjs DEFAULT_MCP_VERSIONS): a
# request in any other version gets a JSON-RPC error with HTTP 400, and the client falls back to
# the older handshake.
GATEWAY_MCP_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"]


@dataclass
class Doc:
    key: str
    title: str
    tags: list[str]
    body: str
    type: str = "runbook"
    status: str = "active"
    env: str | None = None
    danger: bool = False
    summary: str | None = None

    @property
    def folder(self) -> str:
        return self.key.rsplit("/", 1)[0] if "/" in self.key else ""


def default_docs() -> list[Doc]:
    return [
        Doc(
            "infra/k8s/crashloop-runbook",
            "CrashLoopBackOff: find the cause, then fix it",
            ["k8s", "pods", "crashloop", "kubectl"],
            "# CrashLoopBackOff\n\n1. `kubectl -n <ns> get pods` and find the pod that restarts.\n"
            "2. `kubectl -n <ns> logs <pod> --previous` shows why the last run died.\n"
            "3. `kubectl -n <ns> describe pod <pod>`: look at Last State, Exit Code and Events.\n"
            "4. Exit code 137 is OOMKilled: raise the memory limit or fix the leak.\n"
            "5. A failing liveness probe also restarts the pod: check its path and timeout.\n",
        ),
        Doc(
            "infra/k8s/oomkilled-pods",
            "OOMKilled pods: limits, requests and the leak",
            ["k8s", "pods", "memory", "oom"],
            "# OOMKilled\n\nExit code 137. Compare the memory limit with `kubectl top pod`. "
            "Raise the limit only after you know the working set.\n",
            type="reference",
        ),
        Doc(
            "infra/k8s/cert-manager-renewal",
            "cert-manager: renew a stuck certificate",
            ["k8s", "cert-manager", "tls", "letsencrypt"],
            "# Renew a stuck certificate\n\n`kubectl describe certificate <name>` then "
            "`kubectl get challenges -A`. Delete the failed CertificateRequest to retry.\n",
        ),
        Doc(
            "infra/k8s/networkpolicy-basics",
            "NetworkPolicy: only the ingress namespace may reach the pod",
            ["k8s", "networkpolicy", "security"],
            "# NetworkPolicy\n\nStart with deny-all ingress, then allow the ingress namespace.\n",
            type="reference",
        ),
        Doc(
            "infra/postgres/backup-restore",
            "Postgres: backup and restore one table",
            ["postgres", "backup", "restore"],
            "# Backup and restore\n\nThe nightly backup job uses these settings:\n\n"
            f"    export PGPASSWORD={FAKE_SECRET}\n    export AWS_ACCESS_KEY_ID={FAKE_AWS_KEY}\n\n"
            "Restore one table: `pg_restore --data-only -t documents documents.dump`.\n",
            env="production",
            danger=True,
        ),
        Doc(
            "ai/prompt-writing-guide",
            "Prompt writing guide",
            ["ai", "prompting", "claude"],
            "# Prompt writing\n\nState the objective, the evidence, the constraints and the outcome.\n",
            type="note",
        ),
        Doc(
            "ci/gitlab-runner-troubleshooting",
            "GitLab runner troubleshooting",
            ["ci", "gitlab", "runner", "k8s"],
            "# GitLab runner\n\nA job stuck in pending: check the runner tags and `gitlab-runner verify`.\n",
        ),
        Doc(
            "notes/crashloop-ideas",
            "Ideas about crashloop alerts",
            ["notes", "k8s", "crashloop"],
            "Draft: alert when a pod restarts more than 5 times in 10 minutes.\n",
            type="note",
            status="draft",
        ),
        Doc(
            "linux/systemd-timers",
            "A systemd timer instead of cron",
            ["linux", "systemd", "cron"],
            "# systemd timers\n\n`systemctl list-timers`, then a .timer next to the .service.\n",
            type="snippet",
        ),
        # Close to the crashloop runbook (kubectl, logs) without its words "crashloop" or
        # "previous": a related document, and a context document for the tag k8s.
        Doc(
            "infra/k8s/pod-logs",
            "Pod logs: follow and filter them",
            ["k8s", "pods", "kubectl", "logs"],
            "# Pod logs\n\n`kubectl logs -f <pod>` follows the output, `-c <container>` picks one "
            "container and `--since=10m` keeps it short.\n",
            type="snippet",
        ),
        Doc(
            "infra/k8s/deployment-restart",
            "Restart a deployment without downtime",
            ["k8s", "deployments"],
            "# Rollout restart\n\n`kubectl rollout restart deployment/<name>` replaces the pods one by "
            "one. Watch them with `kubectl rollout status` and read their logs with "
            "`kubectl logs -l app=<name>`.\n",
        ),
        # Noise: they share only generic words (model, docs, results, numbers) with a request
        # about something else. Grimoire's search returns them anyway when no document has all
        # the words; the knowledge agent must not add them.
        Doc(
            "projects/trippers/architecture",
            "Trippers: architecture",
            ["trippers", "architecture"],
            "# Trippers\n\nThree services: api, worker and web. The data model is in docs/model.md; "
            "the worker writes the results of each trip to the database.\n",
            type="reference",
        ),
        Doc(
            "projects/english-coach/architecture",
            "Example App: architecture",
            ["english-coach", "architecture"],
            "# Example App\n\nA small language model corrects the sentences of a lesson. The "
            "numbers of each lesson are kept as JSON results.\n",
            type="reference",
        ),
        *wattson_docs(),
    ]


def procedure_body(when: str, evidence: str, steps: str, done: str, do_not: str, related: str) -> str:
    """A Wattson procedure body with its fixed headings (grimoire-procedures/wattson/*.md)."""
    return (
        f"## When to use\n\n{when}\n\n## Evidence Wattson collects\n\n{evidence}\n\n"
        f"## Steps for Claude Code\n\n{steps}\n\n## Done when\n\n{done}\n\n## Do not\n\n{do_not}\n\n"
        f"## Related runbooks\n\n{related}\n"
    )


def wattson_docs() -> list[Doc]:
    """Three invented Wattson procedures (folder "wattson"). Grimoire builds the key as folder +
    title + ".md" and adds the folder name to the tags: here "wattson" is written in the tags.
    Their words stay away from the queries of the other tests (no crashloop, model, docs, ...)."""
    return [
        Doc(
            "wattson/k8s-tls-ingress.md",
            "k8s-tls-ingress",
            ["devops-investigation", "tls", "certificate", "ingress", "https", "wattson"],
            procedure_body(
                "An app on the cluster cannot be reached at its address while its workloads are fine: "
                "HTTPS or certificate errors, or the Ingress answers 404 or 502.",
                "- `k8s.get ingresses`: hosts, class and address\n"
                "- `k8s.describe ingresses <name>`: the TLS secret name and the backends\n"
                "- `k8s.events`: certificate warnings in the namespace",
                "1. `curl -sv https://<host>/ -o /dev/null`: read the issuer and the expiry date.\n"
                "2. `kubectl -n <namespace> get certificate,challenge`, then describe the one that is "
                "not Ready.\n"
                "3. `kubectl -n <namespace> get endpoints <service>`: no endpoints means the selector "
                "matches nothing.",
                "The broken link (DNS, challenge, issuer, Ingress rule or Service) is named with the "
                "output that proves it, and the fix is a plan with a verify command. Nothing is applied.",
                "- No delete, patch or apply of an Ingress or a Certificate without asking.\n"
                "- Never print TLS private keys.",
                "- infra/k8s/cert-manager-renewal\n- infra/k8s/networkpolicy-basics",
            ),
        ),
        Doc(
            "wattson/code-bug-fix.md",
            "code-bug-fix",
            ["engineering-task", "bug", "regression", "traceback", "failing-test", "wattson"],
            procedure_body(
                "A test fails or the code raises an error it did not raise before: a traceback, a "
                "wrong value, or a regression after a change.",
                "- `git.status` and `git.diffstat`: what changed since the last commit\n"
                "- `git.grep` of the error text: where it is raised\n"
                "- `git.file` of the failing test",
                "1. Run the failing test alone and read the traceback from the bottom up.\n"
                "2. Write a test that fails for the same reason before changing anything.\n"
                "3. Make the smallest change that makes it pass, then run the whole suite.",
                "The new test fails without the fix and passes with it, and the whole suite is green.",
                "- Do not delete or skip a failing test to make the suite green.\n- No push.",
                "- none yet",
            ),
        ),
        Doc(
            "wattson/prompt-rewrite.md",
            "prompt-rewrite",
            ["prompt-optimization", "prompt", "rewrite", "clearer", "wattson"],
            procedure_body(
                "The user has a rough request and wants a clearer prompt for Claude Code, with the "
                "goal, the context and the limits written out.",
                "- `git.tree`: the layout of the repository the prompt is about",
                "1. Say the goal in one sentence.\n"
                "2. List what Claude Code must read first and what it must not touch.\n"
                "3. End with how to check that the work is done.",
                "The prompt names the goal, the context, the limits and the check.",
                "- Do not invent facts about the repository.",
                "- ai/prompt-writing-guide\n- ai/prompt-examples",  # the second one does not exist
            ),
            summary="Rewrite a rough request into a clear prompt for Claude Code.",
        ),
    ]


# ---- TOON, as Grimoire writes it (mcp/output.ts with @toon-format/toon 4.1.1) ---------------

_WHITE_SPACE = re.compile(
    r"[\s\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff\u001c-\u001f]"
)
_NUMERIC_LIKE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?$", re.IGNORECASE)
_UNQUOTED_KEY = re.compile(r"^[A-Za-z_][\w.]*$")

SHAPES: dict[str, dict[str, Any]] = {
    "contextPreview": {"spaced": ["tags"], "optional": ["env", "danger", "summary"], "folderInKey": True},
    "search": {
        "spaced": ["tags"],
        "optional": ["summary", "snippet"],
        "folderInKey": True,
        "snippets": ["snippet"],
    },
    "related": {"spaced": ["tags"], "optional": ["env", "danger", "summary"], "folderInKey": True},
    "tags": {"spaced": ["folders"], "topLevelMark": "/"},
    "suggestions": {},
    "folders": {},
}


def plain_snippet(snippet: str) -> str:
    text = re.sub(r"</?mark>", "", snippet)
    text = re.sub(r"&(amp|lt|gt);", lambda m: {"amp": "&", "lt": "<", "gt": ">"}[m.group(1)], text)
    return re.sub(r"[\s\u001c-\u001f]+", " ", text).strip()


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, list) and not value)


def shape_rows(rows: list[dict[str, Any]], shape: dict[str, Any]) -> list[dict[str, Any]]:
    out = [dict(row) for row in rows]
    for name in shape.get("snippets", []):
        out = [{**r, name: plain_snippet(r[name])} if isinstance(r.get(name), str) else r for r in out]
    for name in shape.get("spaced", []):
        out = _join_with_spaces(out, name, shape.get("topLevelMark"))
    if shape.get("folderInKey") and all(_folder_in_key(r) for r in out):
        out = [{k: v for k, v in r.items() if k != "folder"} for r in out]
    for name in shape.get("optional", []):
        if all(_is_empty(r.get(name)) or r.get(name) is False for r in out):
            out = [{k: v for k, v in r.items() if k != name} for r in out]
    return out


def _folder_in_key(row: dict[str, Any]) -> bool:
    key, folder = row.get("key"), row.get("folder")
    if not isinstance(key, str) or not isinstance(folder, str):
        return False
    return folder == (key.rsplit("/", 1)[0] if "/" in key else "")


def _join_with_spaces(rows: list[dict[str, Any]], name: str, mark: str | None) -> list[dict[str, Any]]:
    joined = []
    for row in rows:
        items = row.get(name)
        if not isinstance(items, list):
            return rows
        words = []
        for item in items:
            if not isinstance(item, str) or _WHITE_SPACE.search(item):
                return rows
            if (item == "" and not mark) or (mark and item == mark):
                return rows
            words.append(mark if item == "" else item)
        joined.append(" ".join(words))
    return [{**row, name: joined[i]} for i, row in enumerate(rows)]


def _toon_string(value: str) -> str:
    safe = (
        value
        and not re.search(r"^[ \t]|[ \t]$", value)
        and value not in ("true", "false", "null")
        and not _NUMERIC_LIKE.match(value)
        and not any(c in value for c in ':"\\[]{},')
        and not re.search(r"[\x00-\x1f]", value)
        and not value.startswith(("-", "#"))
    )
    if safe:
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    escaped = re.sub(r"[\x00-\x1f]", lambda m: f"\\u{ord(m.group(0)):04x}", escaped)
    return f'"{escaped}"'


def _toon_primitive(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    return _toon_string(str(value))


def _toon_key(key: str) -> str:
    return key if _UNQUOTED_KEY.match(key) else '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toon_encode_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "[]"
    fields = list(rows[0])
    tabular = all(list(r) == fields or set(r) == set(fields) for r in rows) and all(
        not isinstance(v, list | dict) for r in rows for v in r.values()
    )
    if tabular:
        header = f"[{len(rows)}]{{{','.join(_toon_key(f) for f in fields)}}}:"
        lines = [header] + ["  " + ",".join(_toon_primitive(r[f]) for f in fields) for r in rows]
        return "\n".join(lines)
    lines = [f"[{len(rows)}]:"]
    for row in rows:
        first = True
        for name, value in row.items():
            if isinstance(value, list):
                text = f"{_toon_key(name)}[{len(value)}]:" + (
                    " " + ",".join(_toon_primitive(v) for v in value) if value else ""
                )
            else:
                text = f"{_toon_key(name)}: {_toon_primitive(value)}"
            lines.append(("  - " if first else "    ") + text)
            first = False
        if first:
            lines.append("  -")
    return "\n".join(lines)


def toon_encode_record(record: dict[str, Any]) -> str:
    return "\n".join(f"{_toon_key(k)}: {_toon_primitive(v)}" for k, v in record.items() if not _is_empty(v))


# ---- the MCP tools, with Grimoire's names and input schemas --------------------------------


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = [
    types.Tool(
        name="get_context",
        description="Tag-driven context retrieval (stub).",
        input_schema=_schema(
            {
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "mode": {"type": "string", "enum": ["any", "all"], "default": "all"},
                "max_tokens": {"type": "integer", "exclusiveMinimum": 0, "default": 8000},
                "preview": {"type": "boolean", "default": False},
                "keys": {"type": "array", "items": {"type": "string"}},
            },
            ["tags"],
        ),
    ),
    types.Tool(
        name="search_documents",
        description="Free-text search (stub).",
        input_schema=_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
            ["query"],
        ),
    ),
    types.Tool(
        name="read_document",
        description="Full frontmatter + body for one document.",
        input_schema=_schema({"key": {"type": "string", "minLength": 1}}, ["key"]),
    ),
    types.Tool(
        name="get_related_documents",
        description="Documents that share the most tags (stub).",
        input_schema=_schema(
            {
                "key": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            ["key"],
        ),
    ),
    types.Tool(name="list_tags", description="Every tag (stub).", input_schema=_schema({}, [])),
    types.Tool(
        name="suggest_tags",
        description="Trigram similarity (stub).",
        input_schema=_schema({"query": {"type": "string", "minLength": 1}}, ["query"]),
    ),
    types.Tool(name="list_folders", description="The folder tree (stub).", input_schema=_schema({}, [])),
    types.Tool(
        name="write_document",
        description="Create or update a document (stub).",
        input_schema=_schema(
            {
                "key": {"type": "string"},
                "title": {"type": "string", "minLength": 1},
                "type": {"type": "string"},
                "folder": {"type": "string", "default": ""},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "env": {"type": "string", "enum": ["local", "staging", "production"]},
                "danger": {"type": "boolean", "default": False},
                "status": {"type": "string", "enum": ["active", "draft", "archived"], "default": "active"},
                "body": {"type": "string", "minLength": 1},
                "confirm_overwrite": {"type": "boolean", "default": False},
            },
            ["title", "type", "body"],
        ),
    ),
    types.Tool(
        name="delete_document",
        description="Permanently deletes a document by key (stub).",
        input_schema=_schema(
            {"key": {"type": "string", "minLength": 1}, "confirm": {"type": "boolean"}}, ["key", "confirm"]
        ),
    ),
]


def format_frontmatter(doc: Doc) -> str:
    def quote(s: str) -> str:
        return '"' + s.replace('"', '\\"') + '"'

    lines = ["---", f"title: {quote(doc.title)}", f"type: {doc.type}"]
    if doc.env:
        lines.append(f"env: {doc.env}")
    lines.append(f"danger: {'true' if doc.danger else 'false'}")
    if doc.tags:
        lines.append(f"tags: [{', '.join(quote(t) for t in doc.tags)}]")
    lines += [f"status: {doc.status}", "---"]
    return "\n".join(lines)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Library:
    """The invented knowledge base and Grimoire's tools over it."""

    def __init__(self, docs: list[Doc], fmt: str) -> None:
        self.docs = {d.key: d for d in docs}
        self.fmt = fmt

    def answer(self, rows: list[dict[str, Any]], shape: str) -> str:
        if self.fmt == "json":
            return json.dumps(rows, indent=2, ensure_ascii=False)
        return toon_encode_rows(shape_rows(rows, SHAPES[shape]))

    def _live(self) -> list[Doc]:
        return [d for d in self.docs.values() if d.status != "archived"]

    def search(self, query: str, limit: int = 10, type: str | None = None) -> str:
        words = [w for w in re.findall(r"[\w-]+", query.lower()) if w]

        def text_of(d: Doc) -> str:
            return f"{d.title} {' '.join(d.tags)} {d.body}".lower()

        def hits(d: Doc, needed: str) -> int:
            counts = [text_of(d).count(w) for w in words]
            return sum(counts) if (all(counts) if needed == "all" else any(counts)) else 0

        pool = [d for d in self._live() if type is None or d.type == type]
        scored = [(hits(d, "all"), d) for d in pool]
        if not any(s for s, _ in scored):  # Grimoire retries with OR when AND finds nothing
            scored = [(hits(d, "any"), d) for d in pool]
        found = sorted((p for p in scored if p[0]), key=lambda p: (-p[0], p[1].key))[:limit]
        rows = []
        for _, d in found:
            body = d.body
            at = min((body.lower().find(w) for w in words if w in body.lower()), default=0)
            window = body[max(0, at - 40) : at + 160]
            snippet = _escape_html(window)
            for w in words:
                snippet = re.sub(f"(?i)({re.escape(w)})", r"<mark>\1</mark>", snippet)
            rows.append(
                {
                    "key": d.key,
                    "title": d.title,
                    "folder": d.folder,
                    "tags": d.tags,
                    "status": d.status,
                    "summary": d.summary,
                    "snippet": snippet,
                }
            )
        return self.answer(rows, "search")

    def read(self, key: str) -> tuple[str, bool]:
        doc = self.docs.get(key)
        if doc is None:
            return f'No document found for key "{key}".', True
        return f"{format_frontmatter(doc)}\n\n{doc.body}", False

    def related(self, key: str, limit: int = 5) -> str:
        base = self.docs.get(key)
        rows = []
        if base is not None:
            scored = []
            for d in self._live():
                overlap = len(set(d.tags) & set(base.tags))
                if d.key != key and overlap:
                    scored.append((overlap, d))
            for overlap, d in sorted(scored, key=lambda p: (-p[0], p[1].key))[:limit]:
                rows.append(
                    {
                        "key": d.key,
                        "title": d.title,
                        "folder": d.folder,
                        "tags": d.tags,
                        "type": d.type,
                        "status": d.status,
                        "env": d.env,
                        "danger": d.danger,
                        "summary": d.summary,
                        "overlap": overlap,
                    }
                )
        return self.answer(rows, "related")

    def context(
        self,
        tags: list[str],
        mode: str = "all",
        preview: bool = False,
        keys: list[str] | None = None,
        max_tokens: int = 8000,
    ) -> str:
        found = []
        for d in self.docs.values():
            if d.status != "active":  # Grimoire's get_context: WHERE status = 'active'
                continue
            overlap = len(set(tags) & set(d.tags))
            if (mode == "all" and overlap == len(set(tags))) or (mode == "any" and overlap):
                if not keys or d.key in keys:
                    found.append((overlap, d))
        found.sort(key=lambda p: (-p[0], p[1].key))
        if preview:
            rows = [
                {
                    "key": d.key,
                    "title": d.title,
                    "folder": d.folder,
                    "tags": d.tags,
                    "type": d.type,
                    "env": d.env,
                    "danger": d.danger,
                    "summary": d.summary,
                    "overlap": overlap,
                }
                for overlap, d in found
            ]
            return self.answer(rows, "contextPreview")
        parts, budget = [], max_tokens
        for _, d in found:
            block = f"## {d.title}\nfolder: {d.folder} · tags: {', '.join(d.tags)}\n\n{d.body}"
            cost = -(-len(block) // 4)
            if budget - cost < 0:
                parts.append(f"--- {d.title} (truncated — call read_document('{d.key}') for full text) ---")
                break
            parts.append(block)
            budget -= cost
        return "\n\n".join(parts) or "No matching documents."

    def list_tags(self) -> str:
        tags: dict[str, set[str]] = {}
        for d in self._live():
            for t in d.tags:
                tags.setdefault(t, set()).add(d.folder)
        rows = [
            {"tag": t, "count": sum(t in d.tags for d in self._live()), "folders": sorted(f)}
            for t, f in sorted(tags.items())
        ]
        return self.answer(rows, "tags")

    def list_folders(self) -> str:
        counts: dict[str, int] = {}
        for d in self._live():
            counts[d.folder] = counts.get(d.folder, 0) + 1
        return self.answer([{"folder": f, "count": c} for f, c in sorted(counts.items())], "folders")

    def suggest_tags(self, query: str) -> str:
        tags = sorted({t for d in self._live() for t in d.tags})
        rows = [{"tag": t, "similarity": 0.5} for t in tags if query.lower()[:3] in t][:5]
        return self.answer(rows, "suggestions")


# ---- the stub server ------------------------------------------------------------------------


def _b64url_sha256(text: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).decode().rstrip("=")


@dataclass
class GrimoireStub:
    url: str
    mcp_url: str
    fmt: str
    delay_s: float
    token_ttl_s: int
    calls: list[str] = field(default_factory=list)
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)  # token -> {client_id, exp}
    refresh_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)  # token -> {client_id, used}
    issued: list[str] = field(default_factory=list)  # every token ever given out (for "never shown" tests)
    refuse_refresh: bool = False
    mcp_versions: list[str] | None = None  # None: every version; GATEWAY_MCP_VERSIONS: like Grimoire
    library: Library | None = None
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def issuer(self) -> str:
        return f"{self.url}/auth/v1"

    def expire_access_tokens(self) -> None:
        """Every access token given out so far stops working now (the client does not know)."""
        for info in self.access_tokens.values():
            info["exp"] = 0

    def revoke_refresh_tokens(self) -> None:
        """The token endpoint refuses every refresh from now on: the login has to be done again."""
        self.refuse_refresh = True

    def tool_calls(self) -> list[str]:
        return [c.removeprefix("tool ") for c in self.calls if c.startswith("tool ")]

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> GrimoireStub:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _metadata_routes(stub: GrimoireStub) -> list[Route]:
    async def protected_resource(request: Request) -> Response:
        return JSONResponse(
            {
                "resource": stub.mcp_url,
                "authorization_servers": [stub.issuer],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["email"],
                "resource_name": "Grimoire: MCP",
            }
        )

    async def authorization_server(request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": stub.issuer,
                "authorization_endpoint": f"{stub.issuer}/oauth/authorize",
                "token_endpoint": f"{stub.issuer}/oauth/token",
                "registration_endpoint": f"{stub.issuer}/oauth/clients/register",
                "jwks_uri": f"{stub.issuer}/.well-known/jwks.json",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_basic",
                    "client_secret_post",
                ],
                "scopes_supported": ["openid", "email", "profile", "phone"],
            }
        )

    return [
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/auth/v1", authorization_server, methods=["GET"]),
        Route("/.well-known/openid-configuration/auth/v1", authorization_server, methods=["GET"]),
        Route("/auth/v1/.well-known/oauth-authorization-server", authorization_server, methods=["GET"]),
    ]


def _oauth_routes(stub: GrimoireStub) -> list[Route]:
    def error(status: int, name: str, description: str) -> Response:
        return JSONResponse({"error": name, "error_description": description}, status_code=status)

    async def register(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return error(400, "invalid_client_metadata", "the body is not JSON")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            return error(400, "invalid_redirect_uri", "redirect_uris is required")
        client_id = "stub-client-" + secrets.token_hex(8)
        record = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": body.get("client_name"),
            "redirect_uris": uris,
            "grant_types": body.get("grant_types") or ["authorization_code", "refresh_token"],
            "response_types": body.get("response_types") or ["code"],
            "token_endpoint_auth_method": body.get("token_endpoint_auth_method") or "none",
        }
        stub.clients[client_id] = record
        return JSONResponse(record, status_code=201)

    async def authorize(request: Request) -> Response:
        q = request.query_params
        client = stub.clients.get(q.get("client_id", ""))
        if client is None:
            return error(400, "invalid_client", "unknown client")
        redirect_uri = q.get("redirect_uri", "")
        if redirect_uri not in client["redirect_uris"]:
            return error(400, "invalid_request", "redirect_uri is not registered")
        if (
            q.get("response_type") != "code"
            or q.get("code_challenge_method") != "S256"
            or not q.get("code_challenge")
        ):
            return error(400, "invalid_request", "PKCE S256 and response_type=code are required")
        code = "stub-code-" + secrets.token_urlsafe(16)
        stub.codes[code] = {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "challenge": q["code_challenge"],
            "exp": time.time() + 300,
        }
        return RedirectResponse(
            f"{redirect_uri}?{urlencode({'code': code, 'state': q.get('state', '')})}", status_code=302
        )

    def issue(client_id: str) -> Response:
        access = "stub-at-" + secrets.token_urlsafe(24)
        refresh = "stub-rt-" + secrets.token_urlsafe(24)
        stub.access_tokens[access] = {"client_id": client_id, "exp": time.time() + stub.token_ttl_s}
        stub.refresh_tokens[refresh] = {"client_id": client_id, "used": False}
        stub.issued += [access, refresh]
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "bearer",
                "expires_in": stub.token_ttl_s,
                "expires_at": int(time.time()) + stub.token_ttl_s,
                "refresh_token": refresh,
                "scope": "email",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def token(request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        client_id = str(form.get("client_id", ""))
        if client_id not in stub.clients:
            return error(401, "invalid_client", "unknown client")
        if grant == "authorization_code":
            info = stub.codes.pop(str(form.get("code", "")), None)
            if info is None or info["exp"] < time.time() or info["client_id"] != client_id:
                return error(400, "invalid_grant", "the code is unknown, used or expired")
            if form.get("redirect_uri") != info["redirect_uri"]:
                return error(400, "invalid_grant", "redirect_uri does not match")
            if _b64url_sha256(str(form.get("code_verifier", ""))) != info["challenge"]:
                return error(400, "invalid_grant", "PKCE verification failed")
            return issue(client_id)
        if grant == "refresh_token":
            info = stub.refresh_tokens.get(str(form.get("refresh_token", "")))
            if stub.refuse_refresh or info is None or info["used"] or info["client_id"] != client_id:
                return error(400, "invalid_grant", "Invalid Refresh Token: Refresh Token Not Found")
            info["used"] = True  # rotation, like Supabase Auth
            return issue(client_id)
        return error(400, "unsupported_grant_type", "only authorization_code and refresh_token")

    return [
        Route("/auth/v1/oauth/clients/register", register, methods=["POST"]),
        Route("/auth/v1/oauth/authorize", authorize, methods=["GET"]),
        Route("/auth/v1/oauth/token", token, methods=["POST"]),
    ]


def _mcp_server(stub: GrimoireStub) -> Server[Any]:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        stub.calls.append("tools/list")
        if stub.delay_s:
            await anyio.sleep(stub.delay_s)
        return types.ListToolsResult(tools=TOOLS)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        name, args = params.name, dict(params.arguments or {})
        stub.calls.append(f"tool {name}")
        if stub.delay_s:
            await anyio.sleep(stub.delay_s)
        lib = stub.library
        assert lib is not None
        is_error = False
        if name == "search_documents":
            text = lib.search(str(args["query"]), int(args.get("limit", 10)), args.get("type"))
        elif name == "read_document":
            text, is_error = lib.read(str(args["key"]))
        elif name == "get_related_documents":
            text = lib.related(str(args["key"]), int(args.get("limit", 5)))
        elif name == "get_context":
            text = lib.context(
                list(args["tags"]),
                args.get("mode", "all"),
                bool(args.get("preview", False)),
                args.get("keys"),
                int(args.get("max_tokens", 8000)),
            )
        elif name == "list_tags":
            text = lib.list_tags()
        elif name == "list_folders":
            text = lib.list_folders()
        elif name == "suggest_tags":
            text = lib.suggest_tags(str(args["query"]))
        elif name == "write_document":
            key = args.get("key") or f"{args.get('folder', '')}/{args['title']}".strip("/").lower().replace(
                " ", "-"
            )
            lib.docs[key] = Doc(
                key, args["title"], list(args.get("tags", [])), args["body"], type=args["type"]
            )
            text = (
                toon_encode_record({"key": key}) if lib.fmt == "toon" else json.dumps({"key": key}, indent=2)
            )
        elif name == "delete_document":
            if not args.get("confirm"):
                text, is_error = f'Refusing to delete "{args["key"]}" without confirm: true.', True
            elif lib.docs.pop(str(args["key"]), None) is None:
                text, is_error = f'No document found for key "{args["key"]}".', True
            else:
                text = f'Deleted "{args["key"]}".'
        else:
            text, is_error = f"Unknown tool: {name}", True
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)

    return Server("grimoire", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)


class _BearerGate:
    """What Grimoire's gateway does in front of /mcp: a token from the login server, or 401."""

    def __init__(self, app: Any, stub: GrimoireStub) -> None:
        self.app, self.stub = app, stub

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        headers = dict(scope.get("headers", []))
        sent = headers.get(b"authorization", b"").decode()
        metadata = f"{self.stub.url}/.well-known/oauth-protected-resource/mcp"
        if not sent.startswith("Bearer "):
            response: Response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
            )
            await response(scope, receive, send)
            return
        info = self.stub.access_tokens.get(sent.removeprefix("Bearer "))
        if info is None or info["exp"] < time.time():
            response = JSONResponse(
                {"error": "invalid_token", "error_description": "The token has expired or is not valid"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer error="invalid_token", error_description="expired", '
                    f'resource_metadata="{metadata}"'
                },
            )
            await response(scope, receive, send)
            return
        version = headers.get(b"mcp-protocol-version", b"").decode()
        if version and self.stub.mcp_versions is not None and version not in self.stub.mcp_versions:
            self.stub.calls.append(f"refused version {version}")
            supported = ", ".join(self.stub.mcp_versions)
            message = (
                f"Bad Request: Unsupported protocol version: {version[:40]} (supported versions: {supported})"
            )
            response = JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": message}, "id": None}, status_code=400
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _Recorder:
    def __init__(self, app: Any, stub: GrimoireStub) -> None:
        self.app, self.stub = app, stub

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.stub.calls.append(f"{scope['method']} {scope['path']}")
        await self.app(scope, receive, send)


def build_app(stub: GrimoireStub) -> Any:
    manager = StreamableHTTPSessionManager(app=_mcp_server(stub), stateless=True)
    mcp_endpoint = _BearerGate(StreamableHTTPASGIApp(manager), stub)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    routes = [
        *_metadata_routes(stub),
        *_oauth_routes(stub),
        Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
    return _Recorder(Starlette(routes=routes, lifespan=lifespan), stub)


def start_grimoire_stub(
    port: int = 0,
    fmt: str = "toon",
    delay_s: float = 0,
    token_ttl_s: int = 3600,
    docs: list[Doc] | None = None,
    mcp_versions: list[str] | None = None,
) -> GrimoireStub:
    """Start the stub on 127.0.0.1 in a background thread; returns when it answers."""
    if fmt not in ("toon", "json"):
        raise ValueError("fmt must be toon or json")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    real_port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{real_port}"
    stub = GrimoireStub(url=base, mcp_url=f"{base}/mcp", fmt=fmt, delay_s=delay_s, token_ttl_s=token_ttl_s)
    stub.mcp_versions = mcp_versions
    stub.library = Library(docs if docs is not None else default_docs(), fmt)
    config = uvicorn.Config(build_app(stub), log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True, name="grimoire-stub"
    )
    stub._server, stub._thread = server, thread
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            stub.close()
            raise RuntimeError("the Grimoire stub did not start")
        time.sleep(0.01)
    stub.calls.clear()
    return stub


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A stand-in for Grimoire: OAuth 2.1 + MCP over streamable HTTP."
    )
    parser.add_argument("--port", type=int, default=18090)
    parser.add_argument("--format", choices=["toon", "json"], default="toon")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds before each tool answer")
    parser.add_argument("--token-ttl", type=int, default=3600, help="lifetime of an access token in seconds")
    parser.add_argument(
        "--gateway-versions", action="store_true", help="refuse MCP versions Grimoire's gateway refuses"
    )
    args = parser.parse_args()
    stub = start_grimoire_stub(
        port=args.port,
        fmt=args.format,
        delay_s=args.delay,
        token_ttl_s=args.token_ttl,
        mcp_versions=GATEWAY_MCP_VERSIONS if args.gateway_versions else None,
    )
    print(f"grimoire-stub ready on {stub.url}", flush=True)
    try:
        while stub._thread is not None and stub._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stub.close()


if __name__ == "__main__":
    main()
