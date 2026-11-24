import argparse
import asyncio
import time
import traceback
from pathlib import Path

from tests.scenarios import _results, _stack
from tests.scenarios.perf.benchmarks import BENCHMARKS


async def run_one(id_, runner, quick):
    ws = _stack.Workspace(prefix="tu-perf-")
    try:
        return await runner(ws, quick)
    except BaseException:
        print(ws.logs(1500))
        raise
    finally:
        ws.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tests.scenarios.perf")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--quick", action="store_true", help="smoke run, under 60 s")
    parser.add_argument("--only", help="comma-separated benchmark IDs")
    args = parser.parse_args(argv)
    out = args.out or _results.results_dir() / "nightly" / "perf.json"
    wanted = {x.strip() for x in args.only.split(",")} if args.only else set(BENCHMARKS)

    started_at = _results.utc_now()
    scenarios, metrics = [], []
    for id_, (title, runner) in BENCHMARKS.items():
        if id_ not in wanted:
            continue
        started = time.monotonic()
        try:
            metrics += asyncio.run(run_one(id_, runner, args.quick))
            status, detail = "pass", ""
        except Exception:  # noqa: BLE001 - a failed benchmark is reported, not raised
            status, detail = "error", traceback.format_exc()[-1800:]
            print(detail)
        took = time.monotonic() - started
        scenarios.append(_results.scenario_entry(id_, title, status, took, detail))
        print(f"{id_} {status} {took:.1f}s  {title}", flush=True)
    doc = _results.document("nightly", started_at, scenarios, metrics)
    _results.write(out, doc)
    for m in metrics:
        s = m["stats"]
        print(
            f"  {m['id']} {m['name']:<30} {m['labels']} {m['unit']}: "
            f"n={s['n']} p50={s['p50']} p95={s['p95']} p99={s['p99']} max={s['max']}"
        )
    print(f"wrote {out}")
    return 1 if any(s["status"] == "error" for s in scenarios) else 0


if __name__ == "__main__":
    raise SystemExit(main())
