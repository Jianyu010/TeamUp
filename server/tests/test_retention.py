import sqlite3
import subprocess
from base64 import urlsafe_b64decode
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from queue import Queue
from threading import Thread
from time import monotonic, sleep

import httpx
import pytest
from teamup_server import collaboration as collaboration_module
from teamup_server.collaboration import Collaboration, RetentionPass
from teamup_server.maintenance import RETENTION_CADENCE
from test_contract import (
    _V2_DATABASE,
    api,
    assert_utc_timestamp,
    confirm,
    credential,
    enroll,
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
)

OLD = "2020-01-01T00:00:00Z"
OLD_FRACTIONAL = "2020-01-01T00:00:00.123456Z"


@contextmanager
def retention_api(db_path, *, hours=1):
    with live_server(db_path, extra=("--room-retention-hours", str(hours))) as (
        client,
        _,
    ):
        yield client


def expire_in_sqlite(db_path, stamps):
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executemany(
            "UPDATE messages SET created_at = ? WHERE message_id = ?",
            [(stamp, message_id) for message_id, stamp in stamps.items()],
        )


def wait_for(condition, timeout, *, step=0.2):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return
        sleep(step)
    pytest.fail(f"Condition not met within {timeout} seconds")


def next_event_line(lines, timeout=5):
    """The next SSE frame, skipping keepalive comments the server may interleave."""
    stop = monotonic() + timeout
    while True:
        remaining = stop - monotonic()
        assert remaining > 0, "The stream closed without the expected event"
        line = lines.get(timeout=remaining)
        if line.startswith(":"):
            continue
        assert line == "event: inbox_changed", line
        return


def test_cli_guards_room_retention_hours(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    for invalid in ("0", "-3", "876001", "1000000000000", "abc", "2.5"):
        result = subprocess.run(
            [*server_command(db_path), "--room-retention-hours", invalid],
            env=server_env(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode != 0, invalid
        assert "--room-retention-hours" in result.stderr, invalid
    accepted = subprocess.run(
        [
            *server_command(db_path),
            "--host",
            "0.0.0.0",
            "--room-retention-hours",
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


def test_expire_due_batches_compare_true_utc_and_keep_counters(tmp_path, monkeypatch):
    db_path = tmp_path / "team.sqlite3"
    collaboration = Collaboration(db_path, {"alice": credential("enroll-alice")})
    token = credential("sender")
    target_token = credential("target")
    for owner, key, member_token in (
        ("alice", "sender-key", token),
        ("alice", "target-key", target_token),
    ):
        collaboration.enroll(
            credential(f"enroll-{owner}"),
            harness_instance_id="harness",
            session_key=key,
            role_description=key,
            member_token=member_token,
        )
    room_id = collaboration.create_room(token, "Build", None, None, "retained")[
        "room_id"
    ]
    collaboration.join_room(target_token, room_id)
    for candidate in (token, target_token):
        collaboration.read_board(candidate, room_id)
        collaboration.confirm_board(candidate, room_id, 1)
    target_id = collaboration.get_member(target_token)["member_id"]
    first = collaboration.send_message(token, room_id, "one", "hello needle", target_id)
    second = collaboration.send_message(token, room_id, "two", "more needle", None)
    third = collaboration.send_message(token, room_id, "three", "kept needle", None)
    other_id = collaboration.create_room(token, "Second", None, None)["room_id"]
    collaboration.read_board(token, other_id)
    collaboration.confirm_board(token, other_id, 1)
    other = collaboration.send_message(token, other_id, "kept", "other needle", None)

    # Fresh messages never expire, with or without an explicit clock.
    assert collaboration.expire_due(timedelta(hours=1)) == RetentionPass(0, 0, False)
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert collaboration.expire_due(timedelta(hours=1), now=moment) == RetentionPass(
        0, 0, False
    )

    # Plain and fractional RFC 3339 stamps both count; batches stay bounded.
    expire_in_sqlite(
        db_path,
        {
            first["message_id"]: OLD,
            second["message_id"]: OLD_FRACTIONAL,
        },
    )
    monkeypatch.setattr(collaboration_module, "_EXPIRE_BATCH", 1)
    assert collaboration.expire_due(timedelta(hours=1), now=moment) == RetentionPass(
        1, 1, True
    )
    assert collaboration.expire_due(timedelta(hours=1), now=moment) == RetentionPass(
        1, 1, True
    )
    assert collaboration.expire_due(timedelta(hours=1), now=moment) == RetentionPass(
        0, 0, False
    )

    page = collaboration.history(token, room_id, 0, 50)
    assert page == {
        "messages": [third],
        "expired_through_message_id": second["message_id"],
        "history_truncated": True,
    }
    # The watermark is per room: a never-pruned room keeps the legacy shape.
    assert collaboration.history(token, other_id, 0, 50) == {"messages": [other]}

    # Unknown timestamps never expire.
    expire_in_sqlite(db_path, {third["message_id"]: None})
    assert collaboration.expire_due(timedelta(hours=1)) == RetentionPass(0, 0, False)
    survivors = collaboration.history(token, room_id, 0, 50)["messages"]
    assert [message["message_id"] for message in survivors] == [third["message_id"]]
    assert survivors[0]["created_at"] is None

    # Exactly the cutoff expires; one microsecond later survives.
    fourth = collaboration.send_message(token, room_id, "four", "edge needle", None)
    fifth = collaboration.send_message(token, room_id, "five", "edge needle", None)
    expire_in_sqlite(
        db_path,
        {
            fourth["message_id"]: "2025-12-31T23:00:00Z",
            fifth["message_id"]: "2025-12-31T23:00:00.000001Z",
        },
    )
    assert collaboration.expire_due(timedelta(hours=1), now=moment) == RetentionPass(
        1, 1, True
    )
    assert collaboration.expire_due(timedelta(hours=1), now=moment) == RetentionPass(
        0, 0, False
    )
    edge = collaboration.history(token, room_id, 0, 50)
    assert [message["message_id"] for message in edge["messages"]] == [
        third["message_id"],
        fifth["message_id"],
    ]
    assert edge["expired_through_message_id"] == fourth["message_id"]
    assert edge["history_truncated"] is True

    # Counters stay monotonic: a deleted key allocates a new, higher message ID.
    retried = collaboration.send_message(token, room_id, "two", "more needle", None)
    assert retried["message_id"] > fifth["message_id"]
    # Invalid retention ages are rejected by the owner entry itself.
    with pytest.raises(ValueError):
        collaboration.expire_due(timedelta(0))


def test_retention_startup_pass_empties_expired_rooms(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        for label in ("sender", "target"):
            assert enroll(client, label).status_code == 200
        room_path = room(client)
        join(client, "target", room_path)
        for label in ("sender", "target"):
            confirm(client, label, room_path)
        target_id = member(client, "target")
        first = send(
            client, room_path, "ack-me", text="first needle", target=target_id
        ).json()
        second = send(client, room_path, "pending-me", text="second needle").json()
        events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"]["message_id"] for event in events] == [
            first["message_id"],
            second["message_id"],
        ]
        acked_event, pending_event = (event["event_id"] for event in events)
        # A normal acknowledgment leaves the transcript intact.
        assert post(client, "target", f"/inbox/{acked_event}/ack").status_code == 200
        assert get(client, "sender", f"{room_path}/messages").json() == {
            "messages": [first, second]
        }
        assert set(get(client, "sender", f"{room_path}/search?q=needle").json()) == {
            "matches"
        }
        watermark = second["message_id"]

    expire_in_sqlite(
        db_path,
        {first["message_id"]: OLD, second["message_id"]: OLD_FRACTIONAL},
    )
    with retention_api(db_path) as client:
        assert get(client, "sender", f"{room_path}/messages").json() == {
            "messages": [],
            "expired_through_message_id": watermark,
            "history_truncated": True,
        }
        assert get(client, "sender", f"{room_path}/search?q=needle").json() == {
            "matches": [],
            "expired_through_message_id": watermark,
            "history_truncated": True,
        }
        # The pending delivery disappeared with the acknowledged one.
        assert get(client, "target", "/inbox").json() == {"events": []}
        error(post(client, "target", f"/inbox/{acked_event}/ack"), 404, "not_found")
        error(post(client, "target", f"/inbox/{pending_event}/ack"), 404, "not_found")
        # The same retry key now creates a message with a new ID and event.
        retried = send(client, room_path, "pending-me", text="second needle").json()
        assert retried["message_id"] > watermark
        assert_utc_timestamp(retried["created_at"])
        target_events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"] for event in target_events] == [retried]
        assert target_events[0]["event_id"] > pending_event
        assert get(client, "sender", f"{room_path}/search?q=needle").json() == {
            "matches": [
                {
                    "message_id": retried["message_id"],
                    "sender_member_id": member(client, "sender"),
                    "created_at": retried["created_at"],
                    "snippet": "second needle",
                }
            ],
            "expired_through_message_id": watermark,
            "history_truncated": True,
        }


def _v3_database(db_path, room_id, member_id):
    old_token = credential("old")
    old_digest = sha256(urlsafe_b64decode(old_token + "=")).digest()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V2_DATABASE)
        connection.execute(
            "ALTER TABLE rooms ADD COLUMN history_policy TEXT NOT NULL "
            "DEFAULT 'since_joining' "
            "CHECK (history_policy IN ('retained', 'since_joining'))"
        )
        connection.execute("ALTER TABLE messages ADD COLUMN created_at TEXT")
        connection.execute(
            "CREATE TABLE id_counters (counter TEXT PRIMARY KEY "
            "CHECK (counter IN ('message_id', 'event_id')), "
            "last_id INTEGER NOT NULL CHECK (last_id >= 0))"
        )
        connection.execute("PRAGMA user_version = 3")
        connection.execute(
            "INSERT INTO members VALUES (?, 'legacy-owner', 'old-harness', "
            "'old-session', 'Old role', ?)",
            (member_id, old_digest),
        )
        connection.execute(
            "INSERT INTO rooms VALUES (?, 'Legacy room', 1, 'Rules', 'Protocol', "
            "'retained')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, member_id)
        )
        connection.execute(
            "INSERT INTO messages VALUES (41, ?, ?, 'old-null', 'No time', NULL, NULL)",
            (room_id, member_id),
        )
        connection.execute(
            "INSERT INTO messages VALUES (42, ?, ?, 'old-stamp', 'Stamped', NULL, ?)",
            (room_id, member_id, OLD),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 42, 'message', 0)", (member_id,)
        )
        connection.execute(
            "INSERT INTO id_counters VALUES ('message_id', 42), ('event_id', 17)"
        )


def test_v3_upgrade_prunes_timestamps_and_keeps_null_times(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    member_id = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    _v3_database(db_path, room_id, member_id)
    room_path = f"/rooms/{room_id}"
    with retention_api(db_path) as client:
        survivor = {
            "message_id": 41,
            "room_id": room_id,
            "sender_member_id": member_id,
            "client_message_id": "old-null",
            "text": "No time",
            "mention_member_id": None,
            "created_at": None,
        }
        assert get(client, "old", f"{room_path}/messages").json() == {
            "messages": [survivor],
            "expired_through_message_id": 42,
            "history_truncated": True,
        }
        sent = post(
            client,
            "old",
            f"{room_path}/messages",
            json={"client_message_id": "fresh", "text": "Fresh start"},
        ).json()
        assert sent["message_id"] == 43
        assert_utc_timestamp(sent["created_at"])
        assert get(client, "old", f"{room_path}/messages").json() == {
            "messages": [survivor, sent],
            "expired_through_message_id": 42,
            "history_truncated": True,
        }
        assert get(client, "old", "/inbox").json() == {"events": []}
        error(post(client, "old", "/inbox/17/ack"), 404, "not_found")
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


def test_retention_startup_pass_drives_bounded_batches_to_completion(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        assert enroll(client, "sender").status_code == 200
        room_path = room(client)
        confirm(client, "sender", room_path)
        sender_id = member(client, "sender")
        room_id = room_path.rsplit("/", 1)[-1]
    batch = 520
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, NULL, ?, ?, ?, NULL, ?)",
            [
                (
                    1000 + index,
                    room_id,
                    sender_id,
                    f"direct-{index}",
                    f"bulk {index}",
                    OLD if index % 2 else OLD_FRACTIONAL,
                )
                for index in range(batch)
            ],
        )
        connection.execute(
            "UPDATE id_counters SET last_id = ? WHERE counter = 'message_id'",
            (999 + batch,),
        )
    with retention_api(db_path) as client:
        wait_for(
            lambda: (
                get(client, "sender", f"{room_path}/messages?limit=100").json()
                == {
                    "messages": [],
                    "expired_through_message_id": 1519,
                    "history_truncated": True,
                }
            ),
            timeout=10,
        )
        sent = send(client, room_path, "after-bulk", text="fresh").json()
        assert sent["message_id"] == 1520


def test_retention_periodic_pass_prunes_while_streams_stay_live(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with retention_api(db_path) as client:
        for label in ("sender", "target"):
            assert enroll(client, label).status_code == 200
        room_path = room(client)
        join(client, "target", room_path)
        for label in ("sender", "target"):
            confirm(client, label, room_path)
        doomed = send(client, room_path, "doomed", text="periodic needle").json()
        event_id = get(client, "target", "/inbox").json()["events"][0]["event_id"]
        lines = Queue()

        def read_stream():
            try:
                with (
                    httpx.Client(
                        base_url=str(client.base_url), trust_env=False, timeout=None
                    ) as streaming,
                    streaming.stream(
                        "GET",
                        "/v1/events",
                        headers={
                            "Authorization": f"Bearer {credential('target')}",
                            "Accept": "text/event-stream",
                        },
                    ) as response,
                ):
                    assert response.status_code == 200
                    for line in response.iter_lines():
                        if line:
                            lines.put(line)
            except (AssertionError, httpx.HTTPError):
                pass

        reader = Thread(target=read_stream, daemon=True)
        reader.start()
        assert lines.get(timeout=3).startswith(":")

        expire_in_sqlite(db_path, {doomed["message_id"]: OLD})
        # The periodic pass commits within the documented cadence.
        wait_for(
            lambda: (
                get(client, "sender", f"{room_path}/messages").json()
                == {
                    "messages": [],
                    "expired_through_message_id": doomed["message_id"],
                    "history_truncated": True,
                }
            ),
            timeout=RETENTION_CADENCE.total_seconds() + 10,
        )
        assert get(client, "target", "/inbox").json() == {"events": []}
        error(post(client, "target", f"/inbox/{event_id}/ack"), 404, "not_found")
        # The open stream still signals fresh committed deliveries.
        while not lines.empty():
            lines.get_nowait()
        fresh = send(client, room_path, "alive", text="still here").json()
        next_event_line(lines)
        assert lines.get(timeout=3) == "data: {}"
        assert get(client, "target", "/inbox").json()["events"][0]["message"] == fresh


def test_retention_metadata_follows_policy_floor_and_pagination(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        for label in ("sender", "target"):
            assert enroll(client, label).status_code == 200
        sender_id = member(client, "sender")
        retained = room(client, history_policy="retained")
        confirm(client, "sender", retained)
        first = send(client, retained, "r-one", text="retained needle one").json()
        second = send(client, retained, "r-two", text="retained needle two").json()
        third = send(client, retained, "r-three", text="retained needle three").json()
        limited = room(client)
        confirm(client, "sender", limited)
        lost = send(client, limited, "s-one", text="lost needle").json()
        join(client, "target", limited)
        confirm(client, "target", limited)
        kept = send(client, limited, "s-two", text="visible needle").json()
        never = room(client)
        confirm(client, "sender", never)
        fresh = send(client, never, "n-one", text="fresh needle").json()

    expire_in_sqlite(
        db_path,
        {
            first["message_id"]: OLD,
            second["message_id"]: OLD_FRACTIONAL,
            lost["message_id"]: OLD,
        },
    )
    with retention_api(db_path) as client:
        watermark = second["message_id"]
        # The retained creator pages the surviving message after the boundary.
        assert get(client, "sender", f"{retained}/messages").json() == {
            "messages": [third],
            "expired_through_message_id": watermark,
            "history_truncated": True,
        }
        assert (
            get(client, "sender", f"{retained}/messages?after={first['message_id']}")
            .json()
            .get("history_truncated")
            is True
        )
        assert get(
            client, "sender", f"{retained}/messages?after={watermark}"
        ).json() == {
            "messages": [third],
            "expired_through_message_id": watermark,
            "history_truncated": False,
        }
        # Backwards pages keep both bounds and the expiry boundary.
        assert get(
            client, "sender", f"{retained}/messages?limit=2&before={watermark}"
        ).json() == {
            "messages": [],
            "expired_through_message_id": watermark,
            "history_truncated": True,
        }
        assert get(
            client,
            "sender",
            f"{retained}/messages?limit=2&before={first['message_id']}",
        ).json() == {
            "messages": [],
            "expired_through_message_id": watermark,
            "history_truncated": False,
        }
        assert get(
            client, "sender", f"{retained}/messages?limit=2&before={2**70}"
        ).json() == {
            "messages": [third],
            "expired_through_message_id": watermark,
            "history_truncated": True,
        }
        # The since_joining floor merges above the expired join boundary.
        assert get(client, "target", f"{limited}/messages").json() == {
            "messages": [kept],
            "expired_through_message_id": lost["message_id"],
            "history_truncated": False,
        }
        assert get(client, "sender", f"{limited}/messages").json() == {
            "messages": [kept],
            "expired_through_message_id": lost["message_id"],
            "history_truncated": True,
        }
        assert get(client, "target", f"{limited}/search?q=visible").json() == {
            "matches": [
                {
                    "message_id": kept["message_id"],
                    "sender_member_id": sender_id,
                    "created_at": kept["created_at"],
                    "snippet": "visible needle",
                }
            ],
            "expired_through_message_id": lost["message_id"],
            "history_truncated": False,
        }
        assert get(client, "sender", f"{limited}/search?q=lost").json() == {
            "matches": [],
            "expired_through_message_id": lost["message_id"],
            "history_truncated": True,
        }
        assert get(
            client, "sender", f"{retained}/search?q=retained&after={watermark}"
        ).json() == {
            "matches": [
                {
                    "message_id": third["message_id"],
                    "sender_member_id": sender_id,
                    "created_at": third["created_at"],
                    "snippet": "retained needle three",
                }
            ],
            "expired_through_message_id": watermark,
            "history_truncated": False,
        }
        # A never-pruned room keeps the exact legacy wrappers.
        assert get(client, "sender", f"{never}/messages").json() == {
            "messages": [fresh]
        }
        assert get(client, "sender", f"{never}/search?q=fresh").json() == {
            "matches": [
                {
                    "message_id": fresh["message_id"],
                    "sender_member_id": sender_id,
                    "created_at": fresh["created_at"],
                    "snippet": "fresh needle",
                }
            ]
        }
