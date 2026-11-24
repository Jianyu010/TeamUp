"""Stage 2 server contract tests: console operator auth, grants, and static hosting (#5).

See docs/modules/control-console.md (stage 2 scope) and
docs/modules/account-login-v1.md ("登录 purpose"). These tests cover the
console login purpose, bidirectional scope isolation, the operator grant
lifecycle through the local accounts CLI, the v2-to-v3 legacy session
migration, GET /v1/console/rooms metadata, and the Gateway static hosting
of the exact three bundled console assets.
"""

import sqlite3
import subprocess
import zipfile
from base64 import urlsafe_b64encode
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes
from time import time
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from test_contract import KEYS, auth, credential, error, get, join, live_server
from test_server_accounts import (
    NEW_PASSWORD,
    PASSWORD,
    account_command,
    admin,
    bind,
    enter_password,
    login,
    login_token,
    server_env,
    token_from_login,
)

CONSOLE_DIR = Path(__file__).parents[1] / "src" / "teamup_server" / "console"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def console_login(client, username, password):
    return client.post(
        "/v1/auth/login",
        json={"username": username, "password": password, "purpose": "console"},
    )


def console_token(client, username, password):
    return token_from_login(console_login(client, username, password))


def console_rooms(client, token):
    return client.get("/v1/console/rooms", headers=auth(token))


def test_console_purpose_login_and_bidirectional_scope_isolation(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        console = console_token(client, "alice", PASSWORD)
        enrollment = login_token(client, "alice", PASSWORD)
        assert console != enrollment
        assert bind(client, enrollment, "sender").status_code == 200

        for purpose in ("member", "enrollment", "", 4):
            response = client.post(
                "/v1/auth/login",
                json={"username": "alice", "password": PASSWORD, "purpose": purpose},
            )
            error(response, 422, "invalid_request")
            assert PASSWORD not in response.text
        response = client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": PASSWORD, "purpose": None},
        )
        assert response.status_code == 200, response.text

        error(console_rooms(client, console), 403, "forbidden")
        error(client.get("/v1/console/rooms"), 401, "unauthorized")
        error(console_rooms(client, "x" * 43), 401, "unauthorized")
        error(console_rooms(client, enrollment), 401, "unauthorized")
        error(console_rooms(client, KEYS["alice"]), 401, "unauthorized")
        error(console_rooms(client, credential("sender")), 401, "unauthorized")
        error(console_rooms(client, "not-a-valid-credential"), 401, "unauthorized")

        error(bind(client, console, "console-bound"), 401, "unauthorized")
        error(
            bind(client, enrollment, "as-member", member_token=console),
            422,
            "invalid_request",
        )
        error(client.get("/v1/members/me", headers=auth(console)), 401, "unauthorized")
        error(client.get("/v1/members", headers=auth(console)), 401, "unauthorized")
        error(client.get("/v1/inbox", headers=auth(console)), 401, "unauthorized")
        error(
            client.post("/v1/rooms", headers=auth(console), json={"name": "nope"}),
            401,
            "unauthorized",
        )
        error(
            client.get(
                "/v1/events", headers={**auth(console), "Accept": "text/event-stream"}
            ),
            401,
            "unauthorized",
        )


def test_console_read_grant_lifecycle_and_rooms_metadata(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        enrollment = login_token(client, "alice", PASSWORD)
        assert bind(client, enrollment, "sender").status_code == 200
        assert bind(client, enrollment, "target").status_code == 200
        created = {}
        for name in ("Alpha", "Beta"):
            response = client.post(
                "/v1/rooms", headers=auth(credential("sender")), json={"name": name}
            )
            assert response.status_code == 200, response.text
            created[response.json()["room_id"]] = name
        room_id = next(iter(created))
        join(client, "target", f"/rooms/{room_id}")

        console = console_token(client, "alice", PASSWORD)
        error(console_rooms(client, console), 403, "forbidden")
        admin(db_path, "grant-console", "alice")
        response = console_rooms(client, console)
        assert response.status_code == 200, response.text
        rooms = response.json()["rooms"]
        assert [entry["room_id"] for entry in rooms] == sorted(created)
        assert response.json() == {
            "rooms": [
                {
                    "room_id": entry["room_id"],
                    "name": created[entry["room_id"]],
                    "member_count": 1 + (entry["room_id"] == room_id),
                    "last_activity": None,
                }
                for entry in rooms
            ]
        }

        assert get(client, "sender", "/inbox").json() == {"events": []}
        assert get(client, "target", "/inbox").json() == {"events": []}
        sent = client.post(
            f"/v1/rooms/{room_id}/messages",
            headers=auth(credential("sender")),
            json={"client_message_id": "probe", "text": "probe"},
        )
        assert sent.status_code == 409, "board ack required before members send"

        admin(db_path, "revoke-console", "alice")
        admin(db_path, "revoke-console", "alice")
        error(console_rooms(client, console), 403, "forbidden")
        admin(db_path, "grant-console", "alice")
        admin(db_path, "grant-console", "alice")
        assert console_rooms(client, console).status_code == 200


def test_console_sessions_revoke_on_password_reset_and_disable(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    admin(db_path, "grant-console", "alice")
    with live_server(db_path) as (client, _):
        console = console_token(client, "alice", PASSWORD)
        assert console_rooms(client, console).status_code == 200

        enter_password(db_path, "reset-password", "alice", NEW_PASSWORD)
        error(console_rooms(client, console), 401, "unauthorized")
        error(login(client, "alice", PASSWORD), 401, "unauthorized")
        console = console_token(client, "alice", NEW_PASSWORD)
        assert console_rooms(client, console).status_code == 200

        admin(db_path, "disable", "alice")
        error(console_rooms(client, console), 401, "unauthorized")
        error(console_login(client, "alice", NEW_PASSWORD), 401, "unauthorized")


def test_transcript_grant_cli_roundtrip(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        enrollment = login_token(client, "alice", PASSWORD)
        assert bind(client, enrollment, "sender").status_code == 200
        room_id = client.post(
            "/v1/rooms", headers=auth(credential("sender")), json={"name": "Grants"}
        ).json()["room_id"]

        for _ in range(2):
            admin(db_path, "grant-transcript", "alice", room_id)
        with closing(sqlite3.connect(db_path)) as connection:
            grants = connection.execute(
                "SELECT owner_id, scope, room_id FROM operator_grants"
            ).fetchall()
        assert len(grants) == 1
        owner_id = grants[0][0]
        assert grants[0][1] == "transcript_read"
        assert grants[0][2] == room_id

        for _ in range(2):
            admin(db_path, "revoke-transcript", "alice", room_id)
        with closing(sqlite3.connect(db_path)) as connection:
            assert (
                connection.execute("SELECT 1 FROM operator_grants").fetchone() is None
            )
            account_row = connection.execute(
                "SELECT owner_id FROM accounts WHERE username = 'alice'"
            ).fetchone()
        assert account_row is not None
        assert owner_id == account_row[0]

    for args in (
        ("grant-transcript", "alice", "not-a-uuid"),
        ("grant-transcript", "alice", "3cb9cbf2-aaf5-459b-a69f-4e256c300454"),
        ("grant-transcript", "nobody", room_id),
        ("revoke-transcript", "nobody", room_id),
        ("grant-console", "nobody"),
        ("revoke-console", "nobody"),
    ):
        result = subprocess.run(
            account_command(db_path, *args),
            env=server_env(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode != 0, args
        assert result.stdout == ""


def test_v2_sessions_migrate_to_enrollment_purpose(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    owner_id = str(uuid4())
    raw = token_bytes(32)
    legacy_token = urlsafe_b64encode(raw).decode().rstrip("=")
    legacy_digest = sha256(raw).digest()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(
            """
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
            CREATE INDEX pending_inbox ON inbox_events (member_id, event_id) WHERE acknowledged = 0;
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
        )
        connection.execute(
            "INSERT INTO accounts (owner_id, username, password_digest) VALUES (?, 'alice', ?)",
            (owner_id, PasswordHasher().hash(PASSWORD)),
        )
        connection.execute(
            "INSERT INTO account_sessions VALUES (?, ?, ?)",
            (legacy_digest, owner_id, int(time()) + 3600),
        )

    with live_server(db_path, legacy=False) as (client, _):
        assert bind(client, legacy_token, "sender").status_code == 200
        error(console_rooms(client, legacy_token), 401, "unauthorized")
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
            purposes = dict(
                connection.execute("SELECT token_digest, purpose FROM account_sessions")
            )
        assert purposes[legacy_digest] == "enrollment"

        admin(db_path, "grant-console", "alice")
        console = console_token(client, "alice", PASSWORD)
        response = console_rooms(client, console)
        assert response.status_code == 200, response.text
        assert response.json() == {"rooms": []}
        assert bind(client, legacy_token, "sender").status_code == 200


def test_console_static_assets_serve_exact_bundled_files(tmp_path):
    with live_server(tmp_path / "team.sqlite3") as (client, _):
        for path, name, media_type in (
            ("/console/", "index.html", "text/html; charset=utf-8"),
            ("/console/console.css", "console.css", "text/css; charset=utf-8"),
            ("/console/console.js", "console.js", "text/javascript; charset=utf-8"),
        ):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == media_type
            assert response.headers["content-security-policy"] == CSP
            assert "unsafe-inline" not in response.headers["content-security-policy"]
            assert "unsafe-eval" not in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["cache-control"] == "no-cache"
            assert "set-cookie" not in response.headers
            assert response.content == (CONSOLE_DIR / name).read_bytes()
        html = (CONSOLE_DIR / "index.html").read_text(encoding="utf-8")
        assert 'href="console.css"' in html
        assert 'src="console.js"' in html

        for path in (
            "/console",
            "/console/index.html",
            "/console/other.js",
            "/console/console.css/",
            "/console/sub/",
            "/console/%2e%2e/pyproject.toml",
            "/console/../server/pyproject.toml",
        ):
            error(client.get(path), 404, "not_found")
        error(client.post("/console/"), 405, "invalid_request")
        error(client.post("/console/console.js"), 405, "invalid_request")


def test_console_static_package_ships_in_built_wheel(tmp_path):
    pytest.importorskip("hatchling", reason="Building the wheel requires hatchling")
    from hatchling.builders.wheel import WheelBuilder

    wheel = Path(WheelBuilder(str(Path(__file__).parents[1])).build(str(tmp_path)))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for name in ("index.html", "console.css", "console.js"):
        assert f"teamup_server/console/{name}" in names
