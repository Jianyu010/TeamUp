"""Versions, releases, results and cadence (scripts/ci), with GitHub faked."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.ci import cadence, release, results, version


class FakeGitHub:
    """main: a - b - c (tag v0.1.0 at a) - m1 (merge of p1) - m2 (merge of p2) - d (direct)."""

    def __init__(self, tags: dict[str, str] | None = None) -> None:
        self.repo = "o/TeamUp"
        self.tags = tags if tags is not None else {"v0.1.0": "c"}
        self.history = {
            "c": ["b"],
            "m1": ["c", "p1"],
            "p1": ["c"],
            "m2": ["m1", "p2"],
            "p2": ["m1"],
            "d": ["m2"],
        }
        self.order = ["c", "p1", "m1", "p2", "m2", "d"]
        self.writes: list[tuple[str, str, Any]] = []

    def pages(
        self, path: str, key: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        assert path == "/tags"
        return [{"name": n, "commit": {"sha": s}} for n, s in self.tags.items()]

    def _ancestors(self, sha: str) -> set[str]:
        seen, todo = set(), [sha]
        while todo:
            cur = todo.pop()
            if cur in seen or cur not in self.history:
                continue
            seen.add(cur)
            todo += self.history[cur]
        return seen

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if method != "GET":
            self.writes.append((method, path, body))
            return {}
        if path.startswith("/compare/"):
            base, head = path.split("/")[2].split("?")[0].split("...")
            if base == head:
                return {"status": "identical", "commits": []}
            if base not in self._ancestors(head):
                return {"status": "behind", "commits": []}
            inside = self._ancestors(head) - self._ancestors(base)
            commits = [
                {"sha": s, "parents": [{"sha": p} for p in self.history[s]]}
                for s in self.order
                if s in inside
            ]
            return {"status": "ahead", "commits": commits}
        if path.endswith("/pulls"):
            sha = path.split("/")[2]
            if sha in ("m1", "m2"):
                n = 11 if sha == "m1" else 12
                title = (
                    "Add room history search"
                    if n == 11
                    else "Fix duplicate mention notices"
                )
                return [
                    {
                        "number": n,
                        "title": title,
                        "body": "Closes #6" if n == 11 else "fixes #9 and refs #3",
                        "merged_at": "2026-11-25T00:00:00Z",
                        "merge_commit_sha": sha,
                        "user": {"login": "dev"},
                        "html_url": f"https://github.com/o/TeamUp/pull/{n}",
                        "labels": [],
                    }
                ]
            return []
        if path.startswith("/commits/"):
            return {
                "commit": {"message": "Bump dashboards\n\nbody"},
                "author": {"login": "owner"},
                "html_url": "u",
            }
        raise AssertionError(f"unexpected GET {path}")


def test_build_version_counts_first_parent_merges_since_the_tag() -> None:
    gh = FakeGitHub()
    assert version.first_parents(gh, "c", "d") == ["d", "m2", "m1"]
    assert version.build(gh, "d").version == "0.1.1.dev3"
    assert version.build(gh, "m1").version == "0.1.1.dev1"
    assert version.build(gh, "c").version == "0.1.0"


def test_checkout_version_counts_only_the_pinned_first_parent_history(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Local build")
    git("config", "user.email", "local@example.test")
    git("commit", "-q", "--allow-empty", "-m", "Stable baseline")
    git("tag", "-a", "v0.0.1", "-m", "First stable release")
    git("switch", "-q", "-c", "topic")
    git("commit", "-q", "--allow-empty", "-m", "First topic commit")
    git("commit", "-q", "--allow-empty", "-m", "Second topic commit")
    git("tag", "v9.0.0")
    git("switch", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "Merge topic", "topic")
    development = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "Next stable release")
    git("tag", "v0.0.2")
    git("commit", "-q", "--allow-empty", "-m", "Development resumes")

    assert version.checkout_build(tmp_path, "v0.0.1").version == "0.0.1"
    first = version.checkout_build(tmp_path, development)
    assert (first.version, first.count, first.tag) == ("0.0.2.dev1", 1, "v0.0.1")
    assert version.checkout_build(tmp_path, "v0.0.2").version == "0.0.2"
    assert version.checkout_build(tmp_path).version == "0.0.3.dev1"


def test_a_tag_off_the_history_is_refused() -> None:
    gh = FakeGitHub({"v0.1.0": "d"})
    with pytest.raises(ValueError, match="not an ancestor"):
        version.first_parents(gh, "d", "m1")


def test_release_plan_patch_and_minor() -> None:
    tags = {(0, 3, 0): "a", (0, 3, 4): "b"}
    assert release.plan(tags, "patch") == ((0, 3, 4), "b", (0, 3, 5))
    assert release.plan(tags, "minor") == ((0, 3, 0), "a", (0, 4, 0))
    assert release.plan({}, "patch") is None


def test_a_patch_release_tags_and_summarises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = FakeGitHub()
    evidence = {
        "suite": "nightly",
        "sha": "d" * 40,
        "scenarios": [
            {"id": "NS-01", "status": "pass"},
            {"id": "NS-08", "status": "skip"},
        ],
        "metrics": [
            {
                "name": "dm_delivery_latency",
                "unit": "ms",
                "labels": {"agents": 2},
                "stats": {"p50": 12.1, "p95": 20.4},
            }
        ],
    }
    line = release.cut(gh, "patch", "d", evidence, "RUN", dry_run=False)
    assert (
        line
        and line["tag"] == "v0.1.1"
        and line["prs"] == [11, 12]
        and line["commits"] == 3
    )
    posted = {p: b for _, p, b in gh.writes}
    assert posted["/git/refs"] == {"ref": "refs/tags/v0.1.1", "sha": "d"}
    body = posted["/releases"]["body"]
    assert (
        "3 change(s) since `v0.1.0`: 2 merged PR(s) by 2 contributor(s), closing 2 issue(s)."
        in body
    )
    assert "## Features\n\n- Add room history search #11 (@dev) — closes #6" in body
    assert "## Fixes\n\n- Fix duplicate mention notices #12 (@dev) — closes #9" in body
    assert "- Bump dashboards `d`" in body  # a direct commit, not a PR
    assert "1/1 scenarios passed, 1 skipped" in body
    assert "| dm_delivery_latency (agents=2) | p50 12.1 ms · p95 20.4 ms |" in body


def test_nothing_to_release_when_nothing_merged() -> None:
    gh = FakeGitHub({"v0.1.0": "c", "v0.1.1": "d"})
    assert release.cut(gh, "patch", "d", None, "", dry_run=False) is None
    assert gh.writes == []


def test_a_weekly_rolls_the_patches_into_a_minor() -> None:
    gh = FakeGitHub({"v0.1.0": "c", "v0.1.1": "d"})
    line = release.cut(gh, "minor", "d", None, "", dry_run=True)
    assert (
        line
        and line["tag"] == "v0.2.0"
        and line["base"] == "v0.1.0"
        and line["prs"] == [11, 12]
    )
    assert gh.writes == []  # dry run


def test_a_release_never_goes_backwards() -> None:
    gh = FakeGitHub({"v0.1.0": "c", "v0.1.1": "d"})
    with pytest.raises(ValueError):
        release.cut(gh, "minor", "m1", None, "", dry_run=True)


def test_results_merge_turns_a_missing_part_into_an_error(tmp_path: Path) -> None:
    part = tmp_path / "scenarios.json"
    part.write_text(
        json.dumps(
            {
                "sha": "s",
                "started_at": "2026-11-25T02:00:00Z",
                "finished_at": "2026-11-25T02:10:00Z",
                "scenarios": [{"id": "NS-01", "title": "x", "status": "pass"}],
                "metrics": [],
            }
        )
    )
    summary = results.merge("nightly", [part, tmp_path / "perf.json"])
    assert summary["sha"] == "s" and summary["started_at"] == "2026-11-25T02:00:00Z"
    assert [s["status"] for s in summary["scenarios"]] == ["pass", "error"]
    assert [s["id"] for s in results.red(summary)] == ["perf"]
    assert results.red({"scenarios": []})  # nothing ran is red
    assert results.red({"scenarios": [{"status": "pass"}, {"status": "skip"}]}) == []


class FakeBranch(results.DataBranch):
    def __init__(self, files: dict[str, bytes] | None) -> None:
        self.files = files
        self.commits: list[dict[str, bytes]] = []

    def head(self) -> str | None:
        return None if self.files is None else "h"

    def read(self, path: str, commit: str | None) -> bytes:
        return (self.files or {}).get(path, b"")

    def commit(self, change: Any, message: str, attempts: int = 5) -> str | None:
        files = change(lambda p: self.read(p, self.head()), self.head())
        self.commits.append(files)
        return "new"


def test_publish_appends_one_line() -> None:
    branch = FakeBranch({"data/nightly.jsonl": b'{"sha":"a"}'})
    results.publish(branch, "nightly", {"sha": "b"})
    assert branch.commits[0]["data/nightly.jsonl"] == b'{"sha":"a"}\n{"sha":"b"}\n'
    fresh = FakeBranch(None)
    results.publish(fresh, "repo", {"date": "2026-11-25"})
    assert fresh.commits[0] == {"data/repo.jsonl": b'{"date":"2026-11-25"}\n'}


def test_cadence_slots() -> None:
    rule = {"at": "02:00"}
    utc = dt.UTC
    # 2026-11-25 01:00 Shanghai is 2026-11-24 17:00 UTC: the slot is the 24th 02:00 local.
    now = dt.datetime(2026, 11, 24, 17, 0, tzinfo=utc)
    assert cadence.last_slot(now, rule, 8) == dt.datetime(
        2026, 11, 23, 18, 0, tzinfo=utc
    )
    now = dt.datetime(2026, 11, 24, 18, 30, tzinfo=utc)  # 02:30 local on the 25th
    assert cadence.last_slot(now, rule, 8) == dt.datetime(
        2026, 11, 24, 18, 0, tzinfo=utc
    )
    weekly = {"weekday": 6, "at": "04:00"}  # Sunday
    now = dt.datetime(2026, 11, 25, 3, 0, tzinfo=utc)  # Wednesday
    assert cadence.last_slot(now, weekly, 8) == dt.datetime(
        2026, 11, 21, 20, 0, tzinfo=utc
    )
    slot = dt.datetime(2026, 11, 24, 18, 0, tzinfo=utc)
    assert cadence.due([{"created_at": "2026-11-24T17:59:00Z"}], slot)
    assert not cadence.due([{"created_at": "2026-11-24T18:05:00Z"}], slot)


def test_ci_stats_tally_counts_verdicts_only() -> None:
    from scripts.ci import ci_stats

    runs = [
        {
            "name": "PR checks",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        },
        {
            "name": "PR checks",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
        },
        {
            "name": "PR checks",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "failure",
        },
        {
            "name": "PR checks",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "cancelled",
        },
        {"name": "PR checks", "event": "pull_request", "status": "in_progress"},
        {
            "name": "PR checks",
            "event": "push",
            "status": "completed",
            "conclusion": "timed_out",
        },
    ]
    pr = ci_stats.tally(runs, "PR checks", "pull_request")
    assert pr == {
        "runs": 5,
        "success": 2,
        "failure": 1,
        "no_verdict": 1,
        "first_attempt_success": 1,
        "pass_rate": 66.7,
    }
    assert ci_stats.tally(runs, "PR checks", "push")["pass_rate"] == 0.0
    assert ci_stats.tally(runs, "Nightly", None)["pass_rate"] is None
