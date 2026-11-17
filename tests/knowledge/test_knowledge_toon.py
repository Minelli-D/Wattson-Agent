"""The TOON decoder and the reading of Grimoire's answers.

fixtures/toon_samples.json holds answers made by Grimoire's own code: mcp/output.ts#formatAnswer with
@toon-format/toon 4.1.1, over the made-up records of grimoire/tests/fixtures/mcp-answers.mjs plus a
few odd cases (quotes, commas, a tag with a space, the top level, an empty list). "expected" is what
the TOON library's own decode() gives back; "json" is the same answer with MCP_OUTPUT_FORMAT=json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from grimoire_stub import SHAPES, Library, default_docs, shape_rows, toon_encode_rows

from wattson.agents.knowledge.answers import AnswerError, parse_document, parse_list, plain_snippet, records
from wattson.agents.knowledge.toon import ToonError, decode

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "toon_samples.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["name"] for s in SAMPLES])
def test_grimoire_samples_decode_exactly(sample: dict) -> None:
    assert decode(sample["toon"]) == sample["expected"]


@pytest.mark.parametrize("name", ["search_documents", "get_related_documents", "get_context (preview)"])
def test_toon_and_json_give_the_same_documents(name: str) -> None:
    sample = next(s for s in SAMPLES if s["name"] == name)
    toon_rows, toon_fmt = parse_list(sample["toon"])
    json_rows, json_fmt = parse_list(sample["json"])
    assert (toon_fmt, json_fmt) == ("toon", "json")
    from_toon, from_json = records(toon_rows, toon_fmt), records(json_rows, json_fmt)
    assert len(from_toon) == len(from_json) > 0
    for a, b in zip(from_toon, from_json, strict=True):
        assert (a.key, a.title, a.folder, a.tags, a.status, a.overlap) == (
            b.key,
            b.title,
            b.folder,
            b.tags,
            b.status,
            b.overlap,
        )
        assert a.snippet == b.snippet  # JSON snippets lose <mark> and the escapes, like TOON's
        assert a.tags and all(" " not in t for t in a.tags)


def test_odd_records() -> None:
    rows, fmt = parse_list(next(s["toon"] for s in SAMPLES if s["name"] == "search: odd titles"))
    found = {r.key: r for r in records(rows, fmt)}
    assert found["notes/2024"].title == "2024"  # a quoted number stays text
    assert found["notes/dash"].title == "- starts with a dash"
    assert found["notes/dash"].snippet == 'say "hi", then \\ back'
    assert found["notes/unicode"].title == "Café — naïve ✓ 日本"
    assert found["top-level"].folder is None and found["top-level"].tags == []
    assert found["notes/null"].title == "null"
    assert found["infra/k8s/crashloop-runbook"].folder == "infra/k8s"  # from the key

    rows, fmt = parse_list(
        next(s["toon"] for s in SAMPLES if s["name"] == "search: tag with a space (blocks)")
    )
    assert [r.tags for r in records(rows, fmt)] == [["two words", "k8s"], ["k8s"]]
    rows, fmt = parse_list(next(s["toon"] for s in SAMPLES if s["name"] == "search: folder not in key"))
    assert [r.folder for r in records(rows, fmt)] == ["elsewhere", "b"]
    assert parse_list("[]") == ([], "json")


def test_the_stub_writes_toon_like_grimoire() -> None:
    lib = Library(default_docs(), "toon")
    for text in (
        lib.search("k8s"),
        lib.related("infra/k8s/crashloop-runbook"),
        lib.list_tags(),
        lib.list_folders(),
    ):
        assert isinstance(decode(text), list)
    rows = [
        {
            "key": "a/b",
            "title": 'Quote " and, comma',
            "folder": "a",
            "tags": ["x", "two words"],
            "status": "active",
            "summary": None,
            "snippet": "line\nbreak",
        },
    ]
    shaped = shape_rows(rows, SHAPES["search"])
    assert decode(toon_encode_rows(shaped)) == shaped


@pytest.mark.parametrize(
    "text",
    [
        "[2]{a,b}:\n  1,2",  # fewer rows than announced
        "[1]{a,b}:\n  1,2\n  3,4",  # more rows
        "[1]{a,b}:\n  1",  # too few values
        "[1]{a,temp{min,max}}:\n  1,2,3",  # nested field groups
        '[1]{a}:\n  "open',  # a quote that never closes
        "[1]{a}:\n   1",  # odd indentation
        '[1]{a}:\n  "bad \\x escape"',
        "  [1]{a}:\n  1",  # the first line indented
    ],
)
def test_broken_toon_is_refused(text: str) -> None:
    with pytest.raises(ToonError):
        decode(text)


def test_plain_text_is_not_a_list() -> None:
    for text in ("No matching documents.", "key: value", '{"conflict": true}'):
        with pytest.raises(AnswerError):
            parse_list(text)


def test_values() -> None:
    assert decode('a: 1\nb: -2.5\nc: true\nd: null\ne: 1e3\nf: 007x\ng: "\\u00e9\\t"') == {
        "a": 1,
        "b": -2.5,
        "c": True,
        "d": None,
        "e": 1000.0,
        "f": "007x",
        "g": "é\t",
    }
    assert decode("outer:\n  inner: 1\n  list[2]: a,b\nempty[0]:") == {
        "outer": {"inner": 1, "list": ["a", "b"]},
        "empty": [],
    }
    assert decode("[3|]: a,b|c|d") == ["a,b", "c", "d"]


def test_plain_snippet() -> None:
    assert (
        plain_snippet("run <mark>kubectl</mark>\n\n &amp;lt; &lt;x&gt;  done ") == "run kubectl &lt; <x> done"
    )


def test_read_document_frontmatter() -> None:
    text = (
        '---\ntitle: "Say \\"hi\\": now"\ntype: runbook\nenv: production\ndanger: true\n'
        'tags: ["k8s", "two words"]\nstatus: draft\n---\n\n# Body\n\ntext'
    )
    front, body = parse_document(text)
    assert front.title == 'Say "hi": now'
    assert (front.type, front.env, front.danger, front.status) == ("runbook", "production", True, "draft")
    assert front.tags == ["k8s", "two words"]
    assert body == "# Body\n\ntext"
    assert parse_document("no frontmatter") == (parse_document("")[0], "no frontmatter")
