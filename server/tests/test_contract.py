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
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import pytest
from teamup_server.collaboration import Collaboration


def server_command(db_path, *extra):
    return [sys.executable, "-m", "teamup_server.main", "--db", str(db_path), *extra]


def server_env():
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "TEAMUP_ENROLLMENT_KEYS": json.dumps(KEYS),
    }


@contextmanager
def live_server(db_path, *, legacy=True, extra=()):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    env = server_env()
    if not legacy:
        env.pop("TEAMUP_ENROLLMENT_KEYS")
    with (
        subprocess.Popen(
            [*server_command(db_path, "--port", str(port), *extra)],
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


def wait_for(condition, timeout, *, step=0.2):
    """Poll ``condition`` until it holds or a ``timeout``-second deadline passes."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return
        sleep(step)
    pytest.fail(f"Condition not met within {timeout} seconds")


class RacingCollaboration(Collaboration):
    """A test double that commits one change between the read and write passes.

    Retention selects its bounded candidate batch in a read transaction and
    deletes in a separate write transaction. The double runs ``interfere``
    once, on the write transaction that follows a read on the same call, so
    a regression can commit a competing change (a concurrent expiry's rows)
    between the two transactions and observe how the write pass rechecks.
    """

    def __init__(self, db_path, keys, interfere):
        self._interfere = interfere
        self._read_open = False
        super().__init__(db_path, keys)

    @contextmanager
    def _transaction(self, *, write=False):
        if write and self._read_open:
            self._read_open = False
            self._interfere()
        elif not write:
            self._read_open = True
        with super()._transaction(write=write) as connection:
            yield connection


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


def find(client, label, room_path, q, **params):
    response = get(client, label, f"{room_path}/search?{urlencode({'q': q, **params})}")
    assert response.status_code == 200, response.text
    return response.json()["matches"]


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


def test_list_rooms_returns_membership_scoped_room_metadata(team):
    error(team.get("/v1/rooms"), 401, "unauthorized")

    def summary(room_id, name, **fields):
        return {
            "room_id": room_id,
            "name": name,
            "acked_board_version": None,
            "current_board_version": 1,
            "latest_message_id": None,
            **fields,
        }

    rooms = {
        name: post(team, "sender", "/rooms", json={"name": name}).json()["room_id"]
        for name in ("alpha", "beta", "gamma")
    }
    assert get(team, "reader", "/rooms").json() == {"rooms": []}
    assert get(team, "target", "/rooms").json() == {"rooms": []}
    assert get(team, "sender", "/rooms").json() == {
        "rooms": [
            summary(room_id, name)
            for room_id, name in sorted(
                (room_id, name) for name, room_id in rooms.items()
            )
        ]
    }
    join(team, "target", f"/rooms/{rooms['alpha']}")
    confirm(team, "sender", f"/rooms/{rooms['alpha']}")
    message = send(team, f"/rooms/{rooms['alpha']}", "hello").json()
    assert get(team, "reader", "/rooms").json() == {"rooms": []}
    assert get(team, "target", "/rooms").json() == {
        "rooms": [
            summary(rooms["alpha"], "alpha", latest_message_id=message["message_id"])
        ]
    }
    confirm(team, "target", f"/rooms/{rooms['alpha']}")
    assert get(team, "target", "/rooms").json() == {
        "rooms": [
            summary(
                rooms["alpha"],
                "alpha",
                acked_board_version=1,
                latest_message_id=message["message_id"],
            )
        ]
    }
    sender_rooms = get(team, "sender", "/rooms").json()["rooms"]
    assert [room["room_id"] for room in sender_rooms] == sorted(rooms.values())
    by_id = {room["room_id"]: room for room in sender_rooms}
    assert by_id[rooms["alpha"]] == summary(
        rooms["alpha"],
        "alpha",
        acked_board_version=1,
        latest_message_id=message["message_id"],
    )
    assert by_id[rooms["beta"]] == summary(rooms["beta"], "beta")


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
    assert get(
        team, "target", f"{room_path}/messages?after={early['message_id']}"
    ).json() == {"messages": []}
    later = send(team, room_path, "later").json()
    assert get(team, "target", f"{room_path}/messages").json() == {
        "messages": [early, later]
    }


def test_history_before_pages_backwards_without_duplicates(team):
    room_path = room(team)
    join(team, "target", room_path)
    confirm(team, "sender", room_path)
    error(
        get(team, "target", f"{room_path}/messages?before={2**70}"),
        409,
        "board_ack_required",
    )
    messages = [
        send(team, room_path, f"note-{index}", text=f"page {index}").json()
        for index in range(1, 6)
    ]
    ids = [message["message_id"] for message in messages]
    confirm(team, "target", room_path)
    # Backwards walks page the closest older window in ascending order.
    walk = [
        get(team, "sender", f"{room_path}/messages?limit=2&before={cursor}").json()[
            "messages"
        ]
        for cursor in (2**70, ids[3], ids[1], ids[0])
    ]
    assert walk == [messages[3:5], messages[1:3], messages[:1], []]
    # Both bounds apply together and huge supported ints stay bounded.
    assert (
        get(
            team,
            "sender",
            f"{room_path}/messages?after={ids[0]}&before={ids[4]}&limit=2",
        ).json()["messages"]
        == messages[2:4]
    )
    assert (
        get(
            team, "sender", f"{room_path}/messages?after={ids[1]}&before={ids[3]}"
        ).json()["messages"]
        == messages[2:3]
    )
    assert get(
        team, "sender", f"{room_path}/messages?after={2**70}&before={2**70 + 5}"
    ).json() == {"messages": []}
    # Legacy forward paging is unchanged.
    assert (
        get(team, "sender", f"{room_path}/messages?limit=2").json()["messages"]
        == (messages[:2])
    )
    assert (
        get(team, "sender", f"{room_path}/messages?after={ids[1]}&limit=2").json()[
            "messages"
        ]
        == messages[2:4]
    )
    # A member with the same window reads the same backwards pages.
    assert (
        get(team, "target", f"{room_path}/messages?limit=2&before={2**70}").json()[
            "messages"
        ]
        == messages[3:5]
    )
    # A late joiner cannot page backwards past its join floor.
    join(team, "reader", room_path)
    confirm(team, "reader", room_path)
    newcomer = send(team, room_path, "newcomer").json()
    assert get(
        team, "reader", f"{room_path}/messages?before={newcomer['message_id']}"
    ).json() == {"messages": []}
    assert get(
        team, "reader", f"{room_path}/messages?before={newcomer['message_id'] + 1}"
    ).json() == {"messages": [newcomer]}
    # Third parties and invalid bounds fail like the rest of the contract.
    assert enroll(team, "outsider").status_code == 200
    error(
        get(team, "outsider", f"{room_path}/messages?before={newcomer['message_id']}"),
        404,
        "not_found",
    )
    for invalid in ("0", "-3", "abc", "true"):
        error(
            get(team, "sender", f"{room_path}/messages?before={invalid}"),
            422,
            "invalid_request",
        )


def test_history_before_above_the_sqlite_max_is_an_unbounded_upper_edge(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with api(db_path) as client:
        assert enroll(client, "sender").status_code == 200
        room_path = room(client)
        confirm(client, "sender", room_path)
        lowly = send(client, room_path, "lowly", text="an ordinary id").json()
        room_id = room_path.rsplit("/", 1)[-1]
        sender_id = member(client, "sender")
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO messages (
                message_id, room_id, sender_member_id, client_message_id, text
            ) VALUES (?, ?, ?, 'max-id', 'the valid max id')
            """,
            (2**63 - 1, room_id, sender_id),
        )
    with api(db_path) as client:
        maximum = {
            **lowly,
            "message_id": 2**63 - 1,
            "client_message_id": "max-id",
            "text": "the valid max id",
            "created_at": None,
        }
        # A before cursor above the SQLite max pages the latest window unbounded.
        for before in (2**63, 2**70):
            assert get(
                client, "sender", f"{room_path}/messages?before={before}"
            ).json() == {"messages": [lowly, maximum]}
        assert get(client, "sender", f"{room_path}/messages?after={2**63}").json() == {
            "messages": []
        }


def test_search_denies_third_parties_and_unconfirmed_boards(team):
    room_path = room(team)
    confirm(team, "sender", room_path)
    send(team, room_path, "s-1", text="Find the shared needle here")
    join(team, "target", room_path)
    error(
        get(team, "target", f"{room_path}/search?q=needle"), 409, "board_ack_required"
    )
    error(get(team, "reader", f"{room_path}/search?q=needle"), 404, "not_found")
    error(get(team, "target", f"/rooms/{uuid4()}/search?q=needle"), 404, "not_found")
    error(team.get(f"/v1{room_path}/search?q=needle"), 401, "unauthorized")
    confirm(team, "target", room_path)
    assert get(team, "target", f"{room_path}/search?q=needle").status_code == 200
    error(get(team, "sender", f"{room_path}/search"), 422, "invalid_request")
    error(get(team, "sender", f"{room_path}/search?q="), 422, "invalid_request")
    error(get(team, "sender", f"{room_path}/search?q=%20%20"), 422, "invalid_request")
    error(get(team, "sender", f"{room_path}/search?q=%FF"), 422, "invalid_request")
    error(
        get(team, "sender", f"{room_path}/search?q=needle&limit=0"),
        422,
        "invalid_request",
    )
    error(
        get(team, "sender", f"{room_path}/search?q=needle&limit=101"),
        422,
        "invalid_request",
    )
    error(
        get(team, "sender", f"{room_path}/search?q=needle&after=-1"),
        422,
        "invalid_request",
    )
    error(
        get(team, "sender", f"{room_path}/search?q={'n' * 201}"), 422, "invalid_request"
    )
    assert get(team, "sender", f"{room_path}/search?q=needle&after={2**70}").json() == {
        "matches": []
    }
    assert get(team, "sender", f"{room_path}/search?q={'n' * 200}").json() == {
        "matches": []
    }


def test_search_authorizes_like_history_per_room_policy(team):
    limited = room(team)
    confirm(team, "sender", limited)
    early = send(team, limited, "limited-pre", text="needle sent before joining").json()
    join(team, "target", limited)
    confirm(team, "target", limited)
    late = send(team, limited, "limited-post", text="needle sent after joining").json()

    kept = room(team, history_policy="retained")
    confirm(team, "sender", kept)
    kept_early = send(team, kept, "kept-pre", text="needle sent before joining").json()
    join(team, "target", kept)
    confirm(team, "target", kept)
    kept_late = send(team, kept, "kept-post", text="needle sent after joining").json()

    # since_joining hides pre-join text from search exactly as from history.
    assert get(team, "target", f"{limited}/messages").json()["messages"] == [late]
    assert find(team, "target", limited, "NEEDLE") == find(
        team, "target", limited, "needle", after=0
    )
    assert [
        match["message_id"] for match in find(team, "target", limited, "needle")
    ] == [late["message_id"]]
    # retained exposes pre-join text after the board acknowledgment.
    assert get(team, "target", f"{kept}/messages").json()["messages"] == [
        kept_early,
        kept_late,
    ]
    assert [match["message_id"] for match in find(team, "target", kept, "needle")] == [
        kept_early["message_id"],
        kept_late["message_id"],
    ]
    # The creator's own window covers both messages and stays room-scoped.
    assert [
        match["message_id"] for match in find(team, "sender", limited, "needle")
    ] == [
        early["message_id"],
        late["message_id"],
    ]
    assert [
        match["message_id"]
        for match in find(team, "sender", limited, "needle sent before joining")
    ] == [early["message_id"]]


def test_search_matches_literals_and_bounded_snippets(team):
    room_path = room(team)
    join(team, "target", room_path)
    confirm(team, "sender", room_path)
    confirm(team, "target", room_path)
    sender_id = member(team, "sender")
    corpus = {
        "plain": "The needle is here",
        "meta": "Needles % _ * ? ( ) [ ] ^ $ | done",
        "escaped": 'it\'s a "quoted" C:\\temp path',
        "unicode": "Der Weg führt über die Straße",
        "cjk": "会话历史记录 needle",
        "longneedle": "y" * 10 + "n" * 250,
        "later": "a" * 300 + "needle" + "b" * 300,
        "twice": "needle" + "x" * 300 + "needle",
        "expansive": "z" * 5 + "ß" * 300,
        "fussball": "Fußball",
        "turkish": "ß" + "w" * 100 + "İstanbul",
        "drift": "y" * 90 + "ß" + "z" * 300,
        "nomatch": "nothing relevant",
    }
    sent = {
        key: send(team, room_path, key, text=text).json()
        for key, text in corpus.items()
    }

    found = find(team, "target", room_path, "NEEDLE")
    expected = [
        sent[key]["message_id"] for key in ("plain", "meta", "cjk", "later", "twice")
    ]
    by_id = {message["message_id"]: message for message in sent.values()}
    assert [match["message_id"] for match in found] == expected
    for match in found:
        assert set(match) == {"message_id", "sender_member_id", "created_at", "snippet"}
        assert match["sender_member_id"] == sender_id
        assert match["created_at"] == by_id[match["message_id"]]["created_at"]

    snippets = {match["message_id"]: match["snippet"] for match in found}
    assert snippets[sent["plain"]["message_id"]] == "The needle is here"
    # The window holds at most 200 characters and leads the match with context.
    assert snippets[sent["later"]["message_id"]] == "a" * 80 + "needle" + "b" * 114
    # The snippet stays around the first of two matches.
    assert snippets[sent["twice"]["message_id"]] == "needle" + "x" * 194
    # A full 200-character needle still fits the snippet.
    (wide,) = find(team, "target", room_path, "n" * 200)
    assert wide["message_id"] == sent["longneedle"]["message_id"]
    assert wide["snippet"] == "n" * 200
    # A needle that casefolds beyond 200 characters stays bounded in original characters.
    (folded,) = find(team, "target", room_path, "ß" * 200)
    assert folded["message_id"] == sent["expansive"]["message_id"]
    assert folded["snippet"] == "ß" * 200
    # Case folding matches Unicode text; the snippet quotes the original.
    (strasse,) = find(team, "target", room_path, "STRASSE")
    assert strasse["message_id"] == sent["unicode"]["message_id"]
    assert strasse["snippet"] == "Der Weg führt über die Straße"
    (cjk,) = find(team, "target", room_path, "历史")
    assert cjk["message_id"] == sent["cjk"]["message_id"]
    assert cjk["snippet"] == "会话历史记录 needle"
    # Pattern metacharacters and quotes match literally.
    (meta,) = find(team, "target", room_path, "% _ * ? ( ) [ ] ^ $ |")
    assert meta["message_id"] == sent["meta"]["message_id"]
    assert meta["snippet"] == "Needles % _ * ? ( ) [ ] ^ $ | done"
    (quoted,) = find(team, "target", room_path, 'it\'s a "quoted" C:\\temp')
    assert quoted["message_id"] == sent["escaped"]["message_id"]
    assert quoted["snippet"] == 'it\'s a "quoted" C:\\temp path'
    # Matches ending inside or spanning a casefold expansion keep the original characters.
    (fussball,) = find(team, "target", room_path, "uß")
    assert fussball["message_id"] == sent["fussball"]["message_id"]
    assert fussball["snippet"] == "Fußball"
    (spanned,) = find(team, "target", room_path, "ußb")
    assert spanned["message_id"] == sent["fussball"]["message_id"]
    assert spanned["snippet"] == "Fußball"
    # A match starting inside an İ expansion still maps back to the original character.
    (turkish,) = find(team, "target", room_path, "̇")
    assert turkish["message_id"] == sent["turkish"]["message_id"]
    assert turkish["snippet"] == "w" * 80 + "İstanbul"
    # Expansions before the match do not shift the original-character window.
    (drifted,) = find(team, "target", room_path, "ßz")
    assert drifted["message_id"] == sent["drift"]["message_id"]
    assert drifted["snippet"] == "y" * 80 + "ß" + "z" * 119
    # after and limit page matches exactly like history, empty pages are valid.
    first = find(team, "target", room_path, "needle", limit=2)
    assert [match["message_id"] for match in first] == expected[:2]
    second = find(
        team, "target", room_path, "needle", after=first[-1]["message_id"], limit=2
    )
    assert [match["message_id"] for match in second] == expected[2:4]
    third = find(team, "target", room_path, "needle", after=second[-1]["message_id"])
    assert [match["message_id"] for match in third] == expected[4:]
    assert find(team, "target", room_path, "needle", after=expected[-1]) == []
    assert find(team, "target", room_path, "absent") == []
    assert find(team, "target", room_path, "needle", after=0) == found


def test_search_quotes_oversized_folded_match_from_its_start(team):
    room_path = room(team)
    join(team, "target", room_path)
    for label in ("sender", "target"):
        confirm(team, label, room_path)
    # The query is 101 characters but casefolds to 202, so the match covers
    # 202 original characters and cannot fit a 200-character snippet.
    oversized = send(team, room_path, "oversized-folded", text="ss" * 101).json()

    (match,) = find(team, "target", room_path, "ß" * 101)
    assert set(match) == {"message_id", "sender_member_id", "created_at", "snippet"}
    assert match["message_id"] == oversized["message_id"]
    assert match["sender_member_id"] == member(team, "sender")
    assert match["created_at"] == oversized["created_at"]
    # The snippet quotes the oversized match from its start up to the cap.
    assert match["snippet"] == "s" * 200
    assert oversized["text"].startswith(match["snippet"])
    # The full original text stays readable via the stable message ID under current auth.
    history = get(team, "target", f"{room_path}/messages").json()["messages"]
    by_id = {message["message_id"]: message for message in history}
    assert by_id[oversized["message_id"]]["text"] == oversized["text"] == "ss" * 101


def test_created_at_is_utc_rfc3339_and_shared_across_send_retry_history_inbox(team):
    room_path = room(team)
    join(team, "target", room_path)
    for label in ("sender", "target"):
        confirm(team, label, room_path)
    target = member(team, "target")
    sent = send(team, room_path, "once", target=target).json()
    assert_utc_timestamp(sent["created_at"])
    assert get(team, "sender", f"{room_path}/messages").json() == {"messages": [sent]}
    assert get(
        team, "sender", f"{room_path}/messages?after={sent['message_id']}"
    ).json() == {"messages": []}
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
