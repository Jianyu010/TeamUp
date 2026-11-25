"""Collaboration contract tests for the operator maintenance console."""

import sqlite3
from base64 import urlsafe_b64encode
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from teamup_server import account_cli
from teamup_server.collaboration import Collaboration, CollaborationError

USERNAME = "operator"
PASSWORD = "operator test password"
WINDOW = {"since": "2026-11-24T12:00:00Z", "until": "2026-11-24T12:15:00Z"}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "teamup.sqlite3"


@pytest.fixture
def owner(db_path):
    owner = Collaboration(db_path, {})
    owner.create_account(USERNAME, PASSWORD)
    return owner


@contextmanager
def denied(code):
    with pytest.raises(CollaborationError) as caught:
        yield
    assert caught.value.code == code


def console_token(owner):
    return owner.login(USERNAME, PASSWORD, "console")["access_token"]


def database_version(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def members(owner):
    enrollment = owner.login(USERNAME, PASSWORD)["access_token"]
    participants = []
    for index, label in enumerate(("sender", "recipient"), 1):
        token = urlsafe_b64encode(bytes([index]) * 32).decode().rstrip("=")
        member = owner.enroll(
            enrollment,
            harness_instance_id=label,
            session_key=label,
            role_description=f"private-role-{label}",
            member_token=token,
        )
        participants.append((token, member["member_id"]))
    return participants


def test_maintenance_health_rechecks_both_grants_on_cached_read(owner, db_path):
    token = console_token(owner)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)
    with denied("forbidden"):
        owner.console_maintenance_snapshot(token)
    owner.grant_console_read(USERNAME)

    snapshot = owner.console_maintenance_snapshot(token)
    assert snapshot["source"] == "collaboration"
    assert snapshot["state"] == "fresh"
    assert snapshot["sample_interval_seconds"] == 15
    assert snapshot["stale_after_seconds"] == 45
    assert snapshot["unavailable_after_seconds"] == 90
    assert snapshot["rates"] is None
    assert snapshot["values"]["schema_version"] == database_version(db_path)
    assert snapshot["values"]["db_size_bytes"] > 0

    owner.set_maintenance_grant(USERNAME, "maintenance_read", False)
    with denied("forbidden"):
        owner.console_maintenance_snapshot(token)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)
    owner.disable_account(USERNAME)
    with denied("unauthorized"):
        owner.console_maintenance_snapshot(token)


def test_room_load_excludes_dm_rows_and_private_content(owner, db_path):
    sender, recipient = members(owner)
    room = owner.create_room(
        sender[0], "private-room", "private-rules", "private-board"
    )
    room_id = room["room_id"]
    owner.join_room(recipient[0], room_id)
    for participant in (sender, recipient):
        board = owner.read_board(participant[0], room_id)
        owner.confirm_board(participant[0], room_id, board["version"])
    owner.send_message(sender[0], room_id, "private-room-key", "private-room-message")
    dm_id = owner.open_dm(sender[0], recipient[1])["dm_id"]
    owner.send_dm(sender[0], dm_id, "private-dm-key", "private-dm-message")
    owner.create_room(sender[0], "second-room", None, None)

    # A legitimate legacy message may have no usable Server timestamp.
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE messages SET created_at = NULL WHERE room_id = ?", (room_id,)
        )
    token = console_token(owner)
    owner.grant_console_read(USERNAME)
    owner.grant_transcript_read(USERNAME, room_id)
    with denied("forbidden"):
        owner.console_room_load(token, room_id)
    owner.revoke_transcript_read(USERNAME, room_id)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)

    snapshot = owner.console_maintenance_snapshot(token)
    values = snapshot["values"]
    assert values["members_stored"] == 2
    assert values["team_rooms_stored"] == 2
    assert values["messages_stored"] == 1
    assert values["pending_inbox_events"] == 1
    assert values["oldest_pending_age_seconds"] is None

    load = owner.console_room_load(token, room_id)["room"]
    assert load == {
        "room_id": room_id,
        "sampled_at": load["sampled_at"],
        "state": "fresh",
        "member_count": 2,
        "messages_stored": 1,
        "pending_inbox_events": 1,
        "oldest_pending_age_seconds": None,
        "last_activity_at": load["last_activity_at"],
        "load_rates": None,
    }
    assert datetime.fromisoformat(load["last_activity_at"]) <= datetime.fromisoformat(
        load["sampled_at"]
    )
    for hidden in ("private-", sender[1], recipient[1], dm_id):
        assert hidden not in str((snapshot, load))
    for identifier in (dm_id, str(uuid4())):
        with denied("not_found"):
            owner.console_room_load(token, identifier)

    # Loads observe the current room instead of reusing the prior read.
    events = owner.inbox(recipient[0], 0, 10, ("message",))
    owner.acknowledge_event(recipient[0], events[0]["event_id"])
    assert owner.console_room_load(token, room_id)["room"]["pending_inbox_events"] == 0


def test_room_pending_age_uses_oldest_usable_delivery(owner, db_path):
    sender, recipient = members(owner)
    room_id = owner.create_room(sender[0], "Room", None, None)["room_id"]
    owner.join_room(recipient[0], room_id)
    board = owner.read_board(sender[0], room_id)
    owner.confirm_board(sender[0], room_id, board["version"])
    for key in ("missing", "new", "old"):
        owner.send_message(sender[0], room_id, key, "Private")
    now = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for key, stamp in (
            ("missing", "not a timestamp"),
            ("new", (now - timedelta(seconds=4)).isoformat()),
            ("old", "2026-11-25T12:59:50+08:00"),
        ):
            connection.execute(
                "UPDATE messages SET created_at = ? WHERE client_message_id = ?",
                (stamp, key),
            )
    owner.grant_console_read(USERNAME)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)
    token = console_token(owner)
    reopened = Collaboration(db_path, {}, clock=lambda: now)

    snapshot = reopened.console_maintenance_snapshot(token)["values"]
    room = reopened.console_room_load(token, room_id)["room"]
    for load in (snapshot, room):
        assert load["pending_inbox_events"] == 3
        assert load["oldest_pending_age_seconds"] == 10


def test_observation_failure_has_no_sample_or_private_values(owner, db_path):
    class UnreadableMessagesCollaboration(Collaboration):
        def _connect(self):
            connection = super()._connect()

            def authorize(action, table, *_):
                if action == sqlite3.SQLITE_READ and table == "messages":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            return connection

    sender, _ = members(owner)
    room_id = owner.create_room(sender[0], "Room", None, None)["room_id"]
    owner.grant_console_read(USERNAME)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)
    token = console_token(owner)
    unavailable = UnreadableMessagesCollaboration(db_path, {})

    snapshot = unavailable.console_maintenance_snapshot(token)
    assert snapshot["state"] == "unavailable"
    assert snapshot["sampled_at"] is None
    assert all(value is None for value in snapshot["values"].values())
    assert unavailable.console_room_load(token, room_id)["room"] == {
        "room_id": room_id,
        "sampled_at": None,
        "state": "unavailable",
        "member_count": None,
        "messages_stored": None,
        "pending_inbox_events": None,
        "oldest_pending_age_seconds": None,
        "last_activity_at": None,
        "load_rates": None,
    }


def test_log_audit_rechecks_grants_and_persists_only_query_metadata(owner, db_path):
    token = console_token(owner)
    enrollment = owner.login(USERNAME, PASSWORD)["access_token"]
    owner.grant_console_read(USERNAME)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)
    with denied("forbidden"):
        owner.authorize_console_logs(token)
    owner.set_maintenance_grant(USERNAME, "logs_read", True)
    with denied("unauthorized"):
        owner.authorize_console_logs(enrollment)
    owner.authorize_console_logs(token)
    owner.set_maintenance_grant(USERNAME, "logs_read", False)
    with denied("forbidden"):
        owner.audit_console_logs(token, **WINDOW, component="gateway", result_count=2)
    owner.set_maintenance_grant(USERNAME, "logs_read", True)
    owner.revoke_console_read(USERNAME)
    with denied("forbidden"):
        owner.authorize_console_logs(token)
    owner.grant_console_read(USERNAME)
    with denied("invalid_request"):
        owner.audit_console_logs(
            token, **WINDOW, component="private-value", result_count=2
        )

    owner.audit_console_logs(token, **WINDOW, component="gateway", result_count=2)
    owner_id = owner.list_accounts()[0]["owner_id"]
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row) for row in connection.execute("SELECT * FROM log_access_audit")
        ]
    assert len(rows) == 1
    assert rows[0] == {
        "owner_id": owner_id,
        "window_since": WINDOW["since"],
        "window_until": WINDOW["until"],
        "component": "gateway",
        "result_count": 2,
        "at": rows[0]["at"],
    }
    assert datetime.fromisoformat(rows[0]["at"]).utcoffset() == timedelta(0)


def test_maintenance_cli_preserves_existing_schema_and_grants(owner, db_path):
    sender, _ = members(owner)
    room_id = owner.create_room(sender[0], "Legacy room", None, None)["room_id"]
    token = console_token(owner)
    owner.grant_console_read(USERNAME)
    owner.grant_transcript_read(USERNAME, room_id)
    previous_version = database_version(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS operator_maintenance_grants")
        connection.execute("DROP TABLE IF EXISTS log_access_audit")

    upgraded = Collaboration(db_path, {})
    assert upgraded.console_room_detail(token, room_id)["transcript_allowed"] is True
    with denied("invalid_request"):
        upgraded.set_maintenance_grant(USERNAME, "console_read", True)

    for scope, read in (
        ("maintenance", lambda: upgraded.console_room_load(token, room_id)),
        ("logs", lambda: upgraded.authorize_console_logs(token)),
    ):
        for action in ("grant", "grant", "revoke", "revoke"):
            args = account_cli.parser().parse_args(
                ["--db", str(db_path), f"{action}-{scope}", USERNAME]
            )
            account_cli.run(args, Collaboration(db_path, {}))
            if action == "grant":
                read()
            else:
                with denied("forbidden"):
                    read()

    assert database_version(db_path) == previous_version
    assert (
        Collaboration(db_path, {}).console_room_detail(token, room_id)[
            "transcript_allowed"
        ]
        is True
    )


def test_owner_counters_include_login_grant_and_sqlite_lock_failures(db_path):
    class ImmediateLockCollaboration(Collaboration):
        def _connect(self):
            connection = super()._connect()
            connection.execute("PRAGMA busy_timeout = 0")
            return connection

    owner = ImmediateLockCollaboration(db_path, {})
    owner.create_account(USERNAME, PASSWORD)
    token = console_token(owner)
    enrollment = owner.login(USERNAME, PASSWORD)["access_token"]

    with denied("unauthorized"):
        owner.console_maintenance_snapshot(enrollment)
    with denied("unauthorized"):
        owner.console_maintenance_snapshot("invalid")
    with denied("forbidden"):
        owner.console_maintenance_snapshot(token)
    for _ in range(5):
        with denied("unauthorized"):
            owner.login(USERNAME, "incorrect password")
    with denied("rate_limited"):
        owner.login(USERNAME, "incorrect password")

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            owner.grant_console_read(USERNAME)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY

    owner.grant_console_read(USERNAME)
    owner.set_maintenance_grant(USERNAME, "maintenance_read", True)
    values = owner.console_maintenance_snapshot(token)["values"]
    assert values["login_failures_total"] == 5
    assert values["rate_limited_total"] == 1
    assert values["sqlite_busy_errors_total"] == 1
    assert values["transactions_total"] > 0
    assert values["transaction_duration_seconds_total"] > 0
    assert values["authorization_denials_total"] == {
        "missing_session": 1,
        "wrong_purpose": 1,
        "inactive_session": 0,
        "missing_grant": 1,
    }
