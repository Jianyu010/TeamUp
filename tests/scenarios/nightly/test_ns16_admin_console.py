"""Check the real Admin Console Gateway routes and browser controls."""

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from _stack import Api, Server, new_secret

from tests.scenarios._scenario_browser import (
    account_cli,
    browser_page,
    client_console,
    console_login,
)

ACCOUNTS = "/v1/console/accounts"
ROOMS = "/v1/console/rooms"
CAPABILITIES = "/v1/console/capabilities"
ENTRY = "/v1/auth/capabilities"
LOGIN = "/v1/auth/login"


@pytest.fixture
def admin_workspace(ws):
    operator_password = new_secret()
    enrollment_key = new_secret()
    server = Server(ws, {"room-owner": enrollment_key})
    account_cli(server, "create", "operator", password=operator_password)
    account_cli(server, "grant-console", "operator")
    account_cli(server, "grant-account-admin", "operator")
    server.start()
    with closing(Api(server.url, trust_env=False)) as api:
        yield server, api, operator_password, enrollment_key


def sign_in(api, username, password, *, console=False):
    credentials = {"username": username, "password": password}
    if console:
        credentials["purpose"] = "console"
    return api.call("POST", LOGIN, json=credentials)["access_token"]


def operator(api, password):
    return sign_in(api, "operator", password, console=True)


def assert_account_response(response, status, username):
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"username"}
    assert payload["username"] == username


@pytest.mark.scenario("NS-16.01", "Admin entry follows live grants and session purpose")
def test_admin_entry_checks_live_grants_and_credential_purpose(ws, admin_workspace):
    server, api, operator_password, _ = admin_workspace
    password = new_secret()
    account_cli(server, "create", "reader", password=password)
    enrollment = sign_in(api, "reader", password)
    console_token = sign_in(api, "reader", password, console=True)
    exchange = "/v1/auth/console-session"
    assert api.call("GET", ENTRY, enrollment) == {"admin": False}
    assert api.call("GET", ENTRY, console_token, error="unauthorized") == 401
    assert api.call("GET", ROOMS, enrollment, error="unauthorized") == 401
    assert api.call("GET", ROOMS, console_token, error="forbidden") == 403
    assert api.call("POST", exchange, enrollment, error="forbidden") == 403

    with client_console(ws, server, "reader") as (_, console, _):
        console_login(console, "reader", password)
        assert console.get("/api/access").json() == {}
        account_cli(server, "grant-console", "reader")
        assert console.get("/api/access").json() == {
            "admin_url": server.url + "/console/"
        }
        assert (
            console.post(
                "/api/admin/session",
                headers={"Origin": str(console.base_url).rstrip("/")},
            ).status_code
            == 404
        )
        issued = api.raw("POST", exchange, enrollment)
        assert issued.status_code == 200
        assert issued.headers["cache-control"] == "no-store"
        issued_payload = issued.json()
        assert set(issued_payload) == {"access_token", "expires_at", "username"}
        assert issued_payload["username"] == "reader"
        fresh_console = issued_payload["access_token"]
        assert api.call("GET", CAPABILITIES, fresh_console) == {
            "maintenance": False,
            "accounts": False,
        }
        assert api.call("GET", CAPABILITIES, operator(api, operator_password)) == {
            "maintenance": False,
            "accounts": True,
        }
        account_cli(server, "revoke-console", "reader")
        assert console.get("/api/access").json() == {}
        assert api.call("GET", ROOMS, fresh_console, error="forbidden") == 403
        assert api.call("POST", exchange, enrollment, error="forbidden") == 403


@pytest.mark.scenario(
    "NS-16.02", "Admin room metadata and scoped transcript grant revocation"
)
def test_admin_room_transcript_scope_and_revocation(admin_workspace):
    server, api, password, key = admin_workspace
    token = operator(api, password)
    member = api.enroll(key, "Room author")
    room = api.create_room(member, "Visible to operators")
    other = api.create_room(member, "Separate grant scope")
    api.read_and_ack_board(member, room)
    first = api.send(member, room, "First transcript message")
    second = api.send(member, room, "Second transcript message")

    listing = api.call("GET", ROOMS, token)
    assert {item["room_id"] for item in listing["rooms"]} == {room, other}
    path = f"{ROOMS}/{room}"
    detail = api.call("GET", path, token)
    assert detail["transcript_allowed"] is False
    (operator_view,) = detail["members"]
    assert operator_view["member_id"] == member.member_id
    assert operator_view["board_state"] == "acknowledged"
    activities = api.call("GET", path + "/timeline", token)["activities"]
    assert [item["message_id"] for item in activities if item["kind"] == "message"] == [
        first["message_id"],
        second["message_id"],
    ]
    assert all("text" not in item for item in activities)
    transcript = path + "/transcript"
    assert api.call("GET", transcript, token, error="forbidden") == 403

    account_cli(server, "grant-transcript", "operator", room)
    assert api.call("GET", path, token)["transcript_allowed"] is True
    assert (
        api.call("GET", f"{ROOMS}/{other}/transcript", token, error="forbidden") == 403
    )
    response = api.raw("GET", transcript, token, params={"limit": 1})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    latest = response.json()
    assert latest["has_more"] is True
    assert [(row["message_id"], row["text"]) for row in latest["messages"]] == [
        (second["message_id"], second["text"])
    ]
    earlier = {"before": second["message_id"], "limit": 1}
    older = api.call("GET", transcript, token, params=earlier)
    assert older["has_more"] is False
    assert [(row["message_id"], row["text"]) for row in older["messages"]] == [
        (first["message_id"], first["text"])
    ]
    account_cli(server, "revoke-transcript", "operator", room)
    assert api.call("GET", transcript, token, params=earlier, error="forbidden") == 403


@pytest.mark.scenario(
    "NS-16.03", "Admin account creation and protected operator accounts"
)
def test_admin_account_creation_and_permissions(admin_workspace):
    server, api, password, _ = admin_workspace
    admin = operator(api, password)
    enrollment = sign_in(api, "operator", password)
    reader_password = new_secret()
    account_cli(server, "create", "reader", password=reader_password)
    account_cli(server, "grant-console", "reader")
    reader = sign_in(api, "reader", reader_password, console=True)
    create = {"username": "new-user", "password": new_secret()}
    for credential, error, status in (
        (None, "unauthorized", 401),
        (enrollment, "unauthorized", 401),
        (reader, "forbidden", 403),
    ):
        assert api.call("GET", ACCOUNTS, credential, error=error) == status
        assert (
            api.call("POST", ACCOUNTS, credential, json=create, error=error) == status
        )
    assert_account_response(
        api.raw("POST", ACCOUNTS, admin, json=create), 201, "new-user"
    )
    assert api.call("POST", ACCOUNTS, admin, json=create, error="conflict") == 409
    assert api.call("GET", ENTRY, sign_in(api, "new-user", create["password"])) == {
        "admin": False
    }
    assert (
        api.call(
            "POST",
            f"{ACCOUNTS}/reader/password",
            admin,
            json={"password": new_secret()},
            error="forbidden",
        )
        == 403
    )
    assert (
        api.call("POST", f"{ACCOUNTS}/operator/disable", admin, error="forbidden")
        == 403
    )
    listing = api.raw("GET", ACCOUNTS, admin)
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    accounts = listing.json()["accounts"]
    assert [item["username"] for item in accounts] == ["new-user", "operator", "reader"]
    for item in accounts:
        assert set(item) == {
            "username",
            "disabled",
            "can_disable",
            "can_reset_password",
        }
        assert item["disabled"] is False
        assert item["can_disable"] is (item["username"] == "new-user")
        assert item["can_reset_password"] is (item["username"] == "new-user")

    account_cli(server, "revoke-account-admin", "operator")
    assert api.call("GET", CAPABILITIES, admin)["accounts"] is False
    assert api.call("GET", ACCOUNTS, admin, error="forbidden") == 403
    assert api.call("POST", ACCOUNTS, admin, json=create, error="forbidden") == 403


@pytest.fixture
def managed_account(admin_workspace):
    server, api, password, _ = admin_workspace
    previous = new_secret()
    account_cli(server, "create", "managed", password=previous)
    enrollment = sign_in(api, "managed", previous)
    console_token = sign_in(api, "managed", previous, console=True)
    member = api.enroll(enrollment, "Managed agent")
    return api, operator(api, password), previous, enrollment, console_token, member


@pytest.mark.scenario(
    "NS-16.04", "Admin password reset revokes human sessions and preserves agents"
)
def test_admin_password_reset_revokes_human_sessions(managed_account):
    api, admin, previous, enrollment, console_token, member = managed_account
    updated = new_secret()
    reset = api.raw(
        "POST", f"{ACCOUNTS}/managed/password", admin, json={"password": updated}
    )
    assert_account_response(reset, 200, "managed")
    assert api.call("GET", ENTRY, enrollment, error="unauthorized") == 401
    assert api.call("GET", ROOMS, console_token, error="unauthorized") == 401
    assert (
        api.call(
            "POST",
            LOGIN,
            json={"username": "managed", "password": previous},
            error="unauthorized",
        )
        == 401
    )
    assert api.call("GET", ENTRY, sign_in(api, "managed", updated)) == {"admin": False}
    assert (
        api.call("GET", "/v1/members/me", member.token)["member_id"] == member.member_id
    )


@pytest.mark.scenario("NS-16.05", "Admin disable revokes human and member access")
def test_admin_disable_revokes_human_and_member_access(managed_account):
    api, admin, password, enrollment, console_token, member = managed_account
    path = f"{ACCOUNTS}/managed/disable"
    for _ in range(2):
        assert_account_response(api.raw("POST", path, admin), 200, "managed")
    assert api.call("GET", ENTRY, enrollment, error="unauthorized") == 401
    assert api.call("GET", ROOMS, console_token, error="unauthorized") == 401
    assert api.call("GET", "/v1/members/me", member.token, error="unauthorized") == 401
    assert (
        api.call(
            "POST",
            LOGIN,
            json={"username": "managed", "password": password},
            error="unauthorized",
        )
        == 401
    )
    (disabled,) = [
        account
        for account in api.call("GET", ACCOUNTS, admin)["accounts"]
        if account["username"] == "managed"
    ]
    assert disabled == {
        "username": "managed",
        "disabled": True,
        "can_disable": False,
        "can_reset_password": False,
    }


@pytest.mark.scenario("NS-16.06", "Browser Admin sign-in and Accounts controls")
def test_browser_admin_signs_in_and_manages_account(admin_workspace):
    server, api, password, _ = admin_workspace
    initial = new_secret()
    updated = new_secret()
    with browser_page(server.url, redact=(password, initial, updated)) as (
        page,
        expect,
    ):
        page.goto(server.url + "/console/", wait_until="domcontentloaded")
        accounts = page.get_by_role("button", name="Accounts", exact=True)
        expect(accounts).to_be_hidden()
        login = page.locator("#loginForm")
        login.get_by_label("Username", exact=True).fill("operator")
        login.get_by_label("Password", exact=True).fill(password)
        login.get_by_role("button", name="Sign in", exact=True).click()
        accounts.click()
        expect(page.locator("#accountsRows")).to_contain_text("operator")
        expect(page.get_by_role("button", name="Disable for operator")).to_have_count(0)
        page.locator("#accountUsername").fill("browser-user")
        page.locator("#accountPassword").fill(initial)
        page.locator("#accountConfirm").fill(initial)
        page.get_by_role("button", name="Create account").click()
        expect(page.locator("#accountStatus")).to_have_text("Created browser-user.")
        old_session = sign_in(api, "browser-user", initial)

        page.get_by_role("button", name="Reset password for browser-user").click()
        page.locator("#accountPassword").fill(updated)
        page.locator("#accountConfirm").fill(updated)
        page.get_by_role("button", name="Reset password", exact=True).click()
        expect(page.locator("#accountStatus")).to_have_text(
            "Password reset for browser-user."
        )
        assert api.call("GET", ENTRY, old_session, error="unauthorized") == 401
        new_session = sign_in(api, "browser-user", updated)

        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Disable for browser-user").click()
        expect(page.locator("#accountStatus")).to_have_text(
            "browser-user was disabled."
        )
        expect(page.get_by_role("row").filter(has_text="browser-user")).to_contain_text(
            "Disabled"
        )
        assert api.call("GET", ENTRY, new_session, error="unauthorized") == 401


@pytest.mark.scenario(
    "NS-16.07", "Admin maintenance scopes and redacted operational events"
)
def test_admin_maintenance_and_logs_require_separate_live_grants(admin_workspace):
    server, api, password, key = admin_workspace
    token = operator(api, password)
    member = api.enroll(key, "Synthetic room author")
    room = api.create_room(
        member, "Synthetic maintenance room", rules="Synthetic board rules"
    )
    api.read_and_ack_board(member, room)
    api.send(member, room, "Synthetic maintenance message body")
    health = "/v1/console/maintenance/health"
    load = f"{ROOMS}/{room}/load"
    transcript = f"{ROOMS}/{room}/transcript"
    logs = "/v1/console/maintenance/logs"
    now = datetime.now(UTC)
    window = {
        "since": (now - timedelta(minutes=1)).isoformat(),
        "until": (now + timedelta(minutes=1)).isoformat(),
        "component": "gateway",
    }
    assert api.call("GET", health, token, error="forbidden") == 403
    assert api.call("GET", load, token, error="forbidden") == 403
    assert api.call("GET", logs, token, params=window, error="forbidden") == 403
    account_cli(server, "grant-maintenance", "operator")
    assert set(api.call("GET", health, token)["sources"]) == {
        "host",
        "gateway",
        "collaboration",
    }
    load_response = api.raw("GET", load, token)
    assert load_response.status_code == 200
    assert load_response.json()["room"]["room_id"] == room
    assert "Synthetic maintenance message body" not in load_response.text
    assert "Synthetic board rules" not in load_response.text
    assert api.call("GET", logs, token, params=window, error="forbidden") == 403
    account_cli(server, "grant-logs", "operator")
    assert api.call("GET", transcript, token, error="forbidden") == 403

    response = api.raw("GET", logs, token, params=window)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["state"] == "available"
    assert any(
        row["route"] == f"{ROOMS}/{{room_id}}/transcript"
        and row["error_code"] == "forbidden"
        for row in body["entries"]
    )
    for row in body["entries"]:
        assert set(row) == {
            "at",
            "component",
            "severity",
            "event_code",
            "route",
            "method",
            "status_class",
            "error_code",
            "duration_ms",
        }
    assert room not in response.text
    assert "Synthetic maintenance message body" not in response.text
    account_cli(server, "revoke-maintenance", "operator")
    assert api.call("GET", health, token, error="forbidden") == 403
    assert api.call("GET", logs, token, params=window)["state"] == "available"
    account_cli(server, "revoke-logs", "operator")
    assert api.call("GET", logs, token, params=window, error="forbidden") == 403
