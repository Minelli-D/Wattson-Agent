"""The operational history: task records and their events in SQLite (MEM-01..03, NFR-05).

Only metadata is kept: ids, statuses, hashes, Grimoire keys, short redacted details. Never a
prompt, a document, evidence, or Claude's answer (SEC-14, design 10.5). The file is mode 600 in
a folder of mode 700; deleted rows are overwritten (secure_delete) so the retention is real."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wattson.agents.orchestrator.flow import OPEN, iso, now_iso
from wattson.shared.contracts import TaskEvent, TaskRecord
from wattson.shared.redact import redact

DB_NAME = "orchestrator.sqlite3"
MAX_DETAIL = 500

# The column "namespace" holds the namespaces of a record: "web", "web,shop", or "*" for all of
# them. So the table of a store made before several namespaces needs no new column.
_NAMESPACE_EXTRA = ("namespaces", "all_namespaces")
_FIELDS = tuple(f for f in TaskRecord.model_fields if f not in _NAMESPACE_EXTRA)
ALL_NAMESPACES = "*"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    workflow TEXT,
    category TEXT,
    confidence REAL,
    method TEXT,
    repo TEXT,
    cluster TEXT,
    namespace TEXT,
    source_keys TEXT NOT NULL DEFAULT '[]',
    proposal_sha256 TEXT,
    approved_sha256 TEXT,
    edited INTEGER,
    session_mode TEXT,
    claude_session_id TEXT,
    error_summary TEXT
);
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks (updated_at);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks (status, updated_at);
CREATE TABLE IF NOT EXISTS task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events (task_id, seq);
"""


def short(text: str | None, limit: int = MAX_DETAIL) -> str | None:
    """Redacted and cut: the only form in which free text reaches the history."""
    if text is None:
        return None
    clean = redact(str(text)).text.strip()
    return clean[:limit] if clean else None


class Store:
    """One SQLite file. Thread-safe: Strands runs sync tools in threads."""

    def __init__(self, data_dir: Path | str) -> None:
        # A folder of our own, so mode 700 holds even when data_dir is a mounted volume.
        self.dir = Path(data_dir) / "history"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / DB_NAME
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA secure_delete=ON")
            self._db.executescript(_SCHEMA)
        self._tighten()

    def _tighten(self) -> None:
        # The WAL and shared-memory files are made with the umask; keep them 600 too.
        for suffix in ("-wal", "-shm"):
            side = Path(f"{self.path}{suffix}")
            if side.exists():
                os.chmod(side, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- tasks ---------------------------------------------------------------------------

    def save_task(self, record: TaskRecord) -> None:
        """Insert or replace a record. Free text is redacted and cut here too."""
        row = _to_row(record)
        names = ", ".join(_FIELDS)
        marks = ", ".join(f":{f}" for f in _FIELDS)
        updates = ", ".join(f"{f} = excluded.{f}" for f in _FIELDS if f not in ("id", "created_at"))
        with self._lock:
            self._db.execute(
                f"INSERT INTO tasks ({names}) VALUES ({marks}) ON CONFLICT (id) DO UPDATE SET {updates}",  # noqa: S608
                row,
            )
        self._tighten()

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _from_row(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_from_row(r) for r in rows]

    def count_tasks(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    # ---- events --------------------------------------------------------------------------

    def add_event(self, event: TaskEvent) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO task_events (task_id, at, from_status, to_status, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (event.task_id, event.at, event.from_status, event.to_status, short(event.detail)),
            )

    def list_events(self, task_id: str) -> list[TaskEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT task_id, at, from_status, to_status, detail FROM task_events"
                " WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [TaskEvent(**dict(r)) for r in rows]

    # ---- changes made outside an agent (expiry, late results) -----------------------------

    def transition(
        self, task_id: str, to_status: str, detail: str | None = None, **changes: Any
    ) -> TaskRecord | None:
        """Move a record to a new status, with an event. None when the task is unknown."""
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        current = _from_row(row)
        at = now_iso()
        record = current.model_copy(update={**changes, "status": to_status, "updated_at": at})
        record = TaskRecord.model_validate(record.model_dump())  # validate the changes
        self.save_task(record)
        self.add_event(
            TaskEvent(
                task_id=task_id, at=at, from_status=current.status, to_status=record.status, detail=detail
            )
        )
        return record

    def expire_open(self, ttl_minutes: int, now: datetime | None = None) -> list[str]:
        """Tasks that waited longer than the TTL become "expired". Returns their ids."""
        cutoff = iso((now or datetime.now(UTC)) - timedelta(minutes=ttl_minutes))
        marks = ", ".join("?" for _ in OPEN)
        with self._lock:
            rows = self._db.execute(
                f"SELECT id FROM tasks WHERE status IN ({marks}) AND updated_at < ?",  # noqa: S608
                (*OPEN, cutoff),
            ).fetchall()
        ids = [r["id"] for r in rows]
        for task_id in ids:
            self.transition(
                task_id, "expired", f"no answer for {ttl_minutes} minutes; the working state was dropped"
            )
        return ids

    def sweep(self, days: int, now: datetime | None = None) -> int:
        """Delete tasks (and their events) not touched for `days`. Returns how many."""
        cutoff = iso((now or datetime.now(UTC)) - timedelta(days=days))
        with self._lock:
            ids = [r["id"] for r in self._db.execute("SELECT id FROM tasks WHERE updated_at < ?", (cutoff,))]
            self._db.execute("BEGIN")
            try:
                self._db.executemany("DELETE FROM task_events WHERE task_id = ?", ((i,) for i in ids))
                self._db.executemany("DELETE FROM tasks WHERE id = ?", ((i,) for i in ids))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            # Move the deleted pages out of the WAL too.
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(ids)


def _to_row(record: TaskRecord) -> dict[str, Any]:
    row = {k: v for k, v in record.model_dump().items() if k not in _NAMESPACE_EXTRA}
    row["namespace"] = ALL_NAMESPACES if record.all_namespaces else ",".join(record.namespaces) or None
    row["source_keys"] = json.dumps(list(record.source_keys))
    row["edited"] = None if record.edited is None else int(record.edited)
    row["error_summary"] = short(record.error_summary)
    return row


def _from_row(row: sqlite3.Row) -> TaskRecord:
    data = dict(row)
    stored = data.pop("namespace", None) or ""
    if stored == ALL_NAMESPACES:
        data["all_namespaces"] = True
    else:
        data["namespaces"] = [n for n in stored.split(",") if n]
    data["source_keys"] = json.loads(data.get("source_keys") or "[]")
    if data.get("edited") is not None:
        data["edited"] = bool(data["edited"])
    return TaskRecord(**data)
