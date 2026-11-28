"""Contract coverage for the member-authorized room change feed."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from test_contract import (
    api,
    auth,
    confirm,
    credential,
    enroll,
    error,
    get,
    join,
    post,
    room,
    send,
    server_command,
)


def _scope(client, label, path):
    member = get(client, label, "/members/me").json()
    directory = get(client, label, "/rooms?include_membership=true").json()["rooms"]
    membership = next(row for row in directory if path.endswith(row["room_id"]))
    return {
        "expected_member_id": member["member_id"],
        "membership_id": membership["membership_id"],
    }


def _sync(client, label, path, scope, **params):
    return client.get(
        f"/v1{path}/sync", headers=auth(credential(label)), params={**scope, **params}
    )


def test_sync_baseline_and_existing_writes_are_scoped(tmp_path):
    with api(tmp_path / "team.sqlite3") as client:
        error(client.get("/v1/sync/capabilities"), 401, "unauthorized")
        for name in ("sender", "reader", "outsider"):
            assert enroll(client, name).status_code == 200
        capabilities = get(client, "reader", "/sync/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["version"] == 1

        path = room(client)
        room_id = path.split("/")[-1]
        confirm(client, "sender", path)
        sent_before_join = send(client, path, "early").json()
        join(client, "reader", path)
        scope = _scope(client, "reader", path)
        error(_sync(client, "reader", path, scope), 409, "board_ack_required")
        error(_sync(client, "outsider", path, scope), 409, "member_identity_changed")
        outsider = {
            **scope,
            "expected_member_id": get(client, "outsider", "/members/me").json()[
                "member_id"
            ],
        }
        error(_sync(client, "outsider", path, outsider), 404, "not_found")
        confirm(client, "reader", path)

        response = _sync(client, "reader", path, scope)
        assert response.status_code == 200, response.text
        baseline = response.json()
        assert baseline["server_generation"] == capabilities.json()["server_generation"]
        assert baseline["member_id"] == scope["expected_member_id"]
        assert baseline["membership_id"] == scope["membership_id"]
        assert (
            baseline["room_id"],
            baseline["board_version"],
            baseline["has_more"],
        ) == (room_id, 1, False)
        assert baseline["coverage"] == {
            "room": {"has_more": False},
            "board": {"has_more": False},
            "roster": {"has_more": False},
            "messages": {"has_older": False},
            "tasks": {"has_more": False},
        }
        assert {row["collection"] for row in baseline["changes"]} == {
            "room",
            "board",
            "roster",
        }
        cursor = baseline["next_cursor"]
        error(
            _sync(client, "reader", path, scope, cursor="corrupt"),
            409,
            "sync_reset_required",
        )

        private_task = post(
            client,
            "sender",
            f"{path}/tasks",
            json={
                "client_task_id": "private",
                "title": "A private task",
                "addressed_member_ids": [outsider["expected_member_id"]],
            },
        )
        # Addressing requires current room membership.
        assert private_task.status_code >= 400
        join(client, "outsider", path)
        private_task = post(
            client,
            "sender",
            f"{path}/tasks",
            json={
                "client_task_id": "private",
                "title": "A private task",
                "addressed_member_ids": [outsider["expected_member_id"]],
            },
        )
        assert private_task.status_code == 200, private_task.text
        sent_after_join = send(client, path, "late", text="After joining").json()
        delta = _sync(client, "reader", path, scope, cursor=cursor)
        assert delta.status_code == 200, delta.text
        assert private_task.json()["task_id"] not in delta.text
        assert "A private task" not in delta.text
        assert [
            row for row in delta.json()["changes"] if row["collection"] == "messages"
        ] == [
            {
                "collection": "messages",
                "entity_id": str(sent_after_join["message_id"]),
                "record": sent_after_join,
            }
        ]
        assert not any(
            row["entity_id"] == str(sent_before_join["message_id"])
            for row in delta.json()["changes"]
        )
        cursor = delta.json()["next_cursor"]
        error(
            _sync(
                client,
                "outsider",
                path,
                _scope(client, "outsider", path),
                cursor=cursor,
            ),
            409,
            "board_ack_required",
        )
        confirm(client, "outsider", path)
        error(
            _sync(
                client,
                "outsider",
                path,
                _scope(client, "outsider", path),
                cursor=cursor,
            ),
            409,
            "sync_reset_required",
        )

        published = post(
            client,
            "sender",
            f"{path}/board",
            json={
                "version": 1,
                "rules": "Updated rules",
                "protocol": "Updated protocol",
            },
        )
        assert published.status_code == 200, published.text
        error(
            _sync(client, "reader", path, scope, cursor=cursor),
            409,
            "board_ack_required",
        )
        assert get(client, "reader", f"{path}/board").status_code == 200
        assert (
            post(client, "reader", f"{path}/board/ack", json={"version": 2}).status_code
            == 200
        )
        error(
            _sync(client, "reader", path, scope, cursor=cursor),
            409,
            "sync_reset_required",
        )

        assert post(client, "reader", f"{path}/leave").status_code == 200
        join(client, "reader", path)
        assert _scope(client, "reader", path) != scope
        error(
            _sync(client, "reader", path, scope, cursor=cursor),
            409,
            "membership_changed",
        )


def test_sync_journals_exact_compatibility_removals_and_archive(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        assert enroll(client, "sender").status_code == 200
        path = room(client, history_policy="retained")
        scope = _scope(client, "sender", path)
        confirm(client, "sender", path)
        retained = [send(client, path, str(uuid4())).json() for _ in range(3)]
        cursor = _sync(client, "sender", path, scope).json()["next_cursor"]
        deleted = retained[1]["message_id"]
        with closing(sqlite3.connect(database)) as fixture, fixture:
            fixture.execute("DELETE FROM inbox_events WHERE message_id = ?", (deleted,))
            fixture.execute("DELETE FROM messages WHERE message_id = ?", (deleted,))
        response = _sync(client, "sender", path, scope, cursor=cursor)
        assert response.status_code == 200, response.text
        delta = response.json()
        assert [row for row in delta["changes"] if row["collection"] == "messages"] == [
            {"collection": "messages", "entity_id": str(deleted), "record": None}
        ]
        assert (
            get(client, "sender", f"{path}/messages").json()["messages"]
            == retained[::2]
        )

        archived = post(client, "sender", f"{path}/archive")
        assert archived.status_code == 200, archived.text
        error(
            _sync(client, "sender", path, scope, cursor=delta["next_cursor"]),
            409,
            "room_archived",
        )
        assert (
            get(client, "sender", f"{path}/messages").json()["messages"]
            == retained[::2]
        )


def test_hidden_task_commit_groups_advance_and_restriction_resets_immediately(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        for name in ("sender", "reader", "target"):
            assert enroll(client, name).status_code == 200
        path = room(client)
        target = get(client, "target", "/members/me").json()["member_id"]
        for name in ("reader", "target"):
            join(client, name, path)
        for name in ("sender", "reader"):
            confirm(client, name, path)
        scope = _scope(client, "reader", path)
        baseline = _sync(client, "reader", path, scope).json()

        for number in range(100):
            hidden = post(
                client,
                "sender",
                f"{path}/tasks",
                json={
                    "client_task_id": f"hidden-{number}",
                    "title": "A private task",
                    "addressed_member_ids": [target],
                },
            )
            assert hidden.status_code == 200, hidden.text
        public = post(
            client,
            "sender",
            f"{path}/tasks",
            json={
                "client_task_id": "public",
                "title": "Public task",
            },
        ).json()
        first = _sync(
            client, "reader", path, scope, cursor=baseline["next_cursor"]
        ).json()
        assert first["has_more"] is True and first["changes"] == []
        assert first["next_cursor"] != baseline["next_cursor"]
        second = _sync(
            client, "reader", path, scope, cursor=first["next_cursor"]
        ).json()
        assert second["has_more"] is False and second["changes"] == [
            {"collection": "tasks", "entity_id": public["task_id"], "record": public}
        ]

        with closing(sqlite3.connect(database)) as fixture, fixture:
            fixture.execute(
                "INSERT INTO task_addressees VALUES (?, ?)", (public["task_id"], target)
            )
        response = _sync(client, "reader", path, scope, cursor=baseline["next_cursor"])
        error(response, 409, "sync_reset_required")
        assert public["task_id"] not in response.text
        assert "tasks" not in [
            row["collection"]
            for row in _sync(client, "reader", path, scope).json()["changes"]
        ]


def test_feed_does_not_expose_private_task_id_in_an_assignment_message(tmp_path):
    with api(tmp_path / "team.sqlite3") as client:
        for name in ("sender", "reader", "target"):
            assert enroll(client, name).status_code == 200
        path = room(client)
        target = get(client, "target", "/members/me").json()["member_id"]
        for name in ("reader", "target"):
            join(client, name, path)
        for name in ("sender", "reader"):
            confirm(client, name, path)
        scope = _scope(client, "reader", path)
        cursor = _sync(client, "reader", path, scope).json()["next_cursor"]

        created = post(
            client,
            "sender",
            f"{path}/tasks",
            json={
                "client_task_id": "private-assignment",
                "title": "A private task",
                "addressed_member_ids": [target],
                "assignment": {"closing_criteria": "Private criteria"},
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        legacy = get(client, "reader", f"{path}/messages").json()["messages"]
        assert legacy[0]["client_message_id"] == f"task-assignment-{task_id}"
        for params in ({}, {"cursor": cursor}):
            response = _sync(client, "reader", path, scope, **params)
            assert response.status_code == 200, response.text
            assert task_id not in response.text
            assert "Private criteria" not in response.text
            message = next(
                row["record"]
                for row in response.json()["changes"]
                if row["collection"] == "messages"
            )
            assert message == {
                **legacy[0],
                "client_message_id": f"task-assignment-message-{legacy[0]['message_id']}",
            }


def test_pruned_cursor_resets_and_baseline_keeps_latest_window(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        assert enroll(client, "sender").status_code == 200
        path = room(client, history_policy="retained")
        room_id = path.split("/")[-1]
        confirm(client, "sender", path)
        scope = _scope(client, "sender", path)
        cursor = _sync(client, "sender", path, scope).json()["next_cursor"]
        with closing(sqlite3.connect(database)) as fixture, fixture:
            fixture.executemany(
                "INSERT INTO messages (message_id, room_id, sender_member_id, client_message_id, text) "
                "VALUES (?, ?, ?, ?, 'fixture body')",
                (
                    (number, room_id, scope["expected_member_id"], str(number))
                    for number in range(1, 10_002)
                ),
            )
        error(
            _sync(client, "sender", path, scope, cursor=cursor),
            409,
            "sync_reset_required",
        )
        response = _sync(client, "sender", path, scope).json()
        assert response["coverage"]["messages"] == {"has_older": True}
        assert [
            row["record"]["message_id"]
            for row in response["changes"]
            if row["collection"] == "messages"
        ] == list(range(9902, 10_002))


def test_generation_survives_restart_and_rotates_for_restore(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        assert enroll(client, "sender").status_code == 200
        path = room(client)
        confirm(client, "sender", path)
        scope = _scope(client, "sender", path)
        baseline = _sync(client, "sender", path, scope).json()

    with api(database) as client:
        response = _sync(client, "sender", path, scope, cursor=baseline["next_cursor"])
        assert response.status_code == 200, response.text
        assert response.json()["server_generation"] == baseline["server_generation"]

    recovery = tmp_path / "recovery"
    recovery.mkdir(mode=0o700)
    restored = recovery / "restored.sqlite3"
    scripts = Path(__file__).resolve().parents[4] / "scripts" / "ops"
    subprocess.run(
        [
            sys.executable,
            str(scripts / "restore_backup.py"),
            "--snapshot",
            str(database),
            "--destination",
            str(restored),
            "--sha256",
            sha256(database.read_bytes()).hexdigest(),
        ],
        check=True,
        capture_output=True,
    )
    restored.replace(database)
    with api(database) as client:
        error(
            _sync(client, "sender", path, scope, cursor=baseline["next_cursor"]),
            409,
            "sync_reset_required",
        )
        restored_baseline = _sync(client, "sender", path, scope).json()
        assert restored_baseline["server_generation"] != baseline["server_generation"]

    command = server_command(database)
    subprocess.run([*command[:-2], "rotate-sync-generation", *command[-2:]], check=True)
    with api(database) as client:
        response = _sync(
            client, "sender", path, scope, cursor=restored_baseline["next_cursor"]
        )
        error(response, 409, "sync_reset_required")
        assert (
            get(client, "sender", "/sync/capabilities").json()["server_generation"]
            != restored_baseline["server_generation"]
        )
