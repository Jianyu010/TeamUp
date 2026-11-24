"""TeamUp's merge gate: the ``merge-gate`` commit status (layer 5 of docs/ci/README.md).

Ported from tweakie's scripts/ci_gate.py (itself from PPMA), which runs on the same hp-laptop
runners. TeamUp adds layer 1 (``routing_problems``): a PR that touches a protected path, deletes a
test or adds a skip/xfail marker never passes the automatic route; the owner reviews it and adds
``ready``. The rules come from ``.github/ci/policy.json``, fetched from ``main`` with this script.

Runs from ``.github/workflows/gate.yml`` on ``pull_request_target``, so it always comes from
``main``: a PR cannot edit the rules that judge it. Standard library only; it talks to the GitHub
REST API with the workflow's ``GITHUB_TOKEN``.

``guard`` (every gate event) keeps ``merge-gate`` honest:

- **PR opened**: ``merge-gate`` goes pending on the head, so a green status the commit got in
  another PR does not carry over;
- **new commits** (``synchronize``/``reopened``): ``merge-gate`` goes pending on the new head, and
  a ``ready`` label is removed with a comment (the override must match the code it let through);
- **title, body or base edited**: ``ready`` is removed the same way. Without ``ready``, only a base
  change matters: the earlier evidence judged another diff (CI does not re-run on ``edited``), so
  ``merge-gate`` goes pending on the head and a failed tier-1 check is put back on top;
- **draft conversion, ready for review, or a ``pr/hold`` change**: the PR is judged again
  (``auto``);
- **``ready`` added** (the maintainer override, ADR-0005): accepted only when the person who added
  it is a maintainer (repository role ``admin`` or ``maintain``), the PR is not a draft, merges
  cleanly and every tier-1 check passed on the head. Accepted, ``merge-gate`` goes green without
  the AI review; otherwise the label is removed and the reason commented. ``ready`` removed hands
  the PR back to the automatic route.

``revoke`` (``workflow_run``: a tier-1 workflow run on a PR finished without success) sets
``merge-gate`` on that commit to failure, so a re-run after a green gate never leaves the merge
open while it fails.

``auto`` is the route to a green ``merge-gate`` with no label: it posts success when every
required tier-1 check (``REQUIRED_CHECKS``) passed on the head, the OpenCode review of that head
reports no blocking findings and every finding it left open was answered, the PR is not a draft,
has no ``pr/hold`` label, does not conflict with its base, and no other open PR has the same head.
Anything missing keeps the gate pending (fail closed). A PR that changes only files under
``docs/`` outside ``docs/adr/`` needs no review (``review_exempt``).

The review is started by the gate, not by the PR (ADR-0005): once tier 1 passed on a head that
has no review run yet, ``auto`` dispatches main's ``opencode-review.yml`` for it, so drafts and red
PRs spend none of the gateway's shared slots. Only a member's PR is dispatched; anyone else's
needs a maintainer's ``ready``. A review counts only when ``github-actions[bot]`` posted it with the
head's marker while the successful ``review`` job of a dispatch of that workflow on ``main`` for
that head was running.
When that run ended with no review (the reviewer could not run), the sweep re-runs it, at most
until its ``REVIEW_RERUNS``-th attempt.

``auto`` runs when a tier-1 run finishes (``workflow_run``), when the review finishes (its
``wake-gate`` job dispatches it for the PR: a run the gate dispatched starts no ``workflow_run``),
when ``pr/hold`` changes or the PR becomes or stops being a draft, on a manual dispatch from
``main``, and
periodically over the PRs it posted (``--all``); the sweep is also what notices answers to review
findings. The sweep does not wait for ``mergeable`` (an unknown one still gets every other check,
and never success), rotates where it starts, and stops after ``SWEEP_BUDGET_S``, so it ends inside
its job's timeout.

``merge-gate`` is a commit status. Because a status belongs to one commit, a new push leaves the
new head without a green ``merge-gate``, which is what makes it safe to require. For the same
reason two open PRs with one head would share it, so the gate never passes such a commit.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import calendar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

POLICY_PATH = Path(
    os.environ.get("CI_POLICY")
    or Path(__file__).resolve().parents[2] / ".github/ci/policy.json"
)


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    """The gate's policy; a missing or unreadable file fails the job (fail closed)."""
    return json.loads(path.read_text(encoding="utf-8"))


LABEL = "ready"
MAINTAINER_ROLES = frozenset({"admin", "maintain"})
HOLD = "pr/hold"
# The pre-rename name still holds for a transition, so a PR held before the rename stays held.
HOLD_NAMES = frozenset({HOLD, "hold"})
# Conclusions that carry no verdict: a job skipped on a draft, or a run superseded by concurrency.
NO_VERDICT = frozenset({"skipped", "cancelled"})
CONTEXT = "merge-gate"
WAITING = "waiting for tier 1 and the AI review on this commit"
DEFAULT_BRANCH = "main"
REVIEW_WORKFLOW = "opencode-review.yml"
REVIEW_AUTHOR = "github-actions[bot]"
REVIEW_JOB = (
    "3 · AI Review"  # the name of opencode-review.yml's job that posts the review
)
# opencode-review.yml's run-name; it ties a dispatched run (whose own head is main) to a PR head.
REVIEW_RUN_NAME = re.compile(r"#(\d+) at ([0-9a-f]{40})$")
REVIEW_MARKER = re.compile(r"<!--\s*opencode-review:sha=([0-9a-f]{40})\s*-->")
REVIEW_STATE = re.compile(r"<!--\s*opencode-review:state=([A-Za-z0-9+/=_-]+)\s*-->")
REVIEW_FINDING = re.compile(r"<!--\s*opencode-review:finding=(F[1-9][0-9]*)\s*-->")
FINDING_REPLY = re.compile(
    r"(?m)^\s*(?:Re:\s*)?(F[1-9][0-9]*)\s*[:-]\s*\S", re.IGNORECASE
)
REVIEW_HEADING = "### OpenCode review"
REVIEW_CLEAN = "**Verdict: ✅ no blocking findings.**"
# Authors whose PRs the gate dispatches the review for (the reviewer's own rule in PPMA).
TRUSTED_AUTHORS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
TIER1_CONCLUDED = re.compile(
    r"`[^`]+` concluded "
)  # tier1_problems' failed-run wording
# The end of every tier-1 failure the gate posts (revoke's, and guard's on a base change).
TIER1_FAILED = "; fix it and push"
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


# Server errors GitHub answers now and then and not a minute later (#86): tried again, briefly.
TRANSIENT = frozenset({429, 500, 502, 503, 504})
CALL_RETRIES = 2
RETRY_BACKOFF_S = 2.0  # then 4 s


def _repeatable(method: str, path: str) -> bool:
    """Whether a second try of the call can do no harm: a read, a status or a label removal."""
    return (
        method == "GET"
        or path.endswith("/graphql")  # the gate's GraphQL calls are queries only
        or path.startswith("/statuses/")
        or (method == "DELETE" and "/labels/" in path)
    )


class BudgetSpent(Exception):
    """The sweep's time is up; the call was not made."""


class GitHub:
    """The few REST calls the gate needs."""

    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        self.deadline: float | None = (
            None  # ``time.monotonic()`` after which no call is made
        )
        # Reads shared within one judgement (``judging``); None outside one.
        self.memo: dict[tuple[Any, ...], Any] | None = None

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """One REST call; returns parsed JSON (None for an empty body)."""
        url = path if path.startswith("http") else f"{API}/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        # A fail-closed status write is never held back by the deadline: the verdict must land.
        # Under a deadline its own short timeout keeps the sweep inside the job (150 + 10 < 180 s).
        # Every other call is bounded by it: a success write too, which must not land without the
        # read that checks it afterwards.
        fail_closed = (
            path.startswith("/statuses/") and (body or {}).get("state") != "success"
        )
        deadline = None if fail_closed else self.deadline
        # A transient server error is tried again, but only for a call that is safe to repeat
        # (a 5xx may follow a write that landed: a second comment or review run is not).
        retries = CALL_RETRIES if _repeatable(method, path) else 0
        for attempt in range(retries + 1):
            timeout = 30.0
            if self.deadline is not None and deadline is None:
                timeout = WRITE_TIMEOUT_S
            elif deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
                if timeout <= 0:
                    raise BudgetSpent(f"{method} {path}")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                # GitHub answered: a real error, whatever the time left, unless it is transient
                # and the pause before the next try ends before the deadline (a fail-closed write
                # past it gets no second try, so the sweep still ends inside its job).
                pause = RETRY_BACKOFF_S * 2**attempt
                if (
                    exc.code not in TRANSIENT
                    or attempt == retries
                    or (
                        self.deadline is not None
                        and time.monotonic() + pause >= self.deadline
                    )
                ):
                    raise
                print(
                    f"gate: {method} {path} answered {exc.code}; trying again in {pause:g} s"
                )
                time.sleep(pause)
            except (TimeoutError, urllib.error.URLError) as exc:
                # A call clipped to the time left that runs out is the budget, not a GitHub failure.
                if deadline is not None and time.monotonic() >= deadline - 1:
                    raise BudgetSpent(f"{method} {path}") from exc
                raise
        return json.loads(raw) if raw else None

    def pull(self, number: int, wait: bool = True) -> dict[str, Any]:
        """The PR, waiting briefly for GitHub to compute ``mergeable`` (unless ``wait`` is off)."""
        data: dict[str, Any] = {}
        for _ in range(6 if wait else 1):
            data = self.call("GET", f"/pulls/{number}")
            if data.get("mergeable") is not None or not wait:
                break
            time.sleep(5)
        return data

    def open_pulls_with_head(self, sha: str) -> list[int]:
        """Numbers of the open PRs whose head commit is ``sha``."""
        data = self.call("GET", f"/commits/{sha}/pulls?per_page=100") or []
        return sorted(
            int(pr["number"])
            for pr in data
            if pr.get("state") == "open" and pr.get("head", {}).get("sha") == sha
        )

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        """Every check run on ``sha``, each tagged with its ``workflow`` when it is known.

        A check run names only its check suite; the head's workflow runs map a suite to the
        workflow file and the event that ran it (a ``push`` and a ``pull_request`` run of one file
        are separate workflows). A re-run attempt keeps its run's check suite. If the workflow
        runs cannot be read, runs stay untagged and tier 1 counts every one.
        """
        data = self.call("GET", f"/commits/{sha}/check-runs?per_page=100&filter=latest")
        runs = list(data.get("check_runs", []))
        try:
            listed = self.workflow_runs(sha)
        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:  # HTTPError is one; BudgetSpent not
            print(
                f"::warning::workflow runs of {sha} unreadable ({exc}); every run counts"
            )
            listed = []
        workflows = {
            run.get("check_suite_id"): (
                f"{run['path']} {run.get('event') or ''}".strip(),
                run.get("id"),
            )
            for run in listed
            if run.get("path")
        }
        tagged = []
        for run in runs:
            workflow, run_id = workflows.get(
                (run.get("check_suite") or {}).get("id"), (None, None)
            )
            tagged.append({**run, "workflow": workflow, "workflow_run_id": run_id})
        return tagged

    def workflow_runs(self, sha: str) -> list[dict[str, Any]]:
        """Every Actions workflow run on head ``sha``."""
        return self.pages(f"/actions/runs?head_sha={sha}", key="workflow_runs", limit=5)

    def pages(
        self, path: str, key: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Every item of a paginated list (``key`` when the list sits inside an object)."""
        items: list[dict[str, Any]] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            data = self.call("GET", f"{path}{sep}per_page=100&page={page}")
            batch = list((data or {}).get(key, []) if key else (data or []))
            items += batch
            if len(batch) < 100:
                break
        return items

    def role(self, user: str) -> str:
        """The user's repository role name (``admin``, ``maintain``, ``write``, ...)."""
        quoted = urllib.parse.quote(user)
        data = self.call("GET", f"/collaborators/{quoted}/permission")
        return str(data.get("role_name") or data.get("permission") or "none")

    def file_entries(self, number: int) -> list[dict[str, Any]]:
        """The PR's changed files as GitHub lists them (``filename``, ``status``, ``patch``)."""
        return self.pages(f"/pulls/{number}/files", limit=30)

    def files(self, number: int) -> list[str]:
        """Every path the PR touches, both names of a rename."""
        paths: set[str] = set()
        for item in self.pages(f"/pulls/{number}/files", limit=30):
            paths.add(str(item["filename"]))
            if item.get("previous_filename"):
                paths.add(str(item["previous_filename"]))
        return sorted(paths)

    def reviews(self, number: int) -> list[dict[str, Any]]:
        """Every review on the PR, oldest first."""
        return self.pages(f"/pulls/{number}/reviews")

    def review_runs(self, sha: str) -> list[dict[str, Any]]:
        """Dispatched runs of main's review workflow for PR head ``sha``, newest first.

        A dispatched run's own head is ``main``; its run-name says which PR head it reviews.
        Only runs on the default branch count: they run the workflow file from ``main``.
        """
        path = (
            f"/actions/workflows/{REVIEW_WORKFLOW}/runs"
            f"?event=workflow_dispatch&branch={DEFAULT_BRANCH}"
        )
        return [
            run
            for run in self.pages(path, key="workflow_runs", limit=1)
            if (m := REVIEW_RUN_NAME.search(str(run.get("display_title") or "")))
            and m.group(2) == sha
        ]

    def review_jobs(self, run_id: int) -> list[dict[str, Any]]:
        """The ``review`` jobs of every attempt of a review run.

        Every attempt, not the latest: a re-run's job starts after the review an earlier attempt
        posted, and must not hide it (#95 F5).
        """
        data = (
            self.call("GET", f"/actions/runs/{run_id}/jobs?filter=all&per_page=100")
            or {}
        )
        return [j for j in data.get("jobs") or [] if j.get("name") == REVIEW_JOB]

    def dispatch_review(self, number: int, sha: str, force: bool = False) -> None:
        """Start main's review workflow for PR ``number`` at head ``sha``.

        ``force`` reviews a head that already has a review (the reviewer skips one otherwise).
        """
        inputs = {"pr": str(number), "sha": sha}
        if force:
            inputs["force"] = (
                "true"  # dispatch inputs are strings; the boolean input reads "true"
            )
        self.call(
            "POST",
            f"/actions/workflows/{REVIEW_WORKFLOW}/dispatches",
            {"ref": DEFAULT_BRANCH, "inputs": inputs},
        )

    def threads(self, number: int) -> list[dict[str, Any]]:
        """The PR's review threads: ``resolved``, first comment's ``author`` and ``body``."""
        owner, name = self.repo.split("/", 1)
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:"
            "$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after)"
            "{pageInfo{hasNextPage endCursor} nodes{isResolved "
            "comments(first:100){nodes{author{login} body}}}}}}}"
        )
        threads: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(30):
            variables = {"owner": owner, "name": name, "number": number, "after": after}
            data = self.call(
                "POST", f"{API}/graphql", {"query": query, "variables": variables}
            )
            pull = ((data or {}).get("data") or {}).get("repository", {}).get(
                "pullRequest"
            ) or {}
            found = pull.get("reviewThreads") or {}
            for node in found.get("nodes") or []:
                nodes = (node.get("comments") or {}).get("nodes") or []
                first = nodes[0] if nodes else {}
                threads.append(
                    {
                        "resolved": bool(node.get("isResolved")),
                        "author": str((first.get("author") or {}).get("login") or ""),
                        "body": str(first.get("body") or ""),
                        "replies": [
                            {
                                "author": str(
                                    (c.get("author") or {}).get("login") or ""
                                ),
                                "body": str(c.get("body") or ""),
                            }
                            for c in nodes[1:]
                        ],
                    }
                )
            info = found.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        return threads  # a finding on an unread page stays unanswered: never a false success

    def issue(self, number: int) -> dict[str, Any] | None:
        """Issue or PR ``number`` as the issues API returns it (None if it does not exist)."""
        try:
            data = self.call("GET", f"/issues/{number}")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return None
            raise
        return data if isinstance(data, dict) else None

    def issue_comments(self, number: int, since: str) -> list[dict[str, Any]]:
        """Conversation comments since ``since``, for findings without inline threads."""
        return self.pages(
            f"/issues/{number}/comments?since={urllib.parse.quote(since, safe=':')}"
        )

    def labeled_by(self, number: int) -> str:
        """Who added ``ready`` most recently ("" if nobody)."""
        events = self.pages(f"/issues/{number}/events")
        added = [
            e
            for e in events
            if e.get("event") == "labeled"
            and (e.get("label") or {}).get("name") == LABEL
        ]
        return str((added[-1].get("actor") or {}).get("login") or "") if added else ""

    def remove_label(self, number: int) -> None:
        """Remove ``ready`` from the PR (absent is fine)."""
        try:
            self.call("DELETE", f"/issues/{number}/labels/{LABEL}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

    def base_changed_at(self, number: int) -> str:
        """When the PR's base branch was last changed ("" if never)."""
        events = self.pages(f"/issues/{number}/events")
        return max(
            (
                str(e.get("created_at") or "")
                for e in events
                if e.get("event") == "base_ref_changed"
            ),
            default="",
        )

    def open_pulls(self) -> list[dict[str, Any]]:
        """Every open PR."""
        return self.pages("/pulls?state=open")

    def gate_description(self, sha: str) -> str:
        """The description of the latest ``merge-gate`` status on ``sha`` ("" if none)."""
        data = self.call("GET", f"/commits/{sha}/status?per_page=100") or {}
        for status in data.get("statuses") or []:
            if status.get("context") == CONTEXT:
                return str(status.get("description") or "")
        return ""

    def comment(self, number: int, text: str) -> None:
        """Comment on the PR."""
        self.call("POST", f"/issues/{number}/comments", {"body": text})

    def status_of(self, sha: str, context: str) -> tuple[str, str]:
        """The latest (state, description) of commit status ``context`` on ``sha`` ("", "" if none)."""
        data = _memo(
            self,
            ("status", sha),
            lambda: self.call("GET", f"/commits/{sha}/status?per_page=100") or {},
        )
        for status in data.get("statuses") or []:
            if status.get("context") == context:
                return str(status.get("state") or ""), str(
                    status.get("description") or ""
                )
        return "", ""

    def status(
        self,
        sha: str,
        state: str,
        description: str,
        url: str | None = None,
        context: str = CONTEXT,
    ) -> None:
        """Set commit status ``context`` (default ``merge-gate``) on ``sha``."""
        body: dict[str, str] = {
            "state": state,
            "context": context,
            "description": description[:140],
        }
        if url:
            body["target_url"] = url
        self.call("POST", f"/statuses/{sha}", body)


def tier1_problems(runs: list[dict[str, Any]], required: list[str]) -> list[str]:
    """Which required tier-1 checks have not passed (missing, running, or not successful).

    The latest run of each (workflow, name) must be completed and successful. A PR can carry
    several runs with one name (``check`` comes from both ``ci.yml`` and ``rust.yml``), so keeping
    only the latest per name would let one workflow's passing ``check`` hide the other's failure.
    A newer run of the same workflow replaces its earlier one (``hygiene.yml`` re-runs when the PR
    body is edited); a run whose workflow is unknown replaces nothing. A run that concluded
    ``skipped`` carries no verdict (the jobs skip drafts, and a PR keeps those runs after it is
    marked ready), and neither does a ``cancelled`` run superseded by concurrency, so both are
    left out; at least one other run of the name must exist.
    """
    problems = []
    for name in required:
        matching = latest_runs(runs, name)
        if not matching:
            problems.append(f"`{name}` has not run on this commit")
            continue
        pending = [run for run in matching if run.get("status") != "completed"]
        if pending:
            problems.append(f"`{name}` is still {pending[0].get('status')}")
            continue
        failed = [run for run in matching if run.get("conclusion") != "success"]
        if failed:
            problems.append(f"`{name}` concluded {failed[0].get('conclusion')}")
    return problems


def latest_runs(runs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The latest run of ``name`` from each workflow, leaving out runs with no verdict.

    A run whose workflow is unknown is its own group, so it is always kept.
    """
    latest: dict[Any, dict[str, Any]] = {}
    for i, run in enumerate(runs):
        if run.get("name") != name or run.get("conclusion") in NO_VERDICT:
            continue
        key = run.get("workflow") or ("unknown", i)
        if key not in latest or _started(run) >= _started(latest[key]):
            latest[key] = run
    return list(latest.values())


def _started(run: dict[str, Any]) -> tuple[int, bool, str, int]:
    """How recent a check run is: its workflow run first, as ``revoke`` orders them.

    Workflow runs are numbered in the order they were created, whenever their jobs started.
    Within one workflow run (or without one), a run that has not started (queued, no
    ``started_at``) is newer than every run that has, and the check run's id breaks a tie.
    """
    started = str(run.get("started_at") or "")
    return (
        int(run.get("workflow_run_id") or 0),
        not started,
        started,
        int(run.get("id") or 0),
    )


def ready_problems(
    role: str, pull: dict[str, Any], runs: list[dict[str, Any]], required: list[str]
) -> list[str]:
    """Why the maintainer override ``ready`` may not stand (empty when it may).

    It skips only the AI review: tier 1 must still have passed.
    """
    problems = []
    if role not in MAINTAINER_ROLES:
        problems.append(
            f"only a maintainer (role admin or maintain) may add `{LABEL}`; this role is {role}"
        )
    if pull.get("draft"):
        problems.append("the PR is a draft; mark it ready for review first")
    if pull.get("mergeable") is False:
        problems.append("the PR conflicts with its base; merge or rebase `main` first")
    elif pull.get("mergeable") is None:
        problems.append(
            "GitHub has not finished checking whether the PR merges cleanly; add it again shortly"
        )
    problems += tier1_problems(runs, required)
    return problems


NO_REVIEW = "no OpenCode review of this commit yet"
# A clean review of the head that no review run of it can vouch for: one posted by a run from
# before run-names named their PR head (#94), or a hand-posted one. It is reviewed again, forced.
UNATTRIBUTED = "no review run of this commit vouches for its OpenCode review"
UNTRUSTED = (
    "the AI review runs only on members' PRs; a maintainer reviews it and adds `ready`"
)
# The sweep re-runs a review run that ended without a review (the reviewer could not run, e.g. the
# gateway's parallel-request limit was used up), up to this attempt, and only once the run has
# been over for a while, so a busy gateway gets time to clear.
REVIEW_RERUNS = 3
REVIEW_RERUN_AFTER_S = 600


def review_exempt(paths: list[str], policy: dict[str, Any] | None = None) -> bool:
    """Whether the PR changes only files that need no AI review (``review_exempt_paths``)."""
    roots = tuple((policy or {}).get("review_exempt_paths") or ())
    return bool(paths) and bool(roots) and all(p.startswith(roots) for p in paths)


ROUTING = "layer 1: "
# An added line that turns a test off. Removing one is fine; adding one needs the owner.
SKIP_MARKER = re.compile(
    r"^\+(?!\+\+).*(?:pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(|"
    r"unittest\.skip|@skip\b|collect_ignore)"
)


def _protected(path: str, policy: dict[str, Any]) -> bool:
    roots = policy.get("protected_paths") or []
    names = set(policy.get("protected_basenames") or [])
    hit = any(path.startswith(r) if r.endswith("/") else path == r for r in roots)
    return hit or path.rsplit("/", 1)[-1] in names


def _is_test(path: str, policy: dict[str, Any]) -> bool:
    name = path.rsplit("/", 1)[-1]
    roots = tuple(policy.get("test_roots") or ())
    return path.startswith(roots) and name.startswith("test_") and name.endswith(".py")


def routing_problems(
    entries: list[dict[str, Any]], policy: dict[str, Any]
) -> list[str]:
    """Layer 1: what the automatic route may not pass without the owner (empty when nothing).

    Read from the PR's changed files as data; no PR code runs. A rename counts under both names.
    """
    if len(entries) >= int(policy.get("max_changed_files") or 300):
        return [
            f"{ROUTING}the PR changes {len(entries)} files or more; the owner reviews it"
        ]
    protected: set[str] = set()
    deleted: set[str] = set()
    skipped: set[str] = set()
    for entry in entries:
        names = [str(entry.get("filename") or "")]
        if entry.get("previous_filename"):
            names.append(str(entry["previous_filename"]))
        protected.update(n for n in names if _protected(n, policy))
        # A removed test, or a test renamed to something that is no longer collected as one.
        gone = names[-1]  # the old name of a rename, else the file itself
        status = entry.get("status")
        if (status == "removed" and _is_test(gone, policy)) or (
            status == "renamed"
            and _is_test(gone, policy)
            and not _is_test(names[0], policy)
        ):
            deleted.add(gone)
        patch = str(entry.get("patch") or "")
        if any(SKIP_MARKER.match(line) for line in patch.splitlines()):
            skipped.add(names[0])
    problems = []
    if protected:
        listed = ", ".join(f"`{p}`" for p in sorted(protected)[:3])
        problems.append(
            f"{ROUTING}protected path {listed}; the owner reviews it and adds `{LABEL}`"
        )
    if deleted:
        listed = ", ".join(f"`{p}`" for p in sorted(deleted)[:3])
        problems.append(
            f"{ROUTING}deletes test {listed}; the owner reviews it and adds `{LABEL}`"
        )
    if skipped:
        listed = ", ".join(f"`{p}`" for p in sorted(skipped)[:3])
        problems.append(
            f"{ROUTING}adds a skip or xfail in {listed}; the owner adds `{LABEL}`"
        )
    return problems


# Layer-1 statuses: informational, never required. They say why a PR is held; ``merge-gate`` alone
# decides. Posted by the gate wherever it judges a head, and only when their text changes.
ROUTE_STATUS = "1 · PR Routing"
PROTECTED_STATUS = "1 · Protected Changes"
LIMITS_STATUS = "1 · Automation Limits"
ISSUE_STATUS = "1 · Linked Issue"
FINDINGS_STATUS = "3 · Review Findings"
NO_LIMIT = 10**9


def _short(problem: str) -> str:
    """A routing problem without its prefix and its instruction to the owner."""
    return problem.removeprefix(ROUTING).split(";")[0]


def _memo(gh: Any, key: tuple[Any, ...], read: Callable[[], Any]) -> Any:
    """``read()``, shared with the rest of the current judgement (``judging``)."""
    memo = getattr(gh, "memo", None)
    if memo is None:
        return read()
    if key not in memo:
        memo[key] = read()
    return memo[key]


class judging:
    """Within the block, ``gh``'s file, review and status reads are made once.

    One judgement (``auto_problems`` and then ``layer1``) reads the same snapshot; a new
    judgement, such as a re-judge after GitHub recomputed ``mergeable``, reads afresh.
    """

    def __init__(self, gh: Any) -> None:
        self.gh = gh

    def __enter__(self) -> None:
        self.gh.memo = {}

    def __exit__(self, *_: object) -> None:
        self.gh.memo = None


ISSUE_REF = r"(?<![\w/&])#([1-9][0-9]*)\b"
NO_ISSUE = (
    f"{ROUTING}the PR links no issue; reference one in its title or description "
    "(`Closes #N`, `Refs #N`)"
)


def referenced_numbers(text: str, repo: str) -> list[int]:
    """Issue or PR numbers ``text`` refers to in this repository (``#N`` or ``owner/repo#N``)."""
    found = re.findall(ISSUE_REF, text or "")
    found += re.findall(rf"(?i)\b{re.escape(repo)}#([1-9][0-9]*)\b", text or "")
    return sorted({int(n) for n in found})


def linked_issue(gh: Any, pull: dict[str, Any]) -> int | None:
    """The first issue (not a PR) of this repository the PR's title or body refers to."""
    number = int(pull["number"])
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    for n in referenced_numbers(text, gh.repo)[:20]:
        if n == number:
            continue
        issue = _memo(gh, ("issue", n), lambda n=n: gh.issue(n))
        if issue and "pull_request" not in issue:
            return n
    return None


def _file_entries(gh: Any, number: int) -> list[dict[str, Any]]:
    return _memo(gh, ("files", number), lambda: gh.file_entries(number))


def _reviews(gh: Any, number: int) -> list[dict[str, Any]]:
    return _memo(gh, ("reviews", number), lambda: gh.reviews(number))


def _review_runs(gh: Any, sha: str) -> list[dict[str, Any]]:
    return _memo(gh, ("review_runs", sha), lambda: gh.review_runs(sha))


def _threads(gh: Any, number: int) -> list[dict[str, Any]]:
    return _memo(gh, ("threads", number), lambda: gh.threads(number))


def _issue_comments(gh: Any, number: int, since: str) -> list[dict[str, Any]]:
    return _memo(
        gh, ("comments", number, since), lambda: gh.issue_comments(number, since)
    )


def layer1_verdicts(
    gh: GitHub, pull: dict[str, Any], ready: bool
) -> dict[str, tuple[str, str]]:
    """The three layer-1 statuses (context -> (state, description)) for the PR's head.

    ``ready`` is True when a valid maintainer ``ready`` covers the head. Reads the changed files
    as data; nothing of the PR runs.
    """
    number, sha = int(pull["number"]), pull["head"]["sha"]
    policy = load_policy()
    entries = _file_entries(gh, number)
    limit = int(policy.get("max_changed_files") or 300)
    problems = routing_problems(entries, {**policy, "max_changed_files": NO_LIMIT})
    issue = linked_issue(gh, pull)
    paths = sorted({str(e.get("filename") or "") for e in entries})
    exempt = review_exempt(paths, policy)
    labels = {label["name"] for label in pull.get("labels", [])}

    limits: list[str] = []
    if len(entries) >= limit:
        limits.append(f"the PR changes {len(entries)} files or more (limit {limit})")
    if not exempt and str(pull.get("author_association") or "") not in TRUSTED_AUTHORS:
        limits.append("the author is not trusted, so no review is dispatched")
    if (
        not exempt
        and not pull.get("draft")
        and not _own_reviews(sha, _reviews(gh, number))
    ):
        runs = _review_runs(gh, sha)
        latest = max(
            runs,
            key=lambda r: str(r.get("run_started_at") or r.get("created_at") or ""),
            default=None,
        )
        if (
            latest
            and latest.get("status") == "completed"
            and latest.get("conclusion") in ("success", "failure", "timed_out")
            and int(latest.get("run_attempt") or 1) >= REVIEW_RERUNS
        ):
            limits.append(f"the review re-ran {REVIEW_RERUNS} times without a verdict")

    if ready and (problems or limits or issue is None):
        route = f"owner route: `{LABEL}` accepted"
    elif ready:
        route = "automatic route (`ready` set)"
    elif pull.get("draft"):
        route = "draft: layer 2 only"
    elif HOLD_NAMES & labels:
        route = f"held: the PR is on `{HOLD}`"
    elif issue is None:
        route = "waits for a linked issue (`Closes #N` or `Refs #N`)"
    elif problems or limits:
        reason = _short((problems + limits)[0])
        route = f"waits for the owner's `{LABEL}`: {reason}"
    else:
        route = "automatic route"

    if not problems:
        guarded = ("success", "no protected path, deleted test or new skip/xfail")
    elif ready:
        guarded = ("success", f"passed by `{LABEL}`: {_short(problems[0])}")
    else:
        guarded = ("failure", _short(problems[0]))
    if limits:
        capped = ("failure", limits[0])
    else:
        capped = ("success", "within the automation limits")
    if issue is not None:
        linked = ("success", f"links #{issue}")
    elif ready:
        linked = ("success", f"no linked issue; passed by `{LABEL}`")
    else:
        linked = ("failure", "no linked issue: add `Closes #N` or `Refs #N`")
    return {
        ROUTE_STATUS: ("success", route),
        PROTECTED_STATUS: guarded,
        LIMITS_STATUS: capped,
        ISSUE_STATUS: linked,
        FINDINGS_STATUS: findings_verdict(gh, pull, ready, exempt),
    }


def findings_verdict(
    gh: GitHub, pull: dict[str, Any], ready: bool, exempt: bool
) -> tuple[str, str]:
    """``3 · Review Findings``: whether the head's OpenCode findings are handled."""
    if exempt:
        return "success", "no review needed: only review-exempt paths changed"
    sha, number = pull["head"]["sha"], int(pull["number"])
    if not _own_reviews(sha, _reviews(gh, number)):
        return "pending", "waiting for the OpenCode review of this commit"
    problems = review_finding_problems(gh, pull, _reviews(gh, number))
    if not problems:
        return "success", "every finding is handled"
    if ready:
        return "success", f"passed by `{LABEL}`: {_short(problems[0])}"
    return "failure", _short(problems[0])


def layer1(gh: GitHub, pull: dict[str, Any], run_url: str, ready: bool = False) -> None:
    """Post the layer-1 statuses on the PR's head that differ from what it already shows.

    Informational: a failure here never stops the gate (only the deadline does).
    """
    if pull.get("state", "open") != "open":
        return
    sha = pull["head"]["sha"]
    try:
        for context, (state, description) in layer1_verdicts(gh, pull, ready).items():
            description = description[:140]
            if gh.status_of(sha, context) != (state, description):
                gh.status(sha, state, description, run_url, context=context)
    except BudgetSpent:
        raise
    except Exception as exc:  # noqa: BLE001 - these statuses only report
        print(f"layer 1: statuses of {sha[:7]} not posted: {exc}")


def _epoch(stamp: str) -> float | None:
    """Epoch seconds of a GitHub timestamp (``2026-10-20T04:09:46Z``), None if unreadable."""
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def request_review(
    gh: GitHub,
    pull: dict[str, Any],
    sweeping: bool,
    now: float | None = None,
    force: bool = False,
) -> str:
    """Start the head's review when nothing else stops the PR; a note for the status ("" if none).

    The first time, the review is dispatched (a member's PR only). Afterwards only the sweep acts:
    it re-runs the head's newest run when that run completed as success, failure or timed out
    without a review, has had fewer than ``REVIEW_RERUNS`` attempts and ended
    ``REVIEW_RERUN_AFTER_S`` ago. A run still going or cancelled (a newer head replaced it) is
    left alone. ``force`` dispatches a review of a head the reviewer would skip as reviewed.
    """
    number, sha = int(pull["number"]), pull["head"]["sha"]
    runs = gh.review_runs(sha)
    if not runs:
        if str(pull.get("author_association") or "") not in TRUSTED_AUTHORS:
            return ""
        gh.dispatch_review(number, sha, force=force)
        forced = " (forced)" if force else ""
        print(f"auto: #{number}: dispatched the review of {sha[:7]}{forced}")
        return "review requested"
    run = max(
        runs, key=lambda r: str(r.get("run_started_at") or r.get("created_at") or "")
    )
    if run.get("status") != "completed":
        return "review running"
    attempt = int(run.get("run_attempt") or 1)
    if (
        not sweeping
        or run.get("conclusion") not in ("success", "failure", "timed_out")
        or attempt >= REVIEW_RERUNS
    ):
        return ""
    ended = _epoch(str(run.get("updated_at") or ""))
    if (
        ended is None
        or (time.time() if now is None else now) - ended < REVIEW_RERUN_AFTER_S
    ):
        return ""  # an unreadable end counts as too recent
    try:
        gh.call("POST", f"/actions/runs/{run['id']}/rerun")
    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:  # HTTPError too; BudgetSpent goes on
        print(
            f"auto: re-running review run {run.get('id')} for {sha[:7]} failed: {exc}"
        )
        return ""
    print(
        f"auto: {sha[:7]} has no review; re-ran review run {run.get('id')} ({attempt + 1})"
    )
    return f"review re-run ({attempt + 1}/{REVIEW_RERUNS})"


def verdict(body: str) -> str:
    """The review's verdict line: the first non-empty line after its heading ("" if none).

    Only that line counts; the same words later in the body (a summary or finding, which the
    model writes) are not the verdict.
    """
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] != REVIEW_HEADING:
        return ""
    return REVIEW_CLEAN if lines[1].startswith(REVIEW_CLEAN) else lines[1]


def _own_reviews(sha: str, reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reviewer's reviews that carry ``sha``'s marker."""
    return [
        r
        for r in reviews
        if (r.get("user") or {}).get("login") == REVIEW_AUTHOR
        and sha in REVIEW_MARKER.findall(str(r.get("body") or ""))
    ]


def review_problems(
    sha: str,
    reviews: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    review_jobs: Callable[[int], list[dict[str, Any]]],
    since: str = "",
) -> list[str]:
    """Why the OpenCode review does not clear ``sha`` (empty when it does).

    ``runs`` are main's review runs for ``sha``; ``review_jobs`` reads one run's ``review`` jobs,
    one per attempt. The review counts when such a job succeeded and was running when the review
    was posted. The job,
    not the run: the run is still going while its ``wake-gate`` job re-judges the PR (#94). Jobs
    are read only for a clean review, and only for runs that started before it was posted.
    """
    own = _own_reviews(sha, reviews)
    if not own:
        return [NO_REVIEW]
    latest = max(own, key=lambda r: str(r.get("submitted_at") or ""))
    if verdict(str(latest.get("body") or "")) != REVIEW_CLEAN:
        return [
            "the OpenCode review of this commit has blocking findings; fix them and push"
        ]
    at = str(latest.get("submitted_at") or "")
    # Only a run created before the review was posted can have posted it.
    candidates = [run for run in runs if str(run.get("created_at") or "") <= at]
    produced = any(
        job.get("conclusion") == "success"
        and str(job.get("started_at") or "") <= at <= str(job.get("completed_at") or "")
        for run in candidates
        for job in review_jobs(int(run["id"]))
    )
    if not produced:
        if not candidates:
            # No run could have posted it: review the head again (#95 F6). A later run, such as
            # that forced review, ending without a review is then re-run like any other (F7).
            return [UNATTRIBUTED]
        return [
            "the OpenCode review of this commit did not come from main's review workflow"
        ]
    if since and at < since:
        return ["the OpenCode review of this commit predates the PR's base change"]
    return []


# The reviewer's severities as priorities: P0 and P1 must be fixed, P2 must be answered.
PRIORITY = {"critical": "P0", "high": "P1", "medium": "P2"}
# Closed for a P0/P1 only when the reviewer says so: a reply or a resolved thread is not a fix.
MAJOR_HANDLED = frozenset({"fixed", "answered", "gone"})
MINOR_HANDLED = MAJOR_HANDLED | {"dismissed"}


def _priority(finding: dict[str, Any]) -> str:
    """P0, P1 or P2; an unknown severity counts as P1, so it is never only answered."""
    return PRIORITY.get(str(finding.get("severity") or ""), "P1")


def _open_findings(body: str) -> dict[str, str] | None:
    """Unhandled findings in the reviewer's bounded, structured ledger (ID -> P0/P1/P2).

    A P0/P1 is handled only once the reviewer marks it fixed or answered (or its file left the
    diff); a P2 also once its thread is resolved. None means missing or invalid evidence when the
    review reported findings. A review with no findings and no ledger has nothing open.
    """
    markers = REVIEW_STATE.findall(body)
    if not markers:
        if "New findings:" in body or "Still open from earlier rounds" in body:
            return None
        return {}
    try:
        encoded = markers[-1].replace("-", "+").replace("_", "/")
        encoded += "=" * (-len(encoded) % 4)
        packed = base64.b64decode(encoded, validate=True)
        if len(packed) > 100_000:
            return None
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, 250_001)
        if not decoder.eof or decoder.unconsumed_tail or len(raw) > 250_000:
            return None
        findings = json.loads(raw)["findings"]
        if not isinstance(findings, list) or len(findings) > 200:
            return None
        ids: dict[str, str] = {}
        for finding in findings:
            if not isinstance(finding, dict):
                return None
            fid = finding.get("id")
            if not isinstance(fid, str) or not re.fullmatch(r"F[1-9][0-9]*", fid):
                return None
            # An unknown status is outstanding, never silently closed.
            priority = _priority(finding)
            handled = MINOR_HANDLED if priority == "P2" else MAJOR_HANDLED
            if finding.get("status") not in handled:
                ids[fid] = priority
        return ids
    except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
        return None


def review_finding_problems(
    gh: GitHub, pull: dict[str, Any], reviews: list[dict[str, Any]]
) -> list[str]:
    """Require every P0/P1 finding fixed and every P2 finding answered (docs/ci "AI 审查发现").

    The reviewer's ledger names findings F1, F2, … across rounds, so PR text cannot erase one. A
    P0/P1 closes only when a later review marks it fixed or answered. A P2 closes with a thread
    reply from the PR's author or the repository owner, a resolved thread, or a conversation
    comment starting ``F2: <fix or reason>``.
    """
    sha, number = pull["head"]["sha"], int(pull["number"])
    own = _own_reviews(sha, reviews)
    if not own:
        return [NO_REVIEW]
    latest = max(own, key=lambda r: str(r.get("submitted_at") or ""))
    outstanding = _open_findings(str(latest.get("body") or ""))
    if outstanding is None:
        return [
            "the OpenCode finding ledger is missing or invalid; push to have it reviewed again"
        ]
    if not outstanding:
        return []
    order = lambda fid: int(fid[1:])
    major = sorted((f for f, p in outstanding.items() if p != "P2"), key=order)
    problems = []
    if major:
        listed = ", ".join(f"{fid} ({outstanding[fid]})" for fid in major[:8])
        problems.append(
            f"{len(major)} P0/P1 OpenCode finding(s) not fixed: {listed}; push the fix for the "
            f"next review to confirm (a reply or resolved thread does not close one; the "
            f"owner's `{LABEL}` does)"
        )
    minor = {f for f, p in outstanding.items() if p == "P2"}
    if not minor:
        return problems
    author = str((pull.get("user") or {}).get("login") or "").lower()
    actors = {author, gh.repo.split("/")[0].lower()} - {""}
    answered: set[str] = set()
    for thread in _threads(gh, number):
        if str(thread.get("author") or "").lower() not in {
            REVIEW_AUTHOR,
            "github-actions",
        }:
            continue
        match = REVIEW_FINDING.search(str(thread.get("body") or ""))
        if not match or match.group(1) not in minor:
            continue
        if thread.get("resolved") or any(
            str(reply.get("author") or "").lower() in actors
            and str(reply.get("body") or "").strip()
            for reply in thread.get("replies") or []
        ):
            answered.add(match.group(1))
    if minor - answered:
        since = str(latest.get("submitted_at") or "")
        for comment in _issue_comments(gh, number, since):
            if (
                str((comment.get("user") or {}).get("login") or "").lower()
                not in actors
            ):
                continue
            if str(comment.get("created_at") or "") < since:
                continue
            replies = FINDING_REPLY.findall(str(comment.get("body") or ""))
            answered.update(fid.upper() for fid in replies)
    missing = sorted(minor - answered, key=order)
    if missing:
        problems.append(
            f"{len(missing)} unanswered P2 OpenCode finding(s): {', '.join(missing[:8])}; "
            "reply with the fix or reason, or resolve each inline thread"
        )
    return problems


def auto_problems(gh: GitHub, pull: dict[str, Any], required: list[str]) -> list[str]:
    """Why the automatic route may not pass the PR's head (empty when it may)."""
    number, sha = int(pull["number"]), pull["head"]["sha"]
    labels = {label["name"] for label in pull.get("labels", [])}
    if pull.get("state", "open") != "open":
        return ["the PR is not open"]
    if pull.get("draft"):
        return ["the PR is a draft"]
    if HOLD_NAMES & set(labels):
        return [f"the PR is on `{HOLD}`"]
    if pull.get("mergeable") is False:
        return ["the PR conflicts with its base"]
    if pull.get("mergeable") is None:
        return ["GitHub has not finished checking whether the PR merges cleanly"]
    others = [n for n in gh.open_pulls_with_head(sha) if n != number]
    if others:
        return [f"head is also the head of open PR #{others[0]}"]
    # A base change judged another diff, and CI and the review do not re-run on `edited`: the head
    # needs a new push (or the tier-1 checks have to run again) before the earlier evidence counts.
    since = gh.base_changed_at(number)
    runs = gh.check_runs(sha)
    if since and any(
        r.get("started_at") and str(r["started_at"]) < since
        for n in required
        for r in latest_runs(runs, n)
    ):
        return ["tier 1 ran before the PR's base changed; push to run it again"]
    problems = tier1_problems(runs, required)
    policy = load_policy()
    entries = _file_entries(gh, number)
    problems += routing_problems(entries, policy)
    if linked_issue(gh, pull) is None:
        problems.append(NO_ISSUE)
    paths = sorted({str(e.get("filename") or "") for e in entries})
    if review_exempt(paths, policy):
        return problems
    reviews = _reviews(gh, number)
    review_runs = _review_runs(gh, sha) if _own_reviews(sha, reviews) else []
    review_issues = review_problems(sha, reviews, review_runs, gh.review_jobs, since)
    if not review_issues:
        review_issues = review_finding_problems(gh, pull, reviews)
    return problems + review_issues


def auto(
    gh: GitHub, number: int, required: list[str], run_url: str, wait: bool = True
) -> None:
    """Post ``merge-gate`` on the PR's head by the automatic route (no label).

    With ``wait`` off (the sweep), a PR whose ``mergeable`` GitHub has not computed yet does not
    wait up to 30 s for it: every other check still runs and any problem sets the gate pending
    (fail closed), but success is not posted until ``mergeable`` is known.

    Every write is checked after it lands (``_post``): GitHub stores a change to the PR before
    its event fires, so the read after the write either sees the change, or the change's own
    ``guard`` run starts after the write and posts over it. That is what keeps a run judging an
    old snapshot from restoring a gate that ``guard`` reset.
    """
    for attempt in range(REJUDGES + 1 if wait else 1):
        if attempt:
            print(f"auto: #{number}: mergeability was being recomputed; judging again")
            time.sleep(REJUDGE_DELAY_S)
        if not _judge(gh, number, required, run_url, wait):
            return


def _judge(
    gh: GitHub, number: int, required: list[str], run_url: str, wait: bool
) -> bool:
    """One judgement of ``auto``; True when only GitHub recomputing ``mergeable`` stopped it."""
    pull = gh.pull(number, wait=wait)
    if LABEL in {label["name"] for label in pull.get("labels", [])}:
        override(gh, pull, required, run_url)  # the maintainer route posts the gate
        return False
    unknown = (
        not wait
        and pull.get("mergeable") is None
        and pull.get("state", "open") == "open"
    )
    with judging(gh):
        problems = auto_problems(
            gh, {**pull, "mergeable": True} if unknown else pull, required
        )
        layer1(gh, pull, run_url)
    routing = [p for p in problems if p.startswith(ROUTING)]
    rest = [p for p in problems if not p.startswith(ROUTING)]
    if rest in ([NO_REVIEW], [UNATTRIBUTED]):
        problems = rest
        # Nothing but the review stops the PR, so it is worth a gateway slot now.
        if str(pull.get("author_association") or "") not in TRUSTED_AUTHORS:
            problems = [UNTRUSTED]
        else:
            force = problems == [UNATTRIBUTED]
            note = request_review(gh, pull, sweeping=not wait, force=force)
            if note:
                problems = [f"{problems[0]}; {note}"]
        problems = routing + problems  # layer 1 first: it is what the owner must act on
    if not problems:
        if unknown:
            print(
                f"auto: #{number}: mergeable not computed yet; left for the next sweep"
            )
            return False
        now = gh.pull(number, wait=False)
        if _changed(now, pull):
            print(
                f"auto: #{number}: the PR changed while it was judged; nothing posted"
            )
            return _recomputing(now, pull)
        if gh.gate_description(pull["head"]["sha"]) == AUTO_SUCCESS:
            return False  # identical periodic evaluations are a no-op
        return _post(gh, pull, "success", AUTO_SUCCESS, run_url, required)
    # Only tier 1's own wording, never a model-written reason that happens to say "concluded".
    if any(TIER1_CONCLUDED.match(p) for p in problems):
        print(f"auto: #{number}: {problems[0]} (revoke has failed the gate)")
        return False
    description = f"{AUTO_PENDING}{problems[0]}"
    if gh.gate_description(pull["head"]["sha"]) != description[:140]:
        _post(gh, pull, "pending", description, run_url, required)
    return False


CHANGED = "the PR changed while it was judged; the next event or sweep judges it again"
UNCHECKED = (
    "the success could not be re-checked; the next event or sweep judges it again"
)


def _changed(now: dict[str, Any], judged: dict[str, Any]) -> bool:
    """Whether the PR moved from the snapshot ``auto`` judged in a way that blocks success."""
    labels = {label["name"] for label in now.get("labels", [])}
    return bool(
        now["head"]["sha"] != judged["head"]["sha"]
        or now["base"].get("ref") != judged["base"].get("ref")
        or now.get("state", "open") != "open"
        or now.get("draft")
        or now.get("mergeable")
        is not True  # a conflict, or GitHub recomputing after a base move
        or {LABEL, *HOLD_NAMES} & labels
    )


def _recomputing(now: dict[str, Any], judged: dict[str, Any]) -> bool:
    """Whether the only change since ``judged`` is GitHub recomputing ``mergeable`` (a base move).

    Nothing else will wake the route for that: no event fires when the computation ends.
    """
    return bool(
        now.get("mergeable") is None
        and _changed({**now, "mergeable": True}, judged) is False
    )


def _post(
    gh: GitHub,
    pull: dict[str, Any],
    state: str,
    description: str,
    run_url: str,
    required: list[str],
) -> bool:
    """Post the automatic route's gate, then re-read the PR and repair what changed meanwhile.

    True when the repair was only mergeability being recomputed: the caller judges again.
    """
    number, sha = int(pull["number"]), pull["head"]["sha"]
    gh.status(sha, state, description, run_url)
    try:
        after = gh.pull(number, wait=False)
        # A second PR opened with this head meanwhile shares this commit's status; the gate never
        # passes such a commit (auto_problems), so a success is checked against it too.
        others = (
            [n for n in gh.open_pulls_with_head(sha) if n != number]
            if state == "success"
            else []
        )
    except Exception:
        # An unchecked success does not stand, whatever stopped the check (the deadline, GitHub);
        # this write is fail-closed, so it goes out even past the deadline.
        if state == "success":
            gh.status(sha, "pending", f"{AUTO_PENDING}{UNCHECKED}", run_url)
        raise
    if others:
        # Checked before anything else: the commit may still be the other PR's head even when
        # this PR's head has moved on, so its status still matters there.
        shared = f"head is also the head of open PR #{others[0]}"
        gh.status(sha, "pending", f"{AUTO_PENDING}{shared}", run_url)
        return False
    if after["head"]["sha"] != sha:
        return (
            False  # a new head has its own gate; this commit's status no longer matters
        )
    if LABEL in {label["name"] for label in after.get("labels", [])}:
        # The maintainer route took over meanwhile. Its guard either runs after this write and
        # posts over it, or already ran: reach the verdict it reaches.
        override(gh, after, required, run_url)
        return False
    if state == "success" and _changed(after, pull):
        gh.status(sha, "pending", f"{AUTO_PENDING}{CHANGED}", run_url)
        return _recomputing(after, pull)
    return False


# A base move makes GitHub recompute `mergeable` for a while, and no event ends that: judge again.
REJUDGES = 2
REJUDGE_DELAY_S = 30.0
AUTO_SUCCESS = "tier 1 passed and the AI review is clean (automatic)"
AUTO_PENDING = "automatic route: "
OVERRIDE_SUCCESS = f"tier 1 passed; AI review overridden by a maintainer (`{LABEL}`)"
OVERRIDE_PENDING = f"`{LABEL}`: "
BASE_CHANGED = "the PR's base changed; push to run tier 1 again"
# The sweep's job has 5 minutes and its script fetch may take 2: no PR is started after
# SWEEP_BUDGET_S, and no API call is made after SWEEP_BUDGET_S + SWEEP_GRACE_S (150 s < 180 s).
SWEEP_BUDGET_S = 120.0
SWEEP_GRACE_S = 30.0
WRITE_TIMEOUT_S = 10.0
SWEEP_SLOT_S = 900  # the schedule's interval; the starting PR moves on by one each slot


def sweep(
    gh: GitHub, required: list[str], run_url: str, budget_s: float = SWEEP_BUDGET_S
) -> None:
    """Re-evaluate every open PR whose ``merge-gate`` the automatic route or a tier-1 failure set.

    A PR comment or a thread change that runs no trusted main code still needs a fallback, so a
    scheduled sweep catches it. The sweep never waits for ``mergeable``, starts no PR after
    ``budget_s`` seconds and makes no API call after ``SWEEP_GRACE_S`` more, so it ends inside its
    job's timeout. It starts one PR further along each 15-minute slot, so when the budget runs out
    the same PRs are not the ones left over every time.
    """
    start = time.monotonic()
    gh.deadline = start + budget_s + SWEEP_GRACE_S  # listing the PRs counts too
    pull: dict[str, Any] = {"number": "none"}
    try:
        pulls = sorted(gh.open_pulls(), key=lambda p: int(p["number"]))
        if pulls:
            first = int(time.time() // SWEEP_SLOT_S) % len(pulls)
            pulls = pulls[first:] + pulls[:first]
        for index, pull in enumerate(pulls):
            if time.monotonic() >= start + budget_s:
                print(f"auto: sweep budget spent; {len(pulls) - index} PR(s) left over")
                return
            sha = pull["head"]["sha"]
            ready = LABEL in {label["name"] for label in pull.get("labels", [])}
            description = gh.gate_description(sha)
            # A tier-1 failure posted while the head's workflow runs could not be read may have
            # been superseded since: it is re-judged once no tier-1 workflow's latest run failed
            # (on either route; the maintainer route's other gates are left to its events).
            failure = description.endswith(TIER1_FAILED) or (
                ready
                and description.startswith(OVERRIDE_PENDING)
                and TIER1_CONCLUDED.match(description.removeprefix(OVERRIDE_PENDING))
            )
            if failure:
                if tier1_workflow_failed(gh, sha):
                    continue
            elif ready or not (
                description == AUTO_SUCCESS or description.startswith(AUTO_PENDING)
            ):
                continue
            auto(gh, int(pull["number"]), required, run_url, wait=False)
    except BudgetSpent as spent:
        print(
            f"auto: sweep time is up before {spent}; #{pull['number']} and later left over"
        )
    finally:
        gh.deadline = None


def auto_for_sha(gh: GitHub, sha: str, required: list[str], run_url: str) -> None:
    """Run the automatic route for the one open PR whose head is ``sha``."""
    numbers = gh.open_pulls_with_head(sha)
    if len(numbers) != 1:
        print(f"auto: {len(numbers)} open PRs have head {sha[:7]}; nothing posted")
        return
    auto(gh, numbers[0], required, run_url)


def override(
    gh: GitHub,
    pull: dict[str, Any],
    required: list[str],
    run_url: str,
    sender: str = "",
) -> list[str]:
    """Post the maintainer route's gate for a PR that has ``ready``; the problems ([] if green).

    The person who added the label is ``sender`` on the labeled event, else read from the PR's
    events. Problems leave the gate pending; the caller decides whether the label goes.
    """
    number, sha = int(pull["number"]), pull["head"]["sha"]
    who = sender or gh.labeled_by(number)
    problems = ready_problems(
        gh.role(who) if who else "none", pull, gh.check_runs(sha), required
    )
    others = [n for n in gh.open_pulls_with_head(sha) if n != number]
    if others:
        problems.append(f"head is also the head of open PR #{others[0]}")
    if problems:
        description = f"{OVERRIDE_PENDING}{problems[0]}"
        if gh.gate_description(sha) != description[:140]:
            gh.status(sha, "pending", description, run_url)
    elif gh.gate_description(sha) != OVERRIDE_SUCCESS:
        gh.status(sha, "success", OVERRIDE_SUCCESS, run_url)
    layer1(gh, pull, run_url, ready=not problems)
    return problems


def required_checks() -> list[str]:
    """The layer-2 and layer-3 checks every PR head must pass (env override, else the policy)."""
    if os.environ.get("REQUIRED_CHECKS"):
        return [c for c in os.environ["REQUIRED_CHECKS"].split(",") if c]
    return [str(c) for c in load_policy()["required_checks"]]


def _output(name: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    print(f"{name}={value}")


def guard(gh: GitHub, event: dict[str, Any], required: list[str], run_url: str) -> bool:
    """Handle one gate event; True when a downstream gate should run on the head commit.

    Nothing follows ``guard`` any more (there is no scenario gate), so it always returns False;
    the return value is kept for the workflow's output.
    """
    action = event.get("action")
    pr = event["pull_request"]
    number, sha = int(pr["number"]), pr["head"]["sha"]
    labels = {label["name"] for label in pr.get("labels", [])}
    if action == "opened":
        gh.status(sha, "pending", WAITING)
        layer1(gh, pr, run_url)
        return False
    if action in ("synchronize", "reopened", "edited") and LABEL in labels:
        # The override vouched for one title, body, base and head; any change voids it.
        gh.status(sha, "pending", WAITING)
        layer1(gh, pr, run_url)
        gh.remove_label(number)
        what = (
            "The title, body or base changed"
            if action == "edited"
            else f"New commits (head `{sha[:7]}`)"
        )
        gh.comment(
            number,
            f"{what} since `{LABEL}`, so the label was removed. The automatic route judges the "
            "new head once tier 1 and the AI review are done; a maintainer can add it again.",
        )
        return False
    if action == "edited":
        # Only a base change makes the automatic route's evidence stale (CI and the review do not
        # re-run on `edited`). Any other state (a failure from revoke, a pending gate) already
        # blocks the merge and stays.
        if (
            "base" in (event.get("changes") or {})
            and gh.gate_description(sha) == AUTO_SUCCESS
        ):
            gh.status(sha, "pending", f"{AUTO_PENDING}{BASE_CHANGED}", run_url)
            # revoke may have failed the gate between the read and this write; a failed tier-1
            # check is complete before revoke runs, so reading it now puts the failure back on top.
            failed = [
                p
                for p in tier1_problems(gh.check_runs(sha), required)
                if "concluded" in p
            ]
            if failed:
                gh.status(sha, "failure", f"{failed[0]}{TIER1_FAILED}", run_url)
        return False
    if action in ("synchronize", "reopened"):
        gh.status(sha, "pending", WAITING)
        layer1(gh, pr, run_url)
        return False
    if action in ("converted_to_draft", "ready_for_review"):
        auto(gh, number, required, run_url)
        return False
    label = (event.get("label") or {}).get("name")
    if label in HOLD_NAMES and action in ("labeled", "unlabeled"):
        auto(gh, number, required, run_url)
        return False
    if label != LABEL:
        return False
    if action == "unlabeled":
        auto(gh, number, required, run_url)  # back to the automatic route
        return False
    if action != "labeled":
        return False
    sender = str((event.get("sender") or {}).get("login") or "")
    problems = override(gh, gh.pull(number), required, run_url, sender=sender)
    if problems:
        gh.remove_label(number)
        gh.comment(
            number,
            f"`{LABEL}` was removed (added by @{sender}):\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nFix that and add it again, or let the automatic route merge the PR once "
            + f"tier 1 and the AI review pass. [Gate run]({run_url})",
        )
        auto(gh, number, required, run_url)
    return False


def revoke(
    gh: GitHub, sha: str, workflow: str, conclusion: str, run_url: str, run_id: int = 0
) -> None:
    """Reset ``merge-gate`` on ``sha`` while a tier-1 run there runs or after it failed.

    A failed run (``run_id``) that a newer passing run of the same workflow and event on ``sha``
    has replaced is left alone: tier 1 judges the newer one, and ``auto`` may already have posted
    success. If the runs cannot be read, the failure stands.
    """
    if conclusion == "success" or conclusion in NO_VERDICT:
        return
    if conclusion == "in_progress":
        gh.status(sha, "pending", f"{workflow} is running again")
        return
    if run_id and _superseded(gh, sha, run_id):
        print(
            f"revoke: run {run_id} of {workflow} is superseded on {sha}; gate left as it is"
        )
        return
    gh.status(
        sha,
        "failure",
        f"{workflow} concluded {conclusion}{TIER1_FAILED}",
        run_url,
    )


def tier1_workflow_failed(gh: GitHub, sha: str) -> bool:
    """Whether the latest ``pull_request`` run of some workflow on ``sha`` has not passed.

    These are the runs ``revoke`` reacts to; their conclusion covers every job, also one outside
    the required checks, and a workflow that failed to start. Runs that carry no verdict are left
    out. A latest run still queued or running counts as not passed: it has no verdict yet, and
    its success re-judges the gate anyway. True when the runs cannot be read.
    """
    try:
        runs = gh.workflow_runs(sha)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(
            f"::warning::workflow runs of {sha} unreadable ({exc}); the failure stands"
        )
        return True
    latest: dict[Any, dict[str, Any]] = {}
    for run in runs:
        if run.get("event") != "pull_request" or run.get("conclusion") in NO_VERDICT:
            continue
        key = run.get("path")
        if key not in latest or int(run.get("id") or 0) > int(
            latest[key].get("id") or 0
        ):
            latest[key] = run
    return any(run.get("conclusion") != "success" for run in latest.values())


def _superseded(gh: GitHub, sha: str, run_id: int) -> bool:
    """Whether a newer run of the same workflow and event as run ``run_id`` exists on ``sha``.

    Only a newer run that passed counts. One that was cancelled or skipped carries no verdict
    (tier 1 leaves it out too), and one still running may yet be: the failure stands until it
    passes, and its success re-judges the gate.
    """
    try:
        runs = gh.workflow_runs(sha)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(
            f"::warning::workflow runs of {sha} unreadable ({exc}); the failure stands"
        )
        return False
    mine = next((r for r in runs if r.get("id") == run_id), None)
    return mine is not None and any(
        int(r.get("id") or 0) > run_id
        and r.get("conclusion") == "success"
        and (r.get("path"), r.get("event")) == (mine.get("path"), mine.get("event"))
        for r in runs
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the gate workflow."""
    parser = argparse.ArgumentParser(prog="ci_gate", description="merge gate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("guard", help="handle a pull_request_target event")
    aut = sub.add_parser(
        "auto", help="post merge-gate by the automatic route (no label)"
    )
    aut.add_argument("--pr", type=int)
    aut.add_argument("--sha")
    aut.add_argument(
        "--all", action="store_true", help="every PR the automatic route posted"
    )
    rev = sub.add_parser("revoke", help="fail merge-gate after a failed tier-1 run")
    rev.add_argument("--sha", required=True)
    rev.add_argument("--workflow", required=True)
    rev.add_argument("--conclusion", required=True)
    rev.add_argument(
        "--run-id", type=int, default=0, help="the workflow run that ended"
    )
    args = parser.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{gh.repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    required = required_checks()
    if args.command == "guard":
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
            event = json.load(fh)
        proceed = guard(gh, event, required, run_url)
        _output("proceed", "true" if proceed else "false")
        _output("sha", event["pull_request"]["head"]["sha"])
        return 0
    if args.command == "auto":
        if args.all:
            sweep(gh, required, run_url)
        elif args.pr:
            auto(gh, args.pr, required, run_url)
        elif args.sha:
            auto_for_sha(gh, args.sha, required, run_url)
        else:
            parser.error("auto needs --pr or --sha")
        return 0
    revoke(gh, args.sha, args.workflow, args.conclusion, run_url, args.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
