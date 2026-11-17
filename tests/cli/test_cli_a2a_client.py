"""Parsing the A2A answers into interrupts and outcomes."""

from __future__ import annotations

import json

import pytest

from wattson.cli.a2a_client import ProtocolError, parse_task
from wattson.shared.contracts import CLARIFY, ClarifyReason


def _task(state, status_parts=None, artifacts=None):
    return {
        "kind": "task",
        "id": "a2a-1",
        "contextId": "c-1",
        "status": {"state": state, "message": {"parts": status_parts or []}},
        "artifacts": artifacts or [],
    }


OUTCOME = {"task_id": "task-1", "status": "completed", "summary": "done"}


def test_input_required_is_validated():
    reason = {"task_id": "task-1", "questions": [{"id": "q1", "field": "detail", "text": "What?"}]}
    view = parse_task(
        _task(
            "input-required",
            [
                {"kind": "text", "text": "Agent requires input"},
                {
                    "kind": "data",
                    "data": {"interrupts": [{"interruptId": "i1", "name": CLARIFY, "reason": reason}]},
                },
            ],
        )
    )
    assert view.interrupts[0].id == "i1" and isinstance(view.interrupts[0].reason, ClarifyReason)


def test_unknown_interrupt_or_bad_reason():
    with pytest.raises(ProtocolError, match="does not know"):
        parse_task(
            _task(
                "input-required",
                [
                    {
                        "kind": "data",
                        "data": {"interrupts": [{"interruptId": "i1", "name": "x.y", "reason": {}}]},
                    }
                ],
            )
        )
    with pytest.raises(ProtocolError, match="contract"):
        parse_task(
            _task(
                "input-required",
                [
                    {
                        "kind": "data",
                        "data": {"interrupts": [{"interruptId": "i1", "name": CLARIFY, "reason": {"x": 1}}]},
                    }
                ],
            )
        )


def test_completed_text_artifact_in_chunks():
    text = json.dumps(OUTCOME)
    view = parse_task(
        _task(
            "completed",
            artifacts=[
                {
                    "parts": [
                        {"kind": "text", "text": text[:10]},
                        {"kind": "text", "text": text[10:]},
                        {"kind": "text", "text": ""},
                    ]
                }
            ],
        )
    )
    assert view.outcome is not None and view.outcome.summary == "done"


def test_completed_data_artifact_and_error():
    view = parse_task(_task("completed", artifacts=[{"parts": [{"kind": "data", "data": OUTCOME}]}]))
    assert view.outcome is not None
    view = parse_task(
        _task("completed", artifacts=[{"parts": [{"kind": "text", "text": '{"error": "boom"}'}]}])
    )
    assert view.outcome is None and view.error == "boom"


def test_failed():
    view = parse_task(_task("failed", [{"kind": "text", "text": "Agent execution failed"}]))
    assert view.error == "Agent execution failed"
