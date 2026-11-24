"""Unit tests for scripts/ci/repo_stats.py with the HTTP seam replaced."""

from __future__ import annotations

import importlib.util
import urllib.parse
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/ci/repo_stats.py"
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)
DAY = date(2026, 11, 24)


@pytest.fixture
def repo_stats():
    spec = importlib.util.spec_from_file_location("repo_stats", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _handler(
    *,
    search=None,
    traffic_status=200,
    latest_status=200,
    release_pages=None,
    repo_data=None,
):
    counts = {
        "repo:acme/teamup is:issue is:open": 4,
        "repo:acme/teamup is:pr is:open": 6,
        "repo:acme/teamup is:issue created:2026-11-24": 1,
        "repo:acme/teamup is:issue closed:2026-11-24": 0,
        "repo:acme/teamup is:pr created:2026-11-24": 2,
        "repo:acme/teamup is:pr merged:2026-11-24": 1,
    }
    counts.update(search or {})
    pages = release_pages if release_pages is not None else {1: []}
    repository = repo_data or {
        "stargazers_count": 3,
        "subscribers_count": 2,
        "forks_count": 0,
    }

    def handler(url):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/repos/acme/teamup":
            return 200, repository
        if path == "/search/issues":
            return 200, {"total_count": counts[query["q"][0]]}
        if path == "/repos/acme/teamup/releases":
            page = int(query.get("page", ["1"])[0])
            assert query["per_page"] == ["100"]
            return 200, pages.get(page, [])
        if path.endswith("/traffic/clones"):
            if traffic_status != 200:
                return traffic_status, {"message": "forbidden"}
            return 200, {"count": 10, "uniques": 4}
        if path.endswith("/traffic/views"):
            if traffic_status != 200:
                return traffic_status, {"message": "forbidden"}
            return 200, {"count": 30, "uniques": 8}
        if path == "/repos/acme/teamup/releases/latest":
            if latest_status == 404:
                return 404, {"message": "not found"}
            return 200, {
                "tag_name": "v1.2.3",
                "published_at": "2026-11-23T20:00:00Z",
            }
        raise AssertionError(f"unexpected URL: {url}")

    return handler


def _patch(repo_stats, monkeypatch, handler):
    calls = []

    def http_get(url, token=None):
        calls.append((url, token))
        return handler(url)

    monkeypatch.setattr(repo_stats, "http_get", http_get)
    return calls


def _collect(repo_stats):
    return repo_stats.collect(
        "acme/teamup",
        api="https://api.example.test",
        token="secret-token",
        day=DAY,
        now=NOW,
    )


def test_field_mapping(repo_stats, monkeypatch):
    calls = _patch(repo_stats, monkeypatch, _handler())
    stats = _collect(repo_stats)

    assert stats == {
        "date": "2026-11-24",
        "collected_at": "2026-11-24T12:00:00Z",
        "stars": 3,
        "watchers": 2,
        "forks": 0,
        "open_issues": 4,
        "open_prs": 6,
        "issues_opened_1d": 1,
        "issues_closed_1d": 0,
        "prs_opened_1d": 2,
        "prs_merged_1d": 1,
        "release_downloads_total": 0,
        "clones_14d": 10,
        "unique_cloners_14d": 4,
        "views_14d": 30,
        "unique_visitors_14d": 8,
        "latest_release": "v1.2.3",
        "latest_release_at": "2026-11-23T20:00:00Z",
        "days_since_release": 0.7,
    }
    assert all(token == "secret-token" for _, token in calls)
    assert any(
        "/search/issues" in url
        and urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q")
        == ["repo:acme/teamup is:issue is:open"]
        for url, _ in calls
    )


@pytest.mark.parametrize("traffic_status", [401, 403, 404])
def test_traffic_forbidden_writes_null(repo_stats, monkeypatch, traffic_status):
    _patch(repo_stats, monkeypatch, _handler(traffic_status=traffic_status))
    stats = _collect(repo_stats)

    assert stats["clones_14d"] is None
    assert stats["unique_cloners_14d"] is None
    assert stats["views_14d"] is None
    assert stats["unique_visitors_14d"] is None
    assert stats["stars"] == 3


def test_missing_release_writes_nulls(repo_stats, monkeypatch):
    _patch(repo_stats, monkeypatch, _handler(latest_status=404))
    stats = _collect(repo_stats)

    assert stats["latest_release"] is None
    assert stats["latest_release_at"] is None
    assert stats["days_since_release"] is None


def test_open_issues_exclude_pull_requests(repo_stats, monkeypatch):
    handler = _handler(
        search={
            "repo:acme/teamup is:issue is:open": 9,
            "repo:acme/teamup is:pr is:open": 2,
        }
    )
    calls = _patch(repo_stats, monkeypatch, handler)
    stats = _collect(repo_stats)

    assert stats["open_issues"] == 9
    assert stats["open_prs"] == 2
    search_urls = [url for url, _ in calls if "/search/issues" in url]
    assert any(
        urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"]
        == ["repo:acme/teamup is:issue is:open"]
        for url in search_urls
    )
    assert any(
        urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"]
        == ["repo:acme/teamup is:pr is:open"]
        for url in search_urls
    )


def test_releases_are_paginated(repo_stats, monkeypatch):
    first_page = [{"assets": [{"download_count": 1}]} for _ in range(100)]
    second_page = [{"assets": [{"download_count": 5}]}]
    calls = _patch(
        repo_stats,
        monkeypatch,
        _handler(release_pages={1: first_page, 2: second_page}),
    )
    stats = _collect(repo_stats)

    assert stats["release_downloads_total"] == 105
    release_urls = [url for url, _ in calls if "/releases?" in url]
    assert len(release_urls) == 2
    assert "page=2" in release_urls[1]
