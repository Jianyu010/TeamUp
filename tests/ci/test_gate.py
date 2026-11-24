"""The merge gate rules (scripts/ci_gate.py; ported from PPMA)."""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
import zlib
from email.message import Message
from typing import Any, Self

import pytest

from scripts.ci import gate as ci_gate
from scripts.ci.gate import (
    AUTO_PENDING,
    AUTO_SUCCESS,
    BASE_CHANGED,
    CALL_RETRIES,
    CHANGED,
    HOLD,
    LABEL,
    NO_REVIEW,
    OVERRIDE_SUCCESS,
    RETRY_BACKOFF_S,
    REVIEW_AUTHOR,
    REVIEW_CLEAN,
    REVIEW_HEADING,
    REVIEW_WORKFLOW,
    SWEEP_SLOT_S,
    UNATTRIBUTED,
    UNCHECKED,
    UNTRUSTED,
    WAITING,
    BudgetSpent,
    GitHub,
    auto,
    auto_for_sha,
    guard,
    request_review,
    revoke,
    sweep,
    tier1_problems,
)

SHA = "a" * 40
OLD = "b" * 40
REQUIRED = ["check", "hygiene"]
GREEN = [
    {
        "name": "check",
        "status": "completed",
        "conclusion": "success",
        "started_at": "1",
    },
    {
        "name": "hygiene",
        "status": "completed",
        "conclusion": "success",
        "started_at": "1",
    },
]
MARK = f"<!-- opencode-review:sha={SHA} -->"
CLEAN = f"{REVIEW_HEADING}\n\n{REVIEW_CLEAN}\n\n{MARK}"
REVIEWED_AT = "2026-10-20T04:05:00Z"
REVIEW = {"user": {"login": REVIEW_AUTHOR}, "body": CLEAN, "submitted_at": REVIEWED_AT}
REVIEW_RUN = {
    "id": 11,
    "display_title": f"OpenCode review of #7 at {SHA}",
    "status": "completed",
    "conclusion": "success",
    "run_attempt": 1,
    "created_at": "2026-10-20T03:59:50Z",
    "run_started_at": "2026-10-20T04:00:00Z",
    "updated_at": "2026-10-20T04:06:00Z",
}
REVIEW_JOB = {
    "name": "review",
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-10-20T04:01:00Z",
    "completed_at": "2026-10-20T04:05:30Z",
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-judge pause (and the mergeable wait) take no real time in tests."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture
def autos(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Hand-offs to the automatic route, for the guard tests that only check the hand-off."""
    calls: list[int] = []
    monkeypatch.setattr(
        "scripts.ci.gate.auto", lambda gh, number, *a, **k: calls.append(number)
    )
    return calls


class FakeGitHub(GitHub):
    """Canned PR state for the gate and the automatic route; records statuses."""

    def __init__(self, pull: dict[str, Any] | None = None) -> None:
        super().__init__("o/r", "t")
        self.pr: dict[str, Any] = {
            "number": 7,
            "created_at": "2026-10-15T00:00:00Z",
            "state": "open",
            "draft": False,
            "mergeable": True,
            "labels": [],
            "head": {"sha": SHA},
            "base": {"sha": OLD, "ref": "main"},
            "user": {"login": "dev"},
            "author_association": "OWNER",
            **(pull or {}),
        }
        self.paths = ["src/x/src/lib.rs"]
        self.review_list: list[dict[str, Any]] = [dict(REVIEW)]
        self.review_run_list: list[dict[str, Any]] = [dict(REVIEW_RUN)]
        self.review_job_of: dict[int, list[dict[str, Any]]] = {11: [dict(REVIEW_JOB)]}
        self.job_reads: list[int] = []
        self.forced: list[bool] = []
        self.thread_list: list[dict[str, Any]] = []
        self.comment_list: list[dict[str, Any]] = []
        self.dispatched: list[tuple[int, str]] = []
        self.reran: list[str] = []
        self.roles: dict[str, str] = {"maintainer": "admin", "dev": "write"}
        self.labeler = "maintainer"
        self.removed = 0
        self.comments: list[str] = []
        self.runs: list[dict[str, Any]] = list(GREEN)
        self.workflow_run_list: list[dict[str, Any]] = []
        self.workflow_run_fail = False
        self.heads = [7]
        self.posted: list[tuple[str, str, str]] = []
        self.layer1: dict[str, tuple[str, str]] = {}
        self.layer1_posts: list[tuple[str, str]] = []
        self.base_changed = ""
        self.gates: dict[str, str] = {}
        self.waits: list[bool] = []
        self.reads = 0
        self.retarget_to = ""
        self.later: dict[str, Any] = {}
        self.later_from = 2
        self.recovers_after = 0

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if path.startswith("/statuses/"):
            if body["context"] != "merge-gate":  # layer-1 reports are kept apart
                self.layer1[body["context"]] = (body["state"], body["description"])
                self.layer1_posts.append((body["context"], body["target_url"]))
                return None
            self.posted.append(
                (path.removeprefix("/statuses/"), body["state"], body["description"])
            )
        if path.endswith("/rerun"):
            self.reran.append(path)
        return None

    def files(self, number: int) -> list[str]:
        return self.paths

    def file_entries(self, number: int) -> list[dict[str, Any]]:
        return getattr(self, "entries", None) or [
            {"filename": p, "status": "modified", "patch": ""} for p in self.paths
        ]

    def reviews(self, number: int) -> list[dict[str, Any]]:
        return self.review_list

    def review_runs(self, sha: str) -> list[dict[str, Any]]:
        return self.review_run_list

    def review_jobs(self, run_id: int) -> list[dict[str, Any]]:
        self.job_reads.append(run_id)
        return self.review_job_of.get(run_id, [])

    def dispatch_review(self, number: int, sha: str, force: bool = False) -> None:
        self.dispatched.append((number, sha))
        self.forced.append(force)

    def threads(self, number: int) -> list[dict[str, Any]]:
        return self.thread_list

    def issue_comments(self, number: int, since: str) -> list[dict[str, Any]]:
        return self.comment_list

    def role(self, user: str) -> str:
        return self.roles.get(user, "read")

    def labeled_by(self, number: int) -> str:
        return self.labeler

    def remove_label(self, number: int) -> None:
        self.removed += 1
        self.pr["labels"] = [x for x in self.pr["labels"] if x["name"] != LABEL]

    def comment(self, number: int, text: str) -> None:
        self.comments.append(text)

    def pull(self, number: int, wait: bool = True) -> dict[str, Any]:
        self.waits.append(wait)
        self.reads += 1
        if self.retarget_to and self.reads > 1:  # retargeted after the first read
            return {**self.pr, "base": {**self.pr["base"], "ref": self.retarget_to}}
        if self.recovers_after and self.reads >= self.recovers_after + 2:
            return self.pr  # GitHub finished recomputing
        if (
            self.later and self.reads >= self.later_from
        ):  # changed after an earlier read
            return {**self.pr, **self.later}
        return self.pr

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        return self.runs

    def workflow_runs(self, sha: str) -> list[dict[str, Any]]:
        if self.workflow_run_fail:
            raise urllib.error.HTTPError("x", 500, "boom", Message(), None)
        return self.workflow_run_list

    def open_pulls_with_head(self, sha: str) -> list[int]:
        return self.heads

    def base_changed_at(self, number: int) -> str:
        return self.base_changed

    def open_pulls(self) -> list[dict[str, Any]]:
        return [self.pr]

    def gate_description(self, sha: str) -> str:
        return self.gates.get(sha, "")

    def status_of(self, sha: str, context: str) -> tuple[str, str]:
        return self.layer1.get(context, ("", ""))


def _event(
    action: str,
    label: str | None = None,
    labels: tuple[str, ...] = (),
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "action": action,
        "sender": {"login": "someone"},
        "pull_request": {
            "number": 7,
            "head": {"sha": SHA},
            "labels": [{"name": n} for n in labels],
        },
    }
    if label is not None:
        event["label"] = {"name": label}
    if changes is not None:
        event["changes"] = changes
    return event


def _state(gh: FakeGitHub) -> str:
    auto(gh, 7, REQUIRED, "run")
    assert len(gh.posted) <= 1
    return gh.posted[0][1] if gh.posted else "none"


def _reason(gh: FakeGitHub) -> str:
    auto(gh, 7, REQUIRED, "run")
    return gh.posted[-1][2]


# --- guard (what a gate event does) ----------------------------------------------------------


def test_a_new_pr_starts_pending() -> None:
    gh = FakeGitHub()
    assert guard(gh, _event("opened"), REQUIRED, "run") is False
    assert gh.posted == [(SHA, "pending", WAITING)]


def test_new_commits_reset_the_gate() -> None:
    gh = FakeGitHub()
    assert guard(gh, _event("synchronize"), REQUIRED, "run") is False
    assert gh.posted == [(SHA, "pending", WAITING)]


def test_an_edit_without_a_base_change_changes_nothing() -> None:
    gh = FakeGitHub()
    assert guard(gh, _event("edited"), REQUIRED, "run") is False
    assert gh.posted == []


def test_a_base_change_resets_an_auto_green_gate() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_SUCCESS
    event = _event("edited", changes={"base": {"ref": {"from": "main"}}})
    assert guard(gh, event, REQUIRED, "run") is False
    assert gh.posted == [(SHA, "pending", "automatic route: " + BASE_CHANGED)]


def test_a_base_change_puts_a_failed_tier1_back_on_top() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_SUCCESS  # read just before revoke failed the gate
    gh.runs = [{**GREEN[0], "conclusion": "failure"}, GREEN[1]]
    assert (
        guard(
            gh,
            _event("edited", changes={"base": {"ref": {"from": "main"}}}),
            REQUIRED,
            "run",
        )
        is False
    )
    assert [state for _, state, _ in gh.posted] == ["pending", "failure"]


def test_a_base_change_does_not_hide_a_failed_gate() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = (
        "`check` failed on this commit"  # revoke's failure is not overwritten
    )
    assert (
        guard(
            gh,
            _event("edited", changes={"base": {"ref": {"from": "main"}}}),
            REQUIRED,
            "run",
        )
        is False
    )
    assert gh.posted == []


def test_a_title_or_body_edit_leaves_the_automatic_evidence_valid() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_SUCCESS
    assert (
        guard(gh, _event("edited", changes={"title": {"from": "old"}}), REQUIRED, "run")
        is False
    )
    assert gh.posted == []


def test_hold_label_changes_hand_back_to_auto(autos: list[int]) -> None:
    gh = FakeGitHub()
    assert guard(gh, _event("unlabeled", label=HOLD), REQUIRED, "run") is False
    assert autos == [7]


def test_draft_and_ready_events_hand_back_to_auto(autos: list[int]) -> None:
    for action in ("converted_to_draft", "ready_for_review"):
        assert guard(FakeGitHub(), _event(action), REQUIRED, "run") is False
    assert autos == [7, 7]


def test_other_labels_are_ignored() -> None:
    gh = FakeGitHub()
    assert guard(gh, _event("labeled", label="bug"), REQUIRED, "run") is False
    assert gh.posted == []


# --- tier 1 ----------------------------------------------------------------------------------


def test_tier1_requires_every_run_of_a_name_to_pass() -> None:
    """``check`` comes from both ci.yml and rust.yml: a passing one must not hide a failing one."""
    both = [
        GREEN[0],
        {**GREEN[0], "conclusion": "failure", "started_at": "2"},
        GREEN[1],
    ]
    assert tier1_problems(both, REQUIRED) == ["`check` concluded failure"]
    # The masking case: the failing Rust `check` started first, the passing CI `check` later.
    masked = [
        {**GREEN[0], "conclusion": "failure", "started_at": "0"},
        GREEN[0],
        GREEN[1],
    ]
    assert tier1_problems(masked, REQUIRED) == ["`check` concluded failure"]
    assert tier1_problems(GREEN, REQUIRED) == []
    running = [
        GREEN[0],
        {**GREEN[0], "status": "in_progress", "conclusion": None},
        GREEN[1],
    ]
    assert tier1_problems(running, REQUIRED) == ["`check` is still in_progress"]


def test_skipped_runs_carry_no_verdict() -> None:
    """A draft's skipped runs stay on the head after it is marked ready; they must not block it."""
    skipped = [{**run, "conclusion": "skipped", "started_at": "0"} for run in GREEN]
    assert tier1_problems(skipped + GREEN, REQUIRED) == []
    assert tier1_problems(skipped, REQUIRED) == [
        "`check` has not run on this commit",
        "`hygiene` has not run on this commit",
    ]
    failed = {**GREEN[0], "conclusion": "failure", "started_at": "2"}
    assert tier1_problems(skipped + GREEN + [failed], REQUIRED) == [
        "`check` concluded failure"
    ]


def test_cancelled_runs_carry_no_verdict() -> None:
    """A concurrency-cancelled run must not block a later successful run on the same head."""
    cancelled = [{**run, "conclusion": "cancelled", "started_at": "0"} for run in GREEN]
    assert tier1_problems(cancelled + GREEN, REQUIRED) == []
    assert tier1_problems(cancelled, REQUIRED) == [
        "`check` has not run on this commit",
        "`hygiene` has not run on this commit",
    ]
    failed = {**GREEN[0], "conclusion": "failure", "started_at": "2"}
    assert tier1_problems(cancelled + GREEN + [failed], REQUIRED) == [
        "`check` concluded failure"
    ]


def test_a_rerun_of_the_same_workflow_replaces_its_earlier_run() -> None:
    """Editing the PR body re-runs hygiene.yml on the same head; its pass replaces the failure."""
    hygiene = {**GREEN[1], "workflow": ".github/workflows/hygiene.yml"}
    failed = {**hygiene, "id": 1, "conclusion": "failure", "started_at": "1"}
    passed = {**hygiene, "id": 2, "started_at": "2"}
    assert tier1_problems([GREEN[0], failed, passed], REQUIRED) == []
    assert tier1_problems([GREEN[0], passed, failed], REQUIRED) == []
    # The other way round, the latest run's failure stands.
    later_failure = {**failed, "id": 3, "started_at": "3"}
    assert tier1_problems([GREEN[0], passed, later_failure], REQUIRED) == [
        "`hygiene` concluded failure"
    ]
    # A latest run that is running blocks until it ends.
    running = {
        **hygiene,
        "id": 4,
        "status": "in_progress",
        "conclusion": None,
        "started_at": "4",
    }
    assert tier1_problems([GREEN[0], failed, running], REQUIRED) == [
        "`hygiene` is still in_progress"
    ]


def test_the_newer_workflow_run_wins_whenever_its_job_started() -> None:
    """Tier 1 orders a workflow's runs as revoke does: by workflow run, not by job start."""
    hygiene = {**GREEN[1], "workflow": "hygiene.yml pull_request"}
    older = {
        **hygiene,
        "workflow_run_id": 10,
        "conclusion": "failure",
        "started_at": "5",
    }
    newer = {
        **hygiene,
        "workflow_run_id": 11,
        "started_at": "4",
    }  # its job started first
    assert tier1_problems([GREEN[0], older, newer], REQUIRED) == []


def test_a_queued_rerun_is_the_latest() -> None:
    """A queued run has no ``started_at`` yet; it must not lose to the older run it replaces."""
    hygiene = {**GREEN[1], "workflow": "hygiene.yml pull_request"}
    passed = {**hygiene, "id": 1, "started_at": "2026-10-22T16:00:00Z"}
    queued = {
        **hygiene,
        "id": 2,
        "status": "queued",
        "conclusion": None,
        "started_at": None,
    }
    assert tier1_problems([GREEN[0], passed, queued], REQUIRED) == [
        "`hygiene` is still queued"
    ]
    assert tier1_problems([GREEN[0], queued, passed], REQUIRED) == [
        "`hygiene` is still queued"
    ]


def test_a_queued_run_after_a_base_change_is_not_stale() -> None:
    gh = FakeGitHub()
    gh.base_changed = "2"
    queued = {**GREEN[1], "status": "queued", "conclusion": None, "started_at": None}
    gh.runs = [{**GREEN[0], "started_at": "3"}, queued]
    assert _reason(gh).endswith("`hygiene` is still queued")


def test_a_newer_run_of_one_workflow_does_not_hide_another_workflows_failure() -> None:
    """``check`` from rust.yml failed; a later ``check`` from ci.yml passing must not clear it."""
    rust = {
        **GREEN[0],
        "workflow": ".github/workflows/rust.yml",
        "conclusion": "failure",
    }
    ci = {**GREEN[0], "workflow": ".github/workflows/ci.yml", "started_at": "2"}
    assert tier1_problems([rust, ci, GREEN[1]], REQUIRED) == [
        "`check` concluded failure"
    ]


def test_runs_of_an_unknown_workflow_all_count() -> None:
    """Without the workflow (its lookup failed), no run replaces another: every one must pass."""
    failed = {**GREEN[1], "id": 1, "conclusion": "failure", "started_at": "1"}
    passed = {**GREEN[1], "id": 2, "started_at": "2"}
    assert tier1_problems([GREEN[0], failed, passed], REQUIRED) == [
        "`hygiene` concluded failure"
    ]


def test_check_runs_are_tagged_with_their_workflow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The check suite of each run maps to the workflow run, whose path names the workflow."""

    class Api(GitHub):
        def __init__(self, runs_fail: bool | Exception = False) -> None:
            super().__init__("o/r", "t")
            self.runs_fail = runs_fail

        def call(self, method: str, path: str, body: Any = None) -> Any:
            if path.startswith(f"/commits/{SHA}/check-runs"):
                return {
                    "check_runs": [
                        {"name": "hygiene", "check_suite": {"id": 5}},
                        {"name": "other", "check_suite": {"id": 6}},
                    ]
                }
            if path.startswith(f"/actions/runs?head_sha={SHA}"):
                if "page=1" not in path:
                    return {"workflow_runs": []}
                if isinstance(self.runs_fail, Exception):
                    raise self.runs_fail
                if self.runs_fail:
                    raise urllib.error.HTTPError(path, 500, "boom", Message(), None)
                return {
                    "workflow_runs": [
                        {
                            "id": 9,
                            "check_suite_id": 5,
                            "path": ".github/workflows/hygiene.yml",
                            "event": "pull_request",
                        }
                    ]
                }
            raise AssertionError(path)

    runs = Api().check_runs(SHA)
    assert [r.get("workflow") for r in runs] == [
        ".github/workflows/hygiene.yml pull_request",
        None,
    ]
    assert [r.get("workflow_run_id") for r in runs] == [9, None]
    assert [r.get("workflow") for r in Api(runs_fail=True).check_runs(SHA)] == [
        None,
        None,
    ]
    assert "every run counts" in capsys.readouterr().out
    # A network failure (not only an HTTP error) also leaves the runs untagged.
    for exc in (urllib.error.URLError("reset"), TimeoutError("slow")):
        assert [r.get("workflow") for r in Api(runs_fail=exc).check_runs(SHA)] == [
            None,
            None,
        ]
    # The sweep's budget still stops the job.
    with pytest.raises(BudgetSpent):
        Api(runs_fail=BudgetSpent("GET /actions/runs")).check_runs(SHA)


def test_a_missing_check_is_a_problem() -> None:
    assert tier1_problems([GREEN[0]], REQUIRED) == [
        "`hygiene` has not run on this commit"
    ]


# --- revoke ----------------------------------------------------------------------------------


def test_a_failed_tier1_run_revokes_the_gate_and_others_do_not() -> None:
    gh = FakeGitHub()
    revoke(gh, SHA, "PR hygiene", "failure", "run")
    revoke(gh, SHA, "CI", "cancelled", "run")
    assert [state for _, state, _ in gh.posted] == ["failure"]
    assert gh.posted[0][2] == "PR hygiene concluded failure; fix it and push"
    gh = FakeGitHub()
    revoke(gh, SHA, "CI", "success", "run")
    assert gh.posted == []


def test_a_tier1_rerun_resets_the_gate_while_it_runs() -> None:
    gh = FakeGitHub()
    revoke(gh, SHA, "CI", "in_progress", "run")
    assert gh.posted == [(SHA, "pending", "CI is running again")]


# --- the automatic route ---------------------------------------------------------------------


def test_green_tier1_passes_the_gate() -> None:
    gh = FakeGitHub()
    assert _state(gh) == "success"
    assert gh.posted[0] == (SHA, "success", AUTO_SUCCESS)


def test_a_shared_head_keeps_the_gate_pending() -> None:
    gh = FakeGitHub()
    gh.heads = [7, 9]
    assert "open PR #9" in _reason(gh)


def test_hold_draft_and_conflicts_close_the_route() -> None:
    gh = FakeGitHub()
    gh.pr["labels"] = [{"name": HOLD}]
    assert "on `pr/hold`" in _reason(gh)
    gh = FakeGitHub()
    gh.pr["draft"] = True
    assert _state(gh) == "pending"
    gh = FakeGitHub()
    gh.pr["mergeable"] = False
    assert _state(gh) == "pending"


def test_revoke_skips_a_run_a_newer_run_of_its_workflow_replaced() -> None:
    """An old failed run finishing after its re-run must not re-fail the gate auto greened."""
    hygiene = {"path": ".github/workflows/hygiene.yml", "event": "pull_request"}
    done = {"status": "completed", "conclusion": "success"}
    gh = FakeGitHub()
    gh.workflow_run_list = [{**hygiene, "id": 10}, {**hygiene, "id": 11, **done}]
    revoke(gh, SHA, "PR hygiene", "failure", "run", run_id=10)
    assert gh.posted == []
    # The newest run's failure stands, and so does one from another workflow or trigger.
    revoke(gh, SHA, "PR hygiene", "failure", "run", run_id=11)
    gh.workflow_run_list += [{**hygiene, "id": 12, "event": "push"}]
    revoke(gh, SHA, "PR hygiene", "failure", "run", run_id=11)
    assert [state for _, state, _ in gh.posted] == ["failure", "failure"]
    # Only a newer run that passed replaces the failure: one that was cancelled or skipped carries
    # no verdict, and one still running may yet be cancelled (its success re-greens the gate).
    gh = FakeGitHub()
    for later in ("cancelled", "skipped", "failure", None):
        gh.posted.clear()
        gh.workflow_run_list = [
            {**hygiene, "id": 10},
            {
                **hygiene,
                "id": 11,
                "status": "completed" if later else "in_progress",
                "conclusion": later,
            },
        ]
        revoke(gh, SHA, "PR hygiene", "failure", "run", run_id=10)
        assert len(gh.posted) == 1, later
    # Without the runs (a failed lookup, or no run id) the failure stands: fail closed.
    gh = FakeGitHub()
    gh.workflow_run_fail = True
    revoke(gh, SHA, "PR hygiene", "failure", "run", run_id=10)
    revoke(gh, SHA, "PR hygiene", "failure", "run")
    assert [state for _, state, _ in gh.posted] == ["failure", "failure"]


def test_a_failed_tier1_leaves_the_gate_to_revoke_and_a_running_one_waits() -> None:
    gh = FakeGitHub()
    gh.runs = [GREEN[0], {**GREEN[1], "conclusion": "failure"}]
    assert _state(gh) == "none"
    gh = FakeGitHub()
    gh.runs = [GREEN[0], {**GREEN[1], "status": "in_progress"}]
    assert _state(gh) == "pending"


def test_auto_for_a_sha_needs_exactly_one_open_pr() -> None:
    gh = FakeGitHub()
    auto_for_sha(gh, SHA, REQUIRED, "run")
    assert gh.posted[-1][1] == "success"
    gh = FakeGitHub()
    gh.heads = [7, 9]
    auto_for_sha(gh, SHA, REQUIRED, "run")
    assert gh.posted == []


def test_hold_label_changes_re_evaluate_the_route() -> None:
    gh = FakeGitHub()
    assert guard(gh, _event("unlabeled", label=HOLD), REQUIRED, "run") is False
    assert gh.posted[-1][1] == "success"


def test_draft_conversions_re_evaluate_the_route() -> None:
    gh = FakeGitHub()
    gh.pr["draft"] = True
    assert guard(gh, _event("converted_to_draft"), REQUIRED, "run") is False
    assert gh.posted[-1][1:] == ("pending", "automatic route: the PR is a draft")


def test_a_base_change_makes_earlier_evidence_stale() -> None:
    gh = FakeGitHub()
    gh.base_changed = "2"  # after the check runs started ("1")
    assert "before the PR's base changed" in _reason(gh)
    gh = FakeGitHub()
    gh.base_changed = "0"
    assert _state(gh) == "success"


def test_a_rerun_after_a_base_change_clears_the_stale_evidence() -> None:
    """Re-running each workflow on the head counts, though the earlier runs stay listed.

    The review is a separate rule: one submitted before the base change is stale until the
    review runs again.
    """
    gh = FakeGitHub()
    gh.base_changed = "2026-10-20T04:10:00Z"  # after REVIEWED_AT
    old = [
        {
            **run,
            "workflow": f"{run['name']}.yml",
            "id": 1,
            "started_at": "2026-10-20T03:00:00Z",
        }
        for run in GREEN
    ]
    new = [{**run, "started_at": "2026-10-20T04:20:00Z", "id": 2} for run in old]
    gh.runs = old
    assert "before the PR's base changed" in _reason(gh)
    gh.runs = old + new
    assert "predates the PR's base change" in _reason(gh)
    gh.review_list = [{**REVIEW, "submitted_at": "2026-10-20T04:30:00Z"}]
    gh.review_run_list = [{**REVIEW_RUN, "created_at": "2026-10-20T04:24:00Z"}]
    gh.review_job_of = {
        11: [
            {
                **REVIEW_JOB,
                "started_at": "2026-10-20T04:25:00Z",
                "completed_at": "2026-10-20T04:31:00Z",
            }
        ]
    }
    gh.posted.clear()
    assert _state(gh) == "success"


# --- the write is checked after it lands -----------------------------------------------------


def test_a_change_that_lands_after_the_success_write_is_repaired() -> None:
    for later in (
        {"draft": True},
        {"labels": [{"name": HOLD}]},
        {"mergeable": False},
    ):
        gh = FakeGitHub()
        gh.later, gh.later_from = later, 3
        auto(gh, 7, REQUIRED, "run")
        assert [state for _, state, _ in gh.posted] == ["success", "pending"], later
        assert CHANGED in gh.posted[-1][2]


def test_recomputed_mergeability_is_judged_again_instead_of_left_pending() -> None:
    """A base move makes `mergeable` null after the write; no event ends that."""
    gh = FakeGitHub()
    gh.later, gh.later_from = {"mergeable": None}, 3
    gh.recovers_after = 2  # the re-judgement's read sees it computed again
    auto(gh, 7, REQUIRED, "run")
    assert [state for _, state, _ in gh.posted] == ["success", "pending", "success"]


def test_mergeability_that_stays_unknown_gives_up_after_the_re_judgements() -> None:
    gh = FakeGitHub()
    gh.later, gh.later_from = {"mergeable": None}, 3
    auto(gh, 7, REQUIRED, "run")
    states = [state for _, state, _ in gh.posted]
    assert states[-1] == "pending"
    assert "merges cleanly" in gh.posted[-1][2]  # fail closed, with the reason
    assert len(states) <= 2 * (ci_gate.REJUDGES + 1)


def test_a_sweep_never_re_judges() -> None:
    gh = FakeGitHub()
    gh.later, gh.later_from = {"mergeable": None}, 3
    auto(gh, 7, REQUIRED, "run", wait=False)
    assert [state for _, state, _ in gh.posted] == ["success", "pending"]


def test_a_retarget_while_auto_runs_posts_no_success() -> None:
    gh = FakeGitHub()
    gh.retarget_to = "release"
    auto(gh, 7, REQUIRED, "run")
    assert gh.posted == []


def test_a_label_or_draft_while_auto_runs_posts_no_success() -> None:
    for later in (
        {"labels": [{"name": HOLD}]},
        {"draft": True},
        {"state": "closed"},
    ):
        gh = FakeGitHub()
        gh.later = later
        auto(gh, 7, REQUIRED, "run")
        assert gh.posted == [], later


def test_a_conflict_seen_before_the_write_posts_no_success() -> None:
    gh = FakeGitHub()
    gh.later = {"mergeable": False}
    auto(gh, 7, REQUIRED, "run")
    assert gh.posted == []


class _OutOfTimeAfterWrite(FakeGitHub):
    """The sweep's time runs out right after the success write."""

    def pull(self, number: int, wait: bool = True) -> dict[str, Any]:
        if self.posted:
            raise BudgetSpent("GET /pulls/7")
        return super().pull(number, wait)


def test_a_success_that_cannot_be_re_checked_does_not_stand() -> None:
    gh = _OutOfTimeAfterWrite()
    gh.gates[SHA] = AUTO_PENDING + "a problem solved since"
    sweep(gh, REQUIRED, "run")
    assert [state for _, state, _ in gh.posted] == ["success", "pending"]
    assert UNCHECKED in gh.posted[-1][2]


class _ReadFailsAfterWrite(FakeGitHub):
    """GitHub fails the read that checks a success write."""

    def pull(self, number: int, wait: bool = True) -> dict[str, Any]:
        if self.posted:
            raise urllib.error.URLError("connection reset")
        return super().pull(number, wait)


def test_a_success_whose_check_fails_is_set_back_and_the_error_raised() -> None:
    gh = _ReadFailsAfterWrite()
    with pytest.raises(urllib.error.URLError):
        auto(gh, 7, REQUIRED, "run")
    assert [state for _, state, _ in gh.posted] == ["success", "pending"]


class _SecondPrOpensDuringWrite(FakeGitHub):
    """Another PR with the same head opens between the judged reads and the success write."""

    def open_pulls_with_head(self, sha: str) -> list[int]:
        return [7, 9] if self.posted else [7]


def test_a_shared_head_that_appears_during_the_write_is_repaired() -> None:
    gh = _SecondPrOpensDuringWrite()
    auto(gh, 7, REQUIRED, "run")
    assert [state for _, state, _ in gh.posted] == ["success", "pending"]
    assert "open PR #9" in gh.posted[-1][2]


def test_a_shared_head_is_repaired_even_when_this_pr_moved_on() -> None:
    gh = _SecondPrOpensDuringWrite()
    gh.later, gh.later_from = (
        {"head": {"sha": "c" * 40}},
        3,
    )  # a new commit lands after the write
    auto(gh, 7, REQUIRED, "run")
    assert [(sha, state) for sha, state, _ in gh.posted] == [
        (SHA, "success"),
        (SHA, "pending"),
    ]


class _HeadsReadFails(FakeGitHub):
    """GitHub fails the shared-head read that checks a success write."""

    def open_pulls_with_head(self, sha: str) -> list[int]:
        if self.posted:
            raise urllib.error.URLError("connection reset")
        return [7]


def test_a_failed_shared_head_check_sets_the_success_back() -> None:
    gh = _HeadsReadFails()
    with pytest.raises(urllib.error.URLError):
        auto(gh, 7, REQUIRED, "run")
    assert [state for _, state, _ in gh.posted] == ["success", "pending"]
    assert UNCHECKED in gh.posted[-1][2]


# --- the sweep -------------------------------------------------------------------------------


def test_the_sweep_re_checks_only_what_the_automatic_route_posted() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_SUCCESS
    sweep(gh, REQUIRED, "run")
    assert gh.posted == []  # identical periodic evaluations are a no-op
    gh = FakeGitHub()
    gh.gates[SHA] = WAITING
    sweep(gh, REQUIRED, "run")
    assert gh.posted == []
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_PENDING + "a problem solved since"
    sweep(gh, REQUIRED, "run")  # nothing is wrong now
    assert gh.posted[-1][1] == "success"


def test_the_sweep_re_checks_a_tier1_failure() -> None:
    """A failure posted while the runs could not be read must not stick once tier 1 passes."""
    for failed in (
        "PR hygiene concluded failure; fix it and push",  # revoke
        "`hygiene` concluded failure; fix it and push",
    ):  # guard, on a base change
        gh = FakeGitHub()
        gh.gates[SHA] = failed
        sweep(gh, REQUIRED, "run")
        assert gh.posted[-1][1] == "success"
        gh = FakeGitHub()
        gh.gates[SHA] = failed
        gh.runs = [GREEN[0], {**GREEN[1], "conclusion": "failure"}]
        sweep(gh, REQUIRED, "run")
        assert gh.posted == []  # still failing: the failure stays


def test_the_sweep_keeps_a_tier1_failure_no_required_check_shows() -> None:
    """A workflow whose latest run failed keeps the gate failed, whatever its check runs say."""
    hygiene = {"path": ".github/workflows/hygiene.yml", "event": "pull_request"}
    rust = {"path": ".github/workflows/rust.yml", "event": "pull_request"}
    failed = "Rust concluded startup_failure; fix it and push"
    gh = FakeGitHub()
    gh.gates[SHA] = failed
    gh.workflow_run_list = [
        {**rust, "id": 3, "status": "completed", "conclusion": "startup_failure"}
    ]
    sweep(gh, REQUIRED, "run")
    assert gh.posted == []
    # Superseded by a run of the same workflow that passed: re-judged. One still queued or
    # running has no verdict yet, so the failure stays until it passes.
    for later, posted in (
        ({"status": "completed", "conclusion": "success"}, True),
        ({"status": "in_progress", "conclusion": None}, False),
        ({"status": "queued"}, False),
    ):
        gh = FakeGitHub()
        gh.gates[SHA] = failed
        gh.workflow_run_list = [
            {**rust, "id": 3, "status": "completed", "conclusion": "failure"},
            {**rust, "id": 4, **later},
            {**hygiene, "id": 5, "status": "completed", "conclusion": "success"},
        ]
        sweep(gh, REQUIRED, "run")
        assert gh.posted[-1][1] == "success" if posted else gh.posted == [], later
    # A cancelled newer run supersedes nothing; unreadable runs keep the failure.
    gh = FakeGitHub()
    gh.gates[SHA] = failed
    gh.workflow_run_list = [
        {**rust, "id": 3, "status": "completed", "conclusion": "failure"},
        {**rust, "id": 4, "status": "completed", "conclusion": "cancelled"},
    ]
    sweep(gh, REQUIRED, "run")
    gh.workflow_run_fail = True
    sweep(gh, REQUIRED, "run")
    assert gh.posted == []
    # The Gate's own pull_request_target runs are not tier 1.
    gh = FakeGitHub()
    gh.gates[SHA] = failed
    gh.workflow_run_list = [
        {
            "path": ".github/workflows/gate.yml",
            "event": "pull_request_target",
            "id": 6,
            "status": "completed",
            "conclusion": "failure",
        },
    ]
    sweep(gh, REQUIRED, "run")
    assert gh.posted[-1][1] == "success"


def test_the_sweep_re_checks_a_ready_prs_tier1_failure() -> None:
    """The maintainer route stuck on a tier-1 failure that a re-run cleared is re-judged too."""
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.gates[SHA] = f"`{LABEL}`: `hygiene` concluded failure"
    sweep(gh, REQUIRED, "run")
    assert gh.posted[-1][1:] == ("success", OVERRIDE_SUCCESS)
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.gates[SHA] = (
        f"`{LABEL}`: only a maintainer may add it"  # its events re-judge the rest
    )
    sweep(gh, REQUIRED, "run")
    assert gh.posted == []


def test_the_sweep_leaves_an_uncomputed_mergeable_for_the_next_sweep() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_SUCCESS
    gh.pr["mergeable"] = None
    sweep(gh, REQUIRED, "run")
    assert gh.waits == [False]
    assert gh.posted == []  # no 30 s wait, and nothing else is wrong: left as it is
    auto(gh, 7, REQUIRED, "run")  # an event-driven run still waits and then says why
    assert gh.waits[1] is True
    assert gh.posted[-1][1] == "pending"


def test_an_uncomputed_mergeable_still_fails_closed_on_other_problems() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_SUCCESS
    gh.pr["mergeable"] = None
    gh.pr["labels"] = [{"name": HOLD}]  # a hold while GitHub recomputes mergeability
    sweep(gh, REQUIRED, "run")
    assert gh.posted[-1][1] == "pending"
    assert "pr/hold" in gh.posted[-1][2]


def test_the_sweep_stops_when_its_budget_is_spent() -> None:
    gh = FakeGitHub()
    gh.gates[SHA] = AUTO_PENDING + "a problem"
    sweep(gh, REQUIRED, "run", budget_s=0)
    assert gh.posted == []


class _SlowGitHub(FakeGitHub):
    """A sweep whose time runs out in the middle of a PR (or while listing them)."""

    def __init__(self, listing: bool = False) -> None:
        super().__init__()
        self.listing = listing

    def open_pulls(self) -> list[dict[str, Any]]:
        assert self.deadline is not None  # the listing runs under the deadline
        if self.listing:
            raise BudgetSpent("GET /pulls")
        return super().open_pulls()

    def gate_description(self, sha: str) -> str:
        raise BudgetSpent("GET /commits/x/status")


def test_a_sweep_that_runs_out_stops_cleanly() -> None:
    for listing in (False, True):
        gh = _SlowGitHub(listing)
        sweep(gh, REQUIRED, "run")
        assert gh.posted == []
        assert gh.deadline is None


class _ManyPulls(FakeGitHub):
    """Three open PRs; records the order the sweep looks at them."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def open_pulls(self) -> list[dict[str, Any]]:
        return [{"number": n, "labels": [], "head": {"sha": str(n)}} for n in (9, 7, 8)]

    def gate_description(self, sha: str) -> str:
        self.seen.append(sha)
        return ""


def test_the_sweep_starts_one_pr_further_each_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders = []
    for slot in (0, 1, 2, 3):
        monkeypatch.setattr(time, "time", lambda slot=slot: slot * SWEEP_SLOT_S + 5.0)
        gh = _ManyPulls()
        sweep(gh, REQUIRED, "run")
        orders.append(gh.seen)
    assert orders == [
        ["7", "8", "9"],
        ["8", "9", "7"],
        ["9", "7", "8"],
        ["7", "8", "9"],
    ]


# --- the client's timeouts and budget --------------------------------------------------------


def test_an_http_error_near_the_deadline_stays_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError("u", 502, "bad gateway", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    gh = GitHub("o/r", "t")
    gh.deadline = time.monotonic() + 0.5
    with pytest.raises(urllib.error.HTTPError):
        gh.call("GET", "/pulls/7")


class _Flaky:
    """``urlopen`` that answers ``codes`` as HTTP errors in turn, then 200 with ``[]``."""

    def __init__(self, *codes: int) -> None:
        self.codes = list(codes)
        self.sent: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.sent.append(f"{request.get_method()} {request.full_url}")
        if self.codes:
            code = self.codes.pop(0)
            raise urllib.error.HTTPError("u", code, "error", Message(), None)
        return io.BytesIO(b"[]")


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_a_transient_server_error_is_retried(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    flaky = _Flaky(code)  # run 39808716894: a 500, then 200 a minute later
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert GitHub("o/r", "t").open_pulls_with_head(SHA) == []
    assert len(flaky.sent) == 2


def test_a_server_error_that_persists_stays_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flaky = _Flaky(*[500] * (CALL_RETRIES + 1))
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    with pytest.raises(urllib.error.HTTPError):
        GitHub("o/r", "t").call("GET", "/pulls/7")
    assert len(flaky.sent) == CALL_RETRIES + 1


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = _Flaky(404)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    with pytest.raises(urllib.error.HTTPError):
        GitHub("o/r", "t").call("GET", "/pulls/7")
    assert len(flaky.sent) == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/issues/7/comments", {"body": "hi"}),  # a second comment
        (
            f"/actions/workflows/{REVIEW_WORKFLOW}/dispatches",
            {"ref": "main"},
        ),  # a second run
        ("/actions/runs/11/rerun", None),
    ],
)
def test_a_write_that_is_not_idempotent_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, path: str, body: Any
) -> None:
    flaky = _Flaky(502)  # a 502 may follow a write that landed
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    with pytest.raises(urllib.error.HTTPError):
        GitHub("o/r", "t").call("POST", path, body)
    assert len(flaky.sent) == 1


def test_idempotent_writes_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = GitHub("o/r", "t")
    for write in (
        lambda: gh.status(SHA, "pending", "automatic route: a problem"),
        lambda: gh.remove_label(7),
        lambda: gh.call(
            "POST", "https://api.github.com/graphql", {"query": "{viewer{login}}"}
        ),
    ):
        flaky = _Flaky(500)
        monkeypatch.setattr(urllib.request, "urlopen", flaky)
        write()
        assert len(flaky.sent) == 2


def test_a_retry_never_waits_past_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = _Flaky(500, 500)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    gh = GitHub("o/r", "t")
    gh.deadline = time.monotonic() + RETRY_BACKOFF_S / 2  # no time for the pause
    with pytest.raises(urllib.error.HTTPError):
        gh.call("GET", "/pulls/7")
    assert len(flaky.sent) == 1
    # A fail-closed write after the deadline still lands once, but is not retried: 150 + 10 < 180.
    gh.deadline = time.monotonic() - 1
    with pytest.raises(urllib.error.HTTPError):
        gh.status(SHA, "pending", "automatic route: a problem")
    assert len(flaky.sent) == 2


def test_a_graphql_read_is_bounded_by_the_deadline() -> None:
    gh = GitHub("o/r", "t")
    gh.deadline = time.monotonic() - 1
    with pytest.raises(BudgetSpent):
        gh.call("POST", "https://api.github.com/graphql", {"query": "{viewer{login}}"})


def test_a_status_write_lands_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, float]] = []

    class Answer:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def urlopen(request: urllib.request.Request, timeout: float) -> Answer:
        sent.append((request.get_method(), timeout))
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    gh = GitHub("o/r", "t")
    gh.deadline = time.monotonic() - 1  # reads are refused ...
    gh.status(
        SHA, "pending", "automatic route: a problem"
    )  # ... a fail-closed write is not
    assert sent == [("POST", 10.0)]
    with pytest.raises(BudgetSpent):  # success is held: it would land unverified
        gh.status(SHA, "success", AUTO_SUCCESS)
    assert sent == [("POST", 10.0)]


def test_a_clipped_call_that_times_out_is_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    gh = GitHub("o/r", "t")
    gh.deadline = time.monotonic() + 0.5  # the call is clipped and runs out
    with pytest.raises(BudgetSpent):
        gh.call("GET", "/pulls/7")
    gh.deadline = time.monotonic() + 60  # time left: a real failure stays one
    with pytest.raises(TimeoutError):
        gh.call("GET", "/pulls/7")


def test_no_api_call_is_made_after_the_deadline() -> None:
    gh = GitHub("o/r", "t")
    gh.deadline = time.monotonic() - 1
    with pytest.raises(BudgetSpent):
        gh.call("GET", "/pulls/7")


# --- the AI review (ADR-0005) ----------------------------------------------------------------


def _ledger(*findings: tuple[str, str]) -> str:
    """A reviewer ledger marker holding ``findings`` as (id, status)."""
    state = {"findings": [{"id": fid, "status": status} for fid, status in findings]}
    packed = base64.urlsafe_b64encode(
        zlib.compress(json.dumps(state).encode())
    ).decode()
    return f"<!-- opencode-review:state={packed.rstrip('=')} -->"


def test_a_clean_review_of_the_head_passes_the_gate() -> None:
    gh = FakeGitHub()
    assert _state(gh) == "success"
    assert gh.posted[0][2] == AUTO_SUCCESS
    assert gh.dispatched == []


def test_green_tier1_without_a_review_dispatches_one_and_waits() -> None:
    gh = FakeGitHub()
    gh.review_list, gh.review_run_list = [], []
    assert _reason(gh) == f"{AUTO_PENDING}{NO_REVIEW}; review requested"
    assert gh.dispatched == [(7, SHA)]


def test_no_review_is_dispatched_before_tier1_passes() -> None:
    gh = FakeGitHub()
    gh.review_list, gh.review_run_list = [], []
    gh.runs = [GREEN[0], {**GREEN[1], "status": "in_progress", "conclusion": None}]
    assert _state(gh) == "pending"
    assert gh.dispatched == []


def test_a_draft_gets_no_review() -> None:
    gh = FakeGitHub({"draft": True})
    gh.review_list, gh.review_run_list = [], []
    assert _state(gh) == "pending"
    assert gh.dispatched == []


def test_an_outsiders_pr_gets_no_review_and_waits_for_a_maintainer() -> None:
    gh = FakeGitHub({"author_association": "CONTRIBUTOR"})
    gh.review_list, gh.review_run_list = [], []
    assert _reason(gh) == f"{AUTO_PENDING}{UNTRUSTED}"
    assert gh.dispatched == []


def test_a_running_review_is_not_dispatched_again() -> None:
    gh = FakeGitHub()
    gh.review_list = []
    gh.review_run_list = [{**REVIEW_RUN, "status": "in_progress", "conclusion": None}]
    assert _reason(gh) == f"{AUTO_PENDING}{NO_REVIEW}; review running"
    assert gh.dispatched == []


def test_blocking_findings_keep_the_gate_pending() -> None:
    gh = FakeGitHub()
    body = f"{REVIEW_HEADING}\n\n**Verdict: ❌ 2 blocking findings.**\n\n{MARK}"
    gh.review_list = [{**REVIEW, "body": body}]
    assert "has blocking findings" in _reason(gh)


def test_the_verdict_counts_only_on_its_own_line() -> None:
    gh = FakeGitHub()
    body = f"{REVIEW_HEADING}\n\nSummary.\n\n{REVIEW_CLEAN}\n\n{MARK}"
    gh.review_list = [{**REVIEW, "body": body}]
    assert "has blocking findings" in _reason(gh)


def test_a_review_no_run_vouches_for_is_reviewed_again_forced() -> None:
    """#95 F6: a review from a nameless pre-#94 run is ignored, and a forced review replaces it."""
    gh = FakeGitHub()
    gh.review_run_list = []  # the run that posted it is named "OpenCode review of"
    assert _reason(gh) == f"{AUTO_PENDING}{UNATTRIBUTED}; review requested"
    assert gh.dispatched == [(7, SHA)] and gh.forced == [True]
    # a fresh review is not forced
    gh = FakeGitHub()
    gh.review_list, gh.review_run_list = [], []
    _reason(gh)
    assert gh.forced == [False]


def test_a_forced_review_that_left_no_review_is_re_run_by_the_sweep() -> None:
    """#95 F7: the forced run exists but started after the old review, and posted nothing."""
    gh = FakeGitHub()
    after = (
        "2026-10-20T04:30:00Z"  # created after REVIEWED_AT; it ended long before now
    )
    gh.review_run_list = [{**REVIEW_RUN, "created_at": after, "run_started_at": after}]
    gh.review_job_of = {
        11: [{**REVIEW_JOB, "started_at": after, "completed_at": after}]
    }
    auto(gh, 7, REQUIRED, "run", wait=False)
    assert gh.reran == ["/actions/runs/11/rerun"]
    assert gh.posted[-1][2] == f"{AUTO_PENDING}{UNATTRIBUTED}; review re-run (2/3)"
    assert gh.dispatched == []  # the run exists: it is re-run, not dispatched again


def test_a_review_of_another_commit_or_by_someone_else_does_not_count() -> None:
    gh = FakeGitHub()
    gh.review_list = [
        {**REVIEW, "body": CLEAN.replace(SHA, OLD)},
        {**REVIEW, "user": {"login": "dev"}},
    ]
    assert NO_REVIEW in _reason(gh)


def test_a_review_outside_mains_review_run_does_not_count() -> None:
    gh = FakeGitHub()
    gh.review_list = [{**REVIEW, "submitted_at": "2026-10-20T05:00:00Z"}]
    assert "did not come from main's review workflow" in _reason(gh)
    gh = FakeGitHub()
    gh.review_job_of = {11: [{**REVIEW_JOB, "conclusion": "failure"}]}
    assert "did not come from main's review workflow" in _reason(gh)
    gh = FakeGitHub()
    gh.review_job_of = {}  # a run with no review job yet
    assert "did not come from main's review workflow" in _reason(gh)


def test_review_jobs_are_read_only_when_they_can_decide() -> None:
    """A blocking review needs no job; a run that started after the review cannot have posted it."""
    gh = FakeGitHub()
    body = f"{REVIEW_HEADING}\n\n**Verdict: ❌ 1 blocking finding.**\n\n{MARK}"
    gh.review_list = [{**REVIEW, "body": body}]
    _reason(gh)
    assert gh.job_reads == []
    gh = FakeGitHub()
    later = {**REVIEW_RUN, "id": 12, "created_at": "2026-10-20T05:00:00Z"}
    gh.review_run_list = [later, dict(REVIEW_RUN)]
    assert _state(gh) == "success"
    assert gh.job_reads == [11]


def test_a_re_run_does_not_hide_the_review_an_earlier_attempt_posted() -> None:
    """#95 F5: the latest attempt's job started after the review; the first attempt's posted it."""
    gh = FakeGitHub()
    six = "2026-10-20T06:00:00Z"  # a re-run moves the run's run_started_at; created_at stays
    rerun = {**REVIEW_JOB, "started_at": six, "completed_at": "2026-10-20T06:01:00Z"}
    gh.review_run_list = [{**REVIEW_RUN, "run_attempt": 2, "run_started_at": six}]
    gh.review_job_of = {11: [dict(REVIEW_JOB), rerun]}
    assert _state(gh) == "success"


def test_a_review_counts_while_its_run_is_still_going() -> None:
    """#94: wake-gate re-judges the PR from inside the review run, which has not ended yet."""
    gh = FakeGitHub()
    gh.review_run_list = [
        {
            **REVIEW_RUN,
            "status": "in_progress",
            "conclusion": None,
            "updated_at": REVIEWED_AT,
        }
    ]
    assert _state(gh) == "success"
    assert gh.dispatched == []


def test_a_review_older_than_a_base_change_does_not_count() -> None:
    gh = FakeGitHub()
    gh.runs = [{**run, "started_at": "2026-10-20T05:00:00Z"} for run in GREEN]
    gh.base_changed = "2026-10-20T04:30:00Z"
    assert "predates the PR's base change" in _reason(gh)


def test_asset_only_prs_need_no_review_but_docs_do() -> None:
    gh = FakeGitHub()
    gh.review_list, gh.review_run_list = [], []
    gh.paths = ["docs/assets/room-flow.png"]
    assert _state(gh) == "success"
    gh = FakeGitHub()
    gh.review_list, gh.review_run_list = [], []
    gh.paths = ["docs/assets/room-flow.png", "docs/modules/teamup-api.md"]
    assert _state(gh) == "pending"
    assert gh.dispatched == [(7, SHA)]


POLICY = ci_gate.load_policy()


def _entry(
    name: str, status: str = "modified", patch: str = "", old: str = ""
) -> dict[str, Any]:
    entry = {"filename": name, "status": status, "patch": patch}
    if old:
        entry["previous_filename"] = old
    return entry


def test_layer_one_passes_ordinary_code() -> None:
    entries = [
        _entry("server/src/teamup_server/gateway.py"),
        _entry("tests/test_loop.py"),
    ]
    assert ci_gate.routing_problems(entries, POLICY) == []


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/pr.yml",
        "scripts/ci/gate.py",
        "AGENTS.md",
        "docs/protocols/README.md",
        "uv.lock",
        "client/pyproject.toml",
        "tests/test_boundaries.py",
        "docs/ci/README.md",
    ],
)
def test_layer_one_holds_protected_paths(path: str) -> None:
    [problem] = ci_gate.routing_problems([_entry(path)], POLICY)
    assert problem.startswith("layer 1: protected path") and "`ready`" in problem


def test_layer_one_holds_a_rename_out_of_a_protected_path() -> None:
    entry = _entry("docs/notes.md", "renamed", old="docs/protocols/README.md")
    assert ci_gate.routing_problems([entry], POLICY)


def test_layer_one_holds_deleted_tests_but_not_moved_ones() -> None:
    [problem] = ci_gate.routing_problems(
        [_entry("tests/test_wake.py", "removed")], POLICY
    )
    assert "deletes test `tests/test_wake.py`" in problem
    moved = _entry("server/tests/test_wake.py", "renamed", old="tests/test_wake.py")
    assert ci_gate.routing_problems([moved], POLICY) == []
    hidden = _entry("tests/wake_cases.py", "renamed", old="tests/test_wake.py")
    assert ci_gate.routing_problems([hidden], POLICY)


@pytest.mark.parametrize(
    "line",
    [
        "+@pytest.mark.skip(reason='flaky')",
        "+    pytest.xfail('later')",
        "+@pytest.mark.skipif(True, reason='x')",
        "+collect_ignore = ['test_x.py']",
    ],
)
def test_layer_one_holds_added_skips(line: str) -> None:
    patch = f"@@ -1 +1,2 @@\n def f():\n{line}"
    [problem] = ci_gate.routing_problems(
        [_entry("client/tests/test_client.py", patch=patch)], POLICY
    )
    assert "adds a skip or xfail" in problem


def test_layer_one_lets_skips_be_removed() -> None:
    patch = "@@ -1,2 +1 @@\n-@pytest.mark.skip(reason='flaky')\n def test_x():"
    assert (
        ci_gate.routing_problems([_entry("tests/test_loop.py", patch=patch)], POLICY)
        == []
    )


def test_layer_one_holds_huge_prs() -> None:
    entries = [_entry(f"server/src/f{i}.py") for i in range(300)]
    assert "300 files" in ci_gate.routing_problems(entries, POLICY)[0]


def test_a_protected_pr_still_gets_its_review_but_stays_pending() -> None:
    gh = FakeGitHub()
    gh.review_list, gh.review_run_list = [], []
    gh.paths = [".github/workflows/pr.yml"]
    assert _state(gh) == "pending"
    assert gh.dispatched == [
        (7, SHA)
    ]  # the owner reads the review before adding `ready`
    assert gh.posted[-1][2].startswith("automatic route: layer 1: protected path")


def test_open_findings_need_an_answer() -> None:
    def gh_with(comments: list[dict[str, Any]]) -> FakeGitHub:
        gh = FakeGitHub()
        ledger = _ledger(("F1", "open"), ("F2", "fixed"))
        gh.review_list = [{**REVIEW, "body": f"{CLEAN}\n{ledger}"}]
        gh.comment_list = comments
        return gh

    assert "1 unanswered OpenCode finding(s): F1" in _reason(gh_with([]))
    by_author = {
        "user": {"login": "dev"},
        "created_at": "2026-10-20T04:10:00Z",
        "body": "F1: done",
    }
    assert _state(gh_with([by_author])) == "success"
    by_other = {**by_author, "user": {"login": "x"}}
    assert _state(gh_with([by_other])) == "pending"


def test_a_resolved_finding_thread_answers_it() -> None:
    gh = FakeGitHub()
    gh.review_list = [{**REVIEW, "body": CLEAN + "\n" + _ledger(("F1", "open"))}]
    gh.thread_list = [
        {
            "resolved": True,
            "author": REVIEW_AUTHOR,
            "body": "<!-- opencode-review:finding=F1 --> x",
            "replies": [],
        }
    ]
    assert _state(gh) == "success"


def test_a_broken_ledger_keeps_the_gate_pending() -> None:
    gh = FakeGitHub()
    gh.review_list = [
        {**REVIEW, "body": CLEAN + "\n<!-- opencode-review:state=AAAA -->"}
    ]
    assert "ledger is missing or invalid" in _reason(gh)


def test_the_sweep_re_runs_a_review_run_that_left_no_review() -> None:
    gh = FakeGitHub()
    ended = ci_gate._epoch(REVIEW_RUN["updated_at"])
    assert ended is not None
    pull = gh.pr
    assert request_review(gh, pull, sweeping=True, now=ended + 60) == ""  # too recent
    assert (
        request_review(gh, pull, sweeping=False, now=ended + 900) == ""
    )  # events never re-run
    assert (
        request_review(gh, pull, sweeping=True, now=ended + 900)
        == "review re-run (2/3)"
    )
    assert gh.reran == ["/actions/runs/11/rerun"]
    gh.review_run_list = [{**REVIEW_RUN, "run_attempt": 3}]
    assert (
        request_review(gh, pull, sweeping=True, now=ended + 900) == ""
    )  # attempts spent
    gh.review_run_list = [{**REVIEW_RUN, "conclusion": "cancelled"}]
    assert (
        request_review(gh, pull, sweeping=True, now=ended + 900) == ""
    )  # a newer head won


def test_a_model_written_concluded_is_not_a_tier1_failure() -> None:
    gh = FakeGitHub()
    gh.review_list = [
        {**REVIEW, "body": f"{REVIEW_HEADING}\n\n`check` concluded badly\n\n{MARK}"}
    ]
    assert _state(gh) == "pending"  # posted, not left to revoke


# --- the maintainer override `ready` ---------------------------------------------------------


def _ready_event(sender: str = "maintainer") -> dict[str, Any]:
    event = _event("labeled", label=LABEL, labels=(LABEL,))
    event["sender"] = {"login": sender}
    return event


def test_a_maintainers_ready_passes_without_the_review() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.review_list, gh.review_run_list = [], []
    assert guard(gh, _ready_event(), REQUIRED, "run") is False
    assert gh.posted == [(SHA, "success", OVERRIDE_SUCCESS)]
    assert gh.removed == 0
    assert gh.dispatched == []


def test_ready_from_a_non_maintainer_is_removed() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.review_list, gh.review_run_list = [], []
    assert guard(gh, _ready_event("dev"), REQUIRED, "run") is False
    assert gh.removed == 1
    assert "only a maintainer" in gh.comments[0]
    assert all(state != "success" for _, state, _ in gh.posted)
    assert gh.dispatched == [(7, SHA)]  # back on the automatic route


def test_ready_does_not_skip_tier1() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.runs = [GREEN[0], {**GREEN[1], "conclusion": "failure"}]
    assert guard(gh, _ready_event(), REQUIRED, "run") is False
    assert gh.removed == 1
    assert "`hygiene` concluded failure" in gh.comments[0]
    assert all(state != "success" for _, state, _ in gh.posted)


def test_new_commits_or_edits_remove_ready() -> None:
    for action in ("synchronize", "edited"):
        gh = FakeGitHub()
        assert guard(gh, _event(action, labels=(LABEL,)), REQUIRED, "run") is False
        assert gh.posted == [(SHA, "pending", WAITING)]
        assert gh.removed == 1
        assert "so the label was removed" in gh.comments[0]


def test_auto_on_a_ready_pr_re_judges_the_override() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.review_list, gh.review_run_list = [], []
    assert _state(gh) == "success"
    assert gh.posted[0][2] == OVERRIDE_SUCCESS
    assert _state(FakeGitHub({"labels": [{"name": LABEL}], "draft": True})) == "pending"


def test_removing_ready_hands_back_to_auto(autos: list[int]) -> None:
    assert (
        guard(FakeGitHub(), _event("unlabeled", label=LABEL), REQUIRED, "run") is False
    )
    assert autos == [7]


def test_the_sweep_leaves_a_ready_pr_alone() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.gates[SHA] = AUTO_SUCCESS
    sweep(gh, REQUIRED, "run")
    assert gh.posted == []


# --- layer-1 statuses (informational; merge-gate alone decides) --------------------------------

ROUTE, PROTECTED, LIMITS = (
    "1 · PR Routing",
    "1 · Protected Changes",
    "1 · Automation Limits",
)


def _l1(gh: FakeGitHub) -> dict[str, tuple[str, str]]:
    auto(gh, 7, REQUIRED, "run")
    return gh.layer1


def test_a_plain_pr_reports_the_automatic_route() -> None:
    gh = FakeGitHub()
    assert _l1(gh) == {
        ROUTE: ("success", "automatic route"),
        PROTECTED: ("success", "no protected path, deleted test or new skip/xfail"),
        LIMITS: ("success", "within the automation limits"),
    }
    assert {u for _c, u in gh.layer1_posts} == {"run"}


def test_a_protected_path_fails_the_status_and_names_the_path() -> None:
    gh = FakeGitHub()
    gh.paths = [".github/ci/policy.json", "src/a.py"]
    got = _l1(gh)
    assert got[PROTECTED][0] == "failure"
    assert ".github/ci/policy.json" in got[PROTECTED][1]
    assert got[ROUTE][1].startswith("waits for the owner's `ready`: protected path")
    assert got[LIMITS][0] == "success"
    assert all(len(d) <= 140 for _s, d in got.values())


def test_a_deleted_test_and_an_added_skip_fail_the_status() -> None:
    gh = FakeGitHub()
    gh.entries = [{"filename": "tests/ci/test_x.py", "status": "removed"}]
    assert _l1(gh)[PROTECTED] == ("failure", "deletes test `tests/ci/test_x.py`")
    gh = FakeGitHub()
    patch = "@@\n+@pytest.mark.skip\n"
    gh.entries = [
        {"filename": "tests/ci/test_y.py", "status": "modified", "patch": patch}
    ]
    got = _l1(gh)[PROTECTED]
    assert got[0] == "failure" and "tests/ci/test_y.py" in got[1]


def test_too_many_files_fail_the_limits_not_the_protected_status() -> None:
    gh = FakeGitHub()
    gh.entries = [{"filename": f"src/f{i}.py", "status": "added"} for i in range(300)]
    got = _l1(gh)
    assert got[LIMITS][0] == "failure" and "300 files" in got[LIMITS][1]
    assert got[PROTECTED][0] == "success"
    assert "waits for the owner's `ready`" in got[ROUTE][1]


def test_an_untrusted_author_fails_the_limits() -> None:
    gh = FakeGitHub({"author_association": "CONTRIBUTOR"})
    assert _l1(gh)[LIMITS] == (
        "failure",
        "the author is not trusted, so no review is dispatched",
    )


def test_an_untrusted_author_of_a_docs_only_pr_is_within_the_limits() -> None:
    gh = FakeGitHub({"author_association": "CONTRIBUTOR"})
    gh.paths = ["docs/assets/a.png"]
    assert _l1(gh)[LIMITS][0] == "success"


def test_exhausted_review_reruns_fail_the_limits() -> None:
    gh = FakeGitHub()
    gh.review_list = []
    gh.review_run_list = [{**REVIEW_RUN, "run_attempt": ci_gate.REVIEW_RERUNS}]
    assert _l1(gh)[LIMITS][0] == "failure"
    assert "re-ran" in gh.layer1[LIMITS][1]
    gh = FakeGitHub()
    gh.review_list = []
    gh.review_run_list = [{**REVIEW_RUN, "run_attempt": 1}]
    assert _l1(gh)[LIMITS][0] == "success"


def test_a_draft_reports_layer_two_only() -> None:
    gh = FakeGitHub({"draft": True})
    assert _l1(gh)[ROUTE] == ("success", "draft: layer 2 only")


def test_a_valid_ready_passes_the_protected_status() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.paths = [".github/ci/policy.json"]
    gh.labeler = "maintainer"
    got = _l1(gh)
    assert got[PROTECTED][0] == "success"
    assert got[PROTECTED][1].startswith("passed by `ready`")
    assert got[ROUTE] == ("success", "owner route: `ready` accepted")


def test_a_ready_from_a_non_maintainer_does_not_pass_it() -> None:
    gh = FakeGitHub({"labels": [{"name": LABEL}]})
    gh.paths = [".github/ci/policy.json"]
    gh.labeler = "dev"
    assert _l1(gh)[PROTECTED][0] == "failure"


def test_unchanged_statuses_are_not_posted_again() -> None:
    gh = FakeGitHub()
    auto(gh, 7, REQUIRED, "run")
    first = len(gh.layer1_posts)
    assert first == 3
    auto(gh, 7, REQUIRED, "run")
    assert len(gh.layer1_posts) == first


def test_a_changed_verdict_is_posted_again_and_the_gate_is_untouched() -> None:
    plain, held = FakeGitHub(), FakeGitHub()
    held.paths = ["scripts/ci/gate.py", ".github/ci/policy.json"]
    auto(plain, 7, REQUIRED, "run")
    auto(held, 7, REQUIRED, "run")
    assert plain.posted[-1][1] == "success"
    assert held.posted[-1][1] == "pending"
    assert held.posted[-1][2].startswith(AUTO_PENDING)
    held.paths = ["src/a.py"]
    auto(held, 7, REQUIRED, "run")
    assert held.layer1[PROTECTED][0] == "success"
    assert held.posted[-1][1] == "success"


def test_the_guard_reports_layer_one_on_a_new_head() -> None:
    gh = FakeGitHub()
    gh.paths = [".github/ci/policy.json"]
    guard(gh, _event("synchronize"), REQUIRED, "run")
    assert [p[1] for p in gh.posted] == ["pending"]
    assert gh.layer1[PROTECTED][0] == "failure"


def test_a_failing_report_never_stops_the_gate() -> None:
    class Broken(FakeGitHub):
        def status_of(self, sha: str, context: str) -> tuple[str, str]:
            raise RuntimeError("statuses unreadable")

    gh = Broken()
    assert _state(gh) in {"success", "pending"}
    assert gh.layer1 == {}
