import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from teamup_server.collaboration import Collaboration
from teamup_server.gateway import Gateway
from teamup_server.main import create_app
from teamup_server.operational_events import OperationalEvents
from teamup_server.presence import Presence

PASSWORD = "maintenance-test-password"
HEALTH = "/v1/console/maintenance/health"
LOGS = "/v1/console/maintenance/logs"


@pytest.fixture
def console(tmp_path):
    database = tmp_path / "maintenance.sqlite3"
    collaboration = Collaboration(database, {})
    collaboration.create_account("operator", PASSWORD)
    collaboration.grant_console_read("operator")
    with TestClient(create_app(database)) as client:
        response = client.post(
            "/v1/auth/login",
            json={"username": "operator", "password": PASSWORD, "purpose": "console"},
        )
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        yield client, collaboration, headers


def log_window(*, minutes=10):
    until = datetime.now(UTC) + timedelta(seconds=1)
    return {
        "since": (until - timedelta(minutes=minutes)).isoformat(),
        "until": until.isoformat(),
    }


def test_dashboard_uses_the_operator_shell_and_same_origin_assets(console):
    client, _, _ = console
    dashboard = client.get("/dashboard/")
    rooms = client.get("/console/")
    assert dashboard.status_code == rooms.status_code == 200
    assert dashboard.content == rooms.content
    assert (
        dashboard.headers["content-security-policy"]
        == rooms.headers["content-security-policy"]
    )
    assert "/console/maintenance.js" in dashboard.text
    assert client.get("/console/maintenance.js").status_code == 200
    assert client.post("/dashboard/").status_code == 405


def test_maintenance_scopes_are_checked_on_every_read(console):
    client, collaboration, headers = console
    window = log_window()
    room = f"/v1/console/rooms/{uuid4()}/load"
    assert client.get(HEALTH).status_code == 401
    assert client.get(HEALTH, headers=headers).status_code == 403
    assert client.get(room, headers=headers).status_code == 403
    assert client.get(LOGS, headers=headers, params=window).status_code == 403

    enrollment = client.post(
        "/v1/auth/login", json={"username": "operator", "password": PASSWORD}
    ).json()["access_token"]
    member_only = {"Authorization": "Bearer " + enrollment}
    assert client.get(HEALTH, headers=member_only).status_code == 401
    assert client.get(LOGS, headers=member_only, params=window).status_code == 401

    collaboration.set_maintenance_grant("operator", "maintenance_read", True)
    health = client.get(HEALTH, headers=headers)
    assert health.status_code == 200
    assert health.headers["cache-control"] == "no-store"
    assert set(health.json()["sources"]) == {"host", "gateway", "collaboration"}
    assert client.get(room, headers=headers).status_code == 404
    assert client.get(LOGS, headers=headers, params=window).status_code == 403

    collaboration.set_maintenance_grant("operator", "logs_read", True)
    collaboration.set_maintenance_grant("operator", "maintenance_read", False)
    assert client.get(HEALTH, headers=headers).status_code == 403
    assert client.get(LOGS, headers=headers, params=window).status_code == 200
    collaboration.revoke_console_read("operator")
    assert client.get(LOGS, headers=headers, params=window).status_code == 403


def test_maintenance_traffic_and_events_use_safe_labels(console):
    client, collaboration, headers = console
    collaboration.set_maintenance_grant("operator", "maintenance_read", True)
    collaboration.set_maintenance_grant("operator", "logs_read", True)
    room_id, dm_id = uuid4(), uuid4()
    search = "maintenance-search-marker"
    assert (
        client.get(
            f"/v1/rooms/{room_id}/search", params={"q": search}, headers=headers
        ).status_code
        == 401
    )
    assert client.post(
        f"/v1/dms/{dm_id}/messages", json={"text": "maintenance-dm-marker"}
    ).status_code in (401, 422)
    assert client.get("/nonexistent-maintenance-marker").status_code == 404

    health = client.get(HEALTH, headers=headers)
    assert health.status_code == 200
    values = health.json()["sources"]["gateway"]["values"]
    traffic = values["http_requests_total"]
    assert any(r["route"] == "/v1/rooms/{room_id}/search" for r in traffic)
    assert any(r["route"] == "unmatched" for r in traffic)
    assert all(not r["route"].startswith("/v1/dms") for r in traffic)
    assert any(r["seconds"] >= 0 for r in values["http_request_duration_seconds_total"])

    events = client.get(LOGS, headers=headers, params=log_window())
    assert events.status_code == 200
    assert events.headers["cache-control"] == "no-store"
    assert events.json()["state"] == "available"
    entries = events.json()["entries"]
    assert any(e["event_code"] == "server_started" for e in entries)
    assert any(e.get("route") == "/v1/rooms/{room_id}/search" for e in entries)
    serialized = json.dumps([values, entries])
    for value in (
        str(room_id),
        str(dm_id),
        search,
        "maintenance-dm-marker",
        "nonexistent-maintenance-marker",
        headers["Authorization"],
    ):
        assert value not in serialized


def test_maintenance_log_queries_bound_the_window_and_limit(console):
    client, collaboration, headers = console
    collaboration.set_maintenance_grant("operator", "logs_read", True)
    for query in (
        log_window(minutes=61),
        {**log_window(), "limit": 501},
        {**log_window(), "component": "unknown"},
        {**log_window(), "since": "2026-11-25T00:00:00"},
    ):
        result = client.get(LOGS, params=query, headers=headers)
        assert result.status_code == 422
        assert result.json()["error"]["code"] == "invalid_request"


def test_mounted_gateway_excludes_dm_and_inbox_from_observations(console):
    default_client, collaboration, headers = console
    collaboration.set_maintenance_grant("operator", "maintenance_read", True)
    collaboration.set_maintenance_grant("operator", "logs_read", True)
    with TestClient(default_client.app, root_path="/preview") as client:
        dm_path = f"/preview/v1/dms/{uuid4()}/messages"
        assert client.post(dm_path, json={"text": "DM marker"}).status_code in (
            401,
            422,
        )
        assert client.get("/preview/v1/inbox", headers=headers).status_code == 401
        health = client.get("/preview" + HEALTH, headers=headers)
        events = client.get("/preview" + LOGS, headers=headers, params=log_window())

    traffic = health.json()["sources"]["gateway"]["values"]["http_requests_total"]
    entries = events.json()["entries"]
    for entries_with_route in (traffic, entries):
        assert all(
            not row.get("route", "").startswith(("/v1/dms", "/v1/inbox"))
            for row in entries_with_route
        )


def test_gateway_rechecks_logs_access_after_reading_the_event_sink(console):
    _, collaboration, headers = console
    collaboration.set_maintenance_grant("operator", "logs_read", True)

    class RevokingEvents(OperationalEvents):
        def query(self, *, since, until, component, limit):
            result = super().query(
                since=since, until=until, component=component, limit=limit
            )
            collaboration.set_maintenance_grant("operator", "logs_read", False)
            return result

    gateway = Gateway(
        collaboration, Presence(collaboration), event_log=RevokingEvents()
    )
    with TestClient(gateway.app) as client:
        result = client.get(LOGS, params=log_window(), headers=headers)
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "forbidden"
    assert "entries" not in result.json()


def test_missing_providers_report_unavailable(console):
    _, collaboration, headers = console
    collaboration.set_maintenance_grant("operator", "maintenance_read", True)
    collaboration.set_maintenance_grant("operator", "logs_read", True)
    gateway = Gateway(collaboration, Presence(collaboration))
    with TestClient(gateway.app) as client:
        health = client.get(HEALTH, headers=headers)
        events = client.get(LOGS, params=log_window(), headers=headers)
    host = health.json()["sources"]["host"]
    assert host["state"] == "unavailable"
    assert host["sampled_at"] is None
    assert host["values"] and all(value is None for value in host["values"].values())
    assert events.json() == {"state": "unavailable", "entries": [], "truncated": False}
