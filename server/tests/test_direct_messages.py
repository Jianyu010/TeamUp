import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from queue import Empty, Queue
from threading import Thread
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest
from test_contract import (
    api,
    assert_utc_timestamp,
    auth,
    confirm,
    credential,
    enroll,
    error,
    find,
    get,
    join,
    live_server,
    member,
    post,
    room,
    send,
)
from test_server_accounts import PASSWORD, admin, bind, enter_password, login_token

OWNERS = {"sender": "alice", "target": "bob", "reader": "bob"}


def enroll_team(client, labels=("sender", "target", "reader")):
    for label in labels:
        assert enroll(client, label, owner=OWNERS[label]).status_code == 200


def open_dm(client, label, member_id):
    return post(client, label, "/dms", json={"member_id": member_id})


def dm_send(client, label, dm_id, key, text="private hello"):
    return post(
        client,
        label,
        f"/dms/{dm_id}/messages",
        json={"client_message_id": key, "text": text},
    )


def dm_read(client, label, dm_id, query=""):
    return get(client, label, f"/dms/{dm_id}/messages{query}")


def dm_events(client, label, kinds="message,mention,dm"):
    query = "" if kinds is None else f"?kinds={quote(kinds)}"
    response = get(client, label, f"/inbox{query}")
    assert response.status_code == 200
    return response.json()["events"]


def test_open_dm_is_idempotent_symmetric_and_stores_one_row(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client)
        sender = member(client, "sender")
        target = member(client, "target")
        reader = member(client, "reader")

        first = open_dm(client, "sender", target)
        assert first.status_code == 200, first.text
        dm = first.json()
        UUID(dm["dm_id"])
        assert dm == {"dm_id": dm["dm_id"], "peer_member_id": target}
        assert open_dm(client, "sender", target).json() == dm
        assert open_dm(client, "target", sender).json() == {
            "dm_id": dm["dm_id"],
            "peer_member_id": sender,
        }
        other = open_dm(client, "sender", reader)
        assert other.status_code == 200
        assert other.json()["dm_id"] != dm["dm_id"]

        with ThreadPoolExecutor(max_workers=12) as workers:
            responses = list(
                workers.map(
                    lambda pair: open_dm(client, *pair),
                    [("sender", target)] * 6 + [("target", sender)] * 6,
                )
            )
        assert all(response.status_code == 200 for response in responses)
        assert {response.json()["dm_id"] for response in responses} == {dm["dm_id"]}

        error(open_dm(client, "sender", sender), 422, "invalid_peer")
        error(open_dm(client, "sender", str(uuid4())), 422, "invalid_peer")
        error(post(client, "sender", "/dms", json={}), 422, "invalid_request")
        error(
            post(client, "sender", "/dms", json={"member_id": "not-a-uuid"}),
            422,
            "invalid_request",
        )
        error(
            post(
                client, "sender", "/dms", json={"member_id": target, "room_id": reader}
            ),
            422,
            "invalid_request",
        )
        error(client.post("/v1/dms", json={"member_id": target}), 401, "unauthorized")

        with closing(sqlite3.connect(db_path)) as connection:
            pairs = connection.execute(
                """
                SELECT member_low, member_high, COUNT(*) FROM dms
                GROUP BY member_low, member_high
                """
            ).fetchall()
        assert sorted(pairs) == sorted(
            [
                (min(sender, target), max(sender, target), 1),
                (min(sender, reader), max(sender, reader), 1),
            ]
        )


def test_dm_send_retry_is_concurrent_idempotent_and_family_scoped(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client)
        sender = member(client, "sender")
        target = member(client, "target")
        reader = member(client, "reader")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]

        error(dm_send(client, "sender", dm_id, "blank", " \n "), 422, "invalid_request")
        error(
            dm_send(client, "sender", dm_id, "long", "x" * 8001), 422, "invalid_request"
        )
        error(dm_send(client, "sender", dm_id, "x" * 129, "hi"), 422, "invalid_request")
        error(dm_send(client, "sender", dm_id, "", "hi"), 422, "invalid_request")
        error(
            post(
                client,
                "sender",
                f"/dms/{dm_id}/messages",
                json={
                    "client_message_id": "k",
                    "text": "hi",
                    "mention_member_id": target,
                },
            ),
            422,
            "invalid_request",
        )
        error(
            get(client, "sender", f"/dms/{dm_id}/messages?limit=101"),
            422,
            "invalid_request",
        )
        error(
            get(client, "sender", f"/dms/{dm_id}/messages?after=-1"),
            422,
            "invalid_request",
        )
        error(get(client, "sender", "/dms/not-a-uuid/messages"), 422, "invalid_request")
        error(
            client.post(
                f"/v1/dms/{dm_id}/messages",
                json={"client_message_id": "k", "text": "hi"},
            ),
            401,
            "unauthorized",
        )

        with ThreadPoolExecutor(max_workers=12) as workers:
            responses = list(
                workers.map(
                    lambda _: dm_send(client, "sender", dm_id, "same", "retry me"),
                    range(12),
                )
            )
        assert all(response.status_code == 200 for response in responses)
        message = responses[0].json()
        assert all(response.json() == message for response in responses)
        assert set(message) == {
            "message_id",
            "dm_id",
            "sender_member_id",
            "client_message_id",
            "text",
            "created_at",
        }
        assert message["dm_id"] == dm_id
        assert message["sender_member_id"] == sender
        assert message["client_message_id"] == "same"
        assert message["text"] == "retry me"
        assert_utc_timestamp(message["created_at"])

        error(dm_send(client, "sender", dm_id, "same", "changed"), 409, "conflict")
        reply = dm_send(client, "target", dm_id, "same", "retry me")
        assert reply.status_code == 200
        assert reply.json()["message_id"] > message["message_id"]

        other_dm = open_dm(client, "sender", reader).json()["dm_id"]
        assert (
            dm_send(client, "sender", other_dm, "same", "retry me").status_code == 200
        )

        room_path = room(client)
        confirm(client, "sender", room_path)
        assert send(client, room_path, "same", text="retry me").status_code == 200
        another_room = room(client)
        confirm(client, "sender", another_room)
        error(send(client, another_room, "same", text="retry me"), 409, "conflict")

        target_events = dm_events(client, "target", kinds="dm")
        assert [event["message"] for event in target_events] == [message]
        sender_events = dm_events(client, "sender", kinds="dm")
        assert [event["message"] for event in sender_events] == [reply.json()]

        with closing(sqlite3.connect(db_path)) as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE dm_id IS NOT NULL"
            ).fetchone()[0]
            deliveries = connection.execute(
                "SELECT COUNT(*) FROM inbox_events WHERE kind = 'dm' AND message_id = ?",
                (message["message_id"],),
            ).fetchone()[0]
        assert stored == 3
        assert deliveries == 1


def test_dm_history_pages_stably_for_both_participants(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        sender = member(client, "sender")
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        first = dm_send(client, "sender", dm_id, "one", "first private").json()
        second = dm_send(client, "target", dm_id, "two", "second private").json()
        assert first["message_id"] < second["message_id"]
        assert first["dm_id"] == second["dm_id"] == dm_id
        assert first["sender_member_id"] == sender
        assert second["sender_member_id"] == target

        assert dm_read(client, "sender", dm_id).json() == {"messages": [first, second]}
        assert dm_read(client, "target", dm_id).json() == {"messages": [first, second]}
        assert dm_read(
            client, "sender", dm_id, f"?after={first['message_id']}"
        ).json() == {"messages": [second]}
        assert dm_read(client, "target", dm_id, "?limit=1").json() == {
            "messages": [first]
        }
        assert dm_read(
            client, "sender", dm_id, f"?after={second['message_id']}"
        ).json() == {"messages": []}


def test_dm_privacy_is_indistinguishable_and_leaks_no_existence(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client)
        sender = member(client, "sender")
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        dm_send(client, "sender", dm_id, "dm-1", "a private line").json()
        event_id = dm_events(client, "target", kinds="dm")[0]["event_id"]

        unknown_dm = str(uuid4())
        blocked_read = get(client, "reader", f"/dms/{dm_id}/messages")
        missing_read = get(client, "reader", f"/dms/{unknown_dm}/messages")
        error(blocked_read, 404, "not_found")
        error(missing_read, 404, "not_found")
        assert blocked_read.json() == missing_read.json()
        blocked_send = post(
            client,
            "reader",
            f"/dms/{dm_id}/messages",
            json={"client_message_id": "peek", "text": "let me in"},
        )
        missing_send = post(
            client,
            "reader",
            f"/dms/{unknown_dm}/messages",
            json={"client_message_id": "peek", "text": "let me in"},
        )
        error(blocked_send, 404, "not_found")
        error(missing_send, 404, "not_found")
        assert blocked_send.json() == missing_send.json()

        error(post(client, "reader", f"/inbox/{event_id}/ack"), 404, "not_found")
        error(post(client, "sender", f"/inbox/{event_id}/ack"), 404, "not_found")
        assert post(client, "target", f"/inbox/{event_id}/ack").json() == {
            "event_id": event_id
        }
        assert post(client, "target", f"/inbox/{event_id}/ack").json() == {
            "event_id": event_id
        }
        assert get(client, "target", "/inbox?kinds=dm").json() == {"events": []}

        assert get(client, "reader", "/inbox").json() == {"events": []}
        assert dm_events(client, "reader") == []
        profiles = get(client, "reader", "/members").json()["members"]
        assert {record["member_id"] for record in profiles} >= {sender, target}


def test_authorized_room_search_never_returns_dm_phrases(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        room_path = room(client)
        join(client, "target", room_path)
        confirm(client, "sender", room_path)
        confirm(client, "target", room_path)
        room_message = send(
            client, room_path, "room-1", text="the room keeps its own words"
        ).json()
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        dm_message = dm_send(
            client, "sender", dm_id, "dm-search", "zephyr-veil passphrase 7f3d"
        ).json()
        assert dm_message["text"] == "zephyr-veil passphrase 7f3d"

        # Both participants are authorized to search the room, yet the unique DM
        # phrase stays absent from every result; the room's own text still matches.
        for label in ("sender", "target"):
            assert find(client, label, room_path, "zephyr-veil") == []
            assert find(client, label, room_path, "passphrase 7f3d") == []
            assert find(client, label, room_path, "ZEPHYR-VEIL PASSPHRASE 7F3D") == []
            assert [
                match["message_id"]
                for match in find(client, label, room_path, "its own words")
            ] == [room_message["message_id"]]

        # The phrase remains readable only through the DM's own history.
        assert dm_read(client, "target", dm_id).json() == {"messages": [dm_message]}
        assert dm_read(client, "sender", dm_id).json() == {"messages": [dm_message]}


def test_dm_works_without_any_board_flow(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        room_path = room(client)
        join(client, "target", room_path)
        error(send(client, room_path, "blocked"), 409, "board_ack_required")
        error(get(client, "target", f"{room_path}/messages"), 409, "board_ack_required")

        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        sent = dm_send(client, "sender", dm_id, "dm-1", "no board needed")
        assert sent.status_code == 200
        assert dm_read(client, "target", dm_id).json() == {"messages": [sent.json()]}
        assert dm_read(client, "sender", dm_id).status_code == 200

        unjoined_room = room(client)
        error(get(client, "target", f"{unjoined_room}/messages"), 404, "not_found")
        error(
            post(
                client,
                "target",
                f"{unjoined_room}/messages",
                json={"client_message_id": "peek", "text": "let me read"},
            ),
            404,
            "not_found",
        )


def test_inbox_kinds_validate_subsets_and_keep_legacy_paging(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        enroll_team(client, labels=("sender", "target"))
        target = member(client, "target")
        room_path = room(client)
        join(client, "target", room_path)
        confirm(client, "sender", room_path)
        confirm(client, "target", room_path)
        plain = send(client, room_path, "plain", text="plain room note").json()
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        private = dm_send(client, "sender", dm_id, "dm", "private line").json()
        at = send(client, room_path, "at", text="at you", target=target).json()

        events = dm_events(client, "target")
        assert [event["kind"] for event in events] == ["message", "dm", "mention"]
        assert events[0]["event_id"] < events[1]["event_id"] < events[2]["event_id"]
        assert events[0]["message"] == plain
        assert events[2]["message"] == at
        assert events[1] == {
            "event_id": events[1]["event_id"],
            "kind": "dm",
            "message": private,
        }
        assert dm_events(client, "sender") == []

        assert [event["kind"] for event in dm_events(client, "target", kinds=None)] == [
            "message",
            "mention",
        ]
        first_page = get(client, "target", "/inbox?limit=1").json()["events"]
        assert [event["kind"] for event in first_page] == ["message"]
        after_first = first_page[0]["event_id"]
        second_page = get(
            client, "target", f"/inbox?after={after_first}&limit=1"
        ).json()["events"]
        assert [event["kind"] for event in second_page] == ["mention"]
        assert second_page[0]["event_id"] == events[2]["event_id"]
        dm_page = get(
            client, "target", f"/inbox?after={after_first}&kinds=dm&limit=1"
        ).json()["events"]
        assert [event["kind"] for event in dm_page] == ["dm"]
        assert dm_page[0]["event_id"] == events[1]["event_id"]

        assert [
            event["kind"] for event in dm_events(client, "target", kinds="message")
        ] == ["message"]
        assert [
            event["kind"] for event in dm_events(client, "target", kinds="mention")
        ] == ["mention"]
        assert [event["kind"] for event in dm_events(client, "target", kinds="dm")] == [
            "dm"
        ]
        assert [
            event["kind"] for event in dm_events(client, "target", kinds="dm,message")
        ] == [
            "message",
            "dm",
        ]

        assert get(client, "sender", f"{room_path}/messages").json() == {
            "messages": [plain, at]
        }

        for kinds in (
            "",
            "dmx",
            "message,",
            ",dm",
            "message,message",
            " message",
            "message, dm",
            "DM",
            "message;mention",
            "message,mention,dm,member_joined",
        ):
            error(
                get(client, "target", f"/inbox?kinds={quote(kinds)}"),
                422,
                "invalid_request",
            )

        error(client.get("/v1/inbox?kinds=dm"), 401, "unauthorized")
        error(client.get(f"/v1/dms/{dm_id}/messages"), 401, "unauthorized")
        error(
            client.post(f"/v1/inbox/{events[0]['event_id']}/ack"), 401, "unauthorized"
        )


def test_disabled_peer_blocks_new_sends_and_open_but_keeps_history(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    enter_password(db_path, "create", "bob", PASSWORD)
    with api(db_path) as client:
        target = bind(client, login_token(client, "bob", PASSWORD), "target")
        assert target.status_code == 200, target.text
        target_id = target.json()["member_id"]
        assert enroll(client, "sender").status_code == 200

        dm = open_dm(client, "sender", target_id)
        assert dm.status_code == 200, dm.text
        dm_id = dm.json()["dm_id"]
        sent = dm_send(client, "sender", dm_id, "kept", "kept private").json()
        assert dm_read(client, "target", dm_id).json() == {"messages": [sent]}

        admin(db_path, "disable", "bob")
        error(get(client, "target", "/members/me"), 401, "unauthorized")
        error(get(client, "target", f"/dms/{dm_id}/messages"), 401, "unauthorized")
        error(open_dm(client, "sender", target_id), 422, "invalid_peer")
        error(open_dm(client, "sender", str(uuid4())), 422, "invalid_peer")
        error(
            dm_send(client, "sender", dm_id, "blocked", "no delivery"), 404, "not_found"
        )
        assert dm_send(client, "sender", dm_id, "kept", "kept private").json() == sent
        assert dm_read(client, "sender", dm_id).json() == {"messages": [sent]}


def test_dm_sse_notifies_recipient_only_after_commit(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with live_server(db_path) as (client, process):
        enroll_team(client, labels=("sender", "target"))
        sender = member(client, "sender")
        target = member(client, "target")
        dm_id = open_dm(client, "sender", target).json()["dm_id"]
        lines = Queue()

        def read_stream():
            try:
                with client.stream(
                    "GET",
                    "/v1/events",
                    headers={
                        **auth(credential("target")),
                        "Accept": "text/event-stream",
                    },
                ) as response:
                    assert response.status_code == 200
                    for line in response.iter_lines():
                        if line:
                            lines.put(line)
            except (AssertionError, httpx.HTTPError) as exc:
                lines.put(exc)
            finally:
                lines.put(None)

        reader = Thread(target=read_stream, daemon=True)
        reader.start()
        assert lines.get(timeout=3).startswith(":")
        assert dm_events(client, "target") == []
        open_dm(client, "target", sender)
        with pytest.raises(Empty):
            lines.get(timeout=0.15)

        sent = dm_send(client, "sender", dm_id, "dm-1", "wake up quietly")
        assert sent.status_code == 200
        assert lines.get(timeout=3) == "event: inbox_changed"
        assert lines.get(timeout=3) == "data: {}"
        events = get(client, "target", "/inbox?kinds=dm").json()["events"]
        assert events[0]["kind"] == "dm"
        assert events[0]["message"]["text"] == "wake up quietly"

        assert (
            dm_send(client, "sender", dm_id, "dm-1", "wake up quietly").json()
            == events[0]["message"]
        )
        with pytest.raises(Empty):
            lines.get(timeout=0.15)

        process.terminate()
        assert lines.get(timeout=3) is None
        reader.join(timeout=3)
        assert process.wait(timeout=3) in (0, -15)
        stderr = process.stderr.read()
        assert "ERROR:" not in stderr
        assert "wake up quietly" not in stderr


_V3_DATABASE = """
    PRAGMA user_version = 3;
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
    CREATE TABLE messages (
        message_id INTEGER PRIMARY KEY,
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        sender_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_message_id TEXT NOT NULL,
        text TEXT NOT NULL,
        mention_member_id TEXT REFERENCES members(member_id),
        created_at TEXT,
        UNIQUE (sender_member_id, client_message_id)
    );
    CREATE INDEX room_history ON messages (room_id, message_id);
    CREATE TABLE inbox_events (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER NOT NULL REFERENCES messages(message_id),
        kind TEXT NOT NULL CHECK (kind IN ('message', 'mention')),
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


def test_v3_upgrade_keeps_room_state_and_shares_allocators_with_dm(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    late_member = "6f6d3b93-2c2e-4d5f-9a54-6d0f4f65ab21"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V3_DATABASE)
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
            "INSERT INTO room_members VALUES (?, ?, 41, 1, 1)", (room_id, late_member)
        )
        connection.execute(
            "INSERT INTO messages VALUES (41, ?, ?, 'old-key', 'Old text', NULL, NULL)",
            (room_id, old_member),
        )
        connection.execute(
            "INSERT INTO messages VALUES (42, ?, ?, 'old-2', 'Second old', NULL, NULL)",
            (room_id, old_member),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 42, 'message', 0)", (late_member,)
        )
        connection.execute(
            "INSERT INTO id_counters VALUES ('message_id', 42), ('event_id', 17)"
        )

    room_path = f"/rooms/{room_id}"
    with api(db_path) as client:
        history = get(client, "old", f"{room_path}/messages").json()["messages"]
        assert [message["message_id"] for message in history] == [41, 42]
        assert [message["created_at"] for message in history] == [None, None]
        resent = post(
            client,
            "old",
            f"{room_path}/messages",
            json={"client_message_id": "old-key", "text": "Old text"},
        )
        assert resent.status_code == 200, resent.text
        assert resent.json()["message_id"] == 41
        room_event = get(client, "late", "/inbox").json()["events"]
        assert [event["event_id"] for event in room_event] == [17]

        dm = open_dm(client, "old", late_member)
        assert dm.status_code == 200, dm.text
        assert dm.json() == {"dm_id": dm.json()["dm_id"], "peer_member_id": late_member}
        dm_id = dm.json()["dm_id"]
        private = dm_send(
            client, "old", dm_id, "old-key", "same key, other family"
        ).json()
        assert private["message_id"] == 43
        assert private["client_message_id"] == "old-key"
        assert_utc_timestamp(private["created_at"])

        dm_events_after = get(client, "late", "/inbox?kinds=dm").json()["events"]
        assert [event["event_id"] for event in dm_events_after] == [18]
        assert dm_events_after[0]["message"] == private
        assert get(client, "late", "/inbox").json()["events"] == room_event

        room_next = post(
            client,
            "old",
            f"{room_path}/messages",
            json={"client_message_id": "old-3", "text": "Fresh room text"},
        )
        assert room_next.status_code == 200, room_next.text
        assert room_next.json()["message_id"] == 44
        assert_utc_timestamp(room_next.json()["created_at"])

        assert post(client, "late", "/inbox/17/ack").json() == {"event_id": 17}
        assert post(client, "late", "/inbox/18/ack").json() == {"event_id": 18}
        room_new_events = get(client, "late", "/inbox").json()["events"]
        assert [event["event_id"] for event in room_new_events] == [19]
        assert room_new_events[0]["message"] == room_next.json()
        assert post(client, "late", "/inbox/19/ack").json() == {"event_id": 19}
        assert dm_events(client, "late") == []
        assert get(client, "late", f"{room_path}/messages").json() == {
            "messages": [*history, room_next.json()]
        }
