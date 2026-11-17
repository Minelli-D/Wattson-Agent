"""list_procedures: the Wattson procedures, the documents of one Grimoire folder ("wattson").

Grimoire adds the parts of a document's folder to its tags, so the documents of folder "wattson"
carry the tag "wattson". One call finds them: get_context with those tags, mode "all" and
preview (key, title, folder, tags, summary; active documents only, no body). Only the documents
of the asked folder and its subfolders are kept ("wattson/k8s-procedures" too; not a note
elsewhere tagged "wattson"), and only active ones. Names must be unique across the subfolders,
because the model answers a name: a second document with the same name is left out, with a warning.
For each:
  key          its Grimoire key ("wattson/k8s-pod-crashloop.md")
  name         the last part of the key, without ".md" ("k8s-pod-crashloop")
  when_to_use  Grimoire's summary, else the first paragraph under "## When to use" (read_document,
               at most 4 at a time), redacted, on one line, at most 300 characters
  workflow     the first of its tags that is a workflow
  signals      its other tags, without its folder tags ("wattson", "k8s-procedures") and the
               workflow tags
sorted by name, at most MAX_PROCEDURES.

A complete list is kept 5 minutes (per folder). It never raises: not connected, slow or failing
gives an empty list and a warning.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import anyio

from wattson.shared.contracts import WORKFLOWS, ProcedureList, ProcedureListRequest, ProcedureOption
from wattson.shared.redact import redact

from .answers import AnswerError, Record, parse_list, records
from .grimoire import GrimoireClient, GrimoireError, Session, safe_text
from .retrieval import CACHE_TTL_S, DocCache, cut, read_document

log = logging.getLogger(__name__)

MAX_PROCEDURES = 30
MAX_WHEN_TO_USE = 300
READS_AT_ONCE = 4


def procedure_name(key: str) -> str:
    """The last part of a key, without ".md": "wattson/k8s-pod-crashloop.md" -> "k8s-pod-crashloop"."""
    name = key.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") and len(name) > 3 else name


def one_line(text: str, limit: int = MAX_WHEN_TO_USE) -> str:
    """Redacted, on one line, cut to `limit` characters (redact first: no half secret)."""
    return cut(" ".join(redact(text).text.split()), limit, "…")


def _folder_tags(record: Record) -> list[str]:
    folder = record.folder if record.folder is not None else record.key.rpartition("/")[0]
    return [part for part in folder.split("/") if part]


def option_of(record: Record, folder_tags: list[str], when_to_use: str) -> ProcedureOption:
    workflow = next((t for t in record.tags if t in WORKFLOWS), None)
    skip = {*folder_tags, *WORKFLOWS}
    signals = [t for t in dict.fromkeys(record.tags) if t not in skip]
    return ProcedureOption(
        key=record.key,
        name=procedure_name(record.key),
        when_to_use=one_line(when_to_use),
        workflow=workflow,  # type: ignore[arg-type]
        signals=signals,
    )


@dataclass
class _Listing:
    folder: str
    procedures: list[ProcedureOption] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = False  # the folder was listed and every document read: worth keeping

    def restart(self) -> None:
        self.procedures.clear()
        self.warnings.clear()
        self.complete = False

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


class ProcedureCatalog:
    """The procedures of a folder, kept CACHE_TTL_S. Documents read go into the retriever's cache,
    so the procedure picked next comes from memory."""

    def __init__(
        self,
        grimoire: GrimoireClient,
        cache: DocCache,
        budget_s: float = 30.0,
        ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.grimoire = grimoire
        self.cache = cache
        self.budget_s = budget_s
        self.ttl_s = ttl_s
        self.clock = clock
        self._lists: dict[str, tuple[float, ProcedureList]] = {}

    def clear(self) -> None:
        self._lists.clear()

    async def list(self, request: ProcedureListRequest) -> ProcedureList:
        folder = request.folder.strip().strip("/")
        if not folder:
            return ProcedureList(warnings=["Grimoire was not asked: the procedure folder is empty"])
        kept = self._lists.get(folder)
        if kept is not None and self.clock() - kept[0] < self.ttl_s:
            return kept[1].model_copy(deep=True)
        work = _Listing(folder)
        outer: list[str] = []
        try:
            with anyio.move_on_after(self.budget_s) as scope:
                await self.grimoire.run(lambda session: self._run(session, work))
            if scope.cancelled_caught:
                outer.append(f"Grimoire did not list the procedures within {self.budget_s:g} s")
        except GrimoireError as e:
            outer.append(e.reason)
        except Exception as e:  # never raise from the tool
            log.warning("list_procedures failed: %s", type(e).__name__)
            outer.append(f"Grimoire procedure list failed ({type(e).__name__})")
        if outer:  # a part of a list is no list: the model would pick from the wrong options
            return ProcedureList(warnings=work.warnings + [w for w in outer if w not in work.warnings])
        result = ProcedureList(procedures=list(work.procedures), warnings=list(work.warnings))
        if work.complete:
            self._lists[folder] = (self.clock(), result.model_copy(deep=True))
        return result

    async def _run(self, session: Session, work: _Listing) -> None:
        work.restart()
        folder_tags = [part for part in work.folder.split("/") if part]
        answer = await session.call("get_context", {"tags": folder_tags, "mode": "all", "preview": True})
        if answer.is_error:
            work.warn(f"Grimoire could not list folder {work.folder}: {safe_text(answer.text, 120)}")
            return
        try:
            rows, fmt = parse_list(answer.text)
        except AnswerError:
            work.warn(f"Grimoire's list of folder {work.folder} could not be read")
            return
        found: dict[str, Record] = {}
        for record in records(rows, fmt):
            folder = record.folder or ""
            in_folder = folder == work.folder or folder.startswith(work.folder + "/")
            if in_folder and record.status in (None, "active"):
                found.setdefault(record.key, record)
        by_name: dict[str, Record] = {}
        for record in sorted(found.values(), key=lambda r: r.key):
            name = procedure_name(record.key)
            if name in by_name:
                work.warn(
                    f"Grimoire has two Wattson procedures named {name} ({by_name[name].key} and "
                    f"{record.key}): only the first is offered"
                )
                continue
            by_name[name] = record
        picked = sorted(by_name.values(), key=lambda r: (procedure_name(r.key), r.key))
        if len(picked) > MAX_PROCEDURES:
            work.warn(
                f"Grimoire folder {work.folder} has {len(picked)} procedures: "
                f"only the first {MAX_PROCEDURES} are offered"
            )
            picked = picked[:MAX_PROCEDURES]

        texts: dict[str, str] = {r.key: r.summary.strip() for r in picked if r.summary and r.summary.strip()}
        failed = False
        limiter = anyio.CapacityLimiter(READS_AT_ONCE)

        async def when_to_use(key: str) -> None:
            nonlocal failed
            async with limiter:
                doc = self.cache.get(key)
                got = doc if doc is not None else await read_document(session, self.cache, key)
            if isinstance(got, str):
                failed = True
                work.warn(f"Grimoire could not read {key}: {got}")
            else:
                texts[key] = got.when_to_use

        async with anyio.create_task_group() as group:
            for record in picked:
                if record.key not in texts:
                    group.start_soon(when_to_use, record.key)

        work.procedures = [option_of(r, _folder_tags(r), texts.get(r.key, "")) for r in picked]
        if not picked:
            work.warn(f"Grimoire has no active procedure in folder {work.folder}")
        work.complete = not failed
