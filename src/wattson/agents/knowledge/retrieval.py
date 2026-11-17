"""find_procedures: the Grimoire documents relevant to a request (FR-GRIM-02..04, FR-APP-07, NFR-09).

With a chosen Wattson procedure (request.procedure_key): read_document of that procedure
(why="procedure", text at most 6000 characters), then of the runbooks its "## Related runbooks"
section lists, the first 3 ("- none yet" is no runbook; one that cannot be read is a warning), each
why="related". No search is done and no relevance check: the procedure names its runbooks. When
the procedure itself cannot be read, a warning says so and the free search below is done instead.

Without a procedure, the free search:
1. search_documents for each query (at most 3), merged by key, the best rank kept, drafts last:
   up to 8 candidates.
2. read_document for each candidate in that order (text redacted, at most 4000 characters), then
   the relevance check below: the first `limit` that pass are added (why="search").
3. get_related_documents of the first document added: up to 2 more that pass the softer check
   (why="related", FR-GRIM-03).
4. get_context with the request's tags, preview only: up to 2 more that pass the softer check and,
   when 2 or more tags were asked, share at least 2 of them (why="context").
When candidates came back but none passed the check, no document is added and a warning says so.

The relevance check is done here because Grimoire's search answer has no rank, and one common word
("model", "docs") is enough for it to return a document. Words: lower-cased, split on anything that
is not a letter or a digit, kept when 3+ characters long or holding a digit (k8s), with a light
plural strip (models -> model); stop words and generic words (write, docs, results, ...) are
dropped. A document's words come from its title, tags, folder, key and full text. It passes a
query of n words when it holds all of them (n <= 2), else at least max(2, ceil(0.7 n)); the softer
check asks for 1 (n <= 2), else ceil(n / 2). A query with no words left passes nothing. A document
is kept when it passes at least one query.

A document read in the last 5 minutes comes from memory (freshness "cached", with the time it was
read), else it is read now ("live"). Every text and excerpt goes through redact() first.

It never raises and never invents a document: what fails becomes a warning, and the documents
found before a failure are kept.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anyio

from wattson.shared.contracts import (
    WATTSON_FOLDER,
    KnowledgeRequest,
    KnowledgeResult,
    RetrievedDoc,
    SourceRef,
)
from wattson.shared.redact import MARKER_PREFIX, redact

from .answers import (
    AnswerError,
    Record,
    first_paragraph,
    listed_keys,
    parse_document,
    parse_list,
    records,
    section,
    strip_marks,
)
from .grimoire import GrimoireClient, GrimoireError, Session, safe_text

log = logging.getLogger(__name__)

CACHE_TTL_S = 300.0
MAX_QUERIES = 3
MAX_CANDIDATES = 8  # search results read and checked, at most
MAX_TEXT = 4000
MAX_PROCEDURE_TEXT = 6000
RUNBOOKS_MAX = 3  # runbooks of a procedure read, at most
MAX_EXCERPT = 600
RELATED_MAX = 2
CONTEXT_MAX = 2
CONTEXT_TAGS_MAX = 5
TRUNCATED = "\n[… truncated: the rest is in Grimoire]"

_WORD = re.compile(r"[A-Za-z0-9][\w./-]{2,}")
_STOP_WORDS = frozenset(
    "the and for with from that this why what how when where which into onto does doesn not are was "
    "were has have had can could should would please help fix find make get investigate check my our "
    "your its it's i'm about after all also any been but each than then there these they those too "
    "very via who will you just only some such out".split()
)
# Words of an engineering request that do not tell one document from another ("write the docs from
# the results files"). With the stop words they never count as a match, on either side.
_GENERIC_WORDS = frozenset(
    "write docs doc file files code repo change add update new make use run task please json yaml md "
    "desktop home three numbers results create edit want need thing way one two".split()
)
_TOKEN = re.compile(r"[^\W_]+")  # letters and digits: "-", "_", "/", "." and the rest split words
_MARKER = re.compile(re.escape(MARKER_PREFIX) + r"[^\]]*\]")  # [REDACTED:kind] is not a word


def _singular(word: str) -> str:
    """A light plural strip, the same for queries and documents: models -> model, policies -> policy."""
    if len(word) <= 3 or any(c.isdigit() for c in word):
        return word  # k8s, aws, ops stay
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


_IGNORED = frozenset(_singular(w) for w in _STOP_WORDS | _GENERIC_WORDS)


def words_of(text: str) -> frozenset[str]:
    """The words of a text that can tell documents apart (see the module docstring)."""
    out = set()
    for word in _TOKEN.findall(_MARKER.sub(" ", text).lower()):
        if len(word) < 3 and not any(c.isdigit() for c in word):
            continue
        word = _singular(word)
        if word not in _IGNORED:
            out.add(word)
    return frozenset(out)


def passes(query: frozenset[str], doc: frozenset[str], soft: bool = False) -> bool:
    """Whether a document's words hold enough of one query's words. Integers only: 0.7 * 10 > 7."""
    n = len(query)
    if n == 0:
        return False
    if soft:
        need = 1 if n <= 2 else (n + 1) // 2
    else:
        need = n if n <= 2 else max(2, (7 * n + 9) // 10)
    return len(query & doc) >= need


@dataclass(frozen=True)
class CachedDoc:
    title: str | None
    tags: list[str]
    text: str  # redacted, cut to MAX_PROCEDURE_TEXT (MAX_TEXT when it is not the procedure)
    body_start: str  # redacted start of the body, for an excerpt
    words: frozenset[str]  # words_of the title, tags and whole redacted body, for the check
    retrieved_at: str
    stored: float  # monotonic
    when_to_use: str = ""  # redacted first paragraph under "## When to use", on one line
    related: tuple[str, ...] = ()  # the keys listed under "## Related runbooks"


class DocCache:
    """read_document answers by key, for CACHE_TTL_S. Memory only: Grimoire stays the one store."""

    def __init__(self, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self._docs: dict[str, CachedDoc] = {}

    def get(self, key: str) -> CachedDoc | None:
        doc = self._docs.get(key)
        if doc is None:
            return None
        if self.clock() - doc.stored >= self.ttl_s:
            del self._docs[key]
            return None
        return doc

    def put(self, key: str, doc: CachedDoc) -> None:
        self._docs[key] = doc
        if len(self._docs) > 500:  # keep it small: drop the oldest
            oldest = min(self._docs, key=lambda k: self._docs[k].stored)
            del self._docs[oldest]

    def clear(self) -> None:
        self._docs.clear()


def cut(text: str, limit: int, marker: str = TRUNCATED) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(marker)].rstrip() + marker


def _in_wattson_folder(record: Record) -> bool:
    folder = record.folder if record.folder is not None else record.key.rpartition("/")[0]
    return folder == WATTSON_FOLDER or folder.startswith(WATTSON_FOLDER + "/")


def queries_for(request: KnowledgeRequest) -> list[str]:
    """The request's queries (at most 3, no repeats); keywords of the request when it has none."""
    out: list[str] = []
    for query in request.queries:
        query = " ".join(query.split())[:200]
        if query and query.lower() not in (q.lower() for q in out):
            out.append(query)
    if not out:
        words = [w for w in _WORD.findall(request.request) if w.lower() not in _STOP_WORDS]
        if words:
            out.append(" ".join(words[:4]))
    return out[:MAX_QUERIES]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def folder_of(key: str) -> str | None:
    """The folder of a Grimoire key: the part before the last "/" (None: the top level)."""
    return key.rsplit("/", 1)[0] if "/" in key else None


async def read_document(session: Session, cache: DocCache, key: str) -> CachedDoc | str:
    """read_document now, the answer redacted and put in the cache; the error text when it failed."""
    answer = await session.call("read_document", {"key": key})
    if answer.is_error:
        return safe_text(answer.text, 120)
    front, body = parse_document(answer.text)
    clean_body = redact(body).text  # redact first: no half secret, no secret in the words
    doc = CachedDoc(
        title=front.title,
        tags=front.tags,
        text=cut(redact(answer.text).text, MAX_PROCEDURE_TEXT),
        body_start=clean_body[: MAX_EXCERPT * 2],
        words=words_of(" ".join([front.title or "", *front.tags, clean_body])),
        retrieved_at=_now(),
        stored=cache.clock(),
        when_to_use=first_paragraph(section(clean_body, "When to use") or ""),
        related=tuple(listed_keys(section(clean_body, "Related runbooks") or "")),
    )
    cache.put(key, doc)
    return doc


def _overlap(record: Record, tags: list[str]) -> int:
    """How many of the asked tags the document has: Grimoire's count, else counted here."""
    if record.overlap is not None:
        return record.overlap
    return len(set(record.tags) & set(tags))


@dataclass
class _Work:
    request: KnowledgeRequest
    queries: list[str]
    query_words: list[frozenset[str]] = field(init=False)
    docs: list[RetrievedDoc] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    read_now: dict[str, CachedDoc] = field(default_factory=dict)  # read during this retrieval

    def __post_init__(self) -> None:
        self.query_words = [words_of(q) for q in self.queries]

    def restart(self) -> None:
        """A retry of the session: start the lists again (the answers already received are kept)."""
        self.docs.clear()
        self.warnings.clear()

    def has(self, key: str) -> bool:
        return any(d.ref.key == key for d in self.docs)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def relevant(self, record: Record, doc: CachedDoc, soft: bool = False) -> bool:
        """Whether the document passes at least one query (module docstring)."""
        names = " ".join([record.title, *record.tags, record.folder or "", record.key])
        words = doc.words | words_of(names)
        return any(passes(query, words, soft) for query in self.query_words)

    def add(self, record: Record, doc: CachedDoc, freshness: str, why: str) -> None:
        source = record.snippet or record.summary or doc.body_start
        if source:  # no <mark> tags; redact before cutting: no half secret
            source = " ".join(redact(strip_marks(source)).text.split())
        excerpt = cut(source, MAX_EXCERPT, "…") if source else None
        ref = SourceRef(
            key=record.key,
            title=record.title or doc.title or record.key,
            folder=record.folder,
            tags=record.tags or doc.tags,
            why=why,  # type: ignore[arg-type]
            retrieved_at=doc.retrieved_at,
            freshness=freshness,  # type: ignore[arg-type]
            excerpt=excerpt or None,
        )
        text = cut(doc.text, MAX_PROCEDURE_TEXT if why == "procedure" else MAX_TEXT)
        self.docs.append(RetrievedDoc(ref=ref, text=text))


class Retriever:
    def __init__(
        self, grimoire: GrimoireClient, budget_s: float = 30.0, cache: DocCache | None = None
    ) -> None:
        self.grimoire = grimoire
        self.budget_s = budget_s
        self.cache = cache or DocCache()

    async def find(self, request: KnowledgeRequest) -> KnowledgeResult:
        work = _Work(request, queries_for(request))
        outer: list[str] = []
        try:
            with anyio.move_on_after(self.budget_s) as scope:
                await self.grimoire.run(lambda session: self._run(session, work))
            if scope.cancelled_caught:
                outer.append(
                    f"Grimoire retrieval stopped after {self.budget_s:g} s: "
                    f"{len(work.docs)} document(s) found"
                )
        except GrimoireError as e:
            outer.append(e.reason)
        except Exception as e:  # never raise from the tool
            log.warning("find_procedures failed: %s", type(e).__name__)
            outer.append(f"Grimoire retrieval failed ({type(e).__name__})")
        warnings = work.warnings + [w for w in outer if w not in work.warnings]
        return KnowledgeResult(docs=list(work.docs), warnings=warnings)

    async def _run(self, session: Session, work: _Work) -> None:
        work.restart()
        key = (work.request.procedure_key or "").strip()
        if key and await self._procedure(session, work, key):
            return  # the procedure and its runbooks: no free search
        if not work.queries:
            work.warn("Grimoire was not searched: the request has no words to search for")
        candidates = await self._search(session, work)
        checked = await self._add_relevant(session, work, candidates, "search", work.request.limit)
        generic = not any(work.query_words)  # only generic words: nothing can pass
        if candidates and not work.docs and (checked or generic):
            work.warn(
                "Grimoire has no document that matches this request well (searched: "
                + "; ".join(work.queries)
                + "); none was added"
            )
            return  # and no related or context document either
        if work.docs:  # the first document that passed, not Grimoire's first answer
            await self._related(session, work, work.docs[0].ref.key)
        if work.request.tags and not generic:
            await self._context(session, work)

    async def _procedure(self, session: Session, work: _Work, key: str) -> bool:
        """The chosen procedure, then the runbooks it lists. False when the procedure cannot be read."""
        read = await self._read(session, work, key, failed="; a free search was done instead")
        if read is None:
            return False
        doc, freshness = read
        work.add(_record(key, doc), doc, freshness, "procedure")
        for runbook in [k for k in doc.related if k != key][:RUNBOOKS_MAX]:
            if work.has(runbook):
                continue
            found = await self._read(session, work, runbook)
            if found is not None:
                work.add(_record(runbook, found[0]), found[0], found[1], "related")
        return True

    async def _list(
        self, session: Session, work: _Work, tool: str, args: dict[str, object], what: str
    ) -> list[Record]:
        answer = await session.call(tool, args)
        if answer.is_error:
            work.warn(f"Grimoire {what} failed: {safe_text(answer.text, 120)}")
            return []
        try:
            rows, fmt = parse_list(answer.text)
        except AnswerError:
            work.warn(f"Grimoire {what}: the answer could not be read")
            return []
        return records(rows, fmt)

    async def _search(self, session: Session, work: _Work) -> list[Record]:
        """Up to MAX_CANDIDATES documents, in Grimoire's order, drafts last. None is checked yet."""
        best: dict[str, tuple[int, int, Record]] = {}
        for number, query in enumerate(work.queries):
            found = await self._list(
                session,
                work,
                "search_documents",
                {"query": query, "limit": MAX_CANDIDATES},
                f"search for {query!r}",
            )
            for rank, record in enumerate(found):
                if _in_wattson_folder(record):
                    continue  # procedures come only through the pick, never through free search
                seen = best.get(record.key)
                if seen is None or (rank, number) < (seen[0], seen[1]):
                    best[record.key] = (rank, number, record)
        if work.queries and not best:
            work.warn("Grimoire found no document for: " + "; ".join(work.queries))
        ordered = sorted(best.values(), key=lambda hit: (hit[2].status == "draft", hit[0], hit[1]))
        return [hit[2] for hit in ordered[:MAX_CANDIDATES]]

    async def _related(self, session: Session, work: _Work, key: str) -> None:
        found = await self._list(
            session,
            work,
            "get_related_documents",
            {"key": key, "limit": RELATED_MAX + 3},
            "related documents",
        )
        picked = [r for r in found if not work.has(r.key) and r.status != "draft"]
        await self._add_relevant(session, work, picked, "related", RELATED_MAX, soft=True)

    async def _context(self, session: Session, work: _Work) -> None:
        tags = [t for t in dict.fromkeys(t.strip() for t in work.request.tags) if t][:CONTEXT_TAGS_MAX]
        if not tags:
            return
        found = await self._list(
            session, work, "get_context", {"tags": tags, "mode": "any", "preview": True}, "context by tags"
        )
        found.sort(key=lambda r: -_overlap(r, tags))
        least = 2 if len(tags) >= 2 else 1  # with 2+ tags asked, one shared tag is not enough
        picked = [
            r for r in found if not work.has(r.key) and r.status != "draft" and _overlap(r, tags) >= least
        ]
        # the most shared tags first, and only a few read: a common tag can have many documents
        await self._add_relevant(session, work, picked[: CONTEXT_MAX + 3], "context", CONTEXT_MAX, soft=True)

    async def _add_relevant(
        self,
        session: Session,
        work: _Work,
        candidates: list[Record],
        why: str,
        most: int,
        soft: bool = False,
    ) -> int:
        """Read the candidates in order; add the first `most` that pass. Returns how many were checked."""
        added = checked = 0
        if not any(work.query_words):
            return 0  # only generic words: nothing can pass, so nothing is read
        for record in candidates:
            if added >= most:
                break
            if work.has(record.key):
                continue
            read = await self._read(session, work, record.key)
            if read is None:
                continue
            checked += 1
            doc, freshness = read
            if work.relevant(record, doc, soft):
                work.add(record, doc, freshness, why)
                added += 1
        return checked

    async def _read(
        self, session: Session, work: _Work, key: str, failed: str = ""
    ) -> tuple[CachedDoc, str] | None:
        """The document with its freshness: read in this retrieval, from the cache, or read now."""
        doc = work.read_now.get(key)
        if doc is not None:
            return doc, "live"
        doc = self.cache.get(key)
        if doc is not None:
            return doc, "cached"
        got = await read_document(session, self.cache, key)
        if isinstance(got, str):
            work.warn(f"Grimoire could not read {key}: {got.rstrip('. ') + failed if failed else got}")
            return None
        work.read_now[key] = got
        return got, "live"


def _record(key: str, doc: CachedDoc) -> Record:
    """A document read by its key, as a list answer would show it (its "When to use" as summary)."""
    return Record(
        key=key,
        title=doc.title or key,
        folder=folder_of(key),
        tags=list(doc.tags),
        summary=doc.when_to_use or None,
    )
