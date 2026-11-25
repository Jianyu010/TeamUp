import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from time import sleep
from uuid import uuid4

import pytest
from teamup_server.collaboration import Collaboration
from test_contract import (
    KEYS,
    RacingCollaboration,
    api,
    confirm,
    error,
    get,
    join,
    live_server,
    member,
    post,
    room,
    send,
    server_command,
    server_env,
    wait_for,
)
from test_direct_messages import dm_events, dm_read, dm_send, enroll_team, open_dm


def prune(db_path, cutoff):
    return Collaboration(db_path, KEYS).expire_due_dms(cutoff)


def stored(db_path, query, params=()):
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(query, params).fetchall()


def dm_message_ids(db_path):
    return [
        row[0]
        for row in stored(
            db_path,
            "SELECT message_id FROM messages WHERE dm_id IS NOT NULL ORDER BY message_id",
        )
    ]


def test_dm_expiry_prunes_in_one_transaction_and_spares_rooms(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client)
        target = member(client, "target")
        room_path = room(client)
        join(client, "target", room_path)
        confirm(client, "sender", room_path)
        confirm(client, "target", room_path)
        room_message = send(client, room_path, "room-old", text="room body").json()

        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        first = dm_send(client, "sender", dm_id, "one", "first private").json()
        boundary = datetime.now(UTC)
        sleep(0.01)
        second = dm_send(client, "target", dm_id, "two", "second private").json()

        assert dm_read(client, "sender", dm_id).json() == {"messages": [first, second]}
        target_event = dm_events(client, "target", kinds="dm")[0]
        sender_event = dm_events(client, "sender", kinds="dm")[0]
        assert target_event["message"] == first
        assert sender_event["message"] == second

        assert prune(db_path, boundary) == (1, False)

        expected = {
            "messages": [second],
            "expired_through_message_id": first["message_id"],
            "history_truncated": True,
        }
        assert dm_read(client, "sender", dm_id).json() == expected
        assert dm_read(client, "target", dm_id).json() == expected

        assert dm_events(client, "target", kinds="dm") == []
        assert [
            event["message"] for event in dm_events(client, "sender", kinds="dm")
        ] == [second]
        error(
            post(client, "target", f"/inbox/{target_event['event_id']}/ack"),
            404,
            "not_found",
        )
        assert post(
            client, "sender", f"/inbox/{sender_event['event_id']}/ack"
        ).json() == {"event_id": sender_event["event_id"]}
        assert post(
            client, "sender", f"/inbox/{sender_event['event_id']}/ack"
        ).json() == {"event_id": sender_event["event_id"]}
        assert dm_events(client, "sender", kinds="dm") == []
        assert [
            event["message"] for event in dm_events(client, "target", kinds=None)
        ] == [room_message]
        assert get(client, "sender", f"{room_path}/messages").json() == {
            "messages": [room_message]
        }

        assert stored(
            db_path,
            """
            SELECT COUNT(*) FROM inbox_events AS e
            LEFT JOIN messages AS m ON m.message_id = e.message_id
            WHERE m.message_id IS NULL
            """,
        ) == [(0,)]
        assert dm_message_ids(db_path) == [second["message_id"]]
        assert stored(
            db_path, "SELECT COUNT(*) FROM messages WHERE room_id IS NOT NULL"
        ) == [(1,)]
        assert stored(
            db_path,
            "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
            (dm_id,),
        ) == [(first["message_id"],)]
        assert stored(
            db_path,
            "SELECT last_id FROM id_counters WHERE counter = 'message_id'",
        ) == [(second["message_id"],)]

        recreated = dm_send(client, "sender", dm_id, "one", "first private").json()
        assert recreated["message_id"] > second["message_id"]
        assert dm_read(client, "sender", dm_id).json() == {
            "messages": [second, recreated],
            "expired_through_message_id": first["message_id"],
            "history_truncated": True,
        }
        new_events = dm_events(client, "target", kinds="dm")
        assert new_events[0]["event_id"] > target_event["event_id"]
        assert new_events[0]["message"] == recreated


def test_dm_expiry_metadata_is_durable_across_restart_and_complete_prune(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client)
        target = member(client, "target")
        reader = member(client, "reader")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        dm_send(client, "sender", dm_id, "one", "first private")
        second = dm_send(client, "target", dm_id, "two", "second private").json()
        boundary = datetime.now(UTC)
        sleep(0.01)
        other_dm = open_dm(client, "sender", reader).json()["dm_id"]
        fresh = dm_send(client, "sender", other_dm, "fresh", "kept private").json()

        assert prune(db_path, boundary) == (2, False)
        assert prune(db_path, boundary) == (0, False)

        assert dm_read(client, "sender", dm_id).json() == {
            "messages": [],
            "expired_through_message_id": second["message_id"],
            "history_truncated": True,
        }
        assert dm_read(
            client, "sender", dm_id, f"?after={second['message_id']}"
        ).json() == {
            "messages": [],
            "expired_through_message_id": second["message_id"],
            "history_truncated": False,
        }
        assert dm_read(client, "sender", other_dm).json() == {"messages": [fresh]}

    with api(db_path) as client:
        assert dm_read(client, "sender", dm_id).json() == {
            "messages": [],
            "expired_through_message_id": second["message_id"],
            "history_truncated": True,
        }
        later = dm_send(client, "sender", dm_id, "later", "later private").json()
        assert later["message_id"] > second["message_id"]
        assert dm_read(
            client, "sender", dm_id, f"?after={second['message_id']}"
        ).json() == {
            "messages": [later],
            "expired_through_message_id": second["message_id"],
            "history_truncated": False,
        }


def test_dm_expiry_metadata_is_visible_to_participants_only(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client)
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        message = dm_send(client, "sender", dm_id, "secret", "private body").json()

        assert set(dm_read(client, "sender", dm_id).json()) == {"messages"}
        unknown = get(client, "reader", f"/dms/{uuid4()}/messages")
        error(unknown, 404, "not_found")
        error(dm_read(client, "reader", dm_id), 404, "not_found")

        assert prune(db_path, datetime.now(UTC)) == (1, False)

        error(client.get(f"/v1/dms/{dm_id}/messages"), 401, "unauthorized")
        blocked = dm_read(client, "reader", dm_id)
        error(blocked, 404, "not_found")
        assert blocked.json() == unknown.json()
        expected = {
            "messages": [],
            "expired_through_message_id": message["message_id"],
            "history_truncated": True,
        }
        assert dm_read(client, "sender", dm_id).json() == expected
        assert dm_read(client, "target", dm_id).json() == expected


def test_dm_expiry_spares_unknown_timestamps_and_compares_true_instants(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        sender = member(client, "sender")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        live = dm_send(client, "sender", dm_id, "live", "recent private").json()
        with closing(sqlite3.connect(db_path)) as connection, connection:
            rows = (
                (
                    live["message_id"] + 1,
                    "legacy-whole",
                    "legacy whole",
                    "2020-01-01T00:00:00Z",
                ),
                (
                    live["message_id"] + 2,
                    "legacy-offset",
                    "legacy offset",
                    "2020-01-01T02:00:00+02:00",
                ),
                (live["message_id"] + 3, "null-ts", "no timestamp", None),
                (
                    live["message_id"] + 4,
                    "garbage",
                    "garbage timestamp",
                    "not-a-timestamp",
                ),
            )
            for message_id, key, text, created_at in rows:
                connection.execute(
                    """
                    INSERT INTO messages (
                        message_id, room_id, dm_id, sender_member_id, client_message_id,
                        text, mention_member_id, created_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?)
                    """,
                    (message_id, dm_id, sender, key, text, created_at),
                )
            connection.execute(
                "UPDATE id_counters SET last_id = ? WHERE counter = 'message_id'",
                (live["message_id"] + 4,),
            )

        cutoff = datetime(2020, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)
        assert prune(db_path, cutoff) == (2, False)
        assert dm_message_ids(db_path) == [
            live["message_id"],
            live["message_id"] + 3,
            live["message_id"] + 4,
        ]
        assert stored(
            db_path,
            "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
            (dm_id,),
        ) == [(live["message_id"] + 2,)]

        assert prune(db_path, datetime.now(UTC)) == (1, False)
        assert dm_message_ids(db_path) == [
            live["message_id"] + 3,
            live["message_id"] + 4,
        ]
        assert prune(db_path, datetime(2100, 1, 1, tzinfo=UTC)) == (0, False)
        assert dm_message_ids(db_path) == [
            live["message_id"] + 3,
            live["message_id"] + 4,
        ]

        with pytest.raises(ValueError):
            prune(db_path, datetime(2020, 1, 1))
        with pytest.raises(ValueError):
            prune(db_path, "2020-01-01T00:00:00Z")


def test_dm_expiry_rejects_a_cutoff_outside_representable_utc(tmp_path):
    """An aware cutoff that cannot convert to UTC is refused before any work."""
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        due = dm_send(client, "sender", dm_id, "due", "still due private").json()

    for unusable in (
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))),
    ):
        with pytest.raises(ValueError):
            prune(db_path, unusable)

    # The rejected cutoffs deleted nothing; a normal cutoff still prunes.
    assert dm_message_ids(db_path) == [due["message_id"]]
    assert prune(db_path, datetime.now(UTC)) == (1, False)


def test_dm_expiry_prunes_bounded_batches_and_never_reuses_ids(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        sender = member(client, "sender")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        first = dm_send(client, "sender", dm_id, "first", "first private").json()
        with closing(sqlite3.connect(db_path)) as connection, connection:
            last_event_id = connection.execute(
                "SELECT last_id FROM id_counters WHERE counter = 'event_id'"
            ).fetchone()[0]
            for offset in range(501):
                message_id = first["message_id"] + 1 + offset
                connection.execute(
                    """
                    INSERT INTO messages (
                        message_id, room_id, dm_id, sender_member_id, client_message_id,
                        text, mention_member_id, created_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, NULL, '2020-01-01T00:00:00Z')
                    """,
                    (
                        message_id,
                        dm_id,
                        sender,
                        f"legacy-{message_id}",
                        f"bulk {message_id}",
                    ),
                )
            for offset, event_id in enumerate((last_event_id + 1, last_event_id + 2)):
                connection.execute(
                    """
                    INSERT INTO inbox_events (event_id, member_id, message_id, kind)
                    VALUES (?, ?, ?, 'dm')
                    """,
                    (event_id, target, first["message_id"] + 500 + offset),
                )
            connection.execute(
                "UPDATE id_counters SET last_id = ? WHERE counter = 'message_id'",
                (first["message_id"] + 501,),
            )
            connection.execute(
                "UPDATE id_counters SET last_id = ? WHERE counter = 'event_id'",
                (last_event_id + 2,),
            )

        collab = Collaboration(db_path, KEYS)
        assert collab.expire_due_dms(datetime.now(UTC)) == (500, True)
        assert dm_message_ids(db_path) == [
            first["message_id"] + 500,
            first["message_id"] + 501,
        ]
        assert stored(
            db_path,
            "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
            (dm_id,),
        ) == [(first["message_id"] + 499,)]
        assert [
            row[0]
            for row in stored(
                db_path, "SELECT message_id FROM inbox_events ORDER BY message_id"
            )
        ] == [first["message_id"] + 500, first["message_id"] + 501]

        assert collab.expire_due_dms(datetime.now(UTC)) == (2, False)
        assert collab.expire_due_dms(datetime.now(UTC)) == (0, False)

        fresh = dm_send(client, "sender", dm_id, "fresh", "fresh private").json()
        assert fresh["message_id"] == first["message_id"] + 502
        assert dm_read(client, "sender", dm_id).json() == {
            "messages": [fresh],
            "expired_through_message_id": first["message_id"] + 501,
            "history_truncated": True,
        }


_V4_DATABASE = """
    PRAGMA user_version = 4;
    CREATE TABLE members (
        member_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        harness_instance_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        role_description TEXT NOT NULL,
        token_digest BLOB NOT NULL UNIQUE,
        UNIQUE (owner_id, harness_instance_id, session_key)
    );
    CREATE TABLE rooms (
        room_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        rules TEXT NOT NULL,
        protocol TEXT NOT NULL,
        history_policy TEXT NOT NULL DEFAULT 'since_joining'
            CHECK (history_policy IN ('retained', 'since_joining'))
    );
    CREATE TABLE room_members (
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        member_id TEXT NOT NULL REFERENCES members(member_id),
        joined_after INTEGER NOT NULL,
        read_board_version INTEGER,
        acked_board_version INTEGER,
        PRIMARY KEY (room_id, member_id)
    );
    CREATE TABLE dms (
        dm_id TEXT PRIMARY KEY,
        member_low TEXT NOT NULL REFERENCES members(member_id),
        member_high TEXT NOT NULL REFERENCES members(member_id),
        UNIQUE (member_low, member_high),
        CHECK (member_low < member_high)
    );
    CREATE TABLE messages (
        message_id INTEGER PRIMARY KEY,
        room_id TEXT REFERENCES rooms(room_id),
        dm_id TEXT REFERENCES dms(dm_id),
        sender_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_message_id TEXT NOT NULL,
        text TEXT NOT NULL,
        mention_member_id TEXT REFERENCES members(member_id),
        created_at TEXT,
        CHECK ((room_id IS NULL) <> (dm_id IS NULL))
    );
    CREATE INDEX room_history ON messages (room_id, message_id);
    CREATE INDEX dm_history ON messages (dm_id, message_id);
    CREATE UNIQUE INDEX room_message_retry ON messages (sender_member_id, client_message_id)
        WHERE room_id IS NOT NULL;
    CREATE UNIQUE INDEX dm_message_retry ON messages (dm_id, sender_member_id, client_message_id)
        WHERE dm_id IS NOT NULL;
    CREATE TABLE inbox_events (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER NOT NULL REFERENCES messages(message_id),
        kind TEXT NOT NULL CHECK (kind IN ('message', 'mention', 'dm')),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        UNIQUE (member_id, message_id)
    );
    CREATE INDEX pending_inbox ON inbox_events (member_id, event_id)
        WHERE acknowledged = 0;
    CREATE TABLE accounts (
        owner_id TEXT NOT NULL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_digest TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
    );
    CREATE TABLE account_sessions (
        token_digest BLOB NOT NULL PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        expires_at INTEGER NOT NULL
    );
    CREATE INDEX account_sessions_owner ON account_sessions (owner_id);
    CREATE TABLE id_counters (
        counter TEXT PRIMARY KEY CHECK (counter IN ('message_id', 'event_id')),
        last_id INTEGER NOT NULL CHECK (last_id >= 0)
    );
"""


def test_v4_upgrade_adds_dm_watermark_and_prunes_legacy_whole_second_rows(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    dm_id = "5a7d9c4e-0d21-4a8f-8f3d-9c2b1e6a4d01"
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    late_member = "6f6d3b93-2c2e-4d5f-9a54-6d0f4f65ab21"
    member_low, member_high = sorted((old_member, late_member))
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V4_DATABASE)
        for member_id, session, role, seed in (
            (old_member, "old-session", "Old role", b"old"),
            (late_member, "late-session", "Late role", b"late"),
        ):
            connection.execute(
                "INSERT INTO members VALUES (?, 'legacy-owner', 'old-harness', "
                "?, ?, ?)",
                (member_id, session, role, sha256(sha256(seed).digest()).digest()),
            )
        connection.execute(
            "INSERT INTO rooms VALUES (?, 'Legacy room', 1, 'Rules', 'Protocol', 'retained')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, old_member)
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 43, 1, 1)", (room_id, late_member)
        )
        connection.execute(
            "INSERT INTO dms VALUES (?, ?, ?)", (dm_id, member_low, member_high)
        )
        connection.execute(
            "INSERT INTO messages VALUES (40, NULL, ?, ?, 'dm-null', 'No timestamp', NULL, NULL)",
            (dm_id, old_member),
        )
        connection.execute(
            "INSERT INTO messages VALUES (41, NULL, ?, ?, 'dm-whole', 'Legacy whole second', NULL, '2020-01-01T00:00:00Z')",
            (dm_id, old_member),
        )
        connection.execute(
            "INSERT INTO messages VALUES (42, NULL, ?, ?, 'dm-later', 'Later legacy', NULL, '2020-01-02T03:04:05Z')",
            (dm_id, late_member),
        )
        connection.execute(
            "INSERT INTO messages VALUES (43, ?, NULL, ?, 'room-old', 'Legacy room body', NULL, NULL)",
            (room_id, old_member),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 41, 'dm', 0)", (late_member,)
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (18, ?, 42, 'dm', 0)", (old_member,)
        )
        connection.execute(
            "INSERT INTO id_counters VALUES ('message_id', 43), ('event_id', 18)"
        )

    with api(db_path) as client:
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
            assert [
                row[0]
                for row in connection.execute(
                    "SELECT expired_through_message_id FROM dms"
                )
            ] == [None]

        history = get(client, "old", f"/dms/{dm_id}/messages").json()
        assert history == {
            "messages": [
                {
                    "message_id": 40,
                    "dm_id": dm_id,
                    "sender_member_id": old_member,
                    "client_message_id": "dm-null",
                    "text": "No timestamp",
                    "created_at": None,
                },
                {
                    "message_id": 41,
                    "dm_id": dm_id,
                    "sender_member_id": old_member,
                    "client_message_id": "dm-whole",
                    "text": "Legacy whole second",
                    "created_at": "2020-01-01T00:00:00Z",
                },
                {
                    "message_id": 42,
                    "dm_id": dm_id,
                    "sender_member_id": late_member,
                    "client_message_id": "dm-later",
                    "text": "Later legacy",
                    "created_at": "2020-01-02T03:04:05Z",
                },
            ]
        }
        assert get(client, "old", f"/rooms/{room_id}/messages").json() == {
            "messages": [
                {
                    "message_id": 43,
                    "room_id": room_id,
                    "sender_member_id": old_member,
                    "client_message_id": "room-old",
                    "text": "Legacy room body",
                    "mention_member_id": None,
                    "created_at": None,
                }
            ]
        }

        cutoff = datetime(2020, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)
        assert Collaboration(db_path, KEYS).expire_due_dms(cutoff) == (1, False)

        assert get(client, "old", f"/dms/{dm_id}/messages").json() == {
            "messages": [history["messages"][0], history["messages"][2]],
            "expired_through_message_id": 41,
            "history_truncated": True,
        }
        error(post(client, "late", "/inbox/17/ack"), 404, "not_found")
        assert post(client, "old", "/inbox/18/ack").json() == {"event_id": 18}

        sent = dm_send(client, "old", dm_id, "legacy-key", "fresh private").json()
        assert sent["message_id"] == 44
        assert (
            get(client, "late", "/inbox?kinds=dm").json()["events"][0]["event_id"] == 19
        )


def test_cli_guards_dm_retention_hours(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    for invalid in ("0", "-3", "876001", "1000000000000", "abc", "2.5"):
        result = subprocess.run(
            [*server_command(db_path), "--dm-retention-hours", invalid],
            env=server_env(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode != 0, invalid
        assert "--dm-retention-hours" in result.stderr, invalid
    accepted = subprocess.run(
        [
            *server_command(db_path),
            "--host",
            "0.0.0.0",
            "--dm-retention-hours",
            "876000",
        ],
        env=server_env(),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert accepted.returncode != 0
    assert "TLS" in accepted.stderr


def test_dm_retention_startup_pass_prunes_only_dm_content(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        room_path = room(client)
        join(client, "target", room_path)
        confirm(client, "sender", room_path)
        confirm(client, "target", room_path)
        room_message = send(client, room_path, "room-keep", text="room needle").json()
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        dm_send(client, "sender", dm_id, "one", "first private")
        second = dm_send(client, "target", dm_id, "two", "second private").json()
        target_event = dm_events(client, "target", kinds="dm")[0]

    # Age every message out together: only the DM family may leave.
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("UPDATE messages SET created_at = '2020-01-01T00:00:00Z'")
    with live_server(db_path, extra=("--dm-retention-hours", "1")) as (client, _):
        # The startup pass runs on its own thread: poll to its committed state.
        wait_for(
            lambda: dm_read(client, "sender", dm_id).json()
            == {
                "messages": [],
                "expired_through_message_id": second["message_id"],
                "history_truncated": True,
            },
            timeout=10,
        )
        assert get(client, "sender", f"{room_path}/messages").json() == {
            "messages": [{**room_message, "created_at": "2020-01-01T00:00:00Z"}]
        }
        assert [
            match["snippet"]
            for match in get(client, "target", f"{room_path}/search?q=needle").json()[
                "matches"
            ]
        ] == ["room needle"]
        room_events = dm_events(client, "target", kinds=None)
        assert [event["message"]["message_id"] for event in room_events] == [
            room_message["message_id"]
        ]
        assert post(
            client, "target", f"/inbox/{room_events[0]['event_id']}/ack"
        ).json() == {"event_id": room_events[0]["event_id"]}
        assert dm_events(client, "target", kinds="dm") == []
        assert dm_events(client, "sender", kinds="dm") == []
        error(
            post(client, "target", f"/inbox/{target_event['event_id']}/ack"),
            404,
            "not_found",
        )
        fresh = dm_send(client, "sender", dm_id, "fresh", "fresh private").json()
        assert fresh["message_id"] > second["message_id"]
        assert dm_read(client, "target", dm_id).json() == {
            "messages": [fresh],
            "expired_through_message_id": second["message_id"],
            "history_truncated": True,
        }


def test_dm_expiry_spares_naive_timestamps_like_room_expiry(tmp_path):
    """The DM family keeps the fail-closed shared timestamp policy."""
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        naive = dm_send(client, "sender", dm_id, "naive", "naive private").json()
        aware = dm_send(client, "target", dm_id, "aware", "aware private").json()
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.executemany(
                "UPDATE messages SET created_at = ? WHERE message_id = ?",
                [
                    ("2020-01-01T00:00:00", naive["message_id"]),
                    ("2020-01-01T00:00:00Z", aware["message_id"]),
                ],
            )

        assert prune(db_path, datetime(2100, 1, 1, tzinfo=UTC)) == (1, False)
        assert dm_message_ids(db_path) == [naive["message_id"]]
        assert stored(
            db_path,
            "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
            (dm_id,),
        ) == [(aware["message_id"],)]


def test_dm_expiry_skips_candidates_removed_between_transactions(tmp_path):
    """A candidate a concurrent pass already removed is never recounted."""
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        first = dm_send(client, "sender", dm_id, "one", "first private").json()

    def competing_expiry():
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM inbox_events WHERE message_id = ?",
                (first["message_id"],),
            )
            connection.execute(
                "DELETE FROM messages WHERE message_id = ?",
                (first["message_id"],),
            )
            connection.execute(
                "UPDATE dms SET expired_through_message_id = ? WHERE dm_id = ?",
                (first["message_id"], dm_id),
            )

    racing = RacingCollaboration(db_path, KEYS, competing_expiry)
    assert racing.expire_due_dms(datetime(2100, 1, 1, tzinfo=UTC)) == (0, False)
    assert dm_message_ids(db_path) == []
    assert stored(
        db_path,
        "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
        (dm_id,),
    ) == [(first["message_id"],)]


def test_dm_expiry_rechecks_the_cutoff_before_deleting(tmp_path):
    """A candidate re-dated between the two passes is reprieved in full."""
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        first = dm_send(client, "sender", dm_id, "one", "first private").json()

    def reprieve():
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                "UPDATE messages SET created_at = ? WHERE message_id = ?",
                ("2999-01-01T00:00:00Z", first["message_id"]),
            )

    racing = RacingCollaboration(db_path, KEYS, reprieve)
    assert racing.expire_due_dms(datetime(2100, 1, 1, tzinfo=UTC)) == (0, False)
    assert dm_message_ids(db_path) == [first["message_id"]]
    assert stored(
        db_path,
        "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
        (dm_id,),
    ) == [(None,)]


def test_dm_expiry_counts_only_candidates_still_due_after_interference(tmp_path):
    """Only rechecked survivors are deleted, counted and watermarked."""
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        removed = dm_send(client, "sender", dm_id, "removed", "removed private").json()
        kept = dm_send(client, "target", dm_id, "kept", "kept private").json()

    def competing_expiry():
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM inbox_events WHERE message_id = ?",
                (removed["message_id"],),
            )
            connection.execute(
                "DELETE FROM messages WHERE message_id = ?",
                (removed["message_id"],),
            )
            connection.execute(
                "UPDATE dms SET expired_through_message_id = ? WHERE dm_id = ?",
                (removed["message_id"], dm_id),
            )

    racing = RacingCollaboration(db_path, KEYS, competing_expiry)
    assert racing.expire_due_dms(datetime(2100, 1, 1, tzinfo=UTC)) == (1, False)
    assert dm_message_ids(db_path) == []
    assert stored(
        db_path,
        "SELECT expired_through_message_id FROM dms WHERE dm_id = ?",
        (dm_id,),
    ) == [(kept["message_id"],)]
    assert stored(
        db_path,
        """
        SELECT COUNT(*) FROM inbox_events AS e
        LEFT JOIN messages AS m ON m.message_id = e.message_id
        WHERE m.message_id IS NULL
        """,
    ) == [(0,)]
