"""Results file, schema 1, shared by the scenario conftest and the benchmarks.

See docs/ci/contract.md. Standard library only.
"""

import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path

DETAIL_LIMIT = 2000


def utc_now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rounded(value):
    return round(value, 1)


def stats(samples):
    """n, p50, p95, p99, mean and max of ``samples``, rounded to 1 decimal."""
    ordered = sorted(samples)
    n = len(ordered)
    if n == 0:
        return {
            "n": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "mean": None,
            "max": None,
        }

    def percentile(p):
        return ordered[min(n - 1, max(0, round(p / 100 * n + 0.5) - 1))]

    return {
        "n": n,
        "p50": rounded(percentile(50)),
        "p95": rounded(percentile(95)),
        "p99": rounded(percentile(99)),
        "mean": rounded(sum(ordered) / n),
        "max": rounded(ordered[-1]),
    }


def single(value):
    """Stats for a metric that is one number rather than a distribution."""
    value = rounded(value)
    return {
        "n": 1,
        "p50": value,
        "p95": value,
        "p99": value,
        "mean": value,
        "max": value,
    }


def environment():
    return {
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
    }


def scenario_entry(id_, title, status, duration_s, detail=""):
    return {
        "id": id_,
        "title": title,
        "status": status,
        "duration_s": rounded(duration_s),
        "detail": str(detail)[:DETAIL_LIMIT],
    }


def document(suite, started_at, scenarios, metrics):
    return {
        "schema": 1,
        "suite": suite,
        "sha": os.environ.get("TEAMUP_SHA") or None,
        "version": os.environ.get("TEAMUP_VERSION") or None,
        "started_at": started_at,
        "finished_at": utc_now(),
        "host": os.environ.get("TEAMUP_HOST") or None,
        "scenarios": scenarios,
        "metrics": metrics,
    }


def write(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".tmp")
    partial.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    partial.replace(path)
    return path


def results_dir():
    return Path(os.environ.get("TEAMUP_RESULTS_DIR") or "results")
