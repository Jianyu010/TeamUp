"""TeamUp build versions (docs/ci/README.md, "版本与发布").

Released versions are the tags ``vX.Y.Z``. A ``main`` commit after the newest tag is the build
``X.Y.(Z+1).devN`` of the next patch, where N counts the first-parent commits since that tag: one per
merge (or direct owner push) into ``main``. The tagged commit itself is ``X.Y.Z``.

Works from the GitHub API (the runners get trees as tarballs, without ``.git``)::

    python3 -m scripts.ci.version --sha <commit>          # prints e.g. 0.1.1.dev3
    python3 -m scripts.ci.version --sha <commit> --json   # tag, base, count, version

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any

from scripts.ci.gate import GitHub

TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
Version = tuple[int, int, int]


def parse(tag: str) -> Version | None:
    match = TAG.match(tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def render(version: Version) -> str:
    return ".".join(map(str, version))


def tags(gh: GitHub) -> dict[Version, str]:
    """Every release tag: version → commit sha."""
    found: dict[Version, str] = {}
    for item in gh.pages("/tags", limit=20):
        version = parse(str(item.get("name") or ""))
        if version:
            found[version] = str(item["commit"]["sha"])
    return found


def first_parents(gh: GitHub, base: str, head: str) -> list[str]:
    """The first-parent chain from ``head`` back to (not including) ``base``, newest first.

    Raises ValueError when ``base`` is not an ancestor of ``head``: a release tag off ``main``'s
    history means the version cannot be derived, and nothing is released from it.
    """
    if base == head:
        return []
    parents: dict[str, list[str]] = {}
    status = ""
    for page in range(1, 11):
        data = (
            gh.call("GET", f"/compare/{base}...{head}?per_page=100&page={page}") or {}
        )
        status = str(data.get("status") or "")
        commits = data.get("commits") or []
        for commit in commits:
            parents[str(commit["sha"])] = [
                str(p["sha"]) for p in commit.get("parents") or []
            ]
        if len(commits) < 100:
            break
    if status not in ("ahead", "identical"):
        raise ValueError(
            f"{base[:7]} is not an ancestor of {head[:7]} (compare: {status})"
        )
    chain: list[str] = []
    sha = head
    while sha != base:
        if sha not in parents:
            raise ValueError(
                f"first-parent chain from {head[:7]} leaves the compared range"
            )
        chain.append(sha)
        sha = parents[sha][0]
    return chain


@dataclass(frozen=True)
class Build:
    sha: str
    tag: str | None
    tag_sha: str | None
    count: int
    version: str


def build(gh: GitHub, sha: str) -> Build:
    """The build version of commit ``sha`` relative to the newest release tag."""
    released = tags(gh)
    if not released:
        return Build(sha, None, None, 0, "0.0.0")
    latest = max(released)
    count = len(first_parents(gh, released[latest], sha))
    if count == 0:
        return Build(sha, f"v{render(latest)}", released[latest], 0, render(latest))
    major, minor, patch = latest
    return Build(
        sha,
        f"v{render(latest)}",
        released[latest],
        count,
        f"{major}.{minor}.{patch + 1}.dev{count}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="version", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    result: Any = build(gh, args.sha)
    print(json.dumps(asdict(result)) if args.json else result.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
