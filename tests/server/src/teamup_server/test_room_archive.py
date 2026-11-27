import asyncio
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import file_digest
from pathlib import Path
from threading import Barrier

from teamup_server.collaboration import Collaboration
from test_contract import (
    KEYS,
    api,
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
    wait_for,
)


def test_creator_freezes_room_and_keeps_history_and_roster(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        enroll(client, "target", owner="bob")
        enroll(client, "outsider", owner="bob")
        path = room(client)
        join(client, "target", path)
        confirm(client, "sender", path)
        confirm(client, "target", path)
        target = member(client, "target")
        task = post(
            client,
            "sender",
            f"{path}/tasks",
            json={
                "title": "Keep the task",
                "client_task_id": "keep-task",
            },
        ).json()
        message = send(client, path, "keep", "Keep this history", target=target).json()
        inbox = get(client, "target", "/inbox?kind=mention").json()["events"]
        assert inbox[0]["message"]["message_id"] == message["message_id"]
        error(post(client, "target", f"{path}/archive"), 403, "forbidden")
        error(post(client, "outsider", f"{path}/archive"), 404, "not_found")
        result = post(client, "sender", f"{path}/archive")
        assert result.status_code == 200, result.text
        receipt = result.json()
        assert receipt["archive_reason"] == "goal_reached"
        assert post(client, "sender", f"{path}/archive").json() == receipt
        assert get(client, "target", "/rooms").json()["rooms"] == []
        assert get(client, "target", "/room-directory").json()["rooms"] == []
        assert get(client, "target", "/inbox?kind=mention").json()["events"] == []
        assert get(client, "outsider", "/rooms/archive").json()["rooms"] == []
        error(get(client, "outsider", f"{path}/members"), 404, "not_found")
        error(post(client, "outsider", f"{path}/join"), 404, "not_found")
        for suffix in ("join", "leave", f"tasks/{task['task_id']}/finish"):
            error(post(client, "sender", f"{path}/{suffix}"), 409, "room_archived")
        error(send(client, path, "late"), 409, "room_archived")
        error(
            get(client, "target", f"{path}/messages?delivery=true"),
            409,
            "room_archived",
        )
        error(
            post(client, "target", f"/inbox/{inbox[0]['event_id']}/ack"),
            404,
            "not_found",
        )
        assert get(client, "sender", f"{path}/board/view").json()["can_edit"] is False
        roster = get(client, "sender", f"{path}/members?include_membership=true").json()
        assert {row["member_id"] for row in roster["members"]} == {
            member(client, "sender"),
            target,
        }
        assert all("membership_id" not in row for row in roster["members"])
        assert post(client, "target", "/members/me/retire").status_code == 200
    with api(database) as client:
        assert get(client, "sender", "/rooms/archive").json()["rooms"] == [receipt]
        assert get(client, "sender", f"{path}/members").json() == roster
        assert get(client, "sender", f"{path}/messages").json()["messages"] == [message]
        assert (
            get(client, "sender", f"{path}/tasks/{task['task_id']}").json()["status"]
            == "open"
        )


def test_inactivity_and_chat_retry_use_the_original_commit_time(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        empty = room(client).rsplit("/", 1)[1]
        path = room(client)
        active = path.rsplit("/", 1)[1]
        confirm(client, "sender", path)
        start = datetime.now(UTC)
    now = start + timedelta(hours=23)
    collaboration = Collaboration(database, KEYS, clock=lambda: now)
    token = credential("sender")
    original = collaboration.send_message(token, active, "one", "hello")
    now = start + timedelta(hours=25)
    assert collaboration.send_message(token, active, "one", "hello") == original
    assert collaboration.archive_inactive_rooms() is False
    assert [
        r["room_id"]
        for r in collaboration.list_archived_rooms(token, None, 50)["rooms"]
    ] == [empty]
    now = start + timedelta(hours=47)
    assert collaboration.archive_inactive_rooms() is False
    assert collaboration.archive_room(token, active)["archive_reason"] == "inactive"
    assert collaboration.history(token, active, 0, 50)["messages"] == [original]


def test_archive_and_send_serialize(tmp_path):
    with api(tmp_path / "team.sqlite3") as client:
        enroll(client, "sender")
        path = room(client)
        confirm(client, "sender", path)
        together = Barrier(2)

        def archive():
            together.wait(timeout=5)
            return post(client, "sender", f"{path}/archive")

        def chat():
            together.wait(timeout=5)
            return send(client, path, "race")

        with ThreadPoolExecutor(max_workers=2) as workers:
            closing_room = workers.submit(archive)
            racing_chat = workers.submit(chat)
            assert closing_room.result().status_code == 200
            result = racing_chat.result()
        messages = get(client, "sender", f"{path}/messages").json()["messages"]
        if result.is_success:
            assert messages == [result.json()]
        else:
            error(result, 409, "room_archived")
            assert messages == []


def test_server_startup_archives_inactive_rooms_even_with_old_retention_flags(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        path = room(client)
        confirm(client, "sender", path)
        assert send(client, path, "saved").status_code == 200
    cutoff = datetime.now(UTC) - timedelta(days=2)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE rooms SET last_chat_at = ?", (int(cutoff.timestamp() * 1_000_000),)
        )
        connection.execute("UPDATE messages SET created_at = ?", (cutoff.isoformat(),))
    with live_server(
        database, extra=("--room-retention-hours", "1", "--dm-retention-hours", "1")
    ) as (client, _):
        wait_for(lambda: not get(client, "sender", "/rooms").json()["rooms"], 3)
        assert (
            get(client, "sender", f"{path}/messages").json()["messages"][0]["text"]
            == "hello"
        )


def test_legacy_migration_archives_known_inactivity_but_gives_unknown_rooms_a_day(
    tmp_path,
):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        known = room(client)
        legacy = room(client)
        purged = room(client)
        confirm(client, "sender", known)
        message = send(client, known, "known", "Known history").json()
    now = datetime.now(UTC)
    old = (now - timedelta(days=2)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP INDEX rooms_inactivity")
        for column in (
            "last_chat_at",
            "archive_roster",
            "archive_reason",
            "archived_at",
        ):
            connection.execute(f"ALTER TABLE rooms DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 11")
        purged_id = purged.rsplit("/", 1)[1]
        connection.execute(
            "UPDATE room_activity SET at = ? WHERE room_id = ?", (old, purged_id)
        )
        connection.execute(
            """
            INSERT INTO room_activity (room_id, kind, at, actor_member_id)
            SELECT ?, 'message', ?, creator_member_id FROM rooms WHERE room_id = ?
            """,
            (purged_id, recent, purged_id),
        )

        connection.execute(
            "UPDATE messages SET created_at = ? WHERE message_id = ?",
            (old, message["message_id"]),
        )
        connection.execute(
            "UPDATE room_activity SET at = NULL WHERE room_id = ?",
            (legacy.rsplit("/", 1)[1],),
        )
    with api(database) as client:
        wait_for(
            lambda: len(get(client, "sender", "/rooms/archive").json()["rooms"]) == 1, 3
        )
        assert (
            get(client, "sender", "/rooms/archive").json()["rooms"][0]["room_id"]
            == known.rsplit("/", 1)[1]
        )
        assert {
            row["room_id"] for row in get(client, "sender", "/rooms").json()["rooms"]
        } == {
            legacy.rsplit("/", 1)[1],
            purged.rsplit("/", 1)[1],
        }
        assert get(client, "sender", f"{known}/messages").json()["messages"] == [
            {**message, "created_at": old},
        ]


def test_backup_and_restore_preserve_archived_room_through_the_api(tmp_path):
    database = tmp_path / "team.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    ops = Path(__file__).resolve().parents[4] / "scripts/ops"
    with api(database) as client:
        enroll(client, "sender")
        enroll(client, "target", owner="bob")
        path = room(client)
        join(client, "target", path)
        confirm(client, "sender", path)
        confirm(client, "target", path)
        message = send(client, path, "snapshot", text="Survives recovery").json()
        archive = post(client, "sender", f"{path}/archive").json()
        roster = get(client, "target", f"{path}/members").json()
        assert len(roster["members"]) == 2
        subprocess.run(
            [
                sys.executable,
                str(ops / "snapshot_sqlite.py"),
                str(database),
                str(snapshot),
            ],
            check=True,
            timeout=5,
        )
    with snapshot.open("rb") as contents:
        checksum = file_digest(contents, "sha256").hexdigest()
    subprocess.run(
        [
            sys.executable,
            str(ops / "restore_backup.py"),
            "--snapshot",
            str(snapshot),
            "--sha256",
            checksum,
            "--destination",
            str(restored),
        ],
        check=True,
        timeout=5,
    )
    with api(restored) as client:
        assert get(client, "target", "/rooms/archive").json()["rooms"] == [archive]
        assert get(client, "target", f"{path}/members").json() == roster
        assert get(client, "target", f"{path}/messages").json()["messages"] == [message]
        error(send(client, path, "late"), 409, "room_archived")


def test_asgi_lifespan_runs_archive_maintenance(tmp_path):
    import httpx
    from teamup_server.main import create_app

    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        room(client)
    clock = datetime.now(UTC) + timedelta(days=2)

    async def verify():
        app = create_app(database, KEYS, clock=lambda: clock)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            for _ in range(20):
                result = await client.get(
                    "/v1/rooms/archive",
                    headers={"Authorization": f"Bearer {credential('sender')}"},
                )
                if len(result.json()["rooms"]) == 1:
                    return
                await asyncio.sleep(0.1)
            assert len(result.json()["rooms"]) == 1

    asyncio.run(verify())


def test_joined_replacement_session_of_the_creating_account_can_archive(tmp_path):
    with api(tmp_path / "team.sqlite3") as client:
        enroll(client, "sender")
        enroll(client, "replacement")
        path = room(client)
        error(post(client, "replacement", f"{path}/archive"), 404, "not_found")
        join(client, "replacement", path)
        assert post(client, "sender", "/members/me/retire").status_code == 200
        response = post(client, "replacement", f"{path}/archive")
        assert response.status_code == 200
        assert response.json()["archive_reason"] == "goal_reached"
