# Shared CI suite contract

This page defines the interface shared by the nightly scenarios, performance benchmarks, weekly agent end-to-end tests, dashboards, and release scripts. Update this contract before changing a field, path, or command used by any of them.

## Directory layout

```text
tests/scenarios/
  README.md               # Scenario catalog with IDs, titles, coverage, and suite membership
  conftest.py             # Collects tests only for suites selected by TEAMUP_SCENARIOS and writes summary.json
  _stack.py               # Shared fixture that starts the Server, Client daemon, and MCP stdio session
  nightly/test_*.py       # Model-free functional scenarios (NS-xx)
  perf/                   # Model-free performance benchmarks (NP-xx), python -m tests.scenarios.perf
  weekly/                 # Scenarios with real agents (WE-xx), python -m tests.scenarios.weekly
scripts/ci/               # Gate, merge, release, results publishing, and repo statistics (standard library only)
dashboards/site/          # Static dashboard pages, published to the root of the ci-data branch
```

The PR gate's `pytest` invocation uses the `testpaths` from the root `pyproject.toml` and **must exclude** `tests/scenarios/`. The `tests/scenarios/conftest.py` file uses `collect_ignore_glob` to skip all scenarios unless the `TEAMUP_SCENARIOS` environment variable includes the corresponding suite name.

## Running the suites

Run all commands from the repository root. Set up the Python environment with `uv sync --frozen --all-packages` and run each suite with `--no-sync`.

| Suite | Command | Time limit | Model calls |
| --- | --- | --- | --- |
| Nightly scenarios | `TEAMUP_SCENARIOS=nightly uv run --no-sync pytest tests/scenarios/nightly -q` | 15 minutes | No |
| Nightly performance | `uv run --no-sync python -m tests.scenarios.perf --out results/nightly/perf.json` | 25 minutes | No |
| Weekly agent end-to-end tests | `uv run --no-sync python -m tests.scenarios.weekly --out results/weekly/agents.json` | 90 minutes | Yes, via opencode and the csi gateway |
| Weekly pressure and recovery | `uv run --no-sync python -m tests.scenarios.perf --suite weekly` | 35 minutes | No |

The `TEAMUP_RESULTS_DIR` environment variable controls where the summary goes and defaults to `results`. The `conftest.py` file writes `results/nightly/scenarios.json`. The workflow then runs `scripts/ci/results.py merge` to produce `results/nightly/summary.json`.

The nightly report attempts to publish the summary and sets the merge freeze when the verdict is red. The report then fails the GitHub Actions run so its conclusion reflects the suite verdict.

## Check granularity and load profiles

Report separately diagnosable behaviors as individual scenario entries with unique, stable IDs. Related checks may share an ID prefix and a fixture. Each entry records only its own verdict. A failure in one entry does not overwrite neighboring verdicts. A required `fail` or `error` still makes the suite red. Each runner saves completed verdicts as it goes. If it is interrupted, unfinished entries report errors and completed entries retain their results.

Use model-free, bounded workloads for both load profiles. Nightly uses hundreds of rooms and small task groups of 3, 4 and 5. Weekly uses thousands to tens of thousands of rooms and groups of 10, 30, 50 and 100 with scripted participants. The weekly load profile is separate from real-agent scenarios. Hold connected sessions, heartbeat activity and simultaneous sends as separately reported workload dimensions. Mark incorrect deliveries and failed operations with a failing verdict. Publish latency as metrics while comparable runner baselines are collected.

`python -m tests.scenarios.perf --suite weekly` writes `results/weekly/perf.json`, with `suite: "weekly"` and workload labels that distinguish scale. The weekly agent step writes `results/weekly/agents.json`. The workflow merges both files into `results/weekly/summary.json` and publishes the existing schema. Missing parts are errors. The standalone agent command keeps its existing default output. The weekly report records failures without freezing merges.

Run load and recovery checks on disposable loopback Servers and synthetic data. Keep databases, browser profiles, and snapshots in runner-owned temporary workspaces. Clean them after the checks and publish only results and bounded diagnostics. The recovery drill verifies restored data through the public API and does not use deployment data or credentials.

## Results file (schema 1)

```json
{
  "schema": 1,
  "suite": "nightly",
  "sha": "<40-character commit SHA>",
  "version": "0.1.1.dev3",
  "started_at": "2026-11-25T02:00:00Z",
  "finished_at": "2026-11-25T02:21:13Z",
  "host": "hp-laptop",
  "scenarios": [
    {"id": "NS-01", "title": "Workspace installs from the lockfile", "status": "pass", "duration_s": 41.2, "detail": ""}
  ],
  "metrics": [
    {
      "id": "NP-01",
      "name": "dm_delivery_latency",
      "unit": "ms",
      "labels": {"agents": 2},
      "stats": {"n": 200, "p50": 12.1, "p95": 20.4, "p99": 31.0, "mean": 13.0, "max": 44.2},
      "extra": {}
    }
  ]
}
```

- Valid values for `status` are `pass`, `fail`, `error`, and `skip`. The `detail` field summarizes the failure in 2,000 characters or fewer and must be free of secrets.
- The suite fails if any entry in `scenarios` has status `fail` or `error`. A performance check records metrics and its own verdict. It reports an invariant violation as `fail` and a benchmark execution error as `error`.
- Label values must be scalars. The dashboard distinguishes series by `name` and `labels` together. Round numeric values to one decimal place.
- The workflow supplies `sha`, `version`, and `host` through `TEAMUP_SHA`, `TEAMUP_VERSION`, and `TEAMUP_HOST` respectively. Write `null` for any missing values.

## The `ci-data` branch

The `scripts/ci/results.py publish` command writes to the branch through the GitHub Contents API, without using `git push`.

```text
data/nightly.jsonl     # One complete summary per nightly run
data/weekly.jsonl      # One line per weekly run
data/repo.jsonl        # One line of repository statistics per day (see below)
data/releases.jsonl    # One line per release {"tag","version","kind","sha","published_at","prs":[...]}
data/ci.jsonl          # One line of CI health statistics per day (see below)
index.html  repo.html  performance.html  agents.html  assets/*   # Synced from dashboards/site/
```

Example repository statistics record

```json
{"date": "2026-11-25", "collected_at": "...", "stars": 3, "watchers": 2, "forks": 0,
 "open_issues": 4, "open_prs": 6, "issues_opened_1d": 1, "issues_closed_1d": 0,
 "prs_opened_1d": 2, "prs_merged_1d": 1, "release_downloads_total": 0,
 "clones_14d": null, "unique_cloners_14d": null, "views_14d": null, "unique_visitors_14d": null,
 "latest_release": "v0.1.1", "latest_release_at": "...", "days_since_release": 0.4}
```

The CI health record comes from `scripts/ci/ci_stats.py`. Each of `pr_checks`, `main_checks`, `gate`, `review`, `nightly`, and `weekly` contains a set of counts for workflow runs created that day.

```json
{"date": "2026-11-25", "collected_at": "...",
 "pr_checks": {"runs": 12, "success": 9, "failure": 2, "no_verdict": 1, "first_attempt_success": 8, "pass_rate": 81.8},
 "main_checks": {...}, "gate": {...}, "review": {...}, "nightly": {...}, "weekly": {...},
 "prs_merged": 3, "median_hours_to_merge": 5.2, "freeze_open": 0}
```

The `success` count includes passing runs. The `failure` count includes the `failure`, `timed_out`, and `startup_failure` conclusions. The `no_verdict` count includes `cancelled` and `skipped`, which are excluded from the pass rate. If no run has a verdict that day, `pass_rate` is `null`.

Write `null` for fields that cannot be read, for example when the token lacks access to the traffic endpoint. The dashboard leaves those values blank.

## Versioning

The `scripts/ci/version.py` script computes the build version. At a `vX.Y.Z` tag the version is `X.Y.Z`. After the tag, the development version is `X.Y.(Z+1).devN`, where N counts the commits along the first-parent chain. For example, the PEP 440 ordering is `0.0.1 < 0.0.2.dev1 < 0.0.2`. Publish `vX.Y.(Z+1)` when nightly passes and N > 0. Publish `vX.(Y+1).0` when weekly passes and there has been a merge since the last `vX.Y.0` tag.

The Client and Server write their versions to their respective Python package metadata at build time. A build from a Git checkout uses the closest stable tag on the build revision's first-parent chain. Use `python3 -m scripts.ci.version --local --sha <revision>` to compute the same version for a specific revision. Source trees without Git history can carry a `TEAMUP-VERSION` file at the repository root. If that file is absent, CI can supply the computed version via `TEAMUP_VERSION` at build time. Builds with no version have `0.0.0+unknown` in package metadata and report an unknown version at runtime. Source distributions and wheels retain their build versions.
