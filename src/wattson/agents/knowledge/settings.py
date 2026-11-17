"""Settings of the knowledge agent, read from the environment (docs/design.md 10.5, 10.6, 10.7)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from wattson.shared.auth import read_token

DEFAULT_MCP_URL = "https://grimoire.example.com/mcp"
# "Seen before" (cognee.py): cognee-mcp of the pod wattson-cognee, and the dataset of the lessons.
DEFAULT_COGNEE_URL = "http://wattson-cognee:8000/mcp"
DEFAULT_COGNEE_DATASET = "claude_code_memory"
MAX_LESSONS = 3
MAX_COGNEE_TIMEOUT_S = 10.0  # with the 30 s of Grimoire, under the orchestrator's 45 s
_DATASET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")  # no comma: recall reads a list of names


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise SystemExit(f"{name} must be above 0")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    token: str = field(default="", repr=False)
    listen_host: str = "0.0.0.0"  # noqa: S104 - the pod's interface; a NetworkPolicy limits who comes in
    port: int = 9002
    mcp_url: str = DEFAULT_MCP_URL
    data_dir: Path = Path("./.data/knowledge")
    timeout_s: float = 15.0  # each Grimoire call (design 10.6)
    retrieval_budget_s: float = 30.0  # one whole find_procedures
    connect_wait_s: float = 300.0  # how long a login waits for the code from the laptop
    public_url: str | None = None  # the URL in the Agent Card, e.g. http://wattson-knowledge:9002/
    # "Seen before": past lessons from Cognee memory, read-only (cognee.py). Off unless asked.
    cognee_recall: bool = False
    cognee_url: str = DEFAULT_COGNEE_URL
    cognee_dataset: str = DEFAULT_COGNEE_DATASET
    cognee_lessons: int = MAX_LESSONS  # at most, 1 to 3
    cognee_timeout_s: float = 5.0  # the whole recall; then no lessons

    @classmethod
    def from_env(cls) -> Settings:
        mcp_url = os.environ.get("GRIMOIRE_MCP_URL", "").strip() or DEFAULT_MCP_URL
        if not mcp_url.startswith(("https://", "http://")):
            raise SystemExit("GRIMOIRE_MCP_URL must be an https:// URL (http:// only for a local stub)")
        recall = os.environ.get("COGNEE_RECALL", "").strip().lower() or "off"
        if recall not in ("on", "off"):
            raise SystemExit(f"COGNEE_RECALL must be on or off, got {recall!r}")
        cognee_url = os.environ.get("COGNEE_RECALL_URL", "").strip() or DEFAULT_COGNEE_URL
        if not cognee_url.startswith(("http://", "https://")):
            raise SystemExit("COGNEE_RECALL_URL must be an http:// URL (cognee-mcp in the cluster)")
        dataset = os.environ.get("COGNEE_RECALL_DATASET", "").strip() or DEFAULT_COGNEE_DATASET
        if not _DATASET.fullmatch(dataset):
            raise SystemExit("COGNEE_RECALL_DATASET must be one dataset name: letters, digits, _ and -")
        lessons = _int("COGNEE_RECALL_LESSONS", MAX_LESSONS)
        if not 1 <= lessons <= MAX_LESSONS:
            raise SystemExit(f"COGNEE_RECALL_LESSONS must be 1 to {MAX_LESSONS}")
        cognee_timeout_s = _float("COGNEE_RECALL_TIMEOUT_S", 5.0)
        if cognee_timeout_s > MAX_COGNEE_TIMEOUT_S:
            raise SystemExit(f"COGNEE_RECALL_TIMEOUT_S must be at most {MAX_COGNEE_TIMEOUT_S:g}")
        return cls(
            token=read_token("KNOWLEDGE_TOKEN", "KNOWLEDGE_TOKEN_FILE"),
            listen_host=os.environ.get("KNOWLEDGE_LISTEN_HOST", "0.0.0.0"),  # noqa: S104
            port=_int("KNOWLEDGE_PORT", 9002),
            mcp_url=mcp_url,
            data_dir=Path(os.environ.get("KNOWLEDGE_DATA_DIR", "").strip() or "./.data/knowledge"),
            timeout_s=_float("GRIMOIRE_TIMEOUT_S", 15.0),
            public_url=os.environ.get("KNOWLEDGE_PUBLIC_URL") or None,
            cognee_recall=recall == "on",
            cognee_url=cognee_url,
            cognee_dataset=dataset,
            cognee_lessons=lessons,
            cognee_timeout_s=cognee_timeout_s,
        )
