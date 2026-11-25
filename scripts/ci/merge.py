"""The merger: merge every PR with a green ``merge-gate``, oldest first (ported from PPMA's
scripts/ci_merge.py).

Runs as the ``merge`` job of ``.github/workflows/gate-queue.yml`` (main's code, a ``merge``
runner) after every gate event, on the 15-minute sweep and on a dispatch, from the fetched
scripts' root: ``python3 -m scripts.ci_merge [--dry-run]``. Standard library only; the rules that
decide what counts as a green tier 1 come from ``scripts/ci_gate.py``.

``merge-gate`` judges a PR's own change, so ``main`` moving does not make it stale: a PR does not
have to be up to date with ``main`` to merge, and the merger never updates a branch. A textual
conflict blocks itself (GitHub refuses the merge); a semantic one (each PR passes alone, the two
together do not) can break ``main``, which the next CI run on ``main`` reports.

One run looks at every open PR into ``main``; the job's concurrency group keeps runs one at a time:

- a PR is **eligible** when it is not a draft, has no ``pr/hold`` label and does not conflict with
  its base; what it changes does not matter;
- **merge**: every eligible PR whose head has a green ``merge-gate`` is merged with a merge commit,
  oldest first, each pinned to its head (a push in between fails that merge). A green gate counts
  only while every tier-1 check (``REQUIRED_CHECKS``) still passes on the head: a tier-1 run that
  starts again on a green head (a re-run, a hygiene re-check after an edit) no longer sets the gate
  pending, so the merger reads the checks itself. A PR that an earlier merge of the same run put in
  conflict is refused by GitHub and skipped; the next run reports the conflict;
- **conflict**: a PR (not a draft, no ``pr/hold``) that conflicts with ``main`` runs no tier 1 at
  all (GitHub starts no ``pull_request`` workflow for it), so it gets one comment per head asking
  for ``main`` to be merged in, whatever its gate says.

A merge with the workflow's token starts no push workflow on ``main`` (so no CI run there); a
merge with the merge token (``TEAMUP_MERGE_TOKEN``, ``MERGE_WITH_TOKEN``) does start them. The
workflow token cannot be counted on for a PR that changes ``.github/workflows/``: it merged #42,
but GitHub refused #66 for want of the ``workflows`` permission (#98). When GitHub refuses a
merge, the refusal is commented once per head and a person merges.

``--dry-run`` reads everything and prints the steps it would take, writing nothing. ``plan`` is a
pure function of the PRs' facts, so the rules are tested without GitHub.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Any

from scripts.ci.gate import (
    CONTEXT,
    HOLD,
    HOLD_NAMES,
    OVERRIDE_SUCCESS,
    GitHub,
    load_policy,
    require_ready,
    required_checks,
    tier1_problems,
)

BASE = "main"
# How a PR names the freeze issue it repairs: a plain reference, never a closing keyword, which
# would close the freeze issue on merge, before main is proven green (docs/ci/README.md, 冻结).
_REF = r"\s*:?\s+(?:([\w.-]+/[\w.-]+))?#([1-9][0-9]*)\b"
REPAIR_REF = re.compile(r"(?i)\b(?:refs?|references?|part\s+of|repairs?)" + _REF)
CLOSING_REF = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)" + _REF)


def _named(pattern: re.Pattern[str], text: str, repo: str) -> frozenset[int]:
    return frozenset(
        int(n)
        for where, n in pattern.findall(text)
        if not where or where.lower() == repo.lower()
    )


def freeze_refs(
    pull: dict[str, Any], repo: str
) -> tuple[frozenset[int], frozenset[int]]:
    """(numbers the PR refers to with ``Refs``/``Part of``, numbers it would close on merge)."""
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    own = {int(pull.get("number") or 0)}
    return (
        _named(REPAIR_REF, text, repo) - own,
        _named(CLOSING_REF, text, repo) - own,
    )


CONFLICT_MARK = "<!-- ci-merge:conflict sha={sha} -->"
REFUSED_MARK = "<!-- ci-merge:refused-{action} sha={sha} -->"


@dataclass(frozen=True)
class Pull:
    """The facts about one open PR that the queue decides on."""

    number: int
    sha: str
    labels: frozenset[str]
    draft: bool
    gate: str
    """``merge-gate`` on the head: success, pending, failure, error, or missing."""
    conflicts: bool = False
    override: bool = False
    """The green gate is the owner's ``ready`` (it still merges while auto-merge is frozen)."""
    refs: frozenset[int] = frozenset()
    """Numbers its title or body names with ``Refs``/``Part of``: a freeze issue among them is
    one it repairs."""
    closes: frozenset[int] = frozenset()
    """Numbers it would close on merge (``Closes``/``Fixes``/``Resolves``)."""

    def blocked(self) -> str | None:
        """Why the merger leaves this PR alone, or ``None`` when it is eligible."""
        if self.draft:
            return "draft"
        if HOLD_NAMES & self.labels:
            return f"`{HOLD}`"
        if self.conflicts:
            return "conflicts with main"
        return None


@dataclass(frozen=True)
class Step:
    """One thing the merger does: ``merge`` or ``conflict``."""

    action: str
    number: int
    sha: str
    reason: str
    repairs: tuple[int, ...] = ()
    """The freeze issues a merge during a freeze relies on the PR still referring to."""
    freezes: tuple[int, ...] = ()
    """Every freeze issue open when a repair was planned; the merge must close none of them."""


def plan(
    pulls: list[Pull], frozen: bool = False, freeze_issues: frozenset[int] = frozenset()
) -> list[Step]:
    """The steps of one run, in order; see the module docstring.

    While ``frozen`` (an open ``ci/freeze`` issue: ``main`` or the nightly is red), only PRs the
    owner passed with ``ready`` merge, and green PRs that refer to an open freeze issue
    (``freeze_issues``) with ``Refs``, and would close none: the fix or revert can land, and
    nothing else piles on.
    """
    ordered = sorted(pulls, key=lambda p: p.number)
    steps = [
        Step("conflict", pull.number, pull.sha, "conflicts with main")
        for pull in ordered
        if pull.conflicts and not pull.draft and not HOLD_NAMES & pull.labels
    ]
    for pull in ordered:
        if pull.blocked() is not None or pull.gate != "success":
            continue
        reason = f"`{CONTEXT}` green"
        repairs: tuple[int, ...] = ()
        if frozen and not pull.override:
            repairs = tuple(sorted(pull.refs & freeze_issues))
            if not repairs or pull.closes & freeze_issues:
                continue
            reason += f"; repairs freeze #{repairs[0]}"
        frozen_now = tuple(sorted(freeze_issues)) if repairs else ()
        steps.append(Step("merge", pull.number, pull.sha, reason, repairs, frozen_now))
    return steps


def gate_description(gh: GitHub, sha: str) -> str:
    """The newest ``merge-gate`` description on ``sha`` ("" when it has none)."""
    data = gh.call("GET", f"/commits/{sha}/status?per_page=100") or {}
    found = [s for s in data.get("statuses") or [] if s.get("context") == CONTEXT]
    if not found:
        return ""
    newest = max(
        found, key=lambda s: str(s.get("updated_at") or s.get("created_at") or "")
    )
    return str(newest.get("description") or "")


def freezes(gh: GitHub, label: str) -> list[tuple[int, str]]:
    """The open freeze issues as (number, title); empty when auto-merge is not frozen."""
    quoted = urllib.parse.quote(label, safe="")
    return [
        (int(issue.get("number") or 0), str(issue.get("title") or label))
        for issue in gh.pages(f"/issues?state=open&labels={quoted}", limit=2)
        if "pull_request" not in issue
    ]


def gate_state(gh: GitHub, sha: str) -> str:
    """``merge-gate``'s newest state on ``sha`` (``missing`` when it has none).

    The combined status lists one status per context, but the newest is picked by time anyway, so
    a stale ``success`` can never outrank a later ``pending`` or ``failure``.
    """
    data = gh.call("GET", f"/commits/{sha}/status?per_page=100") or {}
    found = [s for s in data.get("statuses") or [] if s.get("context") == CONTEXT]
    if not found:
        return "missing"
    newest = max(
        found, key=lambda s: str(s.get("updated_at") or s.get("created_at") or "")
    )
    return str(newest.get("state") or "missing")


def still_mergeable(
    gh: GitHub,
    number: int,
    sha: str,
    *,
    required: list[str] | None = None,
    checks: GitHub | None = None,
    repairs: tuple[int, ...] = (),
    freezes: tuple[int, ...] = (),
) -> str | None:
    """Why a PR planned for merging may no longer merge (``None`` when it still may).

    One run merges several PRs, so the snapshot ``facts`` read can be old by the time a later
    PR's turn comes. Any of these since then stops that merge: a ``pr/hold`` or a draft conversion,
    a base other than ``main``, a new push, a gate that is no longer green, or a ``required`` tier-1
    check on the head that is running again or failed (the gate and the check runs are read with
    ``checks``, as ``facts`` does). A merge during a freeze that rests on the PR referring to the
    freeze issues ``repairs`` also stops once its title or body no longer does, or would close one.
    """
    checks = checks or gh
    pull = gh.call("GET", f"/pulls/{number}") or {}
    labels = {label.get("name") for label in pull.get("labels") or []}
    if pull.get("state", "open") != "open":
        return "no longer open"
    if (pull.get("base") or {}).get("ref", BASE) != BASE:
        return f"its base is no longer `{BASE}`"
    if pull.get("draft"):
        return "a draft now"
    if HOLD_NAMES & set(labels):
        return f"`{HOLD}` added"
    if (pull.get("head") or {}).get("sha", sha) != sha:
        return "pushed since"
    if repairs:
        named, closing = freeze_refs({**pull, "number": number}, gh.repo)
        closed = sorted(closing & (set(repairs) | set(freezes)))
        if closed:
            return f"it would close freeze #{closed[0]} on merge"
        if not named & set(repairs):
            return f"it no longer refers to freeze #{repairs[0]}"
    gate = gate_state(checks, sha)
    if gate != "success":
        return f"`{CONTEXT}` is {gate} now"
    problems = tier1_problems(checks.check_runs(sha), required) if required else []
    if problems:
        return problems[0]
    return None


def facts(gh: GitHub, required: list[str], checks: GitHub | None = None) -> list[Pull]:
    """The open PRs into ``main``, with what ``plan`` needs (drafts and ``pr/hold`` read cheaply).

    A green ``merge-gate`` reads as ``pending`` while a ``required`` tier-1 check on the head is
    running again or did not pass: ``revoke`` only reacts to a run that ended without success.
    ``checks`` reads the ``merge-gate`` status, the check runs and their workflows (the workflow's
    token, which has ``statuses: read``, ``checks: read`` and ``actions: read``; the merge token is
    granted neither the Commit statuses nor the Checks permission, #112); ``gh`` when not given.
    """
    checks = checks or gh
    pulls: list[Pull] = []
    for item in gh.pages(f"/pulls?state=open&base={BASE}"):
        number, sha = int(item["number"]), str(item["head"]["sha"])
        labels = frozenset(label["name"] for label in item.get("labels", []))
        refs, closes = freeze_refs(item, gh.repo)
        if item.get("draft") or HOLD_NAMES & labels:
            pulls.append(Pull(number, sha, labels, bool(item.get("draft")), "skipped"))
            continue
        gate = gate_state(checks, sha)
        if gate == "success" and tier1_problems(checks.check_runs(sha), required):
            gate = "pending"
        if gate != "success":
            # One read, no wait: a conflict stops tier 1 itself, so it is reported either way.
            detail = gh.call("GET", f"/pulls/{number}") or {}
            conflicts = detail.get("mergeable") is False
            pulls.append(Pull(number, sha, labels, False, gate, conflicts=conflicts))
            continue
        pull = gh.pull(number)
        pulls.append(
            Pull(
                number,
                sha,
                labels,
                False,
                gate,
                conflicts=pull.get("mergeable") is False,
                override=gate_description(checks, sha) == OVERRIDE_SUCCESS,
                refs=refs,
                closes=closes,
            )
        )
    return pulls


def _commented(gh: GitHub, number: int, mark: str) -> bool:
    return any(
        mark in str(c.get("body") or "") for c in gh.pages(f"/issues/{number}/comments")
    )


def _comment_once(gh: GitHub, number: int, mark: str, text: str) -> None:
    """Comment ``text`` with ``mark`` unless a comment with ``mark`` is already there."""
    if not _commented(gh, number, mark):
        gh.comment(number, f"{text}\n\n{mark}")


def apply(
    gh: GitHub,
    steps: list[Step],
    run_url: str,
    *,
    required: list[str] | None = None,
    checks: GitHub | None = None,
) -> None:
    """Take the planned steps; a refused merge is commented once per head, not raised.

    A merge that an earlier merge of the run put in conflict (GitHub: not mergeable) or whose head
    moved (409) is skipped quietly: the next run sees the conflict, or the new head is judged again.

    Each merge first re-reads its PR (``still_mergeable``), so a ``pr/hold`` added while the run
    merges earlier PRs still stops it.
    """
    failed_rechecks: list[int] = []
    for step in steps:
        n = step.number
        if step.action == "merge":
            try:
                why = still_mergeable(
                    gh,
                    n,
                    step.sha,
                    required=required,
                    checks=checks,
                    repairs=step.repairs,
                    freezes=step.freezes,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed for this PR, report below
                # Fail closed for this candidate while processing later PRs. Record a
                # nonzero job outcome below: repeated auth/API failures must never look
                # like a healthy, successful merge sweep.
                failed_rechecks.append(n)
                print(
                    f"merge: #{n} skipped (eligibility re-check failed: "
                    f"{type(exc).__name__}: {str(exc)[:180]})"
                )
                continue
            if why:
                print(f"merge: #{n} skipped ({why})")
                continue
            try:
                gh.call(
                    "PUT",
                    f"/pulls/{n}/merge",
                    {"merge_method": "merge", "sha": step.sha},
                )
            except urllib.error.HTTPError as exc:
                reason = _reason(exc)
                if exc.code == 409 or "not mergeable" in reason.lower():
                    print(f"merge: #{n} skipped ({exc.code}: {reason})")
                    continue
                _comment_once(
                    gh,
                    n,
                    REFUSED_MARK.format(action="merge", sha=step.sha),
                    f"The merger could not merge `{step.sha[:7]}` ({exc.code}: {reason}). "
                    f"A person merges it by hand while `{CONTEXT}` is green. [Run]({run_url})",
                )
                continue
        elif step.action == "conflict":
            _comment_once(
                gh,
                n,
                CONFLICT_MARK.format(sha=step.sha),
                f"This PR conflicts with `{BASE}`, so GitHub runs none of its `pull_request` "
                f"checks (tier 1) and nothing can pass it. Merge `{BASE}` into the branch and "
                f"resolve the conflict. [Run]({run_url})",
            )
    if failed_rechecks:
        # Other eligible PRs were processed. Report the errors after the loop so an
        # expired token/outage cannot yield green.
        failed = ", ".join(f"#{number}" for number in failed_rechecks)
        raise RuntimeError(
            f"merge: eligibility re-check failed for {failed}; job is not green"
        )


def _reason(exc: urllib.error.HTTPError) -> str:
    try:
        return str(exc.read().decode("utf-8", "replace"))[:200]
    except Exception:  # noqa: BLE001 - the reason is only for the comment
        return str(exc.reason)


def hold_label_exists(gh: GitHub) -> bool:
    """Whether the repository has a label that stops the merger (``HOLD`` or its legacy name).

    With none, nobody can hold a PR, so a green one would merge whatever a person intended. Only a
    404 means "missing"; any other error is raised.
    """
    for name in (HOLD, *sorted(HOLD_NAMES - {HOLD})):
        try:
            gh.call("GET", f"/labels/{urllib.parse.quote(name, safe='')}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Entry point for the gate workflow's ``merge`` job."""
    parser = argparse.ArgumentParser(
        prog="ci_merge", description="merge green PRs, oldest first"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the steps, write nothing"
    )
    args = parser.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{gh.repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    # ``MERGE_WITH_TOKEN`` tells which token the workflow chose; a merge with the merge token
    # starts push workflows on ``main``, a merge with the workflow token does not.
    _merge_token = os.environ.get("MERGE_WITH_TOKEN") == "true"
    required = required_checks()
    if not hold_label_exists(gh):
        print(
            f"::error::the repository has no `{HOLD}` (or legacy) label: merging is stopped"
        )
        return 1
    reader = os.environ.get("CHECKS_TOKEN")
    checks = GitHub(gh.repo, reader) if reader else gh
    policy = load_policy()
    open_freezes = freezes(checks, str(policy.get("freeze_label") or "ci/freeze"))
    if open_freezes and not require_ready(policy):
        titles = "; ".join(title for _n, title in open_freezes)
        print(f"merge: frozen ({titles}), but `require_ready` is off: merging as usual")
        open_freezes = []
    if open_freezes:
        titles = "; ".join(title for _n, title in open_freezes)
        numbers = ", ".join(f"#{n}" for n, _t in open_freezes)
        print(
            f"merge: auto-merge is frozen ({titles}); only PRs the owner passed with `ready` "
            f"or that refer to {numbers} merge"
        )
    steps = plan(
        facts(gh, required, checks),
        frozen=bool(open_freezes),
        freeze_issues=frozenset(n for n, _t in open_freezes),
    )
    for step in steps:
        print(
            f"{'would ' if args.dry_run else ''}{step.action} #{step.number} "
            f"({step.sha[:7]}): {step.reason}"
        )
    if not steps:
        print("merge: nothing to do")
    if not args.dry_run:
        apply(gh, steps, run_url, required=required, checks=checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
