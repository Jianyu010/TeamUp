"""The labeling standard's automation (scripts/ci/labels.py, docs/ci/labeling.md)."""

from __future__ import annotations

import re
import urllib.error
from typing import Any

import pytest

from scripts.ci import freeze, gate, labels, release

MANIFEST = labels.read_manifest()
DOC = labels.MANIFEST.read_text(encoding="utf-8")


class FakeGitHub(gate.GitHub):
    """One issue (#7) or PR (#9) and a label list; records every write."""

    def __init__(
        self,
        names: list[str] | None = None,
        *,
        author: dict[str, Any] | None = None,
        assignees: list[str] | None = None,
        comments: list[dict[str, Any]] | None = None,
        repo_labels: list[dict[str, Any]] | None = None,
        linked: dict[int, list[str]] | None = None,
    ) -> None:
        super().__init__("o/r", "t")
        self.names = list(names or [])
        self.author = author or {"login": "alice", "type": "User"}
        self.assignees = assignees or []
        self.comments = comments or []
        self.repo_labels = repo_labels or []
        self.linked = linked or {}
        self.writes: list[tuple[str, str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if method != "GET":
            self.writes.append((method, path, body))
        if method == "GET" and path == "/issues/7":
            return {
                "number": 7,
                "state": "open",
                "user": self.author,
                "assignees": [{"login": a} for a in self.assignees],
                "labels": [{"name": n} for n in self.names],
            }
        if method == "GET" and path.startswith("/issues/") and path[8:].isdigit():
            number = int(path[8:])
            found: dict[str, Any] = {
                "number": number,
                "labels": [{"name": n} for n in self.linked.get(number, [])],
            }
            if number in getattr(self, "prs", ()):
                found["pull_request"] = {}
            return found
        if (
            method == "POST"
            and path.endswith("/labels")
            and path.startswith("/issues/")
        ):
            self.names += [n for n in body["labels"] if n not in self.names]
        if method == "DELETE" and "/labels/" in path:
            name = path.rsplit("/", 1)[1].replace("%2F", "/")
            self.names = [n for n in self.names if n != name]
        return None

    def pages(
        self, path: str, key: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        if path.endswith("/comments"):
            return self.comments
        if path == "/labels":
            return self.repo_labels
        return []

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        return self.checks

    def review_runs(self, sha: str) -> list[dict[str, Any]]:
        return self.reviews


# ---- manifest -----------------------------------------------------------------------------


def test_manifest_lists_every_axis_and_valid_colors() -> None:
    for axis in (
        "type",
        "source",
        "priority",
        "size",
        "agent",
        "status",
        "area",
        "gate",
    ):
        assert any(name.startswith(f"{axis}/") for name in MANIFEST), axis
    for name, (color, description) in MANIFEST.items():
        assert re.fullmatch(r"[0-9A-F]{6}", color), name
        assert description and len(description) <= 100, name
        assert name == name.lower() and " " not in name, name


def test_every_label_the_doc_names_is_in_the_manifest() -> None:
    body = DOC.split(labels.MANIFEST_HEADING)[0]
    named = set(
        re.findall(
            r"`((?:type|source|priority|size|agent|status|area|gate|pr)/[a-z-]+)`", body
        )
    )
    named -= {"gate/<检查>-failed"}
    assert named <= set(MANIFEST), sorted(named - set(MANIFEST))


def test_gate_labels_cover_the_required_checks() -> None:
    assert set(labels.GATE_LABELS) == set(gate.load_policy()["required_checks"])
    for label in [*labels.GATE_LABELS.values(), labels.REVIEW_LABEL]:
        assert label in MANIFEST


def test_labels_the_automation_writes_are_in_the_manifest() -> None:
    written = {
        labels.TRIAGE,
        labels.HUMAN_ONLY,
        labels.REVIEW_NEEDED,
        labels.AGENT_OK,
        labels.NO_ISSUE,
        gate.LABEL,
        gate.HOLD,
        gate.load_policy()["freeze_label"],
        *freeze.REPORT_ONLY.values(),
        *(n for names in freeze.AXIS_LABELS.values() for n in names),
    }
    assert written <= set(MANIFEST), sorted(written - set(MANIFEST))


@pytest.mark.parametrize("source", sorted(freeze.AXIS_LABELS))
def test_freeze_issues_meet_the_standard(source: str) -> None:
    assert labels.meets_lint(freeze.AXIS_LABELS[source])


# ---- lint ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("names", "ok"),
    [
        (["type/bug", "source/user", "priority/low"], True),
        (
            ["type/bug", "type/docs", "source/user", "source/nightly", "priority/high"],
            True,
        ),
        (["type/bug", "source/user"], False),
        (["type/bug", "source/user", "priority/low", "priority/high"], False),
        (["source/user", "priority/low"], False),
        (["type/bug", "source/user", "priority/low", "size/s", "size/m"], False),
        (
            ["type/bug", "source/user", "priority/low", "agent/ok", "agent/human-only"],
            False,
        ),
    ],
)
def test_cardinality(names: list[str], ok: bool) -> None:
    assert labels.meets_lint(names) is ok


def test_lint_adds_triage_and_one_comment_then_clears_it() -> None:
    gh = FakeGitHub(["type/bug"])
    assert labels.lint(gh, 7).startswith("triage")
    assert labels.TRIAGE in gh.names
    posts = [w for w in gh.writes if w[1] == "/issues/7/comments"]
    assert len(posts) == 1 and posts[0][2]["body"].startswith(labels.LINT_MARKER)
    assert "source/*" in posts[0][2]["body"]

    gh.names += ["source/user", "priority/medium"]
    gh.comments = [
        {"id": 5, "body": posts[0][2]["body"], "user": {"login": labels.BOT_LOGIN}}
    ]
    gh.writes.clear()
    assert labels.lint(gh, 7) == "labels complete"
    assert labels.TRIAGE not in gh.names
    assert [w[0] for w in gh.writes] == [
        "DELETE",
        "PATCH",
    ]  # edited in place, not a new comment


def test_lint_pass_without_a_prior_comment_stays_silent() -> None:
    gh = FakeGitHub(["type/bug", "source/user", "priority/low"])
    assert labels.lint(gh, 7) == "labels complete"
    assert gh.writes == []


def test_a_look_alike_comment_by_someone_else_is_not_taken_for_ours() -> None:
    gh = FakeGitHub(
        ["type/bug"],
        comments=[{"id": 1, "body": labels.LINT_MARKER, "user": {"login": "mallory"}}],
    )
    labels.lint(gh, 7)
    assert not any(w[0] == "PATCH" for w in gh.writes)


# ---- triage -------------------------------------------------------------------------------


def test_triage_marks_a_human_issue_assigns_the_owner_and_mentions_them() -> None:
    gh = FakeGitHub()
    assert labels.triage(gh, 7, "Jianyu010") == "triaged"
    assert {labels.HUMAN_ONLY, labels.REVIEW_NEEDED} <= set(gh.names)
    assert ("POST", "/issues/7/assignees", {"assignees": ["Jianyu010"]}) in gh.writes
    comment = next(w for w in gh.writes if w[1] == "/issues/7/comments")
    assert "@Jianyu010" in comment[2]["body"]


def test_triage_keeps_an_existing_assignee_and_repairs_a_half_pair() -> None:
    gh = FakeGitHub([labels.HUMAN_ONLY], assignees=["bob"])
    labels.triage(gh, 7, "Jianyu010")
    assert labels.REVIEW_NEEDED in gh.names
    assert ("POST", "/issues/7/labels", {"labels": [labels.REVIEW_NEEDED]}) in gh.writes
    assert not any(w[1].endswith("/assignees") for w in gh.writes)


@pytest.mark.parametrize(
    ("author", "names"),
    [
        ({"login": "github-actions[bot]", "type": "Bot"}, []),
        ({"login": "dependabot[bot]", "type": "Bot"}, []),
        ({"login": "alice", "type": "User"}, ["agent/ok"]),
    ],
)
def test_triage_leaves_bots_and_granted_issues_alone(
    author: dict[str, Any], names: list[str]
) -> None:
    gh = FakeGitHub(names, author=author)
    assert labels.triage(gh, 7, "Jianyu010").startswith("skipped")
    assert gh.writes == []


# ---- sync ---------------------------------------------------------------------------------


def test_sync_creates_missing_fixes_drift_and_deletes_nothing() -> None:
    manifest = {
        "type/bug": ("1D76DB", "a"),
        "type/ci": ("1D76DB", "b"),
        "blocker": ("000000", "c"),
    }
    gh = FakeGitHub(
        repo_labels=[
            {"name": "type/bug", "color": "1d76db", "description": "a"},
            {"name": "type/ci", "color": "FFFFFF", "description": "b"},
            {"name": "bug", "color": "d73a4a", "description": "x"},
        ]
    )
    assert labels.sync(gh, manifest) == ["updated type/ci", "created blocker"]
    assert not any(w[0] == "DELETE" for w in gh.writes)


# ---- pull requests ------------------------------------------------------------------------


def test_referenced_numbers() -> None:
    body = "Closes #12, refs #3; part of o/r#40 and other/x#9.\nSee &#8; or a/b#7."
    assert labels.referenced_numbers(body, "o/r") == [3, 12, 40]


def test_label_pr_skips_a_reference_whose_lookup_fails() -> None:
    class Flaky(FakeGitHub):
        def call(self, method: str, path: str, body: Any = None) -> Any:
            if method == "GET" and path == "/issues/11":
                raise urllib.error.URLError("timed out")
            return super().call(method, path, body)

    gh = Flaky(linked={12: ["type/bug"]})
    pull = {
        "number": 9,
        "head": {"sha": "a" * 40},
        "labels": [],
        "body": "Refs #11, #12",
    }
    labels.label_pr(gh, pull)
    assert "type/bug" in gh.names and labels.NO_ISSUE not in gh.names


def test_label_pr_counts_any_issue_reference_but_not_a_pr() -> None:
    gh = FakeGitHub(linked={12: ["type/bug"]})
    pull = {"number": 9, "head": {"sha": "a" * 40}, "labels": [], "title": "x (#12)"}
    labels.label_pr(gh, pull)
    assert labels.NO_ISSUE not in gh.names and "type/bug" in gh.names
    gh = FakeGitHub(linked={12: []})
    gh.prs = {12}
    labels.label_pr(gh, {**pull, "labels": []})
    assert labels.NO_ISSUE in gh.names


def test_gate_failures_follow_required_checks_and_the_latest_review() -> None:
    checks = [
        {"name": "2 · Lint", "status": "completed", "conclusion": "failure"},
        {"name": "3 · Linux UT", "status": "completed", "conclusion": "success"},
        {"name": "3 · Linux ST", "status": "in_progress", "conclusion": None},
        {"name": "something else", "status": "completed", "conclusion": "failure"},
    ]
    reviews = [
        {
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-01-02T00:00:00Z",
        },
    ]
    assert labels.gate_failures(checks, reviews) == {"gate/lint-failed"}
    reviews.append(
        {
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-01-03T00:00:00Z",
        }
    )
    assert labels.gate_failures(checks, reviews) == {
        "gate/lint-failed",
        labels.REVIEW_LABEL,
    }


def test_plan_pr_adds_and_clears_only_what_it_manages() -> None:
    add, remove = labels.plan_pr(
        ["gate/linux-ut-failed", "pr/hold", "type/docs"],
        {"gate/lint-failed"},
        ["type/bug"],
        linked=True,
    )
    assert add == ["gate/lint-failed", "type/bug"]
    assert remove == ["gate/linux-ut-failed"]  # pr/hold and a hand-set type/* stay
    add, remove = labels.plan_pr([], set(), [], linked=False)
    assert (add, remove) == ([labels.NO_ISSUE], [])
    add, remove = labels.plan_pr([labels.NO_ISSUE], set(), [], linked=True)
    assert (add, remove) == ([], [labels.NO_ISSUE])


def test_label_pr_mirrors_issue_types_and_gate_failures() -> None:
    gh = FakeGitHub(
        ["gate/linux-ut-failed"], linked={12: ["type/feature", "priority/high"]}
    )
    gh.checks = [
        {"name": "2 · Architecture", "status": "completed", "conclusion": "failure"}
    ]
    pull = {
        "number": 9,
        "head": {"sha": "a" * 40},
        "body": "Closes #12",
        "labels": [{"name": "gate/linux-ut-failed"}],
    }
    labels.label_pr(gh, pull)
    assert (
        "POST",
        "/issues/9/labels",
        {"labels": ["gate/architecture-failed", "type/feature"]},
    ) in gh.writes
    assert ("DELETE", "/issues/9/labels/gate%2Flinux-ut-failed", None) in gh.writes


# ---- release notes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "group"),
    [
        ("type/feature", "Features"),
        ("type/perf", "Performance"),
        ("type/bug", "Fixes"),
        ("type/docs", "Documentation"),
        ("type/ci", "CI and tests"),
        ("type/chore", "Maintenance"),
    ],
)
def test_release_notes_group_by_mirrored_type(label: str, group: str) -> None:
    change = release.Change(sha="a" * 40, title="Something", labels=[label])
    assert change.group == group
    assert group in release.GROUPS
