"""Collect daily repository statistics for the ci-data dashboard.

Usage:
    python3 scripts/ci/repo_stats.py --repo OWNER/NAME --out stats.json [--date YYYY-MM-DD]

Only the Python standard library is used. The single HTTP seam is ``http_get``
so tests can replace it without touching the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from datetime import date as date_type

DEFAULT_API_URL = "https://api.github.com"
USER_AGENT = "TeamUp-repo-stats"
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


class ApiError(Exception):
    """A GitHub API request failed in a way the collector cannot ignore."""


def _decode(raw: bytes) -> object | None:
    text = raw.decode("utf-8", "replace") if raw else ""
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def http_get(url: str, token: str | None = None) -> tuple[int, object | None]:
    """GET ``url`` and return ``(status, decoded_json)``.

    HTTP error responses are returned as their status code rather than raised.
    Server errors are retried up to three times.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last: tuple[int, object | None] = (0, None)
    for attempt in range(MAX_ATTEMPTS):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as exc:
            last = (exc.code, _decode(exc.read()))
            if exc.code < 500 or attempt == MAX_ATTEMPTS - 1:
                return last
        except urllib.error.URLError as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"network error: {exc.reason}") from exc
        time.sleep(RETRY_DELAY_SECONDS)
    return last


def _require_ok(status: int, context: str) -> None:
    if status >= 400 or status == 0:
        raise ApiError(f"{context}: HTTP {status}")


def _search_count(api: str, token: str | None, query: str) -> int:
    params = urllib.parse.urlencode({"q": query, "per_page": 1})
    status, data = http_get(f"{api}/search/issues?{params}", token)
    _require_ok(status, "search/issues")
    if not isinstance(data, dict):
        raise ApiError("search/issues: unexpected response")
    return int(data.get("total_count") or 0)


def _traffic(api: str, token: str | None, path: str) -> dict | None:
    status, data = http_get(f"{api}{path}", token)
    if status in (401, 403, 404):
        return None
    _require_ok(status, path)
    if not isinstance(data, dict):
        raise ApiError(f"{path}: unexpected response")
    return data


def _releases_downloads(api: str, token: str | None, repo: str) -> int:
    total = 0
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        status, data = http_get(f"{api}/repos/{repo}/releases?{query}", token)
        _require_ok(status, "releases")
        if not isinstance(data, list):
            raise ApiError("releases: unexpected response")
        for release in data:
            for asset in release.get("assets") or []:
                total += int(asset.get("download_count") or 0)
        if len(data) < 100:
            return total
        page += 1


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def collect(
    repo: str,
    *,
    api: str,
    token: str | None,
    day: date_type,
    now: datetime | None = None,
) -> dict:
    """Collect one repository statistics row."""
    moment = now or datetime.now(UTC)
    day_text = day.isoformat()

    status, repo_data = http_get(f"{api}/repos/{repo}", token)
    _require_ok(status, "repos")
    if not isinstance(repo_data, dict):
        raise ApiError("repos: unexpected response")

    open_issues = _search_count(api, token, f"repo:{repo} is:issue is:open")
    open_prs = _search_count(api, token, f"repo:{repo} is:pr is:open")
    issues_opened = _search_count(
        api, token, f"repo:{repo} is:issue created:{day_text}"
    )
    issues_closed = _search_count(api, token, f"repo:{repo} is:issue closed:{day_text}")
    prs_opened = _search_count(api, token, f"repo:{repo} is:pr created:{day_text}")
    prs_merged = _search_count(api, token, f"repo:{repo} is:pr merged:{day_text}")

    downloads = _releases_downloads(api, token, repo)

    clones = _traffic(api, token, f"/repos/{repo}/traffic/clones")
    views = _traffic(api, token, f"/repos/{repo}/traffic/views")

    status, release_data = http_get(f"{api}/repos/{repo}/releases/latest", token)
    if status == 404:
        latest_release = None
        latest_release_at = None
    else:
        _require_ok(status, "releases/latest")
        if not isinstance(release_data, dict):
            raise ApiError("releases/latest: unexpected response")
        latest_release = release_data.get("tag_name")
        latest_release_at = release_data.get("published_at")

    published = _parse_timestamp(latest_release_at)
    if published is None:
        days_since_release = None
    else:
        days_since_release = round((moment - published).total_seconds() / 86400, 1)

    return {
        "date": day_text,
        "collected_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stars": repo_data.get("stargazers_count"),
        "watchers": repo_data.get("subscribers_count"),
        "forks": repo_data.get("forks_count"),
        "open_issues": open_issues,
        "open_prs": open_prs,
        "issues_opened_1d": issues_opened,
        "issues_closed_1d": issues_closed,
        "prs_opened_1d": prs_opened,
        "prs_merged_1d": prs_merged,
        "release_downloads_total": downloads,
        "clones_14d": clones.get("count") if clones else None,
        "unique_cloners_14d": clones.get("uniques") if clones else None,
        "views_14d": views.get("count") if views else None,
        "unique_visitors_14d": views.get("uniques") if views else None,
        "latest_release": latest_release,
        "latest_release_at": latest_release_at,
        "days_since_release": days_since_release,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect daily repository statistics as JSON."
    )
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--date", help="UTC date in YYYY-MM-DD form")
    args = parser.parse_args(argv)

    try:
        day = (
            date_type.fromisoformat(args.date)
            if args.date
            else datetime.now(UTC).date() - timedelta(days=1)
        )
    except ValueError:
        print("repo_stats: --date must be YYYY-MM-DD", file=sys.stderr)
        return 1

    api = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).rstrip("/")
    token = os.environ.get("GITHUB_TOKEN")

    try:
        stats = collect(args.repo, api=api, token=token, day=day)
    except (ApiError, RuntimeError) as exc:
        print(f"repo_stats: {exc}", file=sys.stderr)
        return 1

    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(stats, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        print(f"repo_stats: cannot write {args.out}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
