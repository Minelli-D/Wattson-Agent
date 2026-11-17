"""Reading Grimoire's answers: list answers in JSON or TOON, the markdown of read_document, and the
sections of a body (a Wattson procedure's "When to use" and "Related runbooks").

The shapes are those of /home/dm/Desktop/grimoire/mcp/server.ts and mcp/output.ts:
- search_documents: key, title, folder, tags, status, summary, snippet
- get_related_documents: key, title, folder, tags, type, status, env, danger, summary, overlap
- get_context with preview: key, title, folder, tags, type, env, danger, summary, overlap
In TOON the records are cleaned first: tags may be one text with the words separated by spaces,
folder may be missing (then it is the part of the key before the last "/"), fields without a value
in every record are left out, and snippets are plain text. In JSON the snippet still holds the
<mark> tags and the escaped &, < and > of the web page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .toon import ToonError
from .toon import decode as toon_decode


class AnswerError(ValueError):
    """Grimoire's answer is not a list this agent can read."""


@dataclass(frozen=True)
class Record:
    """One document of a list answer."""

    key: str
    title: str
    folder: str | None  # None: the top level
    tags: list[str] = field(default_factory=list)
    status: str | None = None
    snippet: str | None = None
    summary: str | None = None
    overlap: int | None = None


@dataclass(frozen=True)
class Frontmatter:
    title: str | None = None
    type: str | None = None
    env: str | None = None
    danger: bool | None = None
    tags: list[str] = field(default_factory=list)
    status: str | None = None


def parse_list(text: str) -> tuple[list[dict[str, Any]], str]:
    """The records of a list answer and the format it came in ("json" or "toon")."""
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            data = json.loads(stripped)
        except ValueError:
            pass
        else:
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)], "json"
            raise AnswerError("the JSON answer is not a list")
    try:
        data = toon_decode(stripped)
    except ToonError as exc:
        raise AnswerError(f"the answer is neither JSON nor TOON ({exc})") from None
    if not isinstance(data, list):
        raise AnswerError("the TOON answer is not a list")
    return [row for row in data if isinstance(row, dict)], "toon"


def records(rows: list[dict[str, Any]], fmt: str) -> list[Record]:
    """The rows as documents; rows without a key are skipped."""
    out = []
    for row in rows:
        key = row.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        title = row.get("title")
        snippet = row.get("snippet")
        if isinstance(snippet, str) and fmt == "json":
            snippet = plain_snippet(snippet)
        overlap = row.get("overlap")
        out.append(
            Record(
                key=key,
                title=str(title) if title not in (None, "") else key,
                folder=_folder(row, key),
                tags=_tags(row.get("tags")),
                status=row.get("status") if isinstance(row.get("status"), str) else None,
                snippet=snippet if isinstance(snippet, str) and snippet else None,
                summary=row.get("summary")
                if isinstance(row.get("summary"), str) and row.get("summary")
                else None,
                overlap=overlap if isinstance(overlap, int) and not isinstance(overlap, bool) else None,
            )
        )
    return out


def _folder(row: dict[str, Any], key: str) -> str | None:
    folder = row.get("folder")
    if not isinstance(folder, str):
        folder = key.rsplit("/", 1)[0] if "/" in key else ""
    return folder or None


def _tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(t) for t in value if str(t)]
    if isinstance(value, str):
        return [t for t in value.split(" ") if t]
    return []


_MARK = re.compile(r"</?mark>")
_ENTITY = re.compile(r"&(amp|lt|gt);")
_SPACES = re.compile(r"[\s\u001c-\u001f]+")


def strip_marks(text: str) -> str:
    """The text without the <mark> tags that Grimoire's search puts around the matched words."""
    return _MARK.sub("", text)


def plain_snippet(snippet: str) -> str:
    """What Grimoire's TOON answer does to a snippet (mcp/output.ts#plainSnippet)."""
    text = strip_marks(snippet)
    text = _ENTITY.sub(lambda m: {"amp": "&", "lt": "<", "gt": ">"}[m.group(1)], text)
    return _SPACES.sub(" ", text).strip()


_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def parse_document(text: str) -> tuple[Frontmatter, str]:
    """read_document's answer: the frontmatter (lib/markdown.ts#formatFrontmatter) and the body."""
    if not text.startswith("---\n"):
        return Frontmatter(), text.strip()
    end = text.find("\n---", 4)
    if end < 0:
        return Frontmatter(), text.strip()
    after = text.find("\n", end + 4)
    body = text[after + 1 :] if after >= 0 else ""
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        name, sep, value = line.partition(":")
        if sep:
            values[name.strip()] = value.strip()
    title = values.get("title")
    if title and title.startswith('"') and title.endswith('"') and len(title) >= 2:
        title = title[1:-1].replace('\\"', '"')
    danger = values.get("danger")
    return (
        Frontmatter(
            title=title or None,
            type=values.get("type") or None,
            env=values.get("env") or None,
            danger=None if danger is None else danger == "true",
            tags=[m.replace('\\"', '"') for m in _QUOTED.findall(values.get("tags", ""))],
            status=values.get("status") or None,
        ),
        body.strip(),
    )


# ---- the sections of a document body (Wattson procedures: "## When to use", "## Related runbooks")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_KEY = re.compile(r"[\w.-]+(?:/[\w.-]+)*")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def section(body: str, title: str) -> str | None:
    """The text under the heading `title` (any level, case ignored) up to the next heading of the
    same or a higher level; None when there is no such heading. Lines in code fences are text."""
    out: list[str] | None = None
    level = 0
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is not None:
            depth, name = len(heading.group(1)), heading.group(2).strip()
            if out is not None and depth <= level:
                break
            if out is None and name.lower() == title.lower():
                out, level = [], depth
                continue
        if out is not None:
            out.append(line)
    return "\n".join(out).strip() if out is not None else None


def first_paragraph(text: str) -> str:
    """The first paragraph of a text that is not a heading, on one line."""
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.strip().splitlines()]
        if lines and not _HEADING.match(lines[0]):
            return " ".join(" ".join(lines).split())
    return ""


def listed_keys(text: str) -> list[str]:
    """The Grimoire keys of a list ("- infra/k8s/x.md", "- [title](infra/k8s/x.md)"), in order,
    without repeats; "- none yet" and items that are not a key (a key has a "/" or ends in ".md")
    are skipped."""
    keys: list[str] = []
    for line in text.splitlines():
        item = line.strip()
        if not item.startswith(("- ", "* ", "+ ")):
            continue
        item = item[2:].strip()
        link = _LINK.match(item)
        key = link.group(1) if link else (item.split() or [""])[0].strip("`'\"<>,;")
        if item.lower().startswith("none") or not _KEY.fullmatch(key) or key in keys:
            continue
        if "/" not in key and not key.lower().endswith(".md"):
            continue  # a word of a sentence, not a key
        keys.append(key)
    return keys
