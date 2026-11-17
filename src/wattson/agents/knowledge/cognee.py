"""Seen before: the past lessons in Cognee memory that match a request (read-only, D47).

The knowledge agent asks cognee-mcp in the pod wattson-cognee (docs/cognee.md) with Strands'
MCPClient, over streamable HTTP, with no token (the network rule of the pod lets only this agent
and the orchestrator in). READ-ONLY: only the tools in ALLOWED_TOOLS are ever called, and
check_allowed() runs in SeenBefore.call, the only function here that calls a tool, before the
connection is even opened. The MCPClient gets the same list as tool_filters, but its call_tool
does not check them: this module does. No model picks a tool; one call, always the same:
recall with the search type CHUNKS (the pod has no LLM: only the plain search by meaning works)
in one dataset (COGNEE_RECALL_DATASET). Its start() and call_tool_sync() block their caller,
so the call runs in a worker thread and the agent's event loop never waits on them.

What is sent: the request's search queries (the ones Grimoire gets), redacted, at most
MAX_QUERY characters. Never the request itself: Cognee keeps each query in its search history.

Recall has no relevance cutoff (it always returns pieces), and its MCP answer has no score. So
each lesson is checked here by its words, with retrieval.words_of (stop words and generic words
out): it fits a query of 1 or 2 words when it holds all of them, else when it holds at least
half of them, and at least 2. Lessons keep Cognee's order (by meaning); the first `lessons`
that fit are kept, none when none fits.

A lesson is a block of the note format of the skill save-lessons: a heading, then the lines
Problem, Cause, Fix and Lesson (and Tags). A "# <date> <topic>" heading with no such lines gives
its date to the lessons under it. Each kept lesson becomes a RetrievedDoc (kind "cognee", why
"seen_before"), its text redacted and short: at most MAX_TEXT characters for all of them, the
titles at most MAX_TITLE (Granite's context is small). The orchestrator's Guard then checks them
like the Grimoire documents (its rules, then the injection model) before the prompt.

It never raises: slow (timeout_s), down or failing gives no lessons and one short log line with
a safe reason. Nothing is ever written to Cognee.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import uuid
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from strands.tools.mcp import MCPClient

from wattson.shared import telemetry
from wattson.shared.contracts import KnowledgeRequest, RetrievedDoc, SourceRef
from wattson.shared.redact import redact

from .grimoire import ToolRefused
from .retrieval import cut, queries_for, words_of
from .settings import Settings

log = logging.getLogger(__name__)
# Strands logs a failed connection or call with a traceback; Cognee down is a normal event here,
# reported by this module in its own short words.
logging.getLogger("strands.tools.mcp.mcp_client").setLevel(logging.CRITICAL)
# MCPClient.stop() after a failed start leaves one coroutine unawaited (Strands 1.62); harmless.
warnings.filterwarnings("ignore", r"coroutine 'MCPClient\.stop\.<locals>\._set_close_event'", RuntimeWarning)

# The one tool of cognee-mcp that only reads. remember, forget, improve, ... are never called.
ALLOWED_TOOLS: tuple[str, ...] = ("recall",)
SEARCH_TYPE = "CHUNKS"
TOP_K = 10  # pieces asked for: most of them may not fit
MAX_QUERY = 300
MAX_TEXT = 1200  # the texts of all lessons together; with the titles about 1500
MAX_TITLE = 100
MAX_TAGS = 8
COGNEE_SPAN = "wattson.cognee.recall"  # ok, pieces, lessons: never the query or a text

FIELDS = ("Problem", "Cause", "Fix", "Lesson")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_FIELD = re.compile(r"^\s*(?:[-*]\s+)?\**(problem|cause|fix|lesson|tags|context)\**\s*:\s*(.*)$", re.I)
_DATE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")
_TAG = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
_ITEM = re.compile(r"\n\n(?=\[[a-z_]+\] )")
_HTTP_STATUS = re.compile(r"\b([45]\d\d)\b")


def check_allowed(name: str) -> None:
    if name not in ALLOWED_TOOLS:
        raise ToolRefused(f"Cognee tool {name!r} is not in the read-only allow-list: it is never called")


class CogneeError(Exception):
    """Cognee answered, but with an error. The text is short and safe to log."""


@dataclass(frozen=True)
class Lesson:
    title: str
    fields: tuple[tuple[str, str], ...]  # (name, text): Problem, Cause, Fix, Lesson, as written
    tags: tuple[str, ...] = ()

    @property
    def words(self) -> frozenset[str]:
        return words_of(" ".join([self.title, *(text for _, text in self.fields), *self.tags]))


# ---- reading the answer ------------------------------------------------------------------------


def pieces(answer: str) -> list[str]:
    """The texts of recall's answer: a summary line ("3 memories found ..."), then one
    "[graph] <text>" per piece, a blank line apart. "no matching memories" has none."""
    _, _, body = answer.partition("\n")
    if not body.startswith("["):
        return []
    out = []
    for item in _ITEM.split(body):
        source, sep, text = item.partition("] ")
        if sep and source == "[graph" and text.strip():
            out.append(text.strip())
    return out


def _fields(lines: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """The Problem, Cause, Fix and Lesson lines of a block (a field may go on over more lines),
    and its tags. Context lines are skipped."""
    fields: list[list[str]] = []
    tags: list[str] = []
    current: list[str] | None = None
    for line in lines:
        found = _FIELD.match(line)
        if found is None:
            if current is not None and line.strip():
                current[1] = f"{current[1]} {line.strip()}".strip()
            continue
        name, text = found.group(1).capitalize(), found.group(2).strip()
        current = None
        if name == "Tags":
            tags += [t for t in re.split(r"[,\s]+", text.lower()) if _TAG.fullmatch(t)]
        elif name in FIELDS:
            current = [name, text]
            fields.append(current)
    return [(name, text) for name, text in fields if text], tags


def lessons_of(piece: str) -> list[Lesson]:
    """The lessons in one piece of a note. A piece cut out of the middle of a note may start with
    lines and no heading: its title is then the start of its first field."""
    blocks: list[tuple[str, list[str]]] = [("", [])]
    for line in piece.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            blocks.append((heading.group(1).strip(), []))
        else:
            blocks[-1][1].append(line)
    out: list[Lesson] = []
    date = ""
    for title, lines in blocks:
        fields, tags = _fields(lines)
        if not fields:  # the note's "# <date> <topic>" and its Context, or no lesson at all
            found = _DATE.search(title)
            date = found.group(0) if found else date
            continue
        title = title or fields[0][1]
        if date and not _DATE.search(title):
            title = f"{date} {title}"
        out.append(Lesson(title=title, fields=tuple(fields), tags=tuple(dict.fromkeys(tags))))
    return out


def fits(query: frozenset[str], words: frozenset[str]) -> bool:
    """All the words of a query of 1 or 2 words, else at least half of them and at least 2."""
    n = len(query)
    if n == 0:
        return False
    need = n if n <= 2 else max(2, (n + 1) // 2)
    return len(query & words) >= need


def pick(found: list[str], queries: list[str], most: int) -> list[Lesson]:
    """The first `most` lessons that fit one of the queries, in Cognee's order, each once."""
    query_words = [words_of(q) for q in queries]
    picked: list[Lesson] = []
    seen: set[str] = set()
    for piece in found:
        for lesson in lessons_of(piece):
            key = " ".join(lesson.title.lower().split())
            if key in seen or not any(fits(q, lesson.words) for q in query_words):
                continue
            seen.add(key)
            picked.append(lesson)
            if len(picked) >= most:
                return picked
    return picked


# ---- the lessons as documents ------------------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(redact(text).text.split())  # redact first: no half secret after a cut


def lesson_text(lesson: Lesson, budget: int) -> str:
    """The fields of a lesson, one line each, redacted, at most `budget` characters: when they
    are too long, the short lines stay whole and the long ones share what is left."""
    lines = [f"{name}: {_one_line(text)}" for name, text in lesson.fields]
    if len("\n".join(lines)) <= budget:
        return "\n".join(lines)
    share = budget // len(lines)
    short = [line for line in lines if len(line) + 1 <= share]
    each = (budget - sum(len(line) + 1 for line in short)) // (len(lines) - len(short))
    return "\n".join(line if line in short else cut(line, each - 1, "…") for line in lines)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-")


def as_docs(lessons: list[Lesson], dataset: str) -> list[RetrievedDoc]:
    """The lessons as documents for the KnowledgeResult: kind "cognee", why "seen_before"."""
    if not lessons:
        return []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    budget = MAX_TEXT // len(lessons)
    docs = []
    for n, lesson in enumerate(lessons, 1):
        title = cut(_one_line(lesson.title), MAX_TITLE, "…")
        ref = SourceRef(
            kind="cognee",
            key=f"cognee:{dataset}/{_slug(title) or n}",
            title=title,
            tags=list(lesson.tags[:MAX_TAGS]),
            why="seen_before",
            retrieved_at=now,
            freshness="live",
        )
        docs.append(RetrievedDoc(ref=ref, text=lesson_text(lesson, budget)))
    return docs


# ---- the call ------------------------------------------------------------------------------------


def _root(exc: BaseException) -> BaseException:
    """The first cause of an error, through "raise ... from" and exception groups."""
    for _ in range(10):
        if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        elif exc.__cause__ is not None:
            exc = exc.__cause__
        else:
            break
    return exc


def reason(exc: BaseException) -> str:
    """A short reason, safe to log: our own words and an error type, never a text from outside."""
    if isinstance(exc, CogneeError | ToolRefused):
        return str(exc)
    return f"Cognee cannot be used ({type(_root(exc)).__name__})"


class SeenBefore:
    """The lessons of Cognee memory for a request: one recall per request, read-only."""

    def __init__(self, settings: Settings) -> None:
        self.url = settings.cognee_url
        self.dataset = settings.cognee_dataset
        self.most = settings.cognee_lessons
        self.timeout_s = settings.cognee_timeout_s

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """One tool call in its own MCP session; the text of the answer. A tool outside the
        allow-list sends nothing at all. Blocks: run it in a worker thread."""
        check_allowed(name)
        client = MCPClient(
            url=self.url,
            startup_timeout=max(1, math.ceil(self.timeout_s)),
            tool_filters={"allowed": list(ALLOWED_TOOLS)},
            application_name="wattson-knowledge",
        )
        client.start()  # raises when Cognee cannot be reached
        try:
            result = client.call_tool_sync(
                f"cognee-{uuid.uuid4().hex[:12]}",
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=self.timeout_s),
            )
        finally:
            with contextlib.suppress(Exception):  # closing failed after the answer: the answer stands
                client.stop(None, None, None)
        text = "\n".join(block["text"] for block in result.get("content", []) if "text" in block)
        # cognee-mcp answers its own failures as a text that starts with "Error:".
        if result.get("status") == "error" or text.startswith("Error:"):
            status = _HTTP_STATUS.search(text)
            raise CogneeError(
                f"Cognee's {name} answered an error" + (f" (HTTP {status[1]})" if status else "")
            )
        return text

    async def find(self, request: KnowledgeRequest) -> list[RetrievedDoc]:
        """The lessons that fit the request, as documents. Never raises."""
        queries = queries_for(request)
        if not any(words_of(q) for q in queries):
            return []  # only generic words: no lesson could fit, so Cognee is not asked
        query = cut(redact("; ".join(queries)).text, MAX_QUERY, "")
        arguments = {"query": query, "search_type": SEARCH_TYPE, "datasets": self.dataset, "top_k": TOP_K}
        attributes = {"wattson.cognee.ok": False, "wattson.cognee.pieces": 0, "wattson.cognee.lessons": 0}
        with telemetry.span(COGNEE_SPAN, **attributes) as span:
            try:
                # The thread is left to end by itself after a timeout (its own limits are timeout_s).
                with anyio.fail_after(self.timeout_s):
                    answer = await anyio.to_thread.run_sync(
                        self.call, "recall", arguments, abandon_on_cancel=True
                    )
                found = pieces(answer)
                lessons = pick(found, queries, self.most)
                docs = as_docs(lessons, self.dataset)
            except TimeoutError:
                log.warning("cognee: no answer within %g s; no lessons", self.timeout_s)
                return []
            except Exception as exc:  # Cognee must never break a task, nor an answer we cannot read
                log.warning("cognee: %s; no lessons", reason(exc))
                return []
            counts = {"wattson.cognee.pieces": len(found), "wattson.cognee.lessons": len(lessons)}
            telemetry.set_attributes(span, {"wattson.cognee.ok": True, **counts})
        log.info("cognee: %d piece(s), %d lesson(s) fit", len(found), len(lessons))
        return docs
