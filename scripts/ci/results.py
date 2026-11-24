"""Suite results (docs/ci/contract.md): merge, judge, and publish to the ``ci-data`` branch.

    python3 -m scripts.ci.results merge --suite nightly --out results/nightly/summary.json \
        results/nightly/scenarios.json results/nightly/perf.json
    python3 -m scripts.ci.results status results/nightly/summary.json   # exit 1 when red
    python3 -m scripts.ci.results publish --kind nightly results/nightly/summary.json
    python3 -m scripts.ci.results sync-site dashboards/site

``publish`` appends one JSON line to ``data/<kind>.jsonl``; ``sync-site`` copies the static
dashboards to the branch root. Both write one commit through the git data API (no git push: the
runners often cannot reach github.com) and retry when the branch moved. The branch is created as
an orphan on first use. GitHub Pages serves it. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from scripts.ci.gate import GitHub

BRANCH = "ci-data"
KINDS = ("nightly", "weekly", "repo", "releases")
RED = frozenset({"fail", "error"})


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge(suite: str, parts: list[Path]) -> dict[str, Any]:
    """One schema-1 summary from the suite's part files; a missing part is an ``error`` entry."""
    summary: dict[str, Any] = {
        "schema": 1,
        "suite": suite,
        "sha": os.environ.get("TEAMUP_SHA") or None,
        "version": os.environ.get("TEAMUP_VERSION") or None,
        "started_at": None,
        "finished_at": None,
        "host": os.environ.get("TEAMUP_HOST") or None,
        "scenarios": [],
        "metrics": [],
    }
    for part in parts:
        if not part.exists():
            summary["scenarios"].append(
                {
                    "id": part.stem,
                    "title": f"{part.name} was not produced",
                    "status": "error",
                    "duration_s": 0,
                    "detail": "the step that writes it crashed or timed out",
                }
            )
            continue
        data = json.loads(part.read_text(encoding="utf-8"))
        summary["scenarios"] += data.get("scenarios") or []
        summary["metrics"] += data.get("metrics") or []
        for key, pick in (("started_at", min), ("finished_at", max)):
            values = [v for v in (summary[key], data.get(key)) if v]
            summary[key] = pick(values) if values else None
        for key in ("sha", "version", "host"):
            summary[key] = summary[key] or data.get(key)
    summary["finished_at"] = summary["finished_at"] or _now()
    return summary


def red(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """The failed scenarios; a summary with no scenario at all counts as red."""
    scenarios = summary.get("scenarios") or []
    if not scenarios:
        return [
            {"id": "-", "title": "no scenario ran", "status": "error", "detail": ""}
        ]
    return [s for s in scenarios if s.get("status") in RED]


def markdown(summary: dict[str, Any]) -> str:
    """A short table for the job summary and failure issues."""
    lines = [
        (
            f"### {summary.get('suite')} · `{str(summary.get('sha') or '')[:7]}` · "
            f"{summary.get('version') or ''}"
        ),
        "",
        "| Scenario | Status | Time | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for s in summary.get("scenarios") or []:
        detail = str(s.get("detail") or "").replace("\n", " ").replace("|", "\\|")[:160]
        lines.append(
            f"| {s.get('id')} {s.get('title') or ''} | {s.get('status')} | "
            f"{s.get('duration_s', '')} s | {detail} |"
        )
    metrics = summary.get("metrics") or []
    if metrics:
        lines += [
            "",
            f"{len(metrics)} metric series recorded; see the performance dashboard.",
        ]
    return "\n".join(lines) + "\n"


class DataBranch:
    """Commits to the ``ci-data`` branch through the git data API."""

    def __init__(self, gh: GitHub, branch: str = BRANCH) -> None:
        self.gh = gh
        self.branch = branch

    def head(self) -> str | None:
        try:
            ref = self.gh.call("GET", f"/git/ref/heads/{self.branch}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return str(ref["object"]["sha"])

    def read(self, path: str, commit: str | None) -> bytes:
        """A file's bytes at ``commit`` (empty when absent)."""
        if commit is None:
            return b""
        quoted = urllib.parse.quote(path)
        try:
            data = self.gh.call("GET", f"/contents/{quoted}?ref={commit}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return b""
            raise
        if data.get("content") and data.get("encoding") == "base64":
            return base64.b64decode(data["content"])
        blob = self.gh.call(
            "GET", f"/git/blobs/{data['sha']}"
        )  # over 1 MB: content is empty
        return base64.b64decode(blob["content"])

    def commit(self, change: Any, message: str, attempts: int = 5) -> str | None:
        """Apply ``change(read, head) -> {path: bytes}`` as one commit; None when nothing changed."""
        for attempt in range(attempts):
            head = self.head()
            files: dict[str, bytes] = change(lambda p, h=head: self.read(p, h), head)
            if head is not None and all(
                self.read(p, head) == b for p, b in files.items()
            ):
                print(f"results: {self.branch} already up to date")
                return None
            tree = [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": self.gh.call(
                        "POST",
                        "/git/blobs",
                        {
                            "content": base64.b64encode(data).decode(),
                            "encoding": "base64",
                        },
                    )["sha"],
                }
                for path, data in sorted(files.items())
            ]
            base_tree = (
                self.gh.call("GET", f"/git/commits/{head}")["tree"]["sha"]
                if head
                else None
            )
            body: dict[str, Any] = {"tree": tree}
            if base_tree:
                body["base_tree"] = base_tree
            new_tree = self.gh.call("POST", "/git/trees", body)["sha"]
            commit = self.gh.call(
                "POST",
                "/git/commits",
                {
                    "message": message,
                    "tree": new_tree,
                    "parents": [head] if head else [],
                },
            )["sha"]
            try:
                if head is None:
                    self.gh.call(
                        "POST",
                        "/git/refs",
                        {"ref": f"refs/heads/{self.branch}", "sha": commit},
                    )
                else:
                    self.gh.call(
                        "PATCH",
                        f"/git/refs/heads/{self.branch}",
                        {"sha": commit, "force": False},
                    )
                print(f"results: {self.branch} → {commit[:7]} ({message})")
                return commit
            except urllib.error.HTTPError as exc:
                if exc.code not in (409, 422) or attempt == attempts - 1:
                    raise
                print(f"results: {self.branch} moved; retrying ({exc.code})")
                time.sleep(2 + attempt * 2)
        return None


def publish(branch: DataBranch, kind: str, record: dict[str, Any]) -> str | None:
    """Append ``record`` as one line of ``data/<kind>.jsonl``."""
    path = f"data/{kind}.jsonl"
    line = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()

    def change(read: Any, _head: str | None) -> dict[str, bytes]:
        current = read(path)
        if current and not current.endswith(b"\n"):
            current += b"\n"
        return {path: current + line}

    label = record.get("sha") or record.get("date") or record.get("tag") or ""
    return branch.commit(change, f"data: {kind} {str(label)[:10]}")


def sync_site(branch: DataBranch, site: Path) -> str | None:
    """Copy every file under ``site`` (sample data excepted) to the branch root."""
    files = {
        str(p.relative_to(site)): p.read_bytes()
        for p in sorted(site.rglob("*"))
        if p.is_file() and "sample-data" not in p.parts and p.name != "preview.sh"
    }
    files[".nojekyll"] = b""
    return branch.commit(lambda _read, _head: files, "site: sync dashboards from main")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="results", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--suite", required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("parts", type=Path, nargs="+")
    s = sub.add_parser("status")
    s.add_argument("summary", type=Path)
    p = sub.add_parser("publish")
    p.add_argument("--kind", choices=KINDS, required=True)
    p.add_argument("file", type=Path)
    y = sub.add_parser("sync-site")
    y.add_argument("site", type=Path)
    args = parser.parse_args(argv)

    if args.command == "merge":
        summary = merge(args.suite, args.parts)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2) + "\n")
        print(
            f"results: {len(summary['scenarios'])} scenario(s), {len(summary['metrics'])} metric(s)"
        )
        return 0
    if args.command == "status":
        summary = json.loads(args.summary.read_text())
        text = markdown(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(text)
        failed = red(summary)
        for item in failed:
            print(
                f"::error::{item.get('id')} {item.get('title')}: {item.get('status')}"
            )
        return 1 if failed else 0
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    branch = DataBranch(gh)
    if args.command == "publish":
        publish(branch, args.kind, json.loads(args.file.read_text()))
    else:
        sync_site(branch, args.site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
