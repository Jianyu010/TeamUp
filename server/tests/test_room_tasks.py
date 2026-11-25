"""HTTP contract tests for room tasks owned by Collaboration."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
from base64 import urlsafe_b64encode
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest
from teamup_server.collaboration import _ACCOUNT_SCHEMA, _SCHEMA


def credential(label):
    return urlsafe_b64encode(sha256(label.encode()).digest()).decode().rstrip("=")


KEYS = {"alice": credential("enroll-alice"), "bob": credential("enroll-bob")}


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
            yield client
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def auth(label):
    return {"Authorization": f"Bearer {credential(label)}"}


def enroll(client, label, *, owner="alice"):
    response = client.post(
        "/v1/members",
        headers={"Authorization": f"Bearer {KEYS[owner]}"},
        json={
            "harness_instance_id": "shared-harness",
            "session_key": label,
            "role_description": label,
            "member_token": credential(label),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def post(client, label, path, **kwargs):
    return client.post(f"/v1{path}", headers=auth(label), **kwargs)


def get(client, label, path):
    return client.get(f"/v1{path}", headers=auth(label))


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def team_room(client):
    members = {
        label: enroll(client, label, owner=owner)
        for label, owner in (
            ("sender", "alice"),
            ("target", "bob"),
            ("reader", "bob"),
        )
    }
    created = post(client, "sender", "/rooms", json={"name": "Tasks"})
    assert created.status_code == 200, created.text
    room_path = f"/rooms/{created.json()['room_id']}"
    for label in ("target", "reader"):
        assert post(client, label, f"{room_path}/join").status_code == 200
    return members, room_path


def acknowledge(client, label, room_path):
    assert get(client, label, f"{room_path}/board").status_code == 200
    response = post(client, label, f"{room_path}/board/ack", json={"version": 1})
    assert response.status_code == 200, response.text


def create(client, label, room_path, key="task-1", **body):
    payload = {"client_task_id": key, "title": "Check API", **body}
    return post(client, label, f"{room_path}/tasks", json=payload)


def test_addressed_visibility_precedes_board_ack(tmp_path):
    with live_server(tmp_path / "team.sqlite3") as client:
        members, room_path = team_room(client)
        acknowledge(client, "sender", room_path)

        created = create(
            client,
            "sender",
            room_path,
            addressed_member_ids=[members["target"]["member_id"]],
        )
        assert created.status_code == 200, created.text
        task = created.json()
        path = f"{room_path}/tasks/{task['task_id']}"
        assert task["task_number"] == 1
        assert task["participant_member_ids"] == []
        assert task["addressed_member_ids"] == [members["target"]["member_id"]]
        assert task["status"] == "open"
        assert task["version"] == 1
        assert task["stalled_since"] is None
        assert task["started_at"] is None
        assert task["last_progress_at"] is None
        assert task["finished_at"] is None

        error(get(client, "reader", path), 404, "not_found")
        error(get(client, "target", path), 409, "board_ack_required")
        error(post(client, "target", f"{path}/join"), 409, "board_ack_required")
        error(get(client, "reader", f"{room_path}/tasks"), 409, "board_ack_required")

        acknowledge(client, "target", room_path)
        acknowledge(client, "reader", room_path)
        error(get(client, "reader", path), 404, "not_found")
        assert get(client, "reader", f"{room_path}/tasks").json() == {
            "tasks": [],
            "next_after": None,
        }
        joined = post(client, "target", f"{path}/join")
        assert joined.status_code == 200, joined.text
        assert joined.json()["participant_member_ids"] == [
            members["target"]["member_id"]
        ]
        assert joined.json()["started_at"] is not None


def test_three_member_multi_join_progress_and_finish(tmp_path):
    with live_server(tmp_path / "team.sqlite3") as client:
        members, room_path = team_room(client)
        for label in ("sender", "target", "reader"):
            acknowledge(client, label, room_path)
        task = create(client, "sender", room_path).json()
        path = f"{room_path}/tasks/{task['task_id']}"

        first = post(client, "target", f"{path}/join").json()
        started_at = first["started_at"]
        second = post(client, "reader", f"{path}/join").json()
        assert second["started_at"] == started_at
        assert second["participant_member_ids"] == sorted(
            [members["target"]["member_id"], members["reader"]["member_id"]]
        )
        assert post(client, "target", f"{path}/join").json() == second

        progress = post(
            client,
            "target",
            f"{path}/progress",
            json={"client_progress_id": "progress-1", "note": "Fixture passes"},
        )
        assert progress.status_code == 200, progress.text
        assert progress.json()["last_progress_at"] is not None
        assert progress.json()["status"] == "open"

        finished = post(client, "sender", f"{path}/finish")
        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "finished"
        assert finished.json()["finished_at"] is not None

        assert get(client, "target", f"{room_path}/tasks?status=open").json() == {
            "tasks": [],
            "next_after": None,
        }
        finished_page = get(client, "target", f"{room_path}/tasks?status=finished")
        assert [task["task_id"] for task in finished_page.json()["tasks"]] == [
            task["task_id"]
        ]
        events = get(client, "target", f"{path}/events").json()
        assert [event["kind"] for event in events["events"]] == [
            "created",
            "joined",
            "joined",
            "progress",
            "finished",
        ]
        assert events["events"][3]["note"] == "Fixture passes"
        assert [event["event_id"] for event in events["events"]] == sorted(
            event["event_id"] for event in events["events"]
        )

        assert post(client, "target", f"{path}/finish").json() == finished.json()
        assert post(client, "target", f"{path}/join").json()["status"] == "finished"
        error(
            post(
                client,
                "reader",
                f"{path}/progress",
                json={"client_progress_id": "late-progress", "note": "Too late"},
            ),
            409,
            "conflict",
        )
        error(post(client, "sender", f"{path}/join"), 409, "conflict")


def test_retries_are_member_scoped_and_survive_finish(tmp_path):
    with live_server(tmp_path / "team.sqlite3") as client:
        _members, room_path = team_room(client)
        for label in ("sender", "target", "reader"):
            acknowledge(client, label, room_path)
        first = create(client, "sender", room_path, key="shared").json()
        path = f"{room_path}/tasks/{first['task_id']}"
        assert create(client, "sender", room_path, key="shared").json() == first

        changed = create(client, "sender", room_path, key="shared", title="Different")
        error(changed, 409, "conflict")

        other_room = post(client, "sender", "/rooms", json={"name": "Other"}).json()
        acknowledge(client, "sender", f"/rooms/{other_room['room_id']}")
        error(
            create(
                client,
                "sender",
                f"/rooms/{other_room['room_id']}",
                key="shared",
            ),
            409,
            "conflict",
        )

        independent = create(client, "target", room_path, key="shared")
        assert independent.status_code == 200, independent.text
        assert independent.json()["task_id"] != first["task_id"]

        assert post(client, "target", f"{path}/join").status_code == 200
        progress_body = {"client_progress_id": "progress-1", "note": "First"}
        updated = post(client, "target", f"{path}/progress", json=progress_body)
        assert updated.status_code == 200, updated.text
        assert (
            post(client, "target", f"{path}/progress", json=progress_body).json()
            == updated.json()
        )
        error(
            post(
                client,
                "target",
                f"{path}/progress",
                json={"client_progress_id": "progress-1", "note": "Changed"},
            ),
            409,
            "conflict",
        )

        finished = post(client, "sender", f"{path}/finish").json()
        retried_progress = post(
            client, "target", f"{path}/progress", json=progress_body
        )
        assert retried_progress.status_code == 200, retried_progress.text
        assert retried_progress.json() == finished
        assert create(client, "sender", room_path, key="shared").json() == finished
        assert post(client, "target", f"{path}/join").json() == finished
        assert post(client, "sender", f"{path}/finish").json() == finished
        events = get(client, "target", f"{path}/events").json()["events"]
        assert [event["kind"] for event in events] == [
            "created",
            "joined",
            "progress",
            "finished",
        ]

        error(
            post(
                client,
                "target",
                f"{path}/progress",
                json={"client_progress_id": "progress-2", "note": "After finish"},
            ),
            409,
            "conflict",
        )


def test_tasks_survive_restart_on_an_existing_database(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for statement in (*_SCHEMA, *_ACCOUNT_SCHEMA):
            connection.execute(statement)
        # The v4 DM fixture predates operator sessions and expiry watermarks.
        connection.execute("ALTER TABLE account_sessions DROP COLUMN purpose")
        connection.execute("ALTER TABLE rooms DROP COLUMN expired_through_message_id")
        connection.execute("ALTER TABLE dms DROP COLUMN expired_through_message_id")
        connection.execute("PRAGMA user_version = 4")

    with live_server(db_path) as client:
        _members, room_path = team_room(client)
        acknowledge(client, "sender", room_path)
        acknowledge(client, "target", room_path)
        task = create(client, "sender", room_path, key="durable").json()
        path = f"{room_path}/tasks/{task['task_id']}"
        assert post(client, "target", f"{path}/join").status_code == 200
        progress = {"client_progress_id": "progress-1", "note": "Saved"}
        updated = post(client, "target", f"{path}/progress", json=progress)
        assert updated.status_code == 200, updated.text
        events = get(client, "target", f"{path}/events").json()
        assert [event["kind"] for event in events["events"]] == [
            "created",
            "joined",
            "progress",
        ]

    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT version FROM room_task_schema WHERE singleton = 1"
            ).fetchone()[0]
            == 1
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "tasks",
            "task_addressees",
            "task_participants",
            "task_events",
        } <= tables

    with live_server(db_path) as client:
        assert get(client, "target", path).json() == updated.json()
        assert get(client, "target", f"{path}/events").json() == events
        assert (
            create(client, "sender", room_path, key="durable").json() == updated.json()
        )
        assert len(get(client, "target", f"{path}/events").json()["events"]) == len(
            events["events"]
        )
        second = create(client, "sender", room_path, key="task-2")
        assert second.status_code == 200, second.text
        assert second.json()["task_number"] == 2
