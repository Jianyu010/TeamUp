import json
import os
import socket
import subprocess
import sys
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
def live_server(db_path):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    with (
        subprocess.Popen(
            [*server_command(db_path), "--port", str(port)],
            env=server_env(),
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
