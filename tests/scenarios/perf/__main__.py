import argparse
import asyncio
import time
import traceback
from pathlib import Path

from tests.scenarios import _results, _stack
from tests.scenarios.perf.benchmarks import BENCHMARKS, BenchmarkFailure


def benchmark_registry(profile):
    from tests.scenarios.perf.console_pressure import CONSOLE_BENCHMARKS
    from tests.scenarios.perf.focused_rooms import FOCUSED_BENCHMARKS
    from tests.scenarios.perf.pressure import PRESSURE_BENCHMARKS
    from tests.scenarios.perf.recovery import ARCHIVE_BENCHMARKS
    from tests.scenarios.perf.room_sync import ROOM_SYNC_BENCHMARKS
    from tests.scenarios.perf.task_pressure import TASK_BENCHMARKS

    scaled = (
        PRESSURE_BENCHMARKS
        | ARCHIVE_BENCHMARKS
        | TASK_BENCHMARKS
        | CONSOLE_BENCHMARKS
        | FOCUSED_BENCHMARKS
    )
    if profile == "weekly":
        return dict(sorted((scaled | ROOM_SYNC_BENCHMARKS).items()))
    return BENCHMARKS | {
        key: value for key, value in sorted(scaled.items()) if key != "NP-10"
    }


async def run_one(runner, quick, profile):
    ws = _stack.Workspace(prefix="tu-perf-")
    try:
        return await runner(ws, quick, profile)
    except BaseException:
        print(ws.logs(1500))
        raise
    finally:
        ws.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tests.scenarios.perf")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--suite", choices=("nightly", "weekly"), default="nightly")
    parser.add_argument("--quick", action="store_true", help="use small smoke fixtures")
    parser.add_argument("--only", help="comma-separated benchmark IDs")
    args = parser.parse_args(argv)
    out = args.out or _results.results_dir() / args.suite / "perf.json"
    benchmarks = benchmark_registry(args.suite)
    wanted = {x.strip() for x in args.only.split(",")} if args.only else set(benchmarks)
    unknown = sorted(wanted - benchmarks.keys())
    if unknown:
        parser.error(f"unknown benchmark(s) for {args.suite}: {', '.join(unknown)}")

    started_at = _results.utc_now()
    selected = {key: pair for key, pair in benchmarks.items() if key in wanted}
    metrics = []
    scenarios = {
        key: _results.scenario_entry(key, title, "error", 0, "benchmark did not finish")
        for key, (title, _) in selected.items()
    }

    def checkpoint():
        doc = _results.document(
            args.suite, started_at, list(scenarios.values()), metrics
        )
        _results.write(out, doc)

    checkpoint()
    for id_, (title, runner) in selected.items():
        started = time.monotonic()
        try:
            metrics += asyncio.run(run_one(runner, args.quick, args.suite))
            status, detail = "pass", ""
        except BenchmarkFailure as failure:
            metrics.extend(failure.metrics)
            status, detail = "fail", str(failure)
        except AssertionError:
            status, detail = "fail", traceback.format_exc()[-1800:]
            print(detail)
        except Exception:  # noqa: BLE001 - a failed benchmark is reported, not raised
            status, detail = "error", traceback.format_exc()[-1800:]
            print(detail)
        took = time.monotonic() - started
        scenarios[id_] = _results.scenario_entry(id_, title, status, took, detail)
        checkpoint()
        print(f"{id_} {status} {took:.1f}s  {title}", flush=True)
    for m in metrics:
        s = m["stats"]
        print(
            f"  {m['id']} {m['name']:<30} {m['labels']} {m['unit']}: "
            f"n={s['n']} p50={s['p50']} p95={s['p95']} p99={s['p99']} max={s['max']}"
        )
    print(f"wrote {out}")
    return 1 if any(s["status"] in {"error", "fail"} for s in scenarios.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
