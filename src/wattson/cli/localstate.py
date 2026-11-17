"""The laptop's own record of an approved task: <state dir>/tasks/<task_id>.json (folder 700,
file 600). It keeps the approved prompt and the Claude Code session until the result is reported
to the orchestrator (design 10.6); then the prompt is dropped. `wattson recover` reads it."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from wattson.cli.config import state_dir
from wattson.shared.contracts import RunResponse, SessionChoice

LocalState = Literal["approved", "submitted", "completed", "failed", "interrupted"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LocalTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    a2a_task_id: str | None = None
    context_id: str | None = None
    request: str
    prompt: str | None = None  # dropped once the result is reported
    prompt_sha256: str
    session: SessionChoice
    new_session_id: str | None = None  # a new conversation's id, chosen before the run (for recover)
    cwd: str
    approved_at: str | None = None
    submitted_at: str | None = None
    state: LocalState = "approved"
    result: RunResponse | None = None
    reported: bool = False  # the orchestrator has the result (or the "interrupted" report)
    updated_at: str | None = None

    @property
    def unfinished(self) -> bool:
        """Still needs something from the user: no result reported, or the run was interrupted."""
        return not self.reported or self.state in ("approved", "submitted", "interrupted")


def tasks_dir() -> Path:
    path = state_dir() / "tasks"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir(), 0o700)
    os.chmod(path, 0o700)
    return path


def task_path(task_id: str) -> Path:
    if not _SAFE_ID.match(task_id):
        raise ValueError(f"not a valid task id: {task_id!r}")
    return tasks_dir() / f"{task_id}.json"


def save(task: LocalTask) -> Path:
    """Write the record atomically, mode 600."""
    task.updated_at = now_iso()
    path = task_path(task.task_id)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(task.model_dump_json(indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def load(task_id: str) -> LocalTask | None:
    try:
        path = task_path(task_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    return LocalTask.model_validate_json(path.read_text(encoding="utf-8"))


def list_all() -> list[LocalTask]:
    out: list[LocalTask] = []
    for path in sorted(tasks_dir().glob("*.json")):
        try:
            out.append(LocalTask.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda t: t.updated_at or "", reverse=True)
    return out


def list_unfinished() -> list[LocalTask]:
    return [t for t in list_all() if t.unfinished]


def record_result(task: LocalTask, result: RunResponse) -> None:
    task.result = result
    task.state = result.state
    task.reported = False
    save(task)


def mark_reported(task: LocalTask) -> None:
    """The orchestrator has the result: the prompt is not needed any more."""
    task.reported = True
    task.prompt = None
    save(task)


def as_dict(task: LocalTask) -> dict[str, Any]:
    return json.loads(task.model_dump_json())
