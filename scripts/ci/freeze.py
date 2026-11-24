"""Freeze and thaw auto-merge (layer 5, docs/ci/README.md).

A freeze is an open issue labelled ``ci/freeze`` whose title starts with ``[ci:<source>]``.
While one is open the merger merges only PRs the owner passed with ``ready`` (scripts/ci/merge.py),
so a fix or revert can land and nothing else piles onto a red ``main``. Each source owns its own
issue: ``main`` (PR checks on a push to main) and ``nightly``. The same source clears it when it
is green again; a person can also close the issue. ``weekly`` uses the same mechanics with the
``weekly-failure`` label instead: real-agent runs can be flaky, so a red weekly reports but never
freezes.

    python3 -m scripts.ci.freeze set --source nightly --reason "NS-05 failed" --url RUN_URL
    python3 -m scripts.ci.freeze clear --source nightly --url RUN_URL

Standard library only; runs on a trusted runner with main's code and the workflow token.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from typing import Any

from scripts.ci.gate import GitHub, load_policy

SOURCES = ("main", "nightly", "weekly")
REPORT_ONLY = {"weekly": "weekly-failure"}


def title_prefix(source: str) -> str:
    return f"[ci:{source}]"


def label_for(source: str, freeze_label: str) -> str:
    return REPORT_ONLY.get(source, freeze_label)


def open_freezes(gh: GitHub, label: str, source: str) -> list[dict[str, Any]]:
    """Open freeze issues of ``source`` (PRs are left out: the issues API lists them too)."""
    quoted = urllib.parse.quote(label, safe="")
    return [
        issue
        for issue in gh.pages(f"/issues?state=open&labels={quoted}", limit=3)
        if "pull_request" not in issue
        and str(issue.get("title") or "").startswith(title_prefix(source))
    ]


def set_freeze(gh: GitHub, label: str, source: str, reason: str, url: str) -> None:
    """Open the source's freeze issue, or comment on the one already open."""
    body = f"{reason}\n\n[Run]({url})" if url else reason
    existing = open_freezes(gh, label, source)
    if existing:
        number = int(existing[0]["number"])
        gh.comment(number, f"Still red. {body}")
        print(f"freeze: #{number} already open for {source}; commented")
        return
    if source in REPORT_ONLY:
        title = f"{title_prefix(source)} {source} is red"
        tail = f"Auto-merge is not frozen. This issue closes itself when {source} is green again."
    else:
        title = f"{title_prefix(source)} auto-merge frozen: {source} is red"
        tail = (
            "Auto-merge is frozen: the merger only merges PRs the owner passed with `ready`. Land "
            f"the fix or revert that way; this issue closes itself when {source} is green again."
        )
    issue = gh.call(
        "POST",
        "/issues",
        {"title": title, "labels": [label], "body": f"{body}\n\n{tail}"},
    )
    print(f"freeze: opened #{(issue or {}).get('number')} for {source}")


def clear_freeze(gh: GitHub, label: str, source: str, url: str) -> None:
    """Close the source's open freeze issues with a note."""
    for issue in open_freezes(gh, label, source):
        number = int(issue["number"])
        resumed = "" if source in REPORT_ONLY else "; auto-merge resumes"
        note = f"{source} is green again{resumed}." + (f" [Run]({url})" if url else "")
        gh.comment(number, note)
        gh.call(
            "PATCH",
            f"/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
        )
        print(f"freeze: closed #{number} ({source} green)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freeze", description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("set", "clear"))
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--url", default="")
    args = parser.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    label = label_for(
        args.source, str(load_policy().get("freeze_label") or "ci/freeze")
    )
    if args.action == "set":
        set_freeze(
            gh, label, args.source, args.reason or f"{args.source} is red", args.url
        )
    else:
        clear_freeze(gh, label, args.source, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
