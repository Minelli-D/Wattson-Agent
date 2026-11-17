"""The rules of the task lifecycle, shared by the tools and the hooks.

The transition table says which step may run from which status (docs/design.md, 10.6). The
approval gate enforces it, so a planner bug or a replayed message cannot skip a step."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import ValidationError

# Statuses after which nothing runs any more. "blocked": the Guard stopped the request.
TERMINAL = frozenset({"rejected", "expired", "completed", "failed", "interrupted", "blocked"})

# Statuses that wait for the user. An open task older than the TTL expires.
OPEN = ("triage", "awaiting-input", "collecting-evidence", "proposed")

# tool -> the statuses it may start from. None: no task yet in this context.
ALLOWED_FROM: dict[str, frozenset[str | None]] = {
    "start_task": frozenset({None, *TERMINAL}),
    # The Guard runs once, right after start_task (status "triage"), before anything else.
    "guard": frozenset({"triage"}),
    "list_procedures": frozenset({"triage"}),
    "triage": frozenset({"triage"}),
    # After the questions: the procedure the user chose decides what is read.
    "find_procedures": frozenset({"triage", "awaiting-input"}),
    "clarify": frozenset({"triage", "awaiting-input"}),
    "plan_evidence": frozenset({"triage", "awaiting-input"}),
    "collect_evidence": frozenset({"triage", "awaiting-input", "collecting-evidence"}),
    "compose": frozenset({"triage", "awaiting-input", "collecting-evidence", "proposed"}),
    "request_approval": frozenset({"proposed"}),
    # "submitted": the same call again, resumed with the run result.
    "run_on_laptop": frozenset({"approved", "submitted"}),
    "finish": frozenset(
        {"triage", "awaiting-input", "collecting-evidence", "proposed", "approved", "submitted", *TERMINAL}
    ),
}

# Late results (POST /v1/tasks/{id}/execution) are accepted only after an approval.
REPORTABLE = frozenset({"approved", "submitted", "interrupted"})

# A second round of questions or answers is a new tool call; this caps the loop.
MAX_ROUNDS = 4

_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_WORKLOAD = re.compile(r"([a-z]{2,40}/)?[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def allowed(tool: str, status: str | None) -> bool:
    return status in ALLOWED_FROM.get(tool, frozenset())


def is_namespace(value: str | None) -> bool:
    """A Kubernetes namespace name (RFC 1123 label)."""
    return bool(value) and _LABEL.fullmatch(value or "") is not None


def is_workload(value: str | None) -> bool:
    """ "deployment/api" or "api"."""
    return bool(value) and _WORKLOAD.fullmatch(value or "") is not None


def is_sha256(value: str | None) -> bool:
    return bool(value) and _HEX64.fullmatch(value or "") is not None


def is_session_id(value: str | None) -> bool:
    return bool(value) and _SESSION_ID.fullmatch(value or "") is not None


def now_iso() -> str:
    """UTC, fixed width: the store compares these as strings."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def describe_errors(e: ValidationError) -> str:
    """Where the data is wrong, without echoing the data (it may hold a secret)."""
    parts = []
    for err in e.errors(include_input=False, include_url=False)[:5]:
        where = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{where}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)
