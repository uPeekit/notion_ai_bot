"""Benchmark Ollama models on the Russian case set.

uv run python -m tools.benchmark_llm --models qwen3:8b,qwen2.5:7b-instruct \
    [--limit N] [--write documentation/BENCHMARK.md]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.interpretation.models import Candidate, Interpretation, Value
from app.llm.base import LLMError
from app.llm.context import Context, ContextBuilder
from app.llm.ollama import OllamaClient
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

DEFAULT_CASES = Path("tests/fixtures/ru_cases.yaml")
INTENTS = {"create", "update", "append", "search", "unknown"}


@dataclass
class CaseResult:
    id: str
    valid: bool
    intent_ok: bool
    target_ok: bool
    item_ok: bool
    fields_ok: bool
    all_ok: bool
    ms: int
    error: str


@dataclass
class Summary:
    model: str
    n: int
    valid: float
    intent: float
    target: float
    fields: float
    all: float
    p50_ms: int
    p95_ms: int


def load_cases(path: Path) -> list[dict]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


def _key_by_name(ctx: Context, kind: str, name: str, target_key: str | None = None) -> str | None:
    for k, r in ctx.keys.items():
        if r.kind != kind or r.name != name:
            continue
        if target_key and not k.startswith(target_key + "."):
            continue
        return k
    return None


def resolve_value(ctx: Context, field_key: str, value: Any) -> Any:
    if isinstance(value, list):
        return [resolve_value(ctx, field_key, v) for v in value]
    if isinstance(value, str):
        ref = ctx.ref(value)
        if ref is not None and ref.kind == "option":
            return ref.name
    return value


def _match(expected: Any, actual: Any) -> bool:
    if expected == "*":
        return actual not in (None, "", [], {})
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int | float) and not isinstance(expected, bool):
        return isinstance(actual, int | float) and float(actual) == float(expected)
    if isinstance(expected, list):
        return isinstance(actual, list) and sorted(map(str, actual)) == sorted(map(str, expected))
    if isinstance(actual, dict) and "start" in actual:  # date
        return str(actual["start"]).startswith(str(expected))
    return str(actual).strip().casefold() == str(expected).strip().casefold()


def _best(interp: Interpretation) -> Candidate:
    return interp.best


def score_case(case: dict, interp: Interpretation, ctx: Context) -> CaseResult:
    errors: list[str] = []
    intent_ok = interp.intent.value == case["intent"]
    if not intent_ok:
        errors.append(f"intent {interp.intent.value}!={case['intent']}")
    best = _best(interp)
    best_name = ctx.ref(best.target).name if ctx.ref(best.target) else best.target

    if case["intent"] == "unknown" and "target" not in case and "targets_any" not in case:
        target_ok = True
    elif "targets_any" in case:
        target_ok = best_name in case["targets_any"]
    else:
        target_ok = best_name == case.get("target")
    if not target_ok:
        errors.append(f"target {best_name}")
    if "min_candidates" in case and len(interp.candidates) < case["min_candidates"]:
        target_ok = False
        errors.append(f"candidates {len(interp.candidates)}<{case['min_candidates']}")

    item_ok = True
    if "item" in case:
        item_name = ctx.ref(best.item).name if best.item and ctx.ref(best.item) else None
        item_ok = item_name == case["item"]
        if not item_ok:
            errors.append(f"item {item_name}")
    elif "item_candidates_min" in case:
        item_ok = best.item is None and len(best.item_candidates) >= case["item_candidates_min"]
        if not item_ok:
            errors.append(f"item_candidates {best.item_candidates} item={best.item}")

    fields_ok = True
    tk = best.target
    for fname, expected in case.get("fields", {}).items():
        fk = _key_by_name(ctx, "field", fname, tk)
        fv = best.fields.get(fk) if fk else None
        if not isinstance(fv, Value) or not _match(expected, resolve_value(ctx, fk, fv.value)):
            fields_ok = False
            got = (
                resolve_value(ctx, fk, fv.value)
                if isinstance(fv, Value)
                else getattr(fv, "status", None)
            )
            errors.append(f"{fname}: {got!r} != {expected!r}")
    for fname, statuses in case.get("statuses", {}).items():
        allowed = statuses if isinstance(statuses, list) else [statuses]
        fk = _key_by_name(ctx, "field", fname, tk)
        fv = best.fields.get(fk) if fk else None
        if fv is None or fv.status not in allowed:
            fields_ok = False
            errors.append(f"{fname}.status {getattr(fv, 'status', None)} not in {allowed}")
    for fname, limit in case.get("max_confidence", {}).items():
        fk = _key_by_name(ctx, "field", fname, tk)
        fv = best.fields.get(fk) if fk else None
        if isinstance(fv, Value) and fv.confidence > limit:
            fields_ok = False
            errors.append(f"{fname}.confidence {fv.confidence}>{limit}")
    if case.get("content") == "*" and not best.content:
        fields_ok = False
        errors.append("content empty")
    if case.get("search_query") == "*" and not best.search_query:
        fields_ok = False
        errors.append("search_query empty")

    all_ok = intent_ok and target_ok and item_ok and fields_ok
    return CaseResult(
        case["id"], True, intent_ok, target_ok, item_ok, fields_ok, all_ok, 0, "; ".join(errors)
    )


def summarize(model: str, results: list[CaseResult]) -> Summary:
    n = max(len(results), 1)
    times = sorted(r.ms for r in results) or [0]
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
    return Summary(
        model=model, n=len(results),
        valid=sum(r.valid for r in results) / n,
        intent=sum(r.intent_ok for r in results) / n,
        target=sum(r.target_ok for r in results) / n,
        fields=sum(r.fields_ok for r in results) / n,
        all=sum(r.all_ok for r in results) / n,
        p50_ms=int(statistics.median(times)), p95_ms=int(p95),
    )


async def run_model(client: OllamaClient, cases: list[dict], ctx: Context, schema: dict,
                    limit: int | None = None) -> list[CaseResult]:
    out: list[CaseResult] = []
    for case in cases[:limit]:
        try:
            interp, trace = await client.interpret(case["text"], ctx, schema)
        except LLMError as e:
            out.append(
                CaseResult(case["id"], False, False, False, False, False, False, 0, str(e)[:200])
            )
            print(f"  {case['id']:<24} INVALID {str(e)[:80]}", flush=True)
            continue
        r = score_case(case, interp, ctx)
        r.ms = trace.duration_ms
        out.append(r)
        mark = "ok " if r.all_ok else "FAIL"
        print(f"  {case['id']:<24} {mark} {r.ms:>6} ms  {r.error}", flush=True)
    return out


def render_table(summaries: list[Summary]) -> str:
    head = (
        "| model | n | valid | intent | target | fields | all | p50 ms | p95 ms |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| {s.model} | {s.n} | {s.valid:.0%} | {s.intent:.0%} | {s.target:.0%} | {s.fields:.0%} | "
        f"{s.all:.0%} | {s.p50_ms} | {s.p95_ms} |"
        for s in summaries
    ]
    return "\n".join([head, *rows])


async def main_async(a: argparse.Namespace) -> int:
    cases = load_cases(a.cases)
    ctx = ContextBuilder("Europe/Tallinn").build(sample_snapshot(), now=SAMPLE_NOW)
    schema = build_schema(ctx)
    summaries: list[Summary] = []
    failures: dict[str, list[CaseResult]] = {}
    for model in a.models.split(","):
        model = model.strip()
        print(f"\n== {model} ==", flush=True)
        async with OllamaClient(a.ollama, model, num_ctx=a.num_ctx, timeout_s=a.timeout) as client:
            results = await run_model(client, cases, ctx, schema, a.limit)
        summaries.append(summarize(model, results))
        failures[model] = [r for r in results if not r.all_ok]
    table = render_table(summaries)
    print("\n" + table)
    if a.write:
        lines = [f"# LLM benchmark — {datetime.now(UTC).date().isoformat()}", "",
                 f"Cases: `{a.cases}` ({len(cases)}), context: `tools/sample_workspace.py`, "
                 f"num_ctx={a.num_ctx}, temperature=0.", "", table, ""]
        for model, fails in failures.items():
            lines.append(f"## {model} failures ({len(fails)})")
            lines.extend(f"- `{r.id}`: {r.error}" for r in fails)
            lines.append("")
        a.write.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {a.write}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="comma-separated Ollama model names")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--write", type=Path)
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
