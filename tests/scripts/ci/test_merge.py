"""The merger (scripts/ci_merge.py; ported from PPMA)."""

from __future__ import annotations

import io
import urllib.error
from email.message import Message
from typing import Any

import pytest

from scripts.ci import merge as ci_merge
from scripts.ci.gate import HOLD, GitHub
from scripts.ci.merge import Pull, Step, apply, facts, plan


@pytest.fixture(autouse=True)
def _required_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fakes name tier 1 ``check`` and ``hygiene``; TeamUp's policy names its own layers."""
    monkeypatch.setenv("REQUIRED_CHECKS", "check,hygiene")


REQUIRED = ["check", "hygiene"]


def _pull(
    number: int,
    *,
    gate: str = "success",
    labels: tuple[str, ...] = (),
    draft: bool = False,
    conflicts: bool = False,
) -> Pull:
    return Pull(
        number,
        f"{number:040x}",
        frozenset(labels),
        draft,
        gate,
        conflicts=conflicts,
    )


def _sha(number: int) -> str:
    return f"{number:040x}"


def _actions(steps: list[Step]) -> list[tuple[str, int]]:
    return [(step.action, step.number) for step in steps]


def test_every_green_pr_merges_oldest_first() -> None:
    """No up-to-date rule: main moving does not make a green gate stale."""
    steps = plan([_pull(9), _pull(5), _pull(7), _pull(6, gate="pending")])
    assert _actions(steps) == [("merge", 5), ("merge", 7), ("merge", 9)]


@pytest.mark.parametrize(
    "pull",
    [
        _pull(5, draft=True),
        _pull(5, labels=(HOLD,)),
        _pull(5, gate="pending"),
        _pull(5, gate="failure"),
        _pull(5, gate="missing"),
    ],
)
def test_nothing_happens_to_a_pr_that_may_not_merge(pull: Pull) -> None:
    assert plan([pull]) == []


def test_a_conflicting_pr_is_reported_never_merged() -> None:
    assert _actions(plan([_pull(5, conflicts=True)])) == [("conflict", 5)]
    assert _actions(plan([_pull(6, gate="pending", conflicts=True)])) == [
        ("conflict", 6)
    ]
    assert plan([_pull(7, conflicts=True, draft=True)]) == []
    assert plan([_pull(7, conflicts=True, labels=(HOLD,))]) == []


class FakeGitHub(GitHub):
    """Canned open PRs; records every write."""

    def __init__(
        self,
        pulls: list[dict[str, Any]],
        *,
        refuse_merge: bytes = b"",
        refuse_status: bool = False,
    ) -> None:
        super().__init__("o/r", "t")
        self.pulls = {int(p["number"]): p for p in pulls}
        self.refuse_merge = refuse_merge  # the 405's body for every merge, when set
        self.refuse_status = (
            refuse_status  # a 403 on commit statuses, like the merge token
        )
        self.comments: dict[int, list[str]] = {}
        self.writes: list[tuple[str, str, Any]] = []

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if method != "GET":
            self.writes.append((method, path, body))
            if path.endswith("/merge") and self.refuse_merge:
                body_ = io.BytesIO(self.refuse_merge)
                raise urllib.error.HTTPError(path, 405, "no", Message(), body_)
            if path.endswith("/comments"):
                number = int(path.split("/")[2])
                self.comments.setdefault(number, []).append(body["body"])
            return None
        if path.startswith("/pulls?state=open"):
            return list(self.pulls.values()) if "page=1" in path else []
        if path.startswith("/commits/") and "/check-runs" in path:
            sha = path.split("/")[2]
            pull = next(p for p in self.pulls.values() if p["head"]["sha"] == sha)
            checks = pull.get("checks", {"check": "success", "hygiene": "success"})
            return {
                "check_runs": [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": state,
                        "started_at": "t",
                    }
                    if state != "in_progress"
                    else {"name": name, "status": "in_progress", "started_at": "t"}
                    for name, state in checks.items()
                ]
            }
        if path.startswith("/actions/runs?head_sha="):
            return {"workflow_runs": []}  # no workflow known: every check run counts
        if path.startswith("/commits/") and "/status?" in path:
            if self.refuse_status:
                raise urllib.error.HTTPError(path, 403, "Forbidden", Message(), None)
            sha = path.split("/")[2]
            pull = next(p for p in self.pulls.values() if p["head"]["sha"] == sha)
            default = [{"context": "merge-gate", "state": pull["gate"]}]
            statuses = pull.get("statuses", default)
            return {"statuses": statuses}
        if path.startswith("/pulls/"):
            pull = self.pulls[int(path.split("/")[2])]
            return {**pull, "mergeable": not pull.get("conflicts")}
        if path.startswith("/issues/") and "/comments" in path:
            number = int(path.split("/")[2])
            return (
                [{"body": b} for b in self.comments.get(number, [])]
                if "page=1" in path
                else []
            )
        if path.startswith("/issues?state=open&labels="):
            return list(getattr(self, "freeze", [])) if "page=1" in path else []
        raise AssertionError(f"unexpected GET {path}")


def _api_pull(number: int, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "created_at": "2026-10-15T00:00:00Z",
        "draft": False,
        "labels": [{"name": n} for n in extra.pop("labels", ())],
        "head": {"sha": f"{number:040x}"},
        "base": {"sha": "f" * 40},
        "gate": "success",
        **extra,
    }


def test_facts_read_the_gate_whatever_the_pr_changes() -> None:
    gh = FakeGitHub(
        [
            _api_pull(5),
            _api_pull(6, files=[".github/workflows/x.yml"]),
            _api_pull(7, gate="pending"),
            _api_pull(8, draft=True),
        ]
    )
    got = {p.number: p for p in facts(gh, REQUIRED)}
    assert got[5].gate == "success"
    assert got[6].blocked() is None  # a CI change needs no person
    assert got[7].gate == "pending"
    assert got[8].draft
    assert gh.writes == []


def test_a_green_gate_counts_only_while_tier1_still_passes() -> None:
    """A check running again (a re-run, hygiene after an edit) holds the merge until it passes."""
    gh = FakeGitHub(
        [
            _api_pull(5, checks={"check": "success", "hygiene": "in_progress"}),
            _api_pull(6, checks={"check": "failure", "hygiene": "success"}),
            _api_pull(7),
        ]
    )
    got = {p.number: p.gate for p in facts(gh, REQUIRED)}
    assert got == {5: "pending", 6: "pending", 7: "success"}
    reader = FakeGitHub([_api_pull(5), _api_pull(6), _api_pull(7)])
    got = {p.number: p.gate for p in facts(gh, REQUIRED, reader)}
    assert got == {
        5: "success",
        6: "success",
        7: "success",
    }  # check runs come from ``checks``
    assert _actions(plan(facts(gh, REQUIRED))) == [("merge", 7)]


def test_apply_merges_pinned_to_the_head() -> None:
    gh = FakeGitHub([_api_pull(5)])
    apply(gh, [Step("merge", 5, _sha(5), "green")], "run")
    assert gh.writes == [
        ("PUT", "/pulls/5/merge", {"merge_method": "merge", "sha": _sha(5)}),
    ]


def test_a_refused_merge_is_commented_once_per_head_not_raised() -> None:
    gh = FakeGitHub([_api_pull(5)], refuse_merge=b"workflows")
    apply(gh, [Step("merge", 5, _sha(5), "green")], "run")
    apply(gh, [Step("merge", 5, _sha(5), "green")], "run")
    assert len(gh.comments[5]) == 1
    assert "could not merge" in gh.comments[5][0]
    assert "workflows" in gh.comments[5][0]


def test_a_pr_an_earlier_merge_put_in_conflict_is_skipped_quietly() -> None:
    """The next run reads the conflict and says so; one run never stops at a refused merge."""
    gh = FakeGitHub(
        [_api_pull(5), _api_pull(6)], refuse_merge=b"Pull Request is not mergeable"
    )
    apply(
        gh,
        [Step("merge", 5, _sha(5), "green"), Step("merge", 6, _sha(6), "green")],
        "run",
    )
    assert [w[1] for w in gh.writes] == ["/pulls/5/merge", "/pulls/6/merge"]
    assert gh.comments == {}


def test_facts_see_a_conflict_on_a_pending_pr_and_it_is_commented_once() -> None:
    gh = FakeGitHub([_api_pull(5, gate="pending", conflicts=True)])
    (pull,) = facts(gh, REQUIRED)
    assert pull.conflicts
    steps = plan([pull])
    apply(gh, steps, "run")
    apply(gh, steps, "run")
    assert len(gh.comments[5]) == 1
    assert "conflicts with `main`" in gh.comments[5][0]


def test_dry_run_prints_the_steps_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_api_pull(5), _api_pull(6)])
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would merge #5" in out
    assert "would merge #6" in out
    assert gh.writes == []


def test_a_missing_hold_label_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_api_pull(5)])
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main([]) == 1
    assert "no `pr/hold` (or legacy) label" in capsys.readouterr().out
    assert gh.writes == []


def test_the_newest_gate_status_wins() -> None:
    """A stale success never outranks a later pending."""
    statuses = [
        {
            "context": "merge-gate",
            "state": "success",
            "updated_at": "2026-10-17T10:00:00Z",
        },
        {
            "context": "merge-gate",
            "state": "pending",
            "updated_at": "2026-10-17T11:00:00Z",
        },
    ]
    gh = FakeGitHub([_api_pull(5, statuses=statuses)])
    assert ci_merge.gate_state(gh, _sha(5)) == "pending"
    assert (
        ci_merge.gate_state(FakeGitHub([_api_pull(6, statuses=[])]), _sha(6))
        == "missing"
    )


@pytest.mark.parametrize(
    ("pull", "why"),
    [
        (_api_pull(5, labels=(HOLD,)), "`pr/hold` added"),
        (_api_pull(5, draft=True), "a draft now"),
        (_api_pull(5, gate="pending"), "`merge-gate` is pending now"),
        (_api_pull(5, state="closed"), "no longer open"),
        (
            _api_pull(5, base={"sha": "f" * 40, "ref": "release"}),
            "its base is no longer `main`",
        ),
    ],
)
def test_each_merge_rereads_its_pr_first(pull: dict[str, Any], why: str) -> None:
    """One run merges several PRs, so a hold added meanwhile still stops a later one."""
    gh = FakeGitHub([pull])
    assert ci_merge.still_mergeable(gh, 5, _sha(5)) == why
    apply(gh, [Step("merge", 5, _sha(5), "green")], "run")
    assert gh.writes == []


def test_the_gate_status_is_read_with_the_checks_token() -> None:
    """The merge token has no Commit statuses permission (403): ``checks`` reads the gate (#112)."""
    gh = FakeGitHub([_api_pull(5)], refuse_status=True)
    reader = FakeGitHub([_api_pull(5)])
    assert _actions(plan(facts(gh, REQUIRED, reader))) == [("merge", 5)]
    assert (
        ci_merge.still_mergeable(gh, 5, _sha(5), required=REQUIRED, checks=reader)
        is None
    )
    apply(
        gh,
        [Step("merge", 5, _sha(5), "green")],
        "run",
        required=REQUIRED,
        checks=reader,
    )
    assert [w[1] for w in gh.writes] == ["/pulls/5/merge"]


def test_a_pr_pushed_since_the_plan_is_not_merged() -> None:
    gh = FakeGitHub([_api_pull(5)])
    assert ci_merge.still_mergeable(gh, 5, "e" * 40) == "pushed since"


def test_a_tier1_run_failing_since_the_plan_stops_the_merge() -> None:
    """The re-check reads the tier-1 runs, as facts does."""
    gh = FakeGitHub([_api_pull(5, checks={"check": "failure", "hygiene": "success"})])
    apply(gh, [Step("merge", 5, _sha(5), "green")], "run", required=REQUIRED)
    assert gh.writes == []
    assert ci_merge.still_mergeable(gh, 5, _sha(5), required=REQUIRED) == (
        "`check` concluded failure"
    )


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("temporary connection reset"),
        urllib.error.HTTPError("url", 503, "unavailable", Message(), None),
        RuntimeError("incomplete review threads"),
    ],
)
def test_merge_recheck_failure_skips_only_affected_pr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """A failed API read must not merge that PR or abort later merges."""
    gh = FakeGitHub([_api_pull(5), _api_pull(6)])
    original = ci_merge.still_mergeable

    def recheck(
        client: GitHub,
        number: int,
        sha: str,
        *,
        required: list[str] | None = None,
        checks: GitHub | None = None,
        repairs: tuple[int, ...] = (),
        freezes: tuple[int, ...] = (),
    ) -> str | None:
        if number == 5:
            raise error
        return original(
            client,
            number,
            sha,
            required=None,
            checks=checks,
            repairs=repairs,
            freezes=freezes,
        )

    monkeypatch.setattr(ci_merge, "still_mergeable", recheck)
    steps = [Step("merge", n, _sha(n), "green") for n in (5, 6)]
    with pytest.raises(RuntimeError, match=r"eligibility re-check failed for #5"):
        apply(gh, steps, "run", required=REQUIRED)
    assert [path for method, path, _ in gh.writes if method == "PUT"] == [
        "/pulls/6/merge"
    ]
    assert "merge: #5 skipped (eligibility re-check failed:" in capsys.readouterr().out


def test_merge_recheck_does_not_treat_unknown_exceptions_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conservative boundary must never call PUT when the read failed."""
    gh = FakeGitHub([_api_pull(5)])

    def broke(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("API response shape changed")

    monkeypatch.setattr(ci_merge, "still_mergeable", broke)
    with pytest.raises(RuntimeError, match=r"eligibility re-check failed for #5"):
        apply(gh, [Step("merge", 5, _sha(5), "green")], "run", required=REQUIRED)
    assert gh.writes == []


def test_global_auth_failure_never_looks_like_a_green_merge_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate each candidate, but fail the overall job after the sweep."""
    gh = FakeGitHub([_api_pull(5), _api_pull(6)])

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.HTTPError("url", 403, "bad credentials", Message(), None)

    monkeypatch.setattr(ci_merge, "still_mergeable", forbidden)
    with pytest.raises(RuntimeError, match=r"eligibility re-check failed for #5, #6"):
        apply(
            gh,
            [Step("merge", n, _sha(n), "green") for n in (5, 6)],
            "run",
            required=REQUIRED,
        )
    assert gh.writes == []


def _label_gh(status: int | None, *, legacy_only: bool = False) -> GitHub:
    class Gh(GitHub):
        def call(self, method: str, path: str, body: Any = None) -> Any:
            assert method == "GET"
            code = 404 if legacy_only and path == "/labels/pr%2Fhold" else status
            if code is not None:
                raise urllib.error.HTTPError(
                    path, code, "x", Message(), io.BytesIO(b"")
                )
            return {"name": path}

    return Gh("o/r", "t")


def test_the_hold_label_check_reads_the_label_the_merger_stops_on() -> None:
    assert ci_merge.hold_label_exists(_label_gh(None)) is True
    assert ci_merge.hold_label_exists(_label_gh(404)) is False
    assert ci_merge.hold_label_exists(_label_gh(None, legacy_only=True)) is True
    with pytest.raises(urllib.error.HTTPError):
        ci_merge.hold_label_exists(_label_gh(500))


def test_a_freeze_lets_only_the_owners_ready_merge() -> None:
    auto = Pull(5, "a" * 40, frozenset(), False, "success")
    owner = Pull(6, "b" * 40, frozenset(), False, "success", override=True)
    assert [step.number for step in plan([auto, owner])] == [5, 6]
    assert [step.number for step in plan([auto, owner], frozen=True)] == [6]


def test_freeze_refs_count_refs_but_not_closing_keywords() -> None:
    pull = {
        "number": 9,
        "title": "Fix the snapshot (refs #40)",
        "body": "Part of o/r#41. Fixes #42, closes: #43. See other/x#44, mentions #45, #9.",
    }
    assert ci_merge.freeze_refs(pull, "o/r") == (
        frozenset({40, 41}),
        frozenset({42, 43}),
    )


def test_a_pr_that_would_close_the_freeze_issue_does_not_merge_as_a_repair() -> None:
    closing = Pull(
        5,
        "a" * 40,
        frozenset(),
        False,
        "success",
        refs=frozenset({40}),
        closes=frozenset({40}),
    )
    assert plan([closing], frozen=True, freeze_issues=frozenset({40})) == []


def test_a_repair_is_re_checked_against_the_current_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for body, why in (
        ("Refs #40", None),
        ("No reference any more", "it no longer refers to freeze #40"),
        ("Refs #40, fixes #40", "it would close freeze #40 on merge"),
        ("Refs #40, closes #41", "it would close freeze #41 on merge"),
        ("Refs #40, closes #3", None),
    ):
        gh = FakeGitHub([{**_api_pull(5), "body": body}])
        got = ci_merge.still_mergeable(gh, 5, _sha(5), repairs=(40,), freezes=(40, 41))
        assert got == why, body


def test_a_freeze_also_lets_a_green_pr_that_refers_to_it_merge() -> None:
    fix = Pull(5, "a" * 40, frozenset(), False, "success", refs=frozenset({40}))
    other = Pull(6, "b" * 40, frozenset(), False, "success", refs=frozenset({3}))
    red = Pull(7, "c" * 40, frozenset(), False, "pending", refs=frozenset({40}))
    steps = plan([fix, other, red], frozen=True, freeze_issues=frozenset({40}))
    assert [(s.number, s.reason, s.repairs, s.freezes) for s in steps] == [
        (5, "`merge-gate` green; repairs freeze #40", (40,), (40,))
    ]
    # Not frozen: the reference changes nothing.
    assert [s.number for s in plan([fix, other, red])] == [5, 6]


def test_the_merger_merges_a_pr_that_refers_to_the_freeze_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([{**_api_pull(5), "body": "Refs #40"}, _api_pull(6)])
    gh.freeze = [{"title": "CI freeze: main is red", "number": 40}]
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "or that refer to #40 merge" in out
    assert "would merge #5" in out and "repairs freeze #40" in out
    assert "would merge #6" not in out


def test_the_merger_reads_the_freeze_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_api_pull(5)])
    gh.freeze = [{"title": "CI freeze: nightly is red", "number": 40}]
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "frozen (CI freeze: nightly is red)" in out
    assert "would merge #5" not in out


@pytest.mark.usefixtures("owner_review_off")
def test_with_require_ready_off_a_freeze_does_not_stop_merges(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_api_pull(5)])
    gh.freeze = [{"title": "CI freeze: main is red", "number": 40}]
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "`require_ready` is off: merging as usual" in out
    assert "would merge #5" in out


@pytest.mark.usefixtures("owner_review_off")
def test_with_require_ready_off_a_pr_that_would_close_a_freeze_still_waits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([{**_api_pull(5), "body": "Fixes #40"}, _api_pull(6)])
    gh.freeze = [{"title": "CI freeze: main is red", "number": 40}]
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would merge #5" not in out
    assert "would merge #6" in out


def test_unfrozen_plan_skips_closers_and_rechecks_the_open_freezes() -> None:
    closer = Pull(5, "a" * 40, frozenset(), False, "success", closes=frozenset({40}))
    other = Pull(6, "b" * 40, frozenset(), False, "success")
    steps = plan([closer, other], frozen=False, freeze_issues=frozenset({40}))
    assert [(s.number, s.freezes) for s in steps] == [(6, (40,))]


def test_any_merge_is_rechecked_against_open_freezes() -> None:
    for body, why in (
        ("Fixes #40", "it would close freeze #40 on merge"),
        ("Refs #40", None),
    ):
        gh = FakeGitHub([{**_api_pull(5), "body": body}])
        assert ci_merge.still_mergeable(gh, 5, _sha(5), freezes=(40,)) == why, body


@pytest.mark.usefixtures("owner_review_off")
def test_with_require_ready_off_a_failed_freeze_read_does_not_stop_merges(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_api_pull(5)])
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)

    def broken(_gh: object, _label: str) -> list[tuple[int, str]]:
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(ci_merge, "freezes", broken)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert ci_merge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "could not read" in out and "would merge #5" in out


def test_with_require_ready_on_a_failed_freeze_read_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = FakeGitHub([_api_pull(5)])
    monkeypatch.setattr(ci_merge, "GitHub", lambda repo, token: gh)
    monkeypatch.setattr(ci_merge, "hold_label_exists", lambda _gh: True)

    def broken(_gh: object, _label: str) -> list[tuple[int, str]]:
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(ci_merge, "freezes", broken)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    with pytest.raises(RuntimeError):
        ci_merge.main(["--dry-run"])
