"""Measure the triage decision model (docs/decision-model.md, docs/design.md 10.8).

    uv run python eval/run.py --llama http://127.0.0.1:18081 --model <name> \
        [--out eval/results/<name>.json] [--min-confidence 0.6] [--container wattson-eval-llama]

It runs the real TriageEngine (the code of the pod) against a llama.cpp server, usually the Docker
stand-in for the 2-CPU pod, on eval/requests.jsonl, and reports: accuracy, per-class P/R/F1,
confusion matrix, ECE (10 bins), accuracy and coverage per threshold, hint exact-match, latency
p50/p95, the keyword-rules baseline, and the container's peak memory (docker stats).
`--rules-only` measures the rules alone, without a server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from wattson.agents.triage import rules
from wattson.agents.triage.engine import Trace, TriageEngine, merge_hints
from wattson.agents.triage.rules import RulesHints
from wattson.agents.triage.settings import DEFAULT_MIN_CONFIDENCE, Settings
from wattson.shared.contracts import CATEGORIES, RepoInfo, TriageRequest, WorkspaceInfo

HERE = Path(__file__).resolve().parent
THRESHOLDS = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
HINT_FIELDS = ("cluster", "namespace", "workload")

# The fake workspace every request is triaged in (the labels assume these kube contexts).
WORKSPACE = WorkspaceInfo(
    repo=RepoInfo(name="shop-platform", root_display="~/src/shop-platform", branch="main", head="1a2b3c4"),
    cwd_name="shop-platform",
    kube_contexts=["my-cluster", "prod-eu", "kind-dev", "staging-us"],
    current_kube_context="kind-dev",
    reader_contexts=["my-cluster", "kind-dev"],
)


# ---- metrics ----------------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def class_report(labels: list[str], predicted: list[str]) -> dict[str, Any]:
    confusion = {t: {p: 0 for p in CATEGORIES} for t in CATEGORIES}
    for t, p in zip(labels, predicted, strict=True):
        confusion[t][p] += 1
    per_class: dict[str, dict[str, float]] = {}
    for c in CATEGORIES:
        tp = confusion[c][c]
        fp = sum(confusion[t][c] for t in CATEGORIES if t != c)
        fn = sum(confusion[c][p] for p in CATEGORIES if p != c)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[c] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "support": tp + fn,
        }
    correct = sum(t == p for t, p in zip(labels, predicted, strict=True))
    return {
        "accuracy": round(correct / len(labels), 4) if labels else None,
        "macro_f1": round(statistics.mean(v["f1"] for v in per_class.values()), 4),
        "per_class": per_class,
        "confusion": confusion,  # confusion[label][predicted]
    }


def ece(confidences: list[float], correct: list[bool], bins: int = 10) -> float:
    total = len(confidences)
    error = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confidences) if (low <= c < high) or (b == bins - 1 and c == 1.0)]
        if idx:
            acc = sum(correct[i] for i in idx) / len(idx)
            conf = sum(confidences[i] for i in idx) / len(idx)
            error += len(idx) / total * abs(acc - conf)
    return round(error, 4)


def reliability(confidences: list[float], correct: list[bool], bins: int = 10) -> list[dict[str, Any]]:
    out = []
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confidences) if (low <= c < high) or (b == bins - 1 and c == 1.0)]
        if idx:
            out.append(
                {
                    "bin": f"{low:.1f}-{high:.1f}",
                    "n": len(idx),
                    "accuracy": round(sum(correct[i] for i in idx) / len(idx), 3),
                    "confidence": round(sum(confidences[i] for i in idx) / len(idx), 3),
                }
            )
    return out


def threshold_table(confidences: list[float], correct: list[bool]) -> list[dict[str, Any]]:
    rows = []
    for t in THRESHOLDS:
        idx = [i for i, c in enumerate(confidences) if c >= t]
        rows.append(
            {
                "threshold": t,
                "coverage": round(len(idx) / len(confidences), 3) if confidences else None,
                "accuracy_above": round(sum(correct[i] for i in idx) / len(idx), 3) if idx else None,
                "n_above": len(idx),
            }
        )
    return rows


def hint_match(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Exact match per field, over all requests (null == null counts), plus recall on the labelled
    ones and precision of the non-null answers."""
    out: dict[str, Any] = {}
    for f in HINT_FIELDS:
        pairs = [(r["label"].get(f), r[key].get(f)) for r in rows if r.get(key) is not None]
        if not pairs:
            continue
        exact = sum(a == b for a, b in pairs)
        labelled = [(a, b) for a, b in pairs if a is not None]
        given = [(a, b) for a, b in pairs if b is not None]
        out[f] = {
            "exact": round(exact / len(pairs), 3),
            "recall": round(sum(a == b for a, b in labelled) / len(labelled), 3) if labelled else None,
            "precision": round(sum(a == b for a, b in given) / len(given), 3) if given else None,
            "labelled": len(labelled),
            "given": len(given),
        }
    return out


# ---- memory ---------------------------------------------------------------------------------------


_UNITS = {"b": 1 / 2**20, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1, "mb": 1, "gib": 1024, "gb": 1024}


def docker_mem_mib(container: str) -> float | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", out)
    return float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1) if m else None


def docker_info(container: str) -> dict[str, Any]:
    try:
        raw = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "inspect", container],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout
        data = json.loads(raw)[0]
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return {}
    host = data.get("HostConfig", {})
    return {
        "image": data.get("Config", {}).get("Image"),
        "args": data.get("Args"),
        "cpus": (host.get("NanoCpus") or 0) / 1e9,
        "memory_limit_mib": (host.get("Memory") or 0) / 2**20,
    }


class MemSampler:
    def __init__(self, container: str | None, every_s: float = 1.5) -> None:
        self.container, self.every_s = container, every_s
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            value = docker_mem_mib(self.container) if self.container else None
            if value is not None:
                self.samples.append(value)
            self._stop.wait(self.every_s)

    def __enter__(self) -> MemSampler:
        if self.container:
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self.container:
            self._thread.join(timeout=30)


# ---- the run ----------------------------------------------------------------------------------------


def load(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        assert r["category"] in CATEGORIES, r
    return rows


def rules_only(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        verdict = rules.classify(item["request"])
        req = TriageRequest(request=item["request"], workspace=WORKSPACE)
        hints = merge_hints(None, rules.extract_hints(item["request"], WORKSPACE), req)
        out.append(
            {
                "id": item["id"],
                "category": verdict.category,
                "strength": verdict.strength,
                "hints": {f: getattr(hints, f) for f in HINT_FIELDS},
            }
        )
    return out


async def model_run(
    items: list[dict[str, Any]], settings: Settings
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    engine = TriageEngine(settings)
    rows: list[dict[str, Any]] = []
    try:
        health = await engine.health()
        if not health.ok:
            raise SystemExit(f"llama.cpp is not ready: {health.detail}")
        # The first call fills the prompt cache; a pod pays it once after a restart. Reported apart.
        started = time.perf_counter()
        await engine.triage(TriageRequest(request="warm-up: list the pods", workspace=WORKSPACE))
        cold_ms = int((time.perf_counter() - started) * 1000)
        for n, item in enumerate(items, 1):
            trace = Trace()
            req = TriageRequest(request=item["request"], workspace=WORKSPACE)
            result = await engine.triage(req, trace)
            mv = trace.model_verdict
            model_hints = merge_hints(trace.model_hints, RulesHints(), req) if trace.model_hints else None
            rows.append(
                {
                    "id": item["id"],
                    "label": {"category": item["category"], **{f: item.get(f) for f in HINT_FIELDS}},
                    "model_category": mv.category if mv else None,
                    "model_confidence": round(mv.confidence, 4) if mv else None,
                    "model_scores": mv.scores if mv else None,
                    "final_category": result.classification.category,
                    "method": result.classification.method,
                    "final_hints": {f: getattr(result.hints, f) for f in HINT_FIELDS},
                    "model_hints": {f: getattr(model_hints, f) for f in HINT_FIELDS} if model_hints else None,
                    "recommended_next": result.hints.recommended_next,
                    "classify_ms": trace.timings_ms.get("classify"),
                    "hints_ms": trace.timings_ms.get("hints"),
                    "total_ms": trace.timings_ms.get("total"),
                    "warnings": result.warnings,
                }
            )
            mark = "ok " if mv and mv.category == item["category"] else "BAD"
            print(
                f"{n:3d}/{len(items)} {mark} {item['id']} {item['category'][:12]:12s} -> "
                f"{(mv.category if mv else '-')[:12]:12s} {mv.confidence if mv else 0:.2f} "
                f"{trace.timings_ms.get('total', 0) / 1000:.1f}s",
                file=sys.stderr,
            )
    finally:
        await engine.aclose()
    return rows, {"cold_first_triage_ms": cold_ms}


def summarize(
    rows: list[dict[str, Any]], rules_rows: list[dict[str, Any]], min_conf: float
) -> dict[str, Any]:
    labels = [r["label"]["category"] for r in rows]
    by_id = {r["id"]: r for r in rules_rows}
    report: dict[str, Any] = {}
    rules_pred = [by_id[r["id"]]["category"] for r in rows]
    report["rules_baseline"] = class_report(labels, rules_pred)
    report["rules_baseline"]["hints"] = hint_match(
        [{"label": r["label"], "h": by_id[r["id"]]["hints"]} for r in rows], "h"
    )
    have_model = [r for r in rows if r.get("model_category")]
    if have_model:
        conf = [r["model_confidence"] for r in have_model]
        correct = [r["model_category"] == r["label"]["category"] for r in have_model]
        report["model_argmax"] = class_report(
            [r["label"]["category"] for r in have_model], [r["model_category"] for r in have_model]
        )
        report["model_argmax"]["ece_10_bins"] = ece(conf, correct)
        report["model_argmax"]["mean_confidence"] = round(statistics.mean(conf), 4)
        report["model_argmax"]["reliability"] = reliability(conf, correct)
        report["model_argmax"]["thresholds"] = threshold_table(conf, correct)
        above = [c for c, ok in zip(conf, correct, strict=True) if c >= min_conf]
        report["model_argmax"]["at_min_confidence"] = {
            "min_confidence": min_conf,
            "coverage": round(len(above) / len(conf), 3),
            "accuracy_above": round(
                sum(ok for c, ok in zip(conf, correct, strict=True) if c >= min_conf) / len(above), 3
            )
            if above
            else None,
        }
        report["model_failures"] = len(rows) - len(have_model)
    if rows and "final_category" in rows[0]:
        report["pipeline"] = class_report(labels, [r["final_category"] for r in rows])
        report["pipeline"]["methods"] = {
            m: sum(r["method"] == m for r in rows) for m in ("model", "rules", "override")
        }
        report["pipeline"]["hints"] = hint_match(rows, "final_hints")
        report["model_hints_only"] = hint_match(rows, "model_hints")
        report["latency_ms"] = {
            "classify_p50": percentile([r["classify_ms"] for r in rows], 50),
            "classify_p95": percentile([r["classify_ms"] for r in rows], 95),
            "hints_p50": percentile([r["hints_ms"] for r in rows if r["hints_ms"] is not None], 50),
            "hints_p95": percentile([r["hints_ms"] for r in rows if r["hints_ms"] is not None], 95),
            "triage_p50": percentile([r["total_ms"] for r in rows], 50),
            "triage_p95": percentile([r["total_ms"] for r in rows], 95),
            "triage_max": max(r["total_ms"] for r in rows),
        }
        report["warnings"] = sum(bool(r["warnings"]) for r in rows)
    return report


def print_summary(name: str, report: dict[str, Any]) -> None:
    def pct(x: float | None) -> str:
        return "-" if x is None else f"{100 * x:.1f}%"

    print(f"\n== {name}")
    rb = report["rules_baseline"]
    print(f"rules baseline: accuracy {pct(rb['accuracy'])}, macro F1 {rb['macro_f1']:.3f}")
    if "model_argmax" in report:
        m = report["model_argmax"]
        acc, f1, err = pct(m["accuracy"]), m["macro_f1"], m["ece_10_bins"]
        print(f"model argmax:   accuracy {acc}, macro F1 {f1:.3f}, ECE {err:.3f}")
        for row in m["thresholds"]:
            cov, acc = pct(row["coverage"]), pct(row["accuracy_above"])
            print(f"  >= {row['threshold']:.1f}: coverage {cov}, accuracy {acc}")
    if "pipeline" in report:
        p = report["pipeline"]
        print(f"pipeline:       accuracy {pct(p['accuracy'])}, methods {p['methods']}")
        for f, v in p["hints"].items():
            exact, rec, prec = pct(v["exact"]), pct(v["recall"]), pct(v["precision"])
            print(f"  hint {f:9s} exact {exact}  recall {rec}  precision {prec}")
        lat = report["latency_ms"]
        print(
            f"latency ms: classify p50 {lat['classify_p50']} p95 {lat['classify_p95']}; "
            f"triage p50 {lat['triage_p50']} p95 {lat['triage_p95']} max {lat['triage_max']}"
        )
    if report.get("memory"):
        print(f"memory: {report['memory']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--llama", default="http://127.0.0.1:18081")
    parser.add_argument("--model", required=True, help="a name for the results, usually the GGUF file name")
    parser.add_argument("--requests", default=str(HERE / "requests.jsonl"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--confidence-temperature", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=50.0)
    parser.add_argument(
        "--container", default="wattson-eval-llama", help="docker container to watch ('' = none)"
    )
    parser.add_argument("--rules-only", action="store_true")
    args = parser.parse_args()

    items = load(Path(args.requests))
    rules_rows = rules_only(items)
    meta: dict[str, Any] = {
        "model": args.model,
        "requests": Path(args.requests).name,
        "n": len(items),
        "labels": {c: sum(i["category"] == c for i in items) for c in CATEGORIES},
        "min_confidence": args.min_confidence,
        "confidence_temperature": args.confidence_temperature,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if args.rules_only:
        rows = [
            {"id": i["id"], "label": {"category": i["category"], **{f: i.get(f) for f in HINT_FIELDS}}}
            for i in items
        ]
        report = summarize(rows, rules_rows, args.min_confidence)
    else:
        settings = Settings(
            token="x" * 40,
            llama_url=args.llama.rstrip("/"),
            model_name=args.model,
            timeout_s=args.timeout,
            min_confidence=args.min_confidence,
            confidence_temperature=args.confidence_temperature,
        )
        container = args.container or None
        meta["docker"] = docker_info(container) if container else {}
        idle = docker_mem_mib(container) if container else None
        with MemSampler(container) as sampler:
            rows, extra = asyncio.run(model_run(items, settings))
        meta.update(extra)
        report = summarize(rows, rules_rows, args.min_confidence)
        if container:
            report["memory"] = {
                "idle_after_load_mib": round(idle, 1) if idle else None,
                "peak_mib": round(max(sampler.samples), 1) if sampler.samples else None,
                "samples": len(sampler.samples),
            }
        report["rows"] = rows
    print_summary(args.model, report)
    out = Path(args.out) if args.out else HERE / "results" / f"{args.model}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"meta": meta, **report}, indent=1) + "\n", encoding="utf-8")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
