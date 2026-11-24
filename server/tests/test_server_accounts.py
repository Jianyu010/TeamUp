import errno
import os
import sqlite3
import subprocess
import sys
from asyncio import gather, run
from contextlib import closing
from hashlib import sha256
from queue import Queue
from select import select
from threading import Thread
from time import monotonic, time
from uuid import UUID

import httpx
import pytest
from teamup_server import collaboration
from teamup_server.main import create_app
from test_contract import (
    KEYS,
    auth,
    confirm,
    credential,
    error,
    get,
    join,
    live_server,
    member,
    room,
    send,
    server_env,
)

PASSWORD = "  TeamUp 口令 for test  "
NEW_PASSWORD = "second TeamUp password"
WRONG_PASSWORD = "a wrong TeamUp password"


def account_command(db_path, *args):
    return [
        sys.executable,
        "-m",
        "teamup_server.main",
        "accounts",
        "--db",
        str(db_path),
        *args,
    ]


def admin(db_path, *args):
    return subprocess.run(
        account_command(db_path, *args),
        env=server_env(),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout


def enter_password(
    db_path, action, username, password, confirmation=None, *, check=True
):
    if os.name != "posix":
        pytest.skip("Terminal password entry requires a POSIX PTY in this test")
    import pty

    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            account_command(db_path, action, username),
            env=server_env(),
            stdin=slave,
            stdout=slave,
            stderr=slave,
        ) as process:
            os.close(slave)
            slave = None
            output = bytearray()
            deadline = monotonic() + 5
            try:
                for expected, value in (
                    (b"Password: ", password),
                    (
                        b"Confirm password: ",
                        password if confirmation is None else confirmation,
                    ),
                ):
                    prompt = bytearray()
                    while expected not in prompt:
                        assert select([master], [], [], max(0, deadline - monotonic()))[
                            0
                        ], output
                        try:
                            chunk = os.read(master, 4096)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                pytest.fail(
                                    f"Expected a password prompt, got {output!r}"
                                )
                            raise
                        assert chunk, output
                        prompt.extend(chunk)
                        output.extend(chunk)
                    os.write(master, (value + "\n").encode())
                exit_code = process.wait(timeout=max(0, deadline - monotonic()))
                while select([master], [], [], 0)[0]:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                assert (exit_code == 0) == check, output
                assert password.encode() not in output
                if confirmation is not None:
                    assert confirmation.encode() not in output
                return output.decode()
            finally:
                if process.poll() is None:
                    process.kill()
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)


def login(client, username, password):
    return client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )


def login_token(client, username, password):
    return token_from_login(login(client, username, password))


def token_from_login(response):
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"access_token", "expires_at"}
    assert (
        int(time()) + 8 * 3600 - 2
        <= response.json()["expires_at"]
        <= int(time()) + 8 * 3600
    )
    assert isinstance(response.json()["expires_at"], int)
    token = response.json()["access_token"]
    assert len(token) == 43
    return token


def bind(client, token, label, *, member_token=None):
    return client.post(
        "/v1/members",
        headers=auth(token),
        json={
            "harness_instance_id": "shared-harness",
            "session_key": label,
            "role_description": label,
            "member_token": credential(label) if member_token is None else member_token,
        },
    )


def test_accounts_cli_refuses_passwords_from_a_pipe(tmp_path):
    result = subprocess.run(
        account_command(tmp_path / "team.sqlite3", "create", "alice"),
        env=server_env(),
        input=f"{PASSWORD}\n{PASSWORD}\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "terminal" in result.stderr.lower()
    assert PASSWORD not in result.stdout + result.stderr


def test_account_cli_confirmation_and_password_policy(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD, NEW_PASSWORD, check=False)
    enter_password(db_path, "create", "alice", "too short", check=False)
    assert not admin(db_path, "list").strip()


def test_account_lifecycle_and_credential_isolation(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    enter_password(db_path, "create", "bob", PASSWORD)
    records = {
        line.split()[0]: line.split()[1:]
        for line in admin(db_path, "list").splitlines()
    }
    alice_owner = records["alice"][1]
    UUID(alice_owner)
    assert records["alice"][0] == "active"
    assert records["bob"][1] != alice_owner
    with live_server(db_path, legacy=False) as (client, _):
        alice = login_token(client, "alice", PASSWORD)
        second_alice = login_token(client, "alice", PASSWORD)
        bob = login_token(client, "bob", PASSWORD)
        assert alice != second_alice != bob
        error(bind(client, KEYS["alice"], "legacy"), 401, "unauthorized")
        error(bind(client, alice, "alice", member_token=bob), 422, "invalid_request")
        first = bind(client, alice, "sender")
        assert first.status_code == 200, first.text
        assert bind(client, second_alice, "sender").json() == first.json()
        second = bind(client, bob, "target")
        assert second.status_code == 200, second.text
        error(bind(client, credential("sender"), "separate"), 401, "unauthorized")
        error(client.get("/v1/members/me", headers=auth(alice)), 401, "unauthorized")
        original_room = room(client)
        confirm(client, "sender", original_room)
        join(client, "target", original_room)
        confirm(client, "target", original_room)
        sent = send(client, original_room, "before", target=second.json()["member_id"])
        assert sent.status_code == 200
        assert get(client, "target", "/inbox").json()["events"][0]["kind"] == "mention"

        enter_password(db_path, "reset-password", "alice", NEW_PASSWORD)
        error(bind(client, alice, "new"), 401, "unauthorized")
        error(bind(client, second_alice, "new"), 401, "unauthorized")
        error(bind(client, bob, "new", member_token=alice), 422, "invalid_request")
        assert member(client, "sender") == first.json()["member_id"]
        error(login(client, "alice", PASSWORD), 401, "unauthorized")
        new_alice = login_token(client, "alice", NEW_PASSWORD)
        assert bind(client, new_alice, "sender").json() == first.json()

        admin(db_path, "disable", "alice")
        admin(db_path, "disable", "alice")
        assert f"alice\tdisabled\t{alice_owner}" in admin(db_path, "list")
        error(login(client, "alice", NEW_PASSWORD), 401, "unauthorized")
        error(bind(client, new_alice, "new"), 401, "unauthorized")
        error(bind(client, bob, "new", member_token=new_alice), 422, "invalid_request")
        error(get(client, "sender", "/inbox"), 401, "unauthorized")
        error(send(client, original_room, "after"), 401, "unauthorized")
        assert get(client, "target", f"{original_room}/messages").json() == {
            "messages": [sent.json()]
        }
        assert bind(client, bob, "target").json() == second.json()
        enter_password(db_path, "create", "alice", NEW_PASSWORD, check=False)


def test_login_rejects_invalid_fields_without_echoing_them(tmp_path):
    with live_server(tmp_path / "team.sqlite3", legacy=False) as (client, _):
        error(login(client, "does-not-exist", WRONG_PASSWORD), 401, "unauthorized")
        for payload in (
            {"username": "ALICE", "password": PASSWORD},
            {"username": "alice", "password": 4},
            {"username": "alice", "password": PASSWORD, "owner_id": "someone"},
        ):
            response = client.post("/v1/auth/login", json=payload)
            error(response, 422, "invalid_request")
            assert PASSWORD not in response.text


def test_login_limits_concurrent_failures_by_username_and_total_checks(
    tmp_path, monkeypatch
):
    clock = 10.0
    monkeypatch.setattr(collaboration, "monotonic", lambda: clock, raising=False)

    async def exercise():
        nonlocal clock
        async with app_client(tmp_path / "team.sqlite3") as client:
            results = await gather(
                *(login(client, "missing", WRONG_PASSWORD) for _ in range(8))
            )
            assert (
                sorted(response.status_code for response in results)
                == [401] * 5 + [429] * 3
            )
            for index in range(55):
                error(
                    await login(client, f"person-{index}", WRONG_PASSWORD),
                    401,
                    "unauthorized",
                )
            error(
                await login(client, "last-person", WRONG_PASSWORD), 429, "rate_limited"
            )
            clock += 60
            error(await login(client, "missing", WRONG_PASSWORD), 401, "unauthorized")

    run(exercise())


def app_client(db_path):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(db_path)), base_url="http://test"
    )


def test_expired_login_cannot_enroll_but_members_remain_authorized(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    clock = int(time())
    monkeypatch.setattr(collaboration, "time", lambda: clock, raising=False)

    async def exercise():
        nonlocal clock
        async with app_client(db_path) as client:
            token = token_from_login(await login(client, "alice", PASSWORD))
            assert (await bind(client, token, "sender")).status_code == 200
        async with app_client(db_path) as client:
            assert (await bind(client, token, "sender")).status_code == 200
            clock += 8 * 3600
            error(await bind(client, token, "target"), 401, "unauthorized")
            assert (await get(client, "sender", "/inbox")).status_code == 200
            response = await login(client, "alice", PASSWORD)
            assert response.status_code == 200
            new_token = response.json()["access_token"]
            error(
                await bind(client, new_token, "target", member_token=token),
                422,
                "invalid_request",
            )

    run(exercise())


def test_disabling_an_account_closes_open_sse_before_the_next_hint(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "bob", PASSWORD)
    with live_server(db_path) as (client, _):
        assert bind(client, KEYS["alice"], "sender").status_code == 200
        assert (
            bind(client, login_token(client, "bob", PASSWORD), "target").status_code
            == 200
        )
        room_path = room(client)
        confirm(client, "sender", room_path)
        join(client, "target", room_path)
        confirm(client, "target", room_path)
        headers = {**auth(credential("target")), "Accept": "text/event-stream"}
        lines = Queue()

        def read_stream():
            try:
                with client.stream("GET", "/v1/events", headers=headers) as response:
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
        assert lines.get(timeout=3) == ": ready"
        admin(db_path, "disable", "bob")
        assert send(client, room_path, "wake").status_code == 200
        assert lines.get(timeout=3) is None
        reader.join(timeout=3)
        error(client.get("/v1/events", headers=headers), 401, "unauthorized")


def test_v0_database_upgrade_keeps_member_credentials_and_history(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    member_id = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(
            """
            PRAGMA user_version = 1;
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
                room_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
                rules TEXT NOT NULL, protocol TEXT NOT NULL
            );
            CREATE TABLE room_members (
                room_id TEXT NOT NULL REFERENCES rooms(room_id),
                member_id TEXT NOT NULL REFERENCES members(member_id),
                joined_after INTEGER NOT NULL, read_board_version INTEGER,
                acked_board_version INTEGER, PRIMARY KEY (room_id, member_id)
            );
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(room_id),
                sender_member_id TEXT NOT NULL REFERENCES members(member_id),
                client_message_id TEXT NOT NULL, text TEXT NOT NULL,
                mention_member_id TEXT REFERENCES members(member_id),
                UNIQUE (sender_member_id, client_message_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO members VALUES (?, 'v0-owner', 'old-harness', 'old-session', 'Old role', ?)",
            (member_id, sha256(sha256(b"legacy").digest()).digest()),
        )
        connection.execute(
            "INSERT INTO rooms VALUES (?, 'Legacy room', 1, 'Rules', 'Protocol')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, member_id)
        )
        connection.execute(
            "INSERT INTO messages VALUES (1, ?, ?, 'old-message', 'Retain history', NULL)",
            (room_id, member_id),
        )

    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path, legacy=False) as (client, _):
        assert member(client, "legacy") == member_id
        history = get(client, "legacy", f"/rooms/{room_id}/messages")
        assert history.status_code == 200
        assert history.json()["messages"][0]["text"] == "Retain history"
        assert (
            bind(client, login_token(client, "alice", PASSWORD), "sender").status_code
            == 200
        )
