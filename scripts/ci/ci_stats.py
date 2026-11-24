"""Daily CI health statistics for the dashboards (docs/ci/contract.md, ``data/ci.jsonl``).

    python3 -m scripts.ci.ci_stats --out ci.json [--date YYYY-MM-DD]   # default: yesterday, UTC

One line per day: how the workflow runs created that day concluded, per workflow, plus merge
throughput. A run counts as passed on ``success`` and failed on ``failure`` or ``timed_out``;
``cancelled`` and ``skipped`` runs (superseded pushes, drafts) carry no verdict and are counted
apart. ``first_attempt`` counts runs whose first attempt passed (re-runs hide flakiness otherwise).
Standard library only; read-only with the workflow token.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import urllib.parse
from typing import Any

from scripts.ci.gate import API, GitHub, load_policy

# Dashboard series → (workflow name, event or None for any event).
SERIES = {
    "pr_checks": ("PR checks", "pull_request"),
    "main_checks": ("PR checks", "push"),
    "gate": ("Gate", None),
    "review": ("OpenCode Review", None),
    "nightly": ("Nightly", None),
    "weekly": ("Weekly", None),
}
PASSED = frozenset({"success"})
FAILED = frozenset({"failure", "timed_out", "startup_failure"})


def tally(
    runs: list[dict[str, Any]], workflow: str, event: str | None
) -> dict[str, Any]:
    """Counts and pass rate for one series."""
    mine = [
        r
        for r in runs
        if r.get("name") == workflow and (event is None or r.get("event") == event)
    ]
    done = [r for r in mine if r.get("status") == "completed"]
    passed = sum(1 for r in done if r.get("conclusion") in PASSED)
    failed = sum(1 for r in done if r.get("conclusion") in FAILED)
    first = sum(
        1
        for r in done
        if r.get("conclusion") in PASSED and int(r.get("run_attempt") or 1) == 1
    )
    judged = passed + failed
    return {
        "runs": len(mine),
        "success": passed,
        "failure": failed,
        "no_verdict": len(done) - judged,
        "first_attempt_success": first,
        "pass_rate": round(100 * passed / judged, 1) if judged else None,
    }


def _hours(start: str, end: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    delta = dt.datetime.strptime(end, fmt) - dt.datetime.strptime(start, fmt)
    return delta.total_seconds() / 3600


def collect(gh: GitHub, day: dt.date, freeze_label: str) -> dict[str, Any]:
    span = f"{day.isoformat()}..{day.isoformat()}"
    runs = gh.pages(f"/actions/runs?created={span}", key="workflow_runs", limit=10)
    line: dict[str, Any] = {
        "date": day.isoformat(),
        "collected_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key, (workflow, event) in SERIES.items():
        line[key] = tally(runs, workflow, event)
    query = urllib.parse.quote(f"repo:{gh.repo} is:pr is:merged merged:{span}")
    found = gh.call("GET", f"{API}/search/issues?q={query}&per_page=100") or {}
    hours = [
        _hours(str(i["created_at"]), str(i["closed_at"]))
        for i in found.get("items") or []
        if i.get("created_at") and i.get("closed_at")
    ]
    line["prs_merged"] = int(found.get("total_count") or 0)
    line["median_hours_to_merge"] = (
        round(statistics.median(hours), 1) if hours else None
    )
    label = urllib.parse.quote(freeze_label, safe="")
    issues = gh.pages(f"/issues?state=open&labels={label}", limit=2)
    line["freeze_open"] = sum(1 for i in issues if "pull_request" not in i)
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci_stats", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--date", help="UTC day (default: yesterday)")
    args = parser.parse_args(argv)
    day = (
        dt.date.fromisoformat(args.date)
        if args.date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    line = collect(gh, day, str(load_policy().get("freeze_label") or "ci/freeze"))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    print(json.dumps(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
