"""Command line: run the WE-xx scenarios and write the schema-1 summary."""

import argparse
import asyncio
import json
import math
import os
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .run import Failure
from .scenarios import SCENARIOS, Scenario

SUITE_SECONDS = 80 * 60  # the contract allows 90 minutes in all
SUITE_TURNS = 120  # target for the whole suite is under 150 agent turns


def stamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stats(values: list[float]) -> dict:
    ordered = sorted(values)

    def pick(fraction):
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]

    return {
        "n": len(ordered),
        "p50": round(pick(0.5), 1),
        "p95": round(pick(0.95), 1),
        "p99": round(pick(0.99), 1),
        "mean": round(sum(ordered) / len(ordered), 1),
        "max": round(ordered[-1], 1),
    }


def metrics_for(scenario: Scenario, run) -> list[dict]:
    labels = {"scenario": scenario.id, "agents": scenario.agents}
    turns = run.turns

    def entry(name, unit, values, extra=None):
        return {
            "id": scenario.id,
            "name": name,
            "unit": unit,
            "labels": labels,
            "stats": stats(values),
            "extra": extra or {},
        }

    out = [
        entry("wall_time", "s", [run.elapsed]),
        entry("agent_turns", "count", [len(turns)]),
        entry("model_calls", "count", [sum(t.steps for t in turns)]),
        entry("teamup_tool_calls", "count", [sum(len(t.calls) for t in turns)]),
    ]
    latencies = [
        min(t.sent_times()) - t.started
        for t in turns
        if t.kind == "wake" and t.sent_times()
    ]
    if latencies:
        out.append(entry("wake_to_reply_latency", "s", latencies))
    total = sum(t.tokens["total"] for t in turns)
    if total:
        out.append(
            entry(
                "llm_tokens",
                "tokens",
                [total],
                {
                    "input": sum(t.tokens["input"] for t in turns),
                    "output": sum(t.tokens["output"] for t in turns),
                },
            )
        )
    return out


async def run_one(scenario: Scenario, options: dict) -> dict:
    started = time.time()
    row = {"id": scenario.id, "title": scenario.title, "status": "pass", "detail": ""}
    if scenario.skip:
        row |= {"status": "skip", "detail": scenario.skip, "duration_s": 0.0}
        return row
    for old in Path(options["transcripts"]).glob(f"{scenario.id}-*.jsonl"):
        old.unlink()
    cap = scenario.max_turns
    options = options | {
        "max_turns": min(options["max_turns"], cap) if options["max_turns"] else cap,
        "max_seconds": options["max_seconds"] or scenario.max_seconds,
    }
    try:
        row["detail"] = await asyncio.wait_for(
            scenario.run(options), options["max_seconds"] + 120
        )
        if options["dry_run"]:
            row["detail"] += " (dry-run, scripted agents, no model calls)"
    except Failure as failure:
        row |= {"status": "fail", "detail": str(failure)}
    except Exception as error:  # noqa: BLE001 - report it as the scenario result
        row |= {"status": "error", "detail": f"{type(error).__name__}: {error}"}
    row["detail"] = row["detail"][:2000]
    row["duration_s"] = round(time.time() - started, 1)
    return row


async def suite(args) -> bool:
    results = Path(os.environ.get("TEAMUP_RESULTS_DIR", "results"))
    options = {
        "dry_run": args.dry_run,
        "max_turns": args.max_turns,
        "max_seconds": 180 if args.dry_run else 0,
        "model": os.environ.get("TEAMUP_AGENT_MODEL", "DeepSeek-V4.1-Flash-1017"),
        "base_url": os.environ.get("TEAMUP_LLM_BASE_URL", "http://127.0.0.1:18081/v1"),
        "turn_timeout": 240,
        "settle": 1 if args.dry_run else 8,
        "transcripts": str(results / "weekly/transcripts"),
    }
    chosen = [s for s in SCENARIOS if not args.only or s.id in args.only]
    started = time.time()
    rows, metrics = [], []

    def write() -> None:
        summary = {
            "schema": 1,
            "suite": "weekly",
            "sha": os.environ.get("TEAMUP_SHA"),
            "version": os.environ.get("TEAMUP_VERSION"),
            "started_at": stamp(started),
            "finished_at": stamp(time.time()),
            "host": os.environ.get("TEAMUP_HOST") or socket.gethostname(),
            "scenarios": rows,
            "metrics": metrics,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        partial = args.out.with_suffix(".tmp")
        partial.write_text(json.dumps(summary, indent=2) + "\n")
        partial.replace(args.out)

    try:
        await scenarios(chosen, options, args, rows, metrics, started)
    finally:
        write()
    return not any(row["status"] in ("fail", "error") for row in rows)


async def scenarios(chosen, options, args, rows, metrics, started) -> None:
    turns = 0
    for scenario in chosen:
        holder = options | {"collector": {}}
        if time.time() - started > SUITE_SECONDS or turns >= SUITE_TURNS:
            rows.append(
                {
                    "id": scenario.id,
                    "title": scenario.title,
                    "status": "error",
                    "duration_s": 0.0,
                    "detail": "suite time or turn budget exhausted before this scenario",
                }
            )
            continue
        row = await run_one(scenario, holder)
        rows.append(row)
        if run := holder["collector"].get(scenario.id):
            turns += len(run.turns)
            metrics += metrics_for(scenario, run)
        print(
            f"{row['id']} {row['status']:5} {row['duration_s']:7.1f}s  {row['detail'][:160]}",
            flush=True,
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.scenarios.weekly")
    parser.add_argument("--out", type=Path, default=Path("results/weekly/summary.json"))
    parser.add_argument(
        "--only", action="append", default=[], help="scenario ID, repeatable (WE-02)"
    )
    parser.add_argument(
        "--max-turns", type=int, default=0, help="override each scenario's turn cap"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scripted agents instead of opencode; makes no model calls",
    )
    args = parser.parse_args(argv)
    args.only = [
        item.upper() for entry in args.only for item in entry.split(",") if item
    ]
    known = {scenario.id for scenario in SCENARIOS}
    if unknown := set(args.only) - known:
        parser.error(f"unknown scenario {sorted(unknown)}; known: {sorted(known)}")
    return 0 if asyncio.run(suite(args)) else 1


if __name__ == "__main__":
    sys.exit(main())
