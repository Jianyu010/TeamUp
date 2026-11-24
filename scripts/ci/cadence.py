"""Is a scheduled suite due? (gate-tick.yml dispatches it when it is.)

GitHub fires cron schedules hours late, so nightly.yml, weekly.yml and dashboard.yml have no
schedule of their own: the gate tick asks this script every hour and dispatches a due suite.

    python3 -m scripts.ci.cadence due nightly     # exit 0 when due, 1 when not

A suite is due once its slot (``cadence`` in .github/ci/policy.json, in the team's UTC offset)
has passed and no run of its workflow was created since that slot. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Any

from scripts.ci.gate import GitHub, load_policy


def last_slot(
    now: dt.datetime, rule: dict[str, Any], offset_hours: float
) -> dt.datetime:
    """The most recent slot at or before ``now`` (UTC), for a daily or weekly ``rule``."""
    tz = dt.timezone(dt.timedelta(hours=offset_hours))
    local = now.astimezone(tz)
    hour, minute = (int(x) for x in str(rule["at"]).split(":"))
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "weekday" in rule:  # Monday = 0 ... Sunday = 6
        slot -= dt.timedelta(days=(local.weekday() - int(rule["weekday"])) % 7)
        if slot > local:
            slot -= dt.timedelta(days=7)
    elif slot > local:
        slot -= dt.timedelta(days=1)
    return slot.astimezone(dt.UTC)


def _created(run: dict[str, Any]) -> dt.datetime:
    return dt.datetime.strptime(str(run["created_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.UTC
    )


def due(runs: list[dict[str, Any]], slot: dt.datetime) -> bool:
    """Whether no run of the workflow was created at or after ``slot``."""
    return not any(_created(run) >= slot for run in runs if run.get("created_at"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cadence", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("due")
    d.add_argument("suite")
    args = parser.parse_args(argv)
    cadence = load_policy()["cadence"]
    rule = cadence[args.suite]
    if rule.get("enabled") is False:
        print(f"cadence: {args.suite} is disabled in the policy")
        return 1
    slot = last_slot(dt.datetime.now(dt.UTC), rule, float(cadence["utc_offset_hours"]))
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    data = gh.call("GET", f"/actions/workflows/{args.suite}.yml/runs?per_page=10") or {}
    is_due = due(data.get("workflow_runs") or [], slot)
    print(
        f"cadence: {args.suite} slot {slot:%Y-%m-%d %H:%M}Z {'due' if is_due else 'done'}"
    )
    return 0 if is_due else 1


if __name__ == "__main__":
    sys.exit(main())
