import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
from stat import S_IMODE
from threading import Thread
from time import monotonic, sleep
from uuid import UUID

import httpx
import pytest


def server_command(db_path):
    return [sys.executable, "-m", "teamup_server.main", "--db", str(db_path)]


def server_env():
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "TEAMUP_ENROLLMENT_KEYS": json.dumps(KEYS),
    }


@contextmanager
def live_server(db_path, *, legacy=True):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    env = server_env()
    if not legacy:
        env.pop("TEAMUP_ENROLLMENT_KEYS")
    with (
        subprocess.Popen(
            [*server_command(db_path), "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        ) as process,
        httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client,
    ):
        try:
            deadline = monotonic() + 5
            while monotonic() < deadline and process.poll() is None:
                try:
                    response = client.get("/v1/members/me")
                except httpx.ConnectError:
                    sleep(0.02)
                    continue
                assert response.status_code == 401
                break
            else:
                if process.poll() is not None:
                    pytest.fail(f"Server exited on startup: {process.stderr.read()}")
                pytest.fail("Server did not start within 5 seconds")
            yield client, process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def credential(label):
    return urlsafe_b64encode(sha256(label.encode()).digest()).decode().rstrip("=")


KEYS = {"alice": credential("enroll-alice"), "bob": credential("enroll-bob")}


@contextmanager
def api(db_path):
    with live_server(db_path) as (client, _):
        yield client


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def enroll(client, label, *, owner="alice", session=None, role=None):
    return client.post(
        "/v1/members",
        headers=auth(KEYS[owner]),
        json={
            "harness_instance_id": "shared-harness",
            "session_key": label if session is None else session,
            "role_description": label if role is None else role,
            "member_token": credential(label),
        },
    )


@pytest.fixture
def team(tmp_path):
    with api(tmp_path / "team.sqlite3") as client:
        for label, owner in (("sender", "alice"), ("target", "bob"), ("reader", "bob")):
            assert enroll(client, label, owner=owner).status_code == 200
        yield client


def member(client, label):
    response = client.get("/v1/members/me", headers=auth(credential(label)))
    assert response.status_code == 200
    return response.json()["member_id"]


def post(client, label, path, **kwargs):
    return client.post(f"/v1{path}", headers=auth(credential(label)), **kwargs)


def get(client, label, path):
    return client.get(f"/v1{path}", headers=auth(credential(label)))


def room(client, **board):
    response = post(client, "sender", "/rooms", json={"name": "  Build  ", **board})
    assert response.status_code == 200
    assert response.json()["name"] == "  Build  "
    return f"/rooms/{response.json()['room_id']}"


def join(client, label, room_path):
    response = post(client, label, f"{room_path}/join")
    assert response.status_code == 200


def confirm(client, label, room_path):
    assert get(client, label, f"{room_path}/board").status_code == 200
    response = post(client, label, f"{room_path}/board/ack", json={"version": 1})
    assert response.status_code == 200
    assert response.json() == {"version": 1}


def send(client, room_path, key, text="hello", target=None):
    return post(
        client,
        "sender",
        f"{room_path}/messages",
        json={"client_message_id": key, "text": text, "mention_member_id": target},
    )


def error(response, status, code):
    assert response.status_code == status
    assert response.json() == {
        "error": {"code": code, "message": response.json()["error"]["message"]}
    }
    assert isinstance(response.json()["error"]["message"], str)


def test_enrollment_retries_concurrently_and_retains_original_profile(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client, ThreadPoolExecutor(max_workers=6) as workers:
        responses = list(
            workers.map(lambda _: enroll(client, "one", role="Straße"), range(12))
        )
        assert all(response.status_code == 200 for response in responses)
        original = responses[0].json()
        assert all(response.json() == original for response in responses)
        UUID(original["member_id"])
    with api(db_path) as client:
        assert enroll(client, "one", role="different").json() == original
        error(enroll(client, "different-token", session="one"), 409, "conflict")
        error(enroll(client, "one", session="different-session"), 409, "conflict")
        assert (
            enroll(client, "another-session").json()["member_id"]
            != original["member_id"]
        )
        assert (
            enroll(client, "another-owner", owner="bob", session="one").status_code
            == 200
        )
        assert get(client, "one", "/members?q=STRASSE").json() == {
            "members": [original]
        }
        assert member(client, "one") == original["member_id"]


def test_credentials_errors_and_undeclared_fields(team):
    error(team.get("/v1/members"), 401, "unauthorized")
    response = team.get("/v1/members", headers=auth(KEYS["alice"]))
    error(response, 401, "unauthorized")
    assert KEYS["alice"] not in response.text
    response = team.post(
        "/v1/members",
        headers=auth(KEYS["alice"]),
        json={
            "harness_instance_id": "h",
            "session_key": "s",
            "role_description": "r",
            "member_token": "secret-value",
        },
    )
    error(response, 422, "invalid_request")
    assert "secret-value" not in response.text
    error(
        post(team, "sender", "/rooms", json={"name": "valid", "owner_id": "bob"}),
        422,
        "invalid_request",
    )
    error(post(team, "sender", "/rooms", json={"name": "\n "}), 422, "invalid_request")
    error(
        post(team, "sender", "/rooms", json={"name": "x", "rules": ""}),
        422,
        "invalid_request",
    )
    error(get(team, "sender", "/rooms/not-a-uuid/board"), 422, "invalid_request")
    error(get(team, "sender", "/inbox?limit=101"), 422, "invalid_request")
    error(get(team, "sender", "/inbox?after=-1"), 422, "invalid_request")
    error(get(team, "sender", "/events"), 422, "invalid_request")
    error(
        team.get("/v1/events", headers={**auth("bad"), "Accept": "text/event-stream"}),
        401,
        "unauthorized",
    )


def test_board_ack_requires_the_same_member_to_read_the_current_version(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        for label in ("sender", "target"):
            assert enroll(client, label).status_code == 200
        room_path = room(client)
        join(client, "target", room_path)
        ack_path = f"{room_path}/board/ack"
        error(
            post(client, "sender", ack_path, json={"version": 1}),
            409,
            "board_read_required",
        )
        board = get(client, "target", f"{room_path}/board").json()
        assert board["version"] == 1
        error(
            post(client, "sender", ack_path, json={"version": 1}),
            409,
            "board_read_required",
        )

    with api(db_path) as client:
        for _ in range(2):
            response = post(
                client, "target", ack_path, json={"version": board["version"]}
            )
            assert response.status_code == 200
            assert response.json() == {"version": board["version"]}


def test_new_database_is_private_to_its_owner(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path):
        assert S_IMODE(db_path.stat().st_mode) == 0o600


def test_room_history_starts_at_first_join_and_requires_board_ack(team):
    room_path = room(
        team, rules="  Keep secrets private. ", protocol=" Discuss openly. "
    )
    error(get(team, "target", f"{room_path}/board"), 404, "not_found")
    error(send(team, room_path, "before"), 409, "board_ack_required")
    assert get(team, "sender", f"{room_path}/board").json() == {
        "room_id": room_path.rsplit("/", 1)[-1],
        "version": 1,
        "rules": "  Keep secrets private. ",
        "protocol": " Discuss openly. ",
    }
    confirm(team, "sender", room_path)
    before = send(team, room_path, "before").json()
    join(team, "target", room_path)
    error(get(team, "target", f"{room_path}/messages"), 409, "board_ack_required")
    error(
        post(team, "target", f"{room_path}/board/ack", json={"version": 2}),
        409,
        "conflict",
    )
    error(
        post(team, "target", f"{room_path}/board/ack", json={"version": True}),
        422,
        "invalid_request",
    )
    confirm(team, "target", room_path)
    after = send(team, room_path, "after", text="  Preserve line\n").json()
    assert after["text"] == "  Preserve line\n"
    join(team, "target", room_path)
    assert get(team, "target", f"{room_path}/messages").json() == {"messages": [after]}
    assert get(team, "sender", f"{room_path}/messages?limit=1").json() == {
        "messages": [before]
    }
    assert get(
        team, "sender", f"{room_path}/messages?after={before['message_id']}"
    ).json() == {"messages": [after]}


def test_message_retry_allocates_one_message_and_one_event_per_recipient(team):
    room_path = room(team)
    for label in ("target", "reader"):
        join(team, label, room_path)
    for label in ("sender", "target", "reader"):
        confirm(team, label, room_path)
    target = member(team, "target")
    with ThreadPoolExecutor(max_workers=6) as workers:
        responses = list(
            workers.map(
                lambda _: send(team, room_path, "same", target=target), range(12)
            )
        )
    assert all(response.status_code == 200 for response in responses)
    message = responses[0].json()
    assert all(response.json() == message for response in responses)
    assert message["sender_member_id"] == member(team, "sender")
    assert get(team, "sender", f"{room_path}/messages").json() == {
        "messages": [message]
    }
    assert get(team, "sender", "/inbox").json() == {"events": []}
    target_events = get(team, "target", "/inbox").json()["events"]
    reader_events = get(team, "reader", "/inbox").json()["events"]
    assert target_events == [
        {
            "kind": "mention",
            "message": message,
            "event_id": target_events[0]["event_id"],
        }
    ]
    assert reader_events == [
        {
            "kind": "message",
            "message": message,
            "event_id": reader_events[0]["event_id"],
        }
    ]
    error(send(team, room_path, "same", text="changed", target=target), 409, "conflict")
    error(send(team, room_path, "same"), 409, "conflict")
    another_room = room(team)
    confirm(team, "sender", another_room)
    error(send(team, another_room, "same"), 409, "conflict")
    error(
        send(team, room_path, "new", target=member(team, "sender")),
        422,
        "invalid_mention",
    )
    response = send(
        team, room_path, "new", target="00000000-0000-0000-0000-000000000001"
    )
    error(response, 422, "invalid_mention")


def test_inbox_is_durable_and_filters_unconfirmed_rooms_before_paging(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        for label in ("sender", "target", "reader"):
            assert enroll(client, label).status_code == 200
        pending = room(client)
        ready = room(client)
        for room_path in (pending, ready):
            confirm(client, "sender", room_path)
            join(client, "target", room_path)
        confirm(client, "target", ready)
        blocked = send(client, pending, "blocked").json()
        visible = send(client, ready, "visible").json()
        events = get(client, "target", "/inbox?limit=1").json()["events"]
        assert [event["message"] for event in events] == [visible]
        visible_event_id = events[0]["event_id"]
        assert get(client, "target", f"/inbox?after={visible_event_id}").json() == {
            "events": []
        }
        error(
            post(client, "reader", f"/inbox/{visible_event_id}/ack"), 404, "not_found"
        )
    with api(db_path) as client:
        assert get(client, "target", "/inbox").json()["events"] == events
        for _ in range(2):
            assert post(client, "target", f"/inbox/{visible_event_id}/ack").json() == {
                "event_id": visible_event_id
            }
        assert get(client, "target", "/inbox").json() == {"events": []}
        confirm(client, "target", pending)
        revealed = get(client, "target", "/inbox").json()["events"]
        assert len(revealed) == 1
        assert revealed[0]["event_id"] < visible_event_id
        assert revealed[0]["message"] == blocked


def test_query_rejects_invalid_utf8(team):
    error(get(team, "sender", "/members?q=%FF"), 422, "invalid_request")


def test_json_rejects_non_utf8(team):
    response = team.post(
        "/v1/rooms",
        content='{"name":"UTF-16 room"}'.encode("utf-16-le"),
        headers={
            "Authorization": f"Bearer {credential('sender')}",
            "content-type": "application/json",
        },
    )
    error(response, 422, "invalid_request")


def test_sse_signals_committed_visible_changes_and_closes_on_shutdown(tmp_path):
    with live_server(tmp_path / "team.sqlite3") as (client, process):
        for label in ("sender", "target"):
            assert enroll(client, label).status_code == 200
        room_path = room(client)
        confirm(client, "sender", room_path)
        join(client, "target", room_path)
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
                    assert response.headers["content-type"].startswith(
                        "text/event-stream"
                    )
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
        first = send(client, room_path, "pending")
        assert first.status_code == 200
        assert get(client, "target", "/inbox").json() == {"events": []}
        with pytest.raises(Empty):
            lines.get(timeout=0.15)
        confirm(client, "target", room_path)
        assert lines.get(timeout=3) == "event: inbox_changed"
        assert lines.get(timeout=3) == "data: {}"
        assert (
            get(client, "target", "/inbox").json()["events"][0]["message"]
            == first.json()
        )
        second = send(client, room_path, "visible")
        assert second.status_code == 200
        assert lines.get(timeout=3) == "event: inbox_changed"
        assert lines.get(timeout=3) == "data: {}"
        assert (
            get(client, "target", "/inbox").json()["events"][-1]["message"]
            == second.json()
        )
        assert send(client, room_path, "visible").json() == second.json()
        with pytest.raises(Empty):
            lines.get(timeout=0.15)
        process.terminate()
        assert lines.get(timeout=3) is None
        reader.join(timeout=3)
        assert process.wait(timeout=3) in (0, -15)
        assert "ERROR:" not in process.stderr.read()


def test_cli_rejects_cleartext_nonloopback_listener(tmp_path):
    result = subprocess.run(
        [*server_command(tmp_path / "team.sqlite3"), "--host", "0.0.0.0"],
        env=server_env(),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "TLS" in result.stderr


_RFC3339_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


def assert_utc_timestamp(value):
    assert _RFC3339_UTC.fullmatch(value) is not None, value
    assert datetime.fromisoformat(value).utcoffset() == timedelta(0)


def test_room_creation_saves_the_requested_history_policy(team):
    default = post(team, "sender", "/rooms", json={"name": "Default"})
    assert default.status_code == 200
    body = default.json()
    UUID(body["room_id"])
    assert body == {
        "room_id": body["room_id"],
        "name": "Default",
        "history_policy": "since_joining",
    }
    retained = post(
        team, "sender", "/rooms", json={"name": "Kept", "history_policy": "retained"}
    )
    assert retained.status_code == 200
    body = retained.json()
    UUID(body["room_id"])
    assert body == {
        "room_id": body["room_id"],
        "name": "Kept",
        "history_policy": "retained",
    }
    for policy in ("weekly", None, 1, "RETAINED"):
        error(
            post(
                team,
                "sender",
                "/rooms",
                json={"name": "Bad", "history_policy": policy},
            ),
            422,
            "invalid_request",
        )


def test_retained_room_reads_pre_join_history_after_board_ack(team):
    room_path = room(team, history_policy="retained")
    confirm(team, "sender", room_path)
    early = send(team, room_path, "early").json()
    join(team, "target", room_path)
    error(get(team, "target", f"{room_path}/messages"), 409, "board_ack_required")
    confirm(team, "target", room_path)
    assert get(team, "target", f"{room_path}/messages").json() == {"messages": [early]}
    assert (
        get(team, "target", f"{room_path}/messages?after={early['message_id']}")
        .json()
        == {"messages": []}
    )
    later = send(team, room_path, "later").json()
    assert get(team, "target", f"{room_path}/messages").json() == {
        "messages": [early, later]
    }


def test_created_at_is_utc_rfc3339_and_shared_across_send_retry_history_inbox(team):
    room_path = room(team)
    join(team, "target", room_path)
    for label in ("sender", "target"):
        confirm(team, label, room_path)
    target = member(team, "target")
    sent = send(team, room_path, "once", target=target).json()
    assert_utc_timestamp(sent["created_at"])
    assert get(team, "sender", f"{room_path}/messages").json() == {"messages": [sent]}
    assert (
        get(team, "sender", f"{room_path}/messages?after={sent['message_id']}")
        .json()
        == {"messages": []}
    )
    assert send(team, room_path, "once", target=target).json() == sent
    target_events = get(team, "target", "/inbox").json()["events"]
    assert target_events == [
        {
            "kind": "mention",
            "message": sent,
            "event_id": target_events[0]["event_id"],
        }
    ]


_V2_DATABASE = """
    PRAGMA user_version = 2;
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
        protocol TEXT NOT NULL
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
"""


def test_v2_upgrade_defaults_rooms_to_since_joining_and_keeps_ids_durable(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    late_member = "6f6d3b93-2c2e-4d5f-9a54-6d0f4f65ab21"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V2_DATABASE)
        connection.execute(
            "INSERT INTO members VALUES (?, 'legacy-owner', 'old-harness', "
            "'old-session', 'Old role', ?)",
            (old_member, sha256(sha256(b"old").digest()).digest()),
        )
        connection.execute(
            "INSERT INTO members VALUES (?, 'legacy-owner', 'old-harness', "
            "'late-session', 'Late role', ?)",
            (late_member, sha256(sha256(b"late").digest()).digest()),
        )
        connection.execute(
            "INSERT INTO rooms VALUES (?, 'Legacy room', 1, 'Rules', 'Protocol')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, old_member)
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 41, 1, 1)", (room_id, late_member)
        )
        connection.execute(
            "INSERT INTO messages VALUES (41, ?, ?, 'old-1', 'First old', NULL)",
            (room_id, old_member),
        )
        connection.execute(
            "INSERT INTO messages VALUES (42, ?, ?, 'old-2', 'Second old', NULL)",
            (room_id, old_member),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 42, 'message', 0)",
            (late_member,),
        )

    room_path = f"/rooms/{room_id}"
    with api(db_path) as client:
        old_history = get(client, "old", f"{room_path}/messages").json()["messages"]
        assert [message["message_id"] for message in old_history] == [41, 42]
        assert [message["created_at"] for message in old_history] == [None, None]
        late_history = get(client, "late", f"{room_path}/messages").json()["messages"]
        assert [message["message_id"] for message in late_history] == [42]
        sent = post(
            client,
            "old",
            f"{room_path}/messages",
            json={"client_message_id": "after-upgrade", "text": "Fresh start"},
        )
        assert sent.status_code == 200, sent.text
        fresh = sent.json()
        assert fresh["message_id"] == 43
        assert_utc_timestamp(fresh["created_at"])
        late_history = get(client, "late", f"{room_path}/messages").json()["messages"]
        assert [message["message_id"] for message in late_history] == [42, 43]
        events = get(client, "late", "/inbox").json()["events"]
        assert [event["event_id"] for event in events] == [17, 18]
        assert [event["message"]["created_at"] for event in events] == [
            None,
            fresh["created_at"],
        ]

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DELETE FROM inbox_events")
        connection.execute("DELETE FROM messages")

    with api(db_path) as client:
        recreated = post(
            client,
            "old",
            f"{room_path}/messages",
            json={"client_message_id": "after-upgrade", "text": "Fresh start"},
        )
        assert recreated.status_code == 200, recreated.text
        assert recreated.json()["message_id"] == 44
        assert_utc_timestamp(recreated.json()["created_at"])
        events = get(client, "late", "/inbox").json()["events"]
        assert [event["event_id"] for event in events] == [19]
        assert [event["message"] for event in events] == [recreated.json()]
        assert get(client, "old", f"{room_path}/messages").json()["messages"] == [
            recreated.json()
        ]
