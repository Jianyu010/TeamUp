"""The labeling standard's automation (docs/ci/labeling.md).

    python3 -m scripts.ci.labels sync                  # create/update every manifest label
    python3 -m scripts.ci.labels lint --issue N        # cardinality → status/triage + one comment
    python3 -m scripts.ci.labels triage --issue N      # human-filed issue → human-only + review-needed
    python3 -m scripts.ci.labels pr --pr N | --sha S | --all  # gate/*-failed, type/*, pr/no-issue

Everything reads issue and PR metadata only; no repository code from a PR runs. Labels written
with the workflow token start no workflow, so none of this retriggers itself. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from scripts.ci.gate import GitHub, load_policy, referenced_numbers

MANIFEST = Path(__file__).resolve().parents[2] / "docs" / "ci" / "labeling.md"
MANIFEST_HEADING = "## 清单"
LINT_MARKER = "<!-- label-lint -->"
TRIAGE_MARKER = "<!-- issue-triage -->"
BOT_LOGIN = "github-actions[bot]"
TRIAGE = "status/triage"
HUMAN_ONLY = "agent/human-only"
REVIEW_NEEDED = "status/review-needed"
AGENT_OK = "agent/ok"
NO_ISSUE = "pr/no-issue"
# Required check (job name) → the gate label it sets while failing.
GATE_LABELS = {
    "2 · Lint": "gate/lint-failed",
    "2 · Type Check": "gate/type-check-failed",
    "2 · Secrets": "gate/secrets-failed",
    "2 · Workflow Lint": "gate/workflow-lint-failed",
    "2 · Dependencies": "gate/dependencies-failed",
    "2 · Architecture": "gate/architecture-failed",
    "3 · Linux UT": "gate/linux-ut-failed",
    "3 · Linux ST": "gate/linux-st-failed",
    "3 · Linux ST · 跨组件": "gate/linux-st-cross-failed",
    "3 · Contract": "gate/contract-failed",
    "3 · Install": "gate/install-failed",
}
REVIEW_LABEL = "gate/ai-review-failed"
FAILED = frozenset({"failure", "timed_out", "startup_failure"})


def parse_manifest(text: str) -> dict[str, tuple[str, str]]:
    """Label → (color, description) from the manifest table."""
    labels: dict[str, tuple[str, str]] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip().startswith(MANIFEST_HEADING)
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if (
            len(cells) < 3
            or cells[0] in ("标签", "Label", "")
            or set(cells[0]) <= set("-: ")
        ):
            continue
        labels[cells[0]] = (cells[1].lstrip("#").upper(), cells[2])
    return labels


def read_manifest(path: Path = MANIFEST) -> dict[str, tuple[str, str]]:
    return parse_manifest(path.read_text(encoding="utf-8"))


def problems(names: list[str]) -> list[str]:
    """What an issue's labels still lack under the standard (empty when it passes)."""

    def count(axis: str) -> int:
        return sum(1 for n in names if n.startswith(f"{axis}/"))

    found: list[str] = []
    if count("type") < 1:
        found.append("至少一个 `type/*`")
    if count("source") < 1:
        found.append("至少一个 `source/*`")
    if count("priority") == 0:
        found.append("正好一个 `priority/*`（目前没有；拿不准时用 `priority/medium`）")
    elif count("priority") > 1:
        found.append("正好一个 `priority/*`（目前有多个）")
    if count("size") > 1:
        found.append("最多一个 `size/*`")
    if count("agent") > 1:
        found.append("最多一个 `agent/*`")
    return found


def meets_lint(names: list[str]) -> bool:
    return not problems(names)


def _names(issue: dict[str, Any]) -> list[str]:
    return [
        str(x["name"] if isinstance(x, dict) else x) for x in issue.get("labels") or []
    ]


def _quote(label: str) -> str:
    return urllib.parse.quote(label, safe="")


def _remove(gh: GitHub, number: int, label: str) -> None:
    try:
        gh.call("DELETE", f"/issues/{number}/labels/{_quote(label)}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def _add(gh: GitHub, number: int, labels: list[str]) -> None:
    if labels:
        gh.call("POST", f"/issues/{number}/labels", {"labels": labels})


def _own_comment(gh: GitHub, number: int, marker: str) -> dict[str, Any] | None:
    for comment in gh.pages(f"/issues/{number}/comments", limit=5):
        author = (comment.get("user") or {}).get("login")
        if author == BOT_LOGIN and str(comment.get("body") or "").startswith(marker):
            return comment
    return None


def _write_comment(
    gh: GitHub, number: int, marker: str, body: str, create: bool = True
) -> None:
    """One marker comment per issue, edited in place; ``create`` False only edits."""
    text = f"{marker}\n{body}"
    mine = _own_comment(gh, number, marker)
    if mine is not None:
        if mine.get("body") != text:
            gh.call("PATCH", f"/issues/comments/{mine['id']}", {"body": text})
    elif create:
        gh.call("POST", f"/issues/{number}/comments", {"body": text})


# ---- sync ---------------------------------------------------------------------------------


def sync(gh: GitHub, manifest: dict[str, tuple[str, str]]) -> list[str]:
    """Create missing labels and fix color/description drift; never delete one."""
    current = {str(x["name"]): x for x in gh.pages("/labels", limit=5)}
    done: list[str] = []
    for name, (color, description) in manifest.items():
        have = current.get(name)
        if have is None:
            gh.call(
                "POST",
                "/labels",
                {"name": name, "color": color, "description": description},
            )
            done.append(f"created {name}")
        elif (
            str(have.get("color") or "").upper() != color
            or (have.get("description") or "") != description
        ):
            gh.call(
                "PATCH",
                f"/labels/{_quote(name)}",
                {"color": color, "description": description},
            )
            done.append(f"updated {name}")
    return done


# ---- issues -------------------------------------------------------------------------------


def lint(gh: GitHub, number: int) -> str:
    """Add or clear ``status/triage`` and keep one comment saying what is missing."""
    issue = gh.call("GET", f"/issues/{number}")
    if "pull_request" in issue:
        return "skipped: a pull request"
    if issue.get("state") != "open":
        return "skipped: closed"
    names = _names(issue)
    missing = problems(names)
    if missing:
        if TRIAGE not in names:
            _add(gh, number, [TRIAGE])
        _write_comment(
            gh,
            number,
            LINT_MARKER,
            "这个 issue 还不符合[标签规范](../blob/main/docs/ci/labeling.md)，还缺：\n\n"
            + "\n".join(f"- {p}" for p in missing)
            + "\n\n补齐之前 `status/triage` 会一直保留，请不要开工。",
        )
        return "triage: " + "; ".join(missing)
    if TRIAGE in names:
        _remove(gh, number, TRIAGE)
    _write_comment(
        gh, number, LINT_MARKER, "标签已齐全，已移除 `status/triage`。", create=False
    )
    return "labels complete"


def is_bot(issue: dict[str, Any]) -> bool:
    user = issue.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login") or "").endswith("[bot]")


def triage(gh: GitHub, number: int, owner: str) -> str:
    """A human-filed issue gets both review labels, an assignee and one comment."""
    issue = gh.call("GET", f"/issues/{number}")
    if "pull_request" in issue:
        return "skipped: a pull request"
    if is_bot(issue):
        return "skipped: filed by a bot"
    names = _names(issue)
    if AGENT_OK in names:
        return "skipped: a maintainer already granted agent/ok"
    _add(gh, number, [x for x in (HUMAN_ONLY, REVIEW_NEEDED) if x not in names])
    if not issue.get("assignees") and owner:
        gh.call("POST", f"/issues/{number}/assignees", {"assignees": [owner]})
    _write_comment(
        gh,
        number,
        TRIAGE_MARKER,
        f"@{owner} 这是人提交的 issue，已标为 `agent/human-only` 和 `status/review-needed`。"
        "维护者看过之后再决定是否交给 agent（`agent/ok`）。",
    )
    return "triaged"


# ---- pull requests ------------------------------------------------------------------------


def gate_failures(
    check_runs: list[dict[str, Any]], review_runs: list[dict[str, Any]]
) -> set[str]:
    """The gate labels a head deserves now: failed required checks and a failed review run."""
    failing: set[str] = set()
    for run in check_runs:
        label = GATE_LABELS.get(str(run.get("name") or ""))
        if (
            label
            and run.get("status") == "completed"
            and run.get("conclusion") in FAILED
        ):
            failing.add(label)
    if review_runs:
        latest = max(
            review_runs,
            key=lambda r: str(r.get("run_started_at") or r.get("created_at") or ""),
        )
        if latest.get("status") == "completed" and latest.get("conclusion") in FAILED:
            failing.add(REVIEW_LABEL)
    return failing


def plan_pr(
    names: list[str], failing: set[str], issue_types: list[str], linked: bool
) -> tuple[list[str], list[str]]:
    """(labels to add, labels to remove) for one PR."""
    want = set(failing) | set(issue_types)
    if not linked:
        want.add(NO_ISSUE)
    managed = {n for n in names if n.startswith("gate/") or n == NO_ISSUE}
    add = sorted(n for n in want if n not in names)
    remove = sorted(n for n in managed if n not in want)
    return add, remove


def label_pr(gh: GitHub, pull: dict[str, Any]) -> str:
    number, sha = int(pull["number"]), str(pull["head"]["sha"])
    names = _names(pull)
    # The same rule as the gate's `1 · Linked Issue`: any reference to an issue, not a PR.
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    refs = [n for n in referenced_numbers(text, gh.repo) if n != number][:20]
    types: list[str] = []
    issues: list[int] = []
    for n in refs:
        try:
            issue = gh.call("GET", f"/issues/{n}")
        except (
            urllib.error.URLError,
            TimeoutError,
        ):  # one bad lookup skips only that number
            continue
        if isinstance(issue, dict) and "pull_request" not in issue:
            issues.append(n)
            types += [x for x in _names(issue) if x.startswith("type/")]
    failing = gate_failures(gh.check_runs(sha), gh.review_runs(sha))
    add, remove = plan_pr(names, failing, types, bool(issues))
    _add(gh, number, add)
    for label in remove:
        _remove(gh, number, label)
    return f"+{','.join(add) or '-'} -{','.join(remove) or '-'}"


# ---- CLI ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labels", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    for name in ("lint", "triage"):
        sub.add_parser(name).add_argument("--issue", type=int, required=True)
    pr = sub.add_parser("pr")
    which = pr.add_mutually_exclusive_group(required=True)
    which.add_argument("--pr", type=int)
    which.add_argument("--all", action="store_true")
    which.add_argument("--sha", help="the open PR(s) whose head is this commit")
    args = parser.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    if args.command == "sync":
        for line in sync(gh, read_manifest()) or ["labels already match the manifest"]:
            print(line)
    elif args.command == "lint":
        print(f"#{args.issue}: {lint(gh, args.issue)}")
    elif args.command == "triage":
        owner = str(load_policy().get("owner") or "")
        print(f"#{args.issue}: {triage(gh, args.issue, owner)}")
    else:
        if args.pr:
            pulls = [gh.call("GET", f"/pulls/{args.pr}")]
        else:
            pulls = gh.pages("/pulls?state=open", limit=3)
            if args.sha:
                pulls = [p for p in pulls if p["head"]["sha"] == args.sha]
        for pull in pulls:
            try:
                print(f"#{pull['number']}: {label_pr(gh, pull)}")
            except (
                urllib.error.URLError,
                TimeoutError,
            ) as exc:  # one PR never stops the rest
                print(f"#{pull['number']}: labeling failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
