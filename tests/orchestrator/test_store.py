"""The history store: records, events, file modes, retention sweep and TTL expiry."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattson.agents.orchestrator.flow import iso
from wattson.agents.orchestrator.store import Store
from wattson.shared.contracts import TaskEvent, TaskRecord

from .conftest import FAKE_GITHUB_TOKEN


def record(
    task_id: str, status: str = "awaiting-input", at: datetime | None = None, **changes: object
) -> TaskRecord:
    when = iso(at or datetime.now(UTC))
    return TaskRecord(id=task_id, created_at=when, updated_at=when, status=status, **changes)  # type: ignore[arg-type]


def test_file_modes(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    store.save_task(record("t-1"))
    assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    for side in (Path(f"{store.path}-wal"), Path(f"{store.path}-shm")):
        if side.exists():
            assert stat.S_IMODE(os.stat(side).st_mode) == 0o600
    store.close()


def test_save_get_list_and_events(tmp_path: Path) -> None:
    store = Store(tmp_path)
    old = datetime.now(UTC) - timedelta(minutes=5)
    store.save_task(record("t-a", at=old, source_keys=["a/b"], edited=True, confidence=0.5))
    store.save_task(record("t-b"))
    got = store.get_task("t-a")
    assert got is not None and got.source_keys == ["a/b"] and got.edited is True and got.confidence == 0.5
    assert [r.id for r in store.list_tasks(10)] == ["t-b", "t-a"]
    assert [r.id for r in store.list_tasks(1)] == ["t-b"]
    assert store.get_task("nope") is None
    assert store.count_tasks() == 2

    # Upsert keeps created_at.
    store.save_task(got.model_copy(update={"status": "proposed", "created_at": iso(datetime.now(UTC))}))
    again = store.get_task("t-a")
    assert again is not None and again.status == "proposed" and again.created_at == got.created_at

    store.add_event(
        TaskEvent(task_id="t-a", at=iso(old), from_status=None, to_status="triage", detail="started")
    )
    store.add_event(TaskEvent(task_id="t-a", at=iso(old), from_status="triage", to_status="proposed"))
    assert [e.to_status for e in store.list_events("t-a")] == ["triage", "proposed"]
    store.close()


def test_free_text_is_redacted_and_short(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.save_task(record("t-s", error_summary=f"failed with {FAKE_GITHUB_TOKEN} " + "x" * 2000))
    store.add_event(
        TaskEvent(task_id="t-s", at=iso(datetime.now(UTC)), to_status="failed", detail=FAKE_GITHUB_TOKEN)
    )
    saved = store.get_task("t-s")
    assert saved is not None and saved.error_summary is not None
    assert FAKE_GITHUB_TOKEN not in saved.error_summary and len(saved.error_summary) <= 500
    assert FAKE_GITHUB_TOKEN not in (store.list_events("t-s")[0].detail or "")
    store.close()
    blob = b"".join(p.read_bytes() for p in store.dir.iterdir())
    assert FAKE_GITHUB_TOKEN.encode() not in blob


def test_expire_open(tmp_path: Path) -> None:
    store = Store(tmp_path)
    now = datetime.now(UTC)
    store.save_task(record("t-old-open", "awaiting-input", at=now - timedelta(minutes=200)))
    store.save_task(record("t-old-proposed", "proposed", at=now - timedelta(minutes=121)))
    store.save_task(record("t-new-open", "collecting-evidence", at=now - timedelta(minutes=10)))
    store.save_task(record("t-old-submitted", "submitted", at=now - timedelta(minutes=500)))
    store.save_task(record("t-old-done", "completed", at=now - timedelta(minutes=500)))

    expired = store.expire_open(120, now=now)

    assert sorted(expired) == ["t-old-open", "t-old-proposed"]
    statuses = {r.id: r.status for r in store.list_tasks(10)}
    assert statuses == {
        "t-old-open": "expired",
        "t-old-proposed": "expired",
        "t-new-open": "collecting-evidence",
        "t-old-submitted": "submitted",  # a run is not abandoned by the TTL; the CLI reports it later
        "t-old-done": "completed",
    }
    events = store.list_events("t-old-open")
    assert events[-1].from_status == "awaiting-input" and events[-1].to_status == "expired"
    assert store.expire_open(120, now=now) == []
    store.close()


def test_sweep(tmp_path: Path) -> None:
    store = Store(tmp_path)
    now = datetime.now(UTC)
    store.save_task(record("t-ancient", "completed", at=now - timedelta(days=91)))
    store.add_event(TaskEvent(task_id="t-ancient", at=iso(now - timedelta(days=91)), to_status="completed"))
    store.save_task(record("t-recent", "completed", at=now - timedelta(days=89)))

    assert store.sweep(90, now=now) == 1
    assert store.get_task("t-ancient") is None
    assert store.list_events("t-ancient") == []
    assert store.get_task("t-recent") is not None
    assert store.sweep(90, now=now) == 0
    store.close()


def test_transition(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.save_task(record("t-x", "submitted"))
    rec = store.transition("t-x", "completed", "late report", claude_session_id="abc")
    assert rec is not None and rec.status == "completed" and rec.claude_session_id == "abc"
    assert store.transition("t-missing", "completed") is None
    assert store.list_events("t-x")[-1].detail == "late report"
    store.close()
