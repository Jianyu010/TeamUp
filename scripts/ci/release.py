"""Cut a TeamUp release with an automated summary of what changed (docs/ci/README.md).

- ``--kind patch`` (a green nightly): when ``main`` has first-parent commits since the newest tag
  ``vX.Y.Z``, tag ``vX.Y.(Z+1)`` at the tested commit.
- ``--kind minor`` (a green weekly): when ``main`` has first-parent commits since ``vX.Y.0`` (the
  minor line of the newest tag), tag ``vX.(Y+1).0`` at the tested commit. A week's patches roll up.

The release notes list every PR merged in the range, grouped by kind, the issues they close, the
contributors, and the suite evidence (scenarios and headline metrics) of the run that proved it.
Deterministic: no model writes them. Runs on a trusted runner with main's code::

    python3 -m scripts.ci.release --kind patch --sha SHA --evidence results/nightly/summary.json \
        --run-url URL --out release.json [--dry-run]

Writes the ``data/releases.jsonl`` line to ``--out`` (nothing when no release is due).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.ci.gate import GitHub
from scripts.ci.version import Version, first_parents, render, tags

CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE
)
GROUPS = (
    "Features",
    "Performance",
    "Fixes",
    "Documentation",
    "CI and tests",
    "Maintenance",
    "Other changes",
)
LABEL_GROUPS = {
    # docs/ci/labeling.md type/* (mirrored onto PRs from their issues), then GitHub's defaults.
    "type/feature": "Features",
    "type/enhancement": "Features",
    "type/perf": "Performance",
    "type/bug": "Fixes",
    "type/security": "Fixes",
    "type/docs": "Documentation",
    "type/ci": "CI and tests",
    "type/test": "CI and tests",
    "type/refactor": "Maintenance",
    "type/chore": "Maintenance",
    "feature": "Features",
    "enhancement": "Features",
    "bug": "Fixes",
    "documentation": "Documentation",
    "docs": "Documentation",
    "ci": "CI and tests",
    "test": "CI and tests",
}
VERB_GROUPS = (
    (
        re.compile(
            r"^(feat|add|support|implement|introduce|integrate|show|enable|list)\b",
            re.IGNORECASE,
        ),
        "Features",
    ),
    (
        re.compile(
            r"^(fix|repair|correct|handle|prevent|stop|avoid|restore)\b", re.IGNORECASE
        ),
        "Fixes",
    ),
    (
        re.compile(r"^(docs?|document|describe|explain)\b", re.IGNORECASE),
        "Documentation",
    ),
    (re.compile(r"^(ci|test|tests|build|chore)\b", re.IGNORECASE), "CI and tests"),
)


@dataclass
class Change:
    sha: str
    title: str
    number: int | None = None
    author: str = ""
    url: str = ""
    labels: list[str] = field(default_factory=list)
    closes: list[int] = field(default_factory=list)

    @property
    def group(self) -> str:
        for label in self.labels:
            if label.lower() in LABEL_GROUPS:
                return LABEL_GROUPS[label.lower()]
        for pattern, group in VERB_GROUPS:
            if pattern.match(self.title.strip()):
                return group
        return "Other changes"


def plan(
    released: dict[Version, str], kind: str
) -> tuple[Version, str, Version] | None:
    """(base version, base sha, next version) for ``kind``; None when there is no tag yet."""
    if not released:
        return None
    latest = max(released)
    major, minor, patch = latest
    if kind == "patch":
        return latest, released[latest], (major, minor, patch + 1)
    line = (major, minor, 0)
    base = line if line in released else latest
    return base, released[base], (major, minor + 1, 0)


def changes(gh: GitHub, chain: list[str]) -> list[Change]:
    """The PR (or direct commit) behind each first-parent commit, oldest first."""
    found: list[Change] = []
    for sha in reversed(chain):
        pulls = gh.call("GET", f"/commits/{sha}/pulls") or []
        merged = [
            p for p in pulls if p.get("merged_at") and p.get("merge_commit_sha") == sha
        ]
        pull = (merged or [p for p in pulls if p.get("merged_at")] or [None])[0]
        if pull:
            body = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
            found.append(
                Change(
                    sha=sha,
                    title=str(pull.get("title") or ""),
                    number=int(pull["number"]),
                    author=str((pull.get("user") or {}).get("login") or ""),
                    url=str(pull.get("html_url") or ""),
                    labels=[
                        str(label.get("name")) for label in pull.get("labels") or []
                    ],
                    closes=sorted({int(n) for n in CLOSES.findall(body)}),
                )
            )
        else:
            commit = gh.call("GET", f"/commits/{sha}") or {}
            message = str(
                (commit.get("commit") or {}).get("message") or ""
            ).splitlines()
            found.append(
                Change(
                    sha=sha,
                    title=message[0] if message else sha[:7],
                    author=str((commit.get("author") or {}).get("login") or ""),
                    url=str(commit.get("html_url") or ""),
                )
            )
    return found


def _metric_line(metric: dict[str, Any]) -> str:
    labels = ", ".join(
        f"{k}={v}" for k, v in sorted((metric.get("labels") or {}).items())
    )
    stats = metric.get("stats") or {}
    unit = metric.get("unit") or ""
    parts = [
        f"{key} {stats[key]} {unit}".strip() for key in ("p50", "p95") if key in stats
    ]
    if not parts and "mean" in stats:
        parts = [f"mean {stats['mean']} {unit}".strip()]
    name = f"{metric.get('name')}" + (f" ({labels})" if labels else "")
    return f"| {name} | {' · '.join(parts) or '—'} |"


def notes(
    version: str,
    kind: str,
    base_tag: str,
    items: list[Change],
    evidence: dict[str, Any] | None,
    run_url: str,
    repo: str,
) -> str:
    """The release body (Markdown)."""
    prs = [c for c in items if c.number]
    people = sorted({c.author for c in items if c.author})
    closed = sorted({n for c in items for n in c.closes})
    cadence = "nightly patch" if kind == "patch" else "weekly minor"
    lines = [
        f"TeamUp {version} — {cadence} release.",
        "",
        "## Summary",
        "",
        f"{len(items)} change(s) since `{base_tag}`: {len(prs)} merged PR(s)"
        + (f" by {len(people)} contributor(s)" if people else "")
        + (f", closing {len(closed)} issue(s)" if closed else "")
        + ".",
    ]
    counts = {g: sum(1 for c in items if c.group == g) for g in GROUPS}
    lines.append(
        "By kind: "
        + ", ".join(f"{g.lower()} {n}" for g, n in counts.items() if n)
        + "."
    )
    for group in GROUPS:
        members = [c for c in items if c.group == group]
        if not members:
            continue
        lines += ["", f"## {group}", ""]
        for c in members:
            ref = f"#{c.number}" if c.number else f"`{c.sha[:7]}`"
            by = f" (@{c.author})" if c.author else ""
            fixes = (
                f" — closes {', '.join(f'#{n}' for n in c.closes)}" if c.closes else ""
            )
            lines.append(f"- {c.title} {ref}{by}{fixes}")
    if evidence:
        scenarios = evidence.get("scenarios") or []
        passed = sum(1 for s in scenarios if s.get("status") == "pass")
        skipped = sum(1 for s in scenarios if s.get("status") == "skip")
        suite = str(evidence.get("suite") or kind)
        lines += [
            "",
            "## Evidence",
            "",
            f"Proved by the {suite} run on `{str(evidence.get('sha') or '')[:7]}`"
            + (f" ([run]({run_url}))" if run_url else "")
            + f": {passed}/{len(scenarios) - skipped} scenarios passed"
            + (f", {skipped} skipped" if skipped else "")
            + ".",
        ]
        metrics = evidence.get("metrics") or []
        if metrics:
            lines += ["", "| Metric | Result |", "| --- | --- |"]
            lines += [_metric_line(m) for m in metrics[:14]]
            if len(metrics) > 14:
                lines.append(f"| … {len(metrics) - 14} more | see the dashboards |")
    owner = repo.split("/")[0]
    name = repo.split("/")[-1]
    lines += [
        "",
        (
            f"Full diff: https://github.com/{repo}/compare/{base_tag}...v{version} · "
            f"Dashboards: https://{owner.lower()}.github.io/{name}/"
        ),
    ]
    return "\n".join(lines) + "\n"


def cut(
    gh: GitHub,
    kind: str,
    sha: str,
    evidence: dict[str, Any] | None,
    run_url: str,
    dry_run: bool,
) -> dict[str, Any] | None:
    """Tag and publish the release; the releases.jsonl line, or None when none is due."""
    released = tags(gh)
    planned = plan(released, kind)
    if planned is None:
        print("release: no vX.Y.Z tag yet; tag the baseline by hand first")
        return None
    base, base_sha, nxt = planned
    if nxt in released:
        print(f"release: v{render(nxt)} already exists")
        return None
    latest = max(released)
    # Raises when the tested commit is not at or after the newest tag (an older commit than one
    # already released): a release never goes backwards.
    if not first_parents(gh, released[latest], sha) and kind == "patch":
        print(f"release: nothing merged since v{render(latest)}")
        return None
    chain = first_parents(gh, base_sha, sha)
    if not chain:
        print(f"release: nothing merged since v{render(base)}")
        return None
    version = render(nxt)
    items = changes(gh, chain)
    body = notes(version, kind, f"v{render(base)}", items, evidence, run_url, gh.repo)
    line = {
        "tag": f"v{version}",
        "version": version,
        "kind": kind,
        "sha": sha,
        "base": f"v{render(base)}",
        "published_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prs": [c.number for c in items if c.number],
        "commits": len(items),
    }
    print(body)
    if dry_run:
        print(f"release: would tag v{version} at {sha[:7]}")
        return line
    try:
        gh.call("POST", "/git/refs", {"ref": f"refs/tags/v{version}", "sha": sha})
    except urllib.error.HTTPError as exc:
        if (
            exc.code != 422
        ):  # 422: the tag exists (a re-run); publish the release anyway
            raise
    gh.call(
        "POST",
        "/releases",
        {
            "tag_name": f"v{version}",
            "name": f"TeamUp {version}",
            "body": body,
            "make_latest": "true",
        },
    )
    print(f"release: published v{version} at {sha[:7]}")
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--kind", choices=("patch", "minor"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    evidence = (
        json.loads(args.evidence.read_text())
        if args.evidence and args.evidence.exists()
        else None
    )
    line = cut(gh, args.kind, args.sha, evidence, args.run_url, args.dry_run)
    if line and args.out:
        args.out.write_text(json.dumps(line) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
