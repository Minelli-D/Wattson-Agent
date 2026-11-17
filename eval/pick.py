"""Measure the pick of one Wattson procedure (the triage agent's procedure call).

Use the PRODUCTION Granite model through KubeAI, never a local model:

    kubectl --context my-cluster -n kubeai port-forward svc/kubeai 18086:80
    uv run python eval/pick.py --url http://127.0.0.1:18086/openai/v1 --model granite-4-2-3b

It runs the SAME code as the triage agent on eval/procedures.jsonl: TriageEngine.triage (the
classification, then the pick among the procedures of that workflow, names in a JSON-schema enum,
checked by the tags of the request: procedures.decide), and procedures.ask_candidates (would the
orchestrator ask?). It prints one line per case, right/total, and right/total per workflow. The
time of the pick is taken inside the whole triage, with the classification and the hint call on the
same model at the same time (the production cost), so it is longer than a pick alone. The
procedures come from grimoire-procedures/wattson/**/*.md (the drafts of Grimoire's folder
"wattson": tags and the "When to use" paragraph), or from --options FILE (a JSON list of
ProcedureOption, or a ProcedureList as the knowledge agent answers it).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from wattson.agents.triage import procedures as proc
from wattson.agents.triage.engine import Trace, TriageEngine
from wattson.agents.triage.settings import Settings
from wattson.shared.contracts import (
    WATTSON_FOLDER,
    WORKFLOWS,
    ProcedureList,
    ProcedureOption,
    TriageRequest,
    WorkspaceInfo,
)

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "procedures.jsonl"
DEFAULT_DRAFTS = HERE.parent / "grimoire-procedures" / "wattson"


def option_from_draft(path: Path) -> ProcedureOption:
    """A ProcedureOption from a draft: front matter tags (the workflow and the signals) and the
    first paragraph under "## When to use"."""
    text = path.read_text(encoding="utf-8")
    tags: list[str] = []
    match = re.search(r"^tags:\s*(\[.*\])\s*$", text, re.M)
    if match:
        tags = [str(t) for t in json.loads(match.group(1))]
    when = ""
    section = re.search(r"^## When to use\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if section:
        when = " ".join(section.group(1).split())[: proc.MAX_WHEN]
    workflow = next((t for t in tags if t in WORKFLOWS), None)
    signals = [t for t in tags if t not in WORKFLOWS and t != WATTSON_FOLDER]
    name = path.stem
    return ProcedureOption(
        key=f"{WATTSON_FOLDER}/{name}.md", name=name, when_to_use=when, workflow=workflow, signals=signals
    )


def load_options(options_file: Path | None, drafts: Path) -> list[ProcedureOption]:
    if options_file is not None:
        data = json.loads(options_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return ProcedureList.model_validate(data).procedures
        return [ProcedureOption.model_validate(o) for o in data]
    # The folder and its subfolders (wattson/k8s-procedures); notes like README.md or SOURCES.md
    # (capital names) are not procedures.
    paths = sorted(q for q in drafts.rglob("*.md") if not q.name[0].isupper())
    if not paths:
        raise SystemExit(f"no procedure drafts in {drafts}; give --options FILE")
    return [option_from_draft(p) for p in paths]


def load_cases(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def decided_name(pick: Any) -> str:
    return pick.name if pick.key else proc.NONE


async def measure(
    url: str,
    model: str,
    cases: list[dict[str, str]],
    options: list[ProcedureOption],
    timeout: float = 60.0,
    api_key: str = "",
    out: Any = sys.stdout,
) -> dict[str, Any]:
    settings = Settings(
        llm_base_url=url, llm_model=model, model_name=model, timeout_s=timeout, llm_api_key=api_key
    )
    engine = TriageEngine(settings)
    options = proc.usable(options)
    names = {o.name for o in options}
    unknown = sorted({c["procedure"] for c in cases} - names - {proc.NONE})
    if unknown:
        print(f"warning: the cases want procedures that are not options: {', '.join(unknown)}", file=out)
    workflow_of = {o.name: o.workflow for o in options}
    rows: list[dict[str, Any]] = []
    try:
        for n, case in enumerate(cases, 1):
            request, want = case["request"], case["procedure"]
            trace = Trace()
            triage = TriageRequest(request=request, workspace=WorkspaceInfo(cwd_name="-"), procedures=options)
            result = await asyncio.wait_for(engine.triage(triage, trace), 3 * timeout)
            pick = result.procedure
            assert pick is not None  # options were given
            answer = trace.model_pick
            error = next((w for w in result.warnings if w.startswith("procedure model")), None)
            latency = trace.timings_ms.get("pick", 0)
            category = result.classification.category
            ask = proc.ask_candidates(pick, request, proc.for_workflow(options, category))
            got = decided_name(pick)
            row = {
                "n": n,
                "request": request,
                "want": want,
                "workflow": category,
                "offered": len(proc.for_workflow(options, category)),
                "model": answer,
                "error": error if answer is None else None,
                "decided": got,
                "method": pick.method,
                "agreed": pick.agreed,
                "ask": ask,
                "latency_ms": latency,
                "triage_ms": trace.timings_ms.get("total", 0),
                "model_right": answer == want,
                "decided_right": got == want,
            }
            rows.append(row)
            mark = "ok  " if row["model_right"] else "MISS"
            then = f"ask {'/'.join(ask)}/none" if ask else f"{got} by {pick.method}"
            said = answer if answer is not None else f"ERROR ({error})"
            print(
                f"{n:2d} {mark} want {want:24s} {category[:6]:6s} model {said:24s} -> {then:32s} "
                f"{latency:6d} ms  {request}",
                file=out,
            )
    finally:
        await engine.aclose()
    total = len(rows)
    latencies = [r["latency_ms"] for r in rows if r["model"] is not None]
    summary = {
        "model": model,
        "total": total,
        "model_right": sum(r["model_right"] for r in rows),
        "decided_right": sum(r["decided_right"] for r in rows),
        "asked": sum(1 for r in rows if r["ask"]),
        "asked_with_the_right_one": sum(1 for r in rows if r["ask"] and r["want"] in [*r["ask"], proc.NONE]),
        "errors": sum(1 for r in rows if r["error"]),
        "latency_ms_median": int(statistics.median(latencies)) if latencies else None,
        "triage_ms_median": int(statistics.median(r["triage_ms"] for r in rows)) if rows else None,
        "by_workflow": by_workflow(rows, workflow_of),
        "rows": rows,
    }
    print(
        f"\nmodel {model}: {summary['model_right']}/{total} right; after the signal words "
        f"{summary['decided_right']}/{total}; the user would be asked {summary['asked']} time(s) "
        f"({summary['asked_with_the_right_one']} with the right answer among the options); "
        f"{summary['errors']} error(s); median {summary['latency_ms_median']} ms for the pick, "
        f"{summary['triage_ms_median']} ms for the whole triage",
        file=out,
    )
    for workflow, (right, count) in summary["by_workflow"].items():
        print(f"  {workflow}: {right}/{count} right after the signal words", file=out)
    return summary


def by_workflow(rows: list[dict[str, Any]], workflow_of: dict[str, Any]) -> dict[str, list[int]]:
    """Wanted workflow (from the wanted procedure; "none" for no procedure) -> [right, total]."""
    out: dict[str, list[int]] = {}
    for r in rows:
        key = workflow_of.get(r["want"]) or proc.NONE
        right_total = out.setdefault(key, [0, 0])
        right_total[0] += int(r["decided_right"])
        right_total[1] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url", required=True, help="the OpenAI base URL, e.g. http://127.0.0.1:18086/openai/v1"
    )
    parser.add_argument("--model", required=True, help="the model name, e.g. granite-4-2-3b")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="JSONL of {request, procedure}")
    parser.add_argument(
        "--drafts", type=Path, default=DEFAULT_DRAFTS, help="folder of procedure drafts (*.md)"
    )
    parser.add_argument("--options", type=Path, help="JSON list of ProcedureOption (instead of the drafts)")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per call (default 60)")
    parser.add_argument("--out", type=Path, help="also write the result as JSON here")
    args = parser.parse_args(argv)
    if not args.url.startswith(("http://", "https://")):
        parser.error("--url must start with http:// or https://")
    options = load_options(args.options, args.drafts)
    cases = load_cases(args.cases)
    summary = asyncio.run(
        measure(
            args.url.rstrip("/"), args.model, cases, options, args.timeout, os.environ.get("LLM_API_KEY", "")
        )
    )
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
