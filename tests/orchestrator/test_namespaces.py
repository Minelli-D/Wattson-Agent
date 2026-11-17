"""Several namespaces, or all of them: the contracts keep the old shape for one namespace, the
target is checked and capped, the answer to "Which namespace?" takes a list or *, and the store
keeps the namespaces in its old column."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wattson.agents.orchestrator import working as w
from wattson.agents.orchestrator.store import Store
from wattson.agents.orchestrator.tools import apply_answers, make_target, parse_namespaces
from wattson.shared.contracts import (
    MAX_NAMESPACES,
    DecisionHints,
    EvidenceRequest,
    Overrides,
    Question,
    StartTask,
    Target,
    TaskRecord,
    WorkspaceInfo,
)

WORKSPACE = WorkspaceInfo(cwd_name="shop", kube_contexts=["my-cluster"])


# ---- the contracts ------------------------------------------------------------------------------


def test_one_namespace_keeps_the_old_shape_on_the_wire() -> None:
    old = {"workflow": None, "cluster": "my-cluster", "namespace": "web", "workload": None, "procedure": None}
    assert Overrides(cluster="my-cluster", namespace="web").model_dump(mode="json") == old
    assert Overrides(cluster="my-cluster", namespaces=["web"]).model_dump(mode="json") == old
    # A message of before reads the same, with the list filled in.
    parsed = Overrides.model_validate(old)
    assert (parsed.namespace, parsed.namespaces, parsed.all_namespaces) == ("web", ["web"], False)
    target = Target(namespace="web").model_dump(mode="json")
    assert "namespaces" not in target and "all_namespaces" not in target


def test_no_namespace_keeps_the_old_shape() -> None:
    data = StartTask(request="x", workspace=WORKSPACE).model_dump(mode="json")["overrides"]
    assert set(data) == {"workflow", "cluster", "namespace", "workload", "procedure"}
    req = EvidenceRequest(id="e1", op="k8s.get", reason="r", namespace="web").model_dump(mode="json")
    assert "all_namespaces" not in req


def test_several_and_all_namespaces_on_the_wire() -> None:
    several = Overrides(namespaces=["web", "shop", "web"]).model_dump(mode="json")
    assert several["namespaces"] == ["web", "shop"] and several["namespace"] is None
    everything = Overrides(all_namespaces=True).model_dump(mode="json")
    assert everything["all_namespaces"] is True and "namespaces" not in everything
    assert Overrides.model_validate(several).namespaces == ["web", "shop"]
    assert EvidenceRequest(id="e1", op="k8s.events", reason="r", all_namespaces=True).model_dump(mode="json")[
        "all_namespaces"
    ]


def test_the_old_field_and_the_list_merge() -> None:
    t = Target(namespace="web", namespaces=["shop"])
    assert (t.namespace, t.namespaces) == (None, ["web", "shop"])


def test_all_and_a_list_do_not_go_together() -> None:
    with pytest.raises(ValidationError, match="do not go together"):
        Overrides(namespace="web", all_namespaces=True)


def test_namespace_words() -> None:
    assert Target(namespace="web").namespace_words() == "namespace web"
    assert Target(namespaces=["web", "shop"]).namespace_words() == "namespaces web, shop"
    assert Target(all_namespaces=True).namespace_words() == "all namespaces"
    assert Target().namespace_words() is None
    assert [
        Target(namespace="web").namespace_short(),
        Target(namespaces=["grimoire", "kubeai"]).namespace_short(),
        Target(all_namespaces=True).namespace_short(),
    ] == ["web", "{grimoire,kubeai}", "*"]


# ---- the target ---------------------------------------------------------------------------------


def target_of(overrides: Overrides, hints: DecisionHints | None = None) -> tuple[Target, list[str]]:
    task: dict = {"warnings": []}
    return make_target(task, WORKSPACE, overrides, hints or DecisionHints()), task["warnings"]


def test_target_from_one_several_and_all() -> None:
    assert target_of(Overrides(namespace="web"))[0].namespace == "web"
    assert target_of(Overrides(), DecisionHints(namespace="web"))[0].namespace == "web"
    several, _ = target_of(Overrides(namespaces=["web", "shop"]), DecisionHints(namespace="other"))
    assert several.namespaces == ["web", "shop"]  # yours win over the hint
    everything, _ = target_of(Overrides(all_namespaces=True), DecisionHints(namespace="web"))
    assert everything.all_namespaces and everything.namespaces == []


def test_target_drops_bad_names_and_caps_the_list() -> None:
    t, warnings = target_of(Overrides(namespaces=["web", "Bad_NS"]))
    assert t.namespace == "web" and any("dropped" in x for x in warnings)
    names = [f"ns-{i}" for i in range(MAX_NAMESPACES + 2)]
    t, warnings = target_of(Overrides(namespaces=names))
    assert t.namespaces == names[:MAX_NAMESPACES]
    assert any("--all-namespaces" in x for x in warnings)


# ---- the answer to "Which namespace?" -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "names", "everything"),
    [("web", ["web"], False), ("web, shop,web", ["web", "shop"], False), (" * ", [], True)],
)
def test_parse_namespaces(value: str, names: list[str], everything: bool) -> None:
    assert parse_namespaces(value) == (names, everything, None)


def test_parse_namespaces_problems() -> None:
    assert "'Bad_NS'" in (parse_namespaces("web, Bad_NS")[2] or "")
    many = ",".join(f"ns-{i}" for i in range(MAX_NAMESPACES + 1))
    assert "type *" in (parse_namespaces(many)[2] or "")


def test_answers_set_the_target() -> None:
    question = Question(id="namespace", field="namespace", text="Which namespace?")
    task: dict = {"workspace": WORKSPACE.model_dump(mode="json"), "target": {"cluster": "my-cluster"}}
    assert apply_answers(task, [question], {"namespace": "web"}) == []
    assert task["target"] == {"cluster": "my-cluster", "namespace": "web"}  # the shape of before
    task["answered_ids"] = []
    assert apply_answers(task, [question], {"namespace": "web, shop"}) == []
    assert Target.model_validate(task["target"]).namespaces == ["web", "shop"]
    assert apply_answers(task, [question], {"namespace": "*"}) == []
    assert Target.model_validate(task["target"]).all_namespaces
    assert apply_answers(task, [question], {"namespace": "NO!"})  # a problem; the target stays
    assert Target.model_validate(task["target"]).all_namespaces


# ---- the record and the store -------------------------------------------------------------------


def record(target: dict) -> TaskRecord:
    task = {"id": "t-1", "created_at": "2026-10-28T10:00:00+00:00", "status": "triage", "target": target}
    return w.record_of(task)


@pytest.mark.parametrize(
    ("target", "column"),
    [
        ({"cluster": "my-cluster", "namespace": "web"}, "web"),
        ({"cluster": "my-cluster", "namespaces": ["grimoire", "kubeai"]}, "grimoire,kubeai"),
        ({"cluster": "my-cluster", "all_namespaces": True}, "*"),
        ({"cluster": "my-cluster"}, None),
    ],
)
def test_store_keeps_the_namespaces_in_its_old_column(
    tmp_path: Path, target: dict, column: str | None
) -> None:
    store = Store(tmp_path)
    rec = record(target)
    store.save_task(rec)
    row = store._db.execute("SELECT namespace FROM tasks WHERE id = 't-1'").fetchone()
    assert row["namespace"] == column
    back = store.get_task("t-1")
    assert back is not None
    assert (back.namespace, back.namespaces, back.all_namespaces) == (
        rec.namespace,
        rec.namespaces,
        rec.all_namespaces,
    )
    store.close()
