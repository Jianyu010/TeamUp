"""Server contract tests for the Control Console read paths (#5).

See docs/modules/control-console.md. Stage 2 covered console login purpose,
bidirectional scope isolation, the operator grant lifecycle through the local
accounts CLI, the v2 legacy session migration, GET /v1/console/rooms
metadata, and the Gateway static hosting of the exact three bundled console
assets. Stage 3 (this file's additions) covers the roster detail route, the
body-free Timeline projection with backfill, the Chats transcript route with
in-transaction audit, scoped transcript grants, revocation taking effect on
the next page, pagination, and Cache-Control: no-store on authorized reads.

After merging the #3 direct messages server, these tests additionally pin
the console migration layering (console on top of the DM v4 schema) for
every supported start -- including the published #5 auth preview v3
(e495544), whose purposes, sessions, grants and room data must survive --
and the rule that DM rows never reach room activity, Timeline, or the
operator transcript even while DMs are enabled. After merging the #6 room
and DM expiry runtime, the schema composes to v6 by adding both retention
watermarks in place, and one test drives the real --room-retention-hours
expiry path against the authenticated console from a published Console v5
start: bodies and outstanding deliveries clear, Timeline keeps its
body-free activities without message links, audits and the DM family stay
untouched.
"""

import json
import sqlite3
import subprocess
import zipfile
from base64 import urlsafe_b64encode
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes
from time import monotonic, sleep, time
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
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
    post,
    send,
)
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


def console_get(client, token, path):
    return client.get(f"/v1/console{path}", headers=auth(token))


def console_room_detail(client, token, room_id):
    return console_get(client, token, f"/rooms/{room_id}")


def console_timeline(client, token, room_id, *, after=0, limit=50):
    return console_get(
        client, token, f"/rooms/{room_id}/timeline?after={after}&limit={limit}"
    )


def console_transcript(client, token, room_id, *, before=None, limit=50):
    query = f"limit={limit}"
    if before is not None:
        query += f"&before={before}"
    return console_get(client, token, f"/rooms/{room_id}/transcript?{query}")


def console_team_room(client, *, name="Console room", messages=(), prefix="key"):
    """Create a room as bound member "sender", confirm, and return its id.

    Binds member "target" as well so tests can join it into the room. Message
    retry keys are "<prefix>-<index>" because a sender's retry key is unique
    across rooms.
    """
    enrollment = login_token(client, "alice", PASSWORD)
    for label in ("sender", "target"):
        assert bind(client, enrollment, label).status_code == 200
    room_id = client.post(
        "/v1/rooms", headers=auth(credential("sender")), json={"name": name}
    ).json()["room_id"]
    room_path = f"/rooms/{room_id}"
    confirm(client, "sender", room_path)
    for index, text in enumerate(messages):
        assert (
            send(client, room_path, f"{prefix}-{index}", text=text).status_code == 200
        )
    return room_id


def audit_rows(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT owner_id, room_id, action, at, page_before, page_limit
                FROM transcript_access_audit
                """
            )
        ]


def room_activity_rows(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT activity_id, room_id, kind, at, actor_member_id, message_id
                FROM room_activity ORDER BY activity_id
                """
            )
        ]


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
        assert response.headers["cache-control"] == "no-store"
        rooms = response.json()["rooms"]
        assert [entry["room_id"] for entry in rooms] == sorted(created)
        for entry in rooms:
            assert set(entry) == {"room_id", "name", "member_count", "last_activity"}
            assert entry["name"] == created[entry["room_id"]]
            assert entry["member_count"] == 1 + (entry["room_id"] == room_id)
            activity = entry["last_activity"]
            assert activity is not None
            assert set(activity) == {"kind", "at", "message_id"}
            assert activity["kind"] == "member_joined"
            assert isinstance(activity["at"], int) and activity["at"] > 0
            assert activity["message_id"] is None

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
        ("revoke-transcript", "alice", "3cb9cbf2-aaf5-459b-a69f-4e256c300454"),
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
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    late_member = "6f6d3b93-2c2e-4d5f-9a54-6d0f4f65ab21"
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

    with live_server(db_path, legacy=False) as (client, _):
        assert bind(client, legacy_token, "sender").status_code == 200
        error(console_rooms(client, legacy_token), 401, "unauthorized")
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            purposes = dict(
                connection.execute("SELECT token_digest, purpose FROM account_sessions")
            )
        assert purposes[legacy_digest] == "enrollment"

        admin(db_path, "grant-console", "alice")
        console = console_token(client, "alice", PASSWORD)
        response = console_rooms(client, console)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "rooms": [
                {
                    "room_id": room_id,
                    "name": "Legacy room",
                    "member_count": 2,
                    "last_activity": {"kind": "message", "at": None, "message_id": 42},
                }
            ]
        }
        timeline = console_timeline(client, console, room_id)
        activities = timeline.json()["activities"]
        assert [activity["kind"] for activity in activities] == [
            "member_joined",
            "member_joined",
            "board_acknowledged",
            "board_acknowledged",
            "message",
            "message",
        ]
        assert [activity["at"] for activity in activities] == [None] * 6
        assert [activity["message_id"] for activity in activities] == [
            None,
            None,
            None,
            None,
            41,
            42,
        ]
        assert [activity["actor_member_id"] for activity in activities] == [
            old_member,
            late_member,
            late_member,
            old_member,
            old_member,
            old_member,
        ]
        assert "First old" not in timeline.text and "Second old" not in timeline.text
        admin(db_path, "grant-transcript", "alice", room_id)
        page = console_transcript(client, console, room_id).json()
        assert page == {
            "messages": [
                {
                    "message_id": 41,
                    "room_id": room_id,
                    "sender_member_id": old_member,
                    "sender_role_description": "Old role",
                    "text": "First old",
                    "mention_member_id": None,
                    "created_at": None,
                },
                {
                    "message_id": 42,
                    "room_id": room_id,
                    "sender_member_id": old_member,
                    "sender_role_description": "Old role",
                    "text": "Second old",
                    "mention_member_id": None,
                    "created_at": None,
                },
            ],
            "has_more": False,
        }
        assert len(audit_rows(db_path)) == 1
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

    builder = WheelBuilder(str(Path(__file__).parents[1]))
    (wheel,) = builder.build(directory=str(tmp_path), versions=["standard"])
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for name in ("index.html", "console.css", "console.js"):
        assert f"teamup_server/console/{name}" in names


def test_console_room_detail_roster_and_transcript_gate(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        enrollment = login_token(client, "alice", PASSWORD)
        assert bind(client, enrollment, "target").status_code == 200
        room_id = console_team_room(client)
        room_path = f"/rooms/{room_id}"
        join(client, "target", room_path)
        for label in ("sender", "target"):
            confirm(client, label, room_path)
        target = member(client, "target")
        sender = member(client, "sender")
        sent = send(client, room_path, "detail-1", text="roster body").json()
        events = get(client, "target", "/inbox").json()["events"]
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )

        console = console_token(client, "alice", PASSWORD)
        error(console_room_detail(client, console, room_id), 403, "forbidden")
        error(
            console_room_detail(client, credential("sender"), room_id),
            401,
            "unauthorized",
        )
        error(
            console_room_detail(client, console, "not-a-uuid"), 422, "invalid_request"
        )
        admin(db_path, "grant-console", "alice")

        detail = console_room_detail(client, console, room_id)
        assert detail.status_code == 200, detail.text
        assert detail.headers["cache-control"] == "no-store"
        payload = detail.json()
        assert set(payload) == {"room", "members", "transcript_allowed"}
        assert payload["transcript_allowed"] is False
        room = payload["room"]
        assert set(room) == {"room_id", "name", "member_count", "last_activity"}
        assert room["room_id"] == room_id
        assert room["name"] == "Console room"
        assert room["member_count"] == 2
        last = room["last_activity"]
        assert set(last) == {"kind", "at", "message_id"}
        assert last["kind"] == "delivery"
        assert isinstance(last["at"], int) and abs(last["at"] - int(time())) < 600
        assert last["message_id"] == sent["message_id"]

        members = payload["members"]
        assert [entry["member_id"] for entry in members] == sorted(
            entry["member_id"] for entry in members
        )
        roles = {entry["role_description"]: entry for entry in members}
        assert set(roles) == {"sender", "target"}
        assert roles["target"]["member_id"] == target
        assert roles["sender"]["member_id"] == sender
        for entry in members:
            assert set(entry) == {
                "member_id",
                "role_description",
                "board_state",
                "last_activity",
                "harness_state",
            }
            assert entry["board_state"] == "acknowledged"
            assert entry["harness_state"] == "unknown"
            activity = entry["last_activity"]
            assert set(activity) == {"kind", "at", "message_id"}
            assert isinstance(activity["at"], int) and activity["at"] > 0
        assert roles["target"]["last_activity"]["kind"] == "delivery"
        assert roles["target"]["last_activity"]["message_id"] == sent["message_id"]
        assert roles["sender"]["last_activity"]["kind"] == "message"
        assert roles["sender"]["last_activity"]["message_id"] == sent["message_id"]

        admin(db_path, "grant-transcript", "alice", room_id)
        assert console_room_detail(client, console, room_id).json()[
            "transcript_allowed"
        ]
        error(console_room_detail(client, console, str(uuid4())), 404, "not_found")
        # Authorization is re-checked before room existence on every request.
        admin(db_path, "revoke-console", "alice")
        error(console_room_detail(client, console, str(uuid4())), 403, "forbidden")


def test_console_timeline_is_body_free_metadata_with_stable_paging(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        room_id = console_team_room(
            client, messages=("secret one", "secret two", "secret three")
        )
        room_path = f"/rooms/{room_id}"
        join(client, "target", room_path)
        confirm(client, "target", room_path)
        assert send(client, room_path, "key-3", text="secret four").status_code == 200
        events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"]["message_id"] for event in events] == [4]
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        sender = member(client, "sender")
        target = member(client, "target")

        console = console_token(client, "alice", PASSWORD)
        error(console_timeline(client, console, room_id), 403, "forbidden")
        error(
            console_timeline(client, credential("sender"), room_id), 401, "unauthorized"
        )
        admin(db_path, "grant-console", "alice")
        error(console_timeline(client, console, str(uuid4())), 404, "not_found")

        full = console_timeline(client, console, room_id, limit=100)
        assert full.status_code == 200, full.text
        assert full.headers["cache-control"] == "no-store"
        activities = full.json()["activities"]
        assert [activity["kind"] for activity in activities] == [
            "member_joined",
            "board_acknowledged",
            "message",
            "message",
            "message",
            "member_joined",
            "board_acknowledged",
            "message",
            "delivery",
        ]
        expected_actors = [
            sender,
            sender,
            sender,
            sender,
            sender,
            target,
            target,
            sender,
            target,
        ]
        assert [activity["actor_member_id"] for activity in activities] == (
            expected_actors
        )
        for activity in activities:
            assert set(activity) == {
                "activity_id",
                "room_id",
                "kind",
                "at",
                "message_id",
                "actor_member_id",
            }
            assert activity["room_id"] == room_id
            assert isinstance(activity["at"], int) and activity["at"] > 0
        assert [activity["message_id"] for activity in activities] == [
            None,
            None,
            1,
            2,
            3,
            None,
            None,
            4,
            4,
        ]
        all_ids = [activity["activity_id"] for activity in activities]
        assert all_ids == sorted(all_ids)
        assert len(set(all_ids)) == len(all_ids)
        assert "secret" not in full.text

        first = console_timeline(client, console, room_id, after=0, limit=4).json()[
            "activities"
        ]
        assert [activity["activity_id"] for activity in first] == all_ids[:4]
        second = console_timeline(
            client, console, room_id, after=first[-1]["activity_id"]
        ).json()["activities"]
        assert [activity["activity_id"] for activity in second] == all_ids[4:]
        assert len(second) < 50

        # Idempotent retries of join, board ack, send, and delivery ack add no
        # duplicate projection rows.
        join(client, "target", room_path)
        confirm(client, "target", room_path)
        assert send(client, room_path, "key-0", text="secret one").status_code == 200
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        retried = console_timeline(client, console, room_id, limit=100).json()
        assert retried == {"activities": activities}

        for query in ("after=-1", "limit=0", "limit=101", "after=abc"):
            error(
                console_get(client, console, f"/rooms/{room_id}/timeline?{query}"),
                422,
                "invalid_request",
            )


def test_console_timeline_keeps_events_when_message_expiry_clears_links(tmp_path):
    """#6 expiry may delete a message; the body-free Timeline event survives.

    See docs/modules/control-console.md: room_activity.message_id keeps its
    foreign key but clears to NULL (ON DELETE SET NULL) when #6's cleanup
    removes an expired message after its inbox references, and the CHECK
    permits that null link for message and delivery activities only; join
    and board-ack activities still require NULL, and live messages keep
    their links. The real runtime expiry path (the maintenance loop under
    --room-retention-hours) is covered by the Console v5 upgrade test below;
    this one pins the FK and CHECK semantics with direct test-only SQL on the
    temporary fixture with foreign keys enforced.
    """
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        room_id = console_team_room(client, messages=("old body",), prefix="soon")
        room_path = f"/rooms/{room_id}"
        join(client, "target", room_path)
        confirm(client, "target", room_path)
        expiring = send(client, room_path, "gone-1", text="expiring body").json()
        survivor = send(client, room_path, "kept-1", text="kept body").json()
        events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"]["message_id"] for event in events] == [
            expiring["message_id"],
            survivor["message_id"],
        ]
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        sender = member(client, "sender")
        target = member(client, "target")

        console = console_token(client, "alice", PASSWORD)
        admin(db_path, "grant-console", "alice")
        live = console_timeline(client, console, room_id, limit=100).json()[
            "activities"
        ]
        assert [(a["kind"], a["message_id"]) for a in live] == [
            ("member_joined", None),
            ("board_acknowledged", None),
            ("message", 1),
            ("member_joined", None),
            ("board_acknowledged", None),
            ("message", expiring["message_id"]),
            ("message", survivor["message_id"]),
            ("delivery", expiring["message_id"]),
        ]
        assert [a["actor_member_id"] for a in live] == [
            sender,
            sender,
            sender,
            target,
            target,
            sender,
            sender,
            target,
        ]

        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            # While the inbox still references it, the message delete is
            # blocked exactly as #6's cleanup must account for.
            with pytest.raises(sqlite3.IntegrityError), connection:
                connection.execute(
                    "DELETE FROM messages WHERE message_id = ?",
                    (expiring["message_id"],),
                )
            # Join and board-ack activities keep requiring a null link.
            for kind in ("member_joined", "board_acknowledged"):
                with pytest.raises(sqlite3.IntegrityError), connection:
                    connection.execute(
                        """
                        INSERT INTO room_activity (
                            room_id, kind, at, actor_member_id, message_id
                        ) VALUES (?, ?, NULL, ?, ?)
                        """,
                        (room_id, kind, target, survivor["message_id"]),
                    )
            # #6's cleanup transaction: drop the delivery references, then
            # the expired message itself.
            with connection:
                connection.execute(
                    "DELETE FROM inbox_events WHERE message_id = ?",
                    (expiring["message_id"],),
                )
                connection.execute(
                    "DELETE FROM messages WHERE message_id = ?",
                    (expiring["message_id"],),
                )
            assert (
                connection.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?",
                    (expiring["message_id"],),
                ).fetchone()
                is None
            )

        expired = console_timeline(client, console, room_id, limit=100).json()[
            "activities"
        ]
        assert [
            (a["activity_id"], a["kind"], a["at"], a["actor_member_id"])
            for a in expired
        ] == [
            (a["activity_id"], a["kind"], a["at"], a["actor_member_id"]) for a in live
        ]
        assert [a["message_id"] for a in expired] == [
            None,
            None,
            1,
            None,
            None,
            None,
            survivor["message_id"],
            None,
        ]

        # Live messages are untouched: the survivor keeps its pending inbox
        # event and its link, and members can still send.
        assert [
            event["message"]["message_id"]
            for event in get(client, "target", "/inbox").json()["events"]
        ] == [survivor["message_id"]]
        kept = send(client, room_path, "kept-2", text="after expiry").json()
        tail = console_timeline(client, console, room_id, limit=100).json()[
            "activities"
        ][-1]
        assert (tail["kind"], tail["message_id"]) == ("message", kept["message_id"])


def test_console_v5_upgrade_runs_real_room_expiry_and_keeps_dm_and_audit(tmp_path):
    """The published Console v5 start meets the real #6 expiry runtime.

    The fixture is downgraded to the published v5 shape (the v6 schema minus
    both retention watermarks) and relaunched under --room-retention-hours:
    the database upgrades in place to v6, the startup retention pass commits
    the expired room batch, and the authenticated console verifies each side
    of the composition -- the login, grants and roster survive, the room body
    and its outstanding delivery are cleared, the body-free Timeline
    activities keep their metadata with null message links, the transcript
    read still writes a body-free audit, and the DM family stays
    participant-only and unexpired.
    """
    db_path = tmp_path / "team.sqlite3"
    old = "2020-01-01T00:00:00Z"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        room_id = console_team_room(client, prefix="expiry")
        room_path = f"/rooms/{room_id}"
        join(client, "target", room_path)
        confirm(client, "target", room_path)
        acked = send(client, room_path, "acked-1", text="expiring acked body").json()
        events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"]["message_id"] for event in events] == [
            acked["message_id"]
        ]
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        outstanding = send(
            client, room_path, "pending-1", text="expiring outstanding body"
        ).json()
        (pending,) = [
            event["event_id"]
            for event in get(client, "target", "/inbox").json()["events"]
        ]
        dm = post(
            client, "sender", "/dms", json={"member_id": member(client, "target")}
        ).json()
        dm_message = post(
            client,
            "sender",
            f"/dms/{dm['dm_id']}/messages",
            json={"client_message_id": "dm-1", "text": "private dm body"},
        ).json()
        sender = member(client, "sender")
        target = member(client, "target")
        with closing(sqlite3.connect(db_path)) as connection:
            owner_id = connection.execute(
                "SELECT owner_id FROM accounts WHERE username = 'alice'"
            ).fetchone()[0]

        admin(db_path, "grant-console", "alice")
        admin(db_path, "grant-transcript", "alice", room_id)
        console = console_token(client, "alice", PASSWORD)
        before = console_timeline(client, console, room_id, limit=100).json()[
            "activities"
        ]
        assert [(activity["kind"], activity["message_id"]) for activity in before] == [
            ("member_joined", None),
            ("board_acknowledged", None),
            ("member_joined", None),
            ("board_acknowledged", None),
            ("message", acked["message_id"]),
            ("delivery", acked["message_id"]),
            ("message", outstanding["message_id"]),
        ]
        assert audit_rows(db_path) == []

    # Recreate the published Console v5 start before multi-targets and watermarks, with
    # the room bodies aged past the retention cutoff while the DM keeps now.
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE messages SET created_at = ? WHERE room_id IS NOT NULL", (old,)
        )
        connection.execute("DROP TABLE room_message_additional_mentions")
        connection.execute("DROP TABLE retired_room_access")
        connection.execute("DROP TABLE room_departures")
        connection.execute("ALTER TABLE room_members DROP COLUMN membership_id")
        connection.execute("ALTER TABLE members DROP COLUMN retired")
        connection.execute("ALTER TABLE rooms DROP COLUMN expired_through_message_id")
        connection.execute("ALTER TABLE dms DROP COLUMN expired_through_message_id")
        connection.execute("PRAGMA user_version = 5")
        assert "expired_through_message_id" not in [
            row[1] for row in connection.execute("PRAGMA table_info(rooms)")
        ]

    with live_server(db_path, extra=("--room-retention-hours", "1")) as (client, _):
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11

        # The startup retention pass commits: the room body is gone and the
        # room's durable watermark reports the cleared range.
        deadline = monotonic() + 10
        history = get(client, "sender", f"{room_path}/messages").json()
        while "expired_through_message_id" not in history:
            assert monotonic() < deadline, "The room expiry pass did not commit"
            sleep(0.2)
            history = get(client, "sender", f"{room_path}/messages").json()
        assert history == {
            "messages": [],
            "expired_through_message_id": outstanding["message_id"],
            "history_truncated": True,
        }

        # The outstanding delivery is cleared with the body: the member inbox
        # no longer offers the event and its acknowledgement reports the
        # expired event's not_found.
        assert get(client, "target", "/inbox").json() == {"events": []}
        error(post(client, "target", f"/inbox/{pending}/ack"), 404, "not_found")

        # The authenticated console keeps its state across the v5 -> v6
        # upgrade and the expiry: grants, roster, and the rooms projection
        # keep the latest activity with its message link cleared.
        console = console_token(client, "alice", PASSWORD)
        detail = console_room_detail(client, console, room_id).json()
        assert detail["room"] == {
            "room_id": room_id,
            "name": "Console room",
            "member_count": 2,
            "last_activity": {
                "kind": "message",
                "at": before[-1]["at"],
                "message_id": None,
            },
        }
        assert detail["transcript_allowed"] is True
        assert [entry["member_id"] for entry in detail["members"]] == sorted(
            (sender, target)
        )
        assert [entry["board_state"] for entry in detail["members"]] == [
            "acknowledged",
            "acknowledged",
        ]
        assert console_rooms(client, console).json() == {
            "rooms": [
                {
                    "room_id": room_id,
                    "name": "Console room",
                    "member_count": 2,
                    "last_activity": {
                        "kind": "message",
                        "at": before[-1]["at"],
                        "message_id": None,
                    },
                }
            ]
        }

        # Timeline keeps every activity row with kind, at and actor intact;
        # only the links to the expired messages are cleared.
        after = console_timeline(client, console, room_id, limit=100).json()[
            "activities"
        ]
        assert [
            (a["activity_id"], a["kind"], a["at"], a["actor_member_id"])
            for a in after
        ] == [
            (a["activity_id"], a["kind"], a["at"], a["actor_member_id"])
            for a in before
        ]
        assert [a["message_id"] for a in after] == [None] * len(before)

        # The transcript body scope holds: the room text is unreadable and
        # the read still writes exactly one body-free audit row.
        transcript = console_transcript(client, console, room_id).json()
        assert transcript == {"messages": [], "has_more": False}
        (audit,) = audit_rows(db_path)
        assert audit["owner_id"] == owner_id
        assert audit["room_id"] == room_id
        assert audit["action"] == "transcript_read"
        assert audit["page_before"] is None
        assert audit["page_limit"] == 50
        assert audit["at"].endswith("Z")
        assert "expiring" not in str(audit) and "private" not in str(audit)

        # The DM family is untouched by the room expiry: both participants
        # keep the exact pre-expiry response shape, and no console response
        # carries the DM body.
        assert get(client, "target", f"/dms/{dm['dm_id']}/messages").json() == {
            "messages": [dm_message]
        }
        for payload in (
            console_rooms(client, console).json(),
            detail,
            after,
            transcript,
        ):
            assert "private dm body" not in json.dumps(payload)


def test_console_transcript_pages_scoped_grants_audit_and_revocation(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        enrollment = login_token(client, "alice", PASSWORD)
        assert bind(client, enrollment, "target").status_code == 200
        room_a = console_team_room(
            client,
            name="Room A",
            messages=("alpha one", "alpha two", "alpha three"),
            prefix="alpha",
        )
        room_b = console_team_room(
            client, name="Room B", messages=("beta one",), prefix="beta"
        )
        join(client, "target", f"/rooms/{room_a}")
        confirm(client, "target", f"/rooms/{room_a}")
        for event in get(client, "target", "/inbox").json()["events"]:
            assert (
                post(client, "target", f"/inbox/{event['event_id']}/ack").status_code
                == 200
            )
        assert get(client, "target", "/inbox").json() == {"events": []}
        sender = member(client, "sender")

        console = console_token(client, "alice", PASSWORD)
        admin(db_path, "grant-console", "alice")
        error(console_transcript(client, console, room_a), 403, "forbidden")
        error(console_transcript(client, console, room_b), 403, "forbidden")
        error(console_transcript(client, "x" * 43, room_a), 401, "unauthorized")
        assert audit_rows(db_path) == []

        admin(db_path, "grant-transcript", "alice", room_a)
        full = console_transcript(client, console, room_a)
        assert full.status_code == 200, full.text
        assert full.headers["cache-control"] == "no-store"
        page = full.json()
        assert set(page) == {"messages", "has_more"}
        assert page["has_more"] is False
        messages = page["messages"]
        assert [message["message_id"] for message in messages] == [1, 2, 3]
        assert [message["text"] for message in messages] == [
            "alpha one",
            "alpha two",
            "alpha three",
        ]
        for message in messages:
            assert set(message) == {
                "message_id",
                "room_id",
                "sender_member_id",
                "sender_role_description",
                "text",
                "mention_member_id",
                "created_at",
            }
            assert message["room_id"] == room_a
            assert message["sender_member_id"] == sender
            assert message["sender_role_description"] == "sender"
            assert message["mention_member_id"] is None
            assert isinstance(message["created_at"], int) and message["created_at"] > 0
        alpha_one = messages[0]

        latest = console_transcript(client, console, room_a, limit=2).json()
        assert [message["message_id"] for message in latest["messages"]] == [2, 3]
        assert latest["has_more"] is True
        earlier = console_transcript(client, console, room_a, before=2, limit=2).json()
        assert earlier == {"messages": [alpha_one], "has_more": False}
        empty = console_transcript(client, console, room_a, before=1, limit=2).json()
        assert empty == {"messages": [], "has_more": False}

        owner_id = None
        with closing(sqlite3.connect(db_path)) as connection:
            owner_id = connection.execute(
                "SELECT owner_id FROM accounts WHERE username = 'alice'"
            ).fetchone()[0]
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(transcript_access_audit)"
                )
            ]
        assert columns == [
            "owner_id",
            "room_id",
            "action",
            "at",
            "page_before",
            "page_limit",
        ]
        rows = audit_rows(db_path)
        assert len(rows) == 4
        assert all(
            row["owner_id"] == owner_id
            and row["room_id"] == room_a
            and row["action"] == "transcript_read"
            for row in rows
        )
        assert [row["page_limit"] for row in rows] == [50, 2, 2, 2]
        assert [row["page_before"] for row in rows] == [None, None, 2, 1]
        assert all(row["at"].endswith("Z") for row in rows)
        assert all("alpha" not in str(value) for row in rows for value in row.values())

        # Room-scoped grants: a transcript grant for room A does not open room B.
        error(console_transcript(client, console, room_b), 403, "forbidden")
        assert len(audit_rows(db_path)) == 4
        admin(db_path, "grant-transcript", "alice", room_b)
        beta = console_transcript(client, console, room_b).json()
        assert [message["text"] for message in beta["messages"]] == ["beta one"]
        assert len(audit_rows(db_path)) == 5
        admin(db_path, "revoke-transcript", "alice", room_b)
        error(console_transcript(client, console, room_b, before=1), 403, "forbidden")

        # Revocation takes effect for the next page, even with an old cursor.
        admin(db_path, "revoke-transcript", "alice", room_a)
        error(console_transcript(client, console, room_a, before=2), 403, "forbidden")
        error(console_transcript(client, console, room_a), 403, "forbidden")
        assert len(audit_rows(db_path)) == 5
        admin(db_path, "revoke-console", "alice")
        error(console_transcript(client, console, room_a), 403, "forbidden")
        admin(db_path, "grant-console", "alice")

        # Console reads are read-only for tenant data: no inbox events, no SSE
        # prompts, no board changes, and members keep sending.
        assert get(client, "target", "/inbox").json() == {"events": []}
        assert get(client, "target", f"/rooms/{room_a}/board").json()["version"] == 1
        assert (
            send(
                client, f"/rooms/{room_a}", "after-reads", text="still writable"
            ).status_code
            == 200
        )

        admin(db_path, "disable", "alice")
        error(console_transcript(client, console, room_a), 401, "unauthorized")
        error(console_rooms(client, console), 401, "unauthorized")
        error(console_timeline(client, console, room_a), 401, "unauthorized")


def test_console_transcript_before_above_sqlite_max_is_an_unbounded_upper_edge(
    tmp_path,
):
    """An oversized positive before cursor is an open upper bound, not a 500.

    The member history endpoint already treats a before above SQLite's 64-bit
    integer maximum as an unbounded edge that keeps the legitimate maximum ID
    eligible; the console transcript route does the same instead of failing
    the SQLite binding with an unhandled OverflowError. The room grant still
    gates such requests first, and the audit row records the effective bound
    (null) rather than the oversized cursor.
    """
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        room_id = console_team_room(
            client, messages=("edge one", "edge two"), prefix="edge"
        )
        console = console_token(client, "alice", PASSWORD)
        admin(db_path, "grant-console", "alice")
        # The room grant gates oversized cursors before any SQL runs.
        error(
            console_transcript(client, console, room_id, before=2**70),
            403,
            "forbidden",
        )
        assert audit_rows(db_path) == []
        admin(db_path, "grant-transcript", "alice", room_id)

        latest = console_transcript(client, console, room_id).json()
        assert [message["text"] for message in latest["messages"]] == [
            "edge one",
            "edge two",
        ]
        assert latest["has_more"] is False
        # Oversized positive cursors page the latest window unbounded; the
        # largest representable SQLite ID stays a normal exclusive bound.
        for before in (2**63 - 1, 2**63, 2**70, 2**70 + 5):
            response = console_transcript(client, console, room_id, before=before)
            assert response.status_code == 200, response.text
            assert response.json() == latest
        rows = audit_rows(db_path)
        assert [row["page_before"] for row in rows] == [
            None,
            2**63 - 1,
            None,
            None,
            None,
        ]
        assert all(row["page_limit"] == 50 for row in rows)
        assert all("edge" not in str(value) for row in rows for value in row.values())


def test_console_transcript_missing_room_resolves_after_console_read(tmp_path):
    """Room existence is resolved after console_read, before transcript_read.

    An operator holding console_read already sees the full room list, so an
    absent room answers 404 not_found on the transcript route like roster
    and timeline, instead of the room-scoped transcript_read 403 answering
    first. Accounts without console_read still get 403 for an unknown room
    (no existence leak), invalid credentials still get 401, an existing
    room without the room-scoped grant stays 403, and only the successful
    read writes an audit row.
    """
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        room_id = console_team_room(client, messages=("kept one",), prefix="kept")
        console = console_token(client, "alice", PASSWORD)
        missing = str(uuid4())

        # Without console_read the account-level check answers first, even
        # for a room that does not exist.
        error(console_transcript(client, console, missing), 403, "forbidden")
        error(console_transcript(client, "x" * 43, missing), 401, "unauthorized")
        assert audit_rows(db_path) == []

        admin(db_path, "grant-console", "alice")
        error(console_transcript(client, console, missing), 404, "not_found")
        error(console_transcript(client, console, room_id), 403, "forbidden")
        assert audit_rows(db_path) == []

        admin(db_path, "grant-transcript", "alice", room_id)
        page = console_transcript(client, console, room_id).json()
        assert [message["text"] for message in page["messages"]] == ["kept one"]
        rows = audit_rows(db_path)
        assert [row["room_id"] for row in rows] == [room_id]


def test_console_room_detail_latest_activity_per_actor_and_room(tmp_path):
    """Roster detail derives the room's and each member's latest activity.

    The latest rows are aggregated in SQL -- one per actor plus the room's
    overall latest -- instead of folding the whole activity history in
    Python; the payload shape, field sets, and member ordering stay exactly
    as published, and a room without recorded activity reports nulls.
    """
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        enrollment = login_token(client, "alice", PASSWORD)
        assert bind(client, enrollment, "reader").status_code == 200
        room_id = console_team_room(client)
        room_path = f"/rooms/{room_id}"
        join(client, "target", room_path)
        join(client, "reader", room_path)
        for label in ("target", "reader"):
            confirm(client, label, room_path)
        first = send(client, room_path, "sql-0", text="first body").json()
        events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"]["message_id"] for event in events] == [
            first["message_id"]
        ]
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        second = send(client, room_path, "sql-1", text="second body").json()
        sender = member(client, "sender")
        target = member(client, "target")
        reader = member(client, "reader")

        console = console_token(client, "alice", PASSWORD)
        admin(db_path, "grant-console", "alice")
        payload = console_room_detail(client, console, room_id).json()
        assert set(payload) == {"room", "members", "transcript_allowed"}
        assert payload["transcript_allowed"] is False
        room = payload["room"]
        assert (room["room_id"], room["name"], room["member_count"]) == (
            room_id,
            "Console room",
            3,
        )
        assert set(room["last_activity"]) == {"kind", "at", "message_id"}
        assert isinstance(room["last_activity"]["at"], int) and (
            room["last_activity"]["at"] > 0
        )
        assert (room["last_activity"]["kind"], room["last_activity"]["message_id"]) == (
            "message",
            second["message_id"],
        )

        members = payload["members"]
        assert [entry["member_id"] for entry in members] == sorted(
            (sender, target, reader)
        )
        for entry in members:
            assert set(entry) == {
                "member_id",
                "role_description",
                "board_state",
                "last_activity",
                "harness_state",
            }
            assert entry["board_state"] == "acknowledged"
            assert entry["harness_state"] == "unknown"
            assert set(entry["last_activity"]) == {"kind", "at", "message_id"}
            assert isinstance(entry["last_activity"]["at"], int) and (
                entry["last_activity"]["at"] > 0
            )
        # Each actor's latest is its own newest activity, not its first: the
        # sender ends on the last message, the target on the delivery ack of
        # the first message, and the join-only reader on its board ack.
        latest = {entry["member_id"]: entry["last_activity"] for entry in members}
        assert set(latest) == {sender, target, reader}
        assert (latest[sender]["kind"], latest[sender]["message_id"]) == (
            "message",
            second["message_id"],
        )
        assert (latest[target]["kind"], latest[target]["message_id"]) == (
            "delivery",
            first["message_id"],
        )
        assert (latest[reader]["kind"], latest[reader]["message_id"]) == (
            "board_acknowledged",
            None,
        )

        # A room without recorded activity reports null latest summaries, in
        # the roster detail and in the rooms list (whose LEFT JOIN keeps the
        # room row with null activity columns instead of dropping it).
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM room_activity WHERE room_id = ?", (room_id,)
            )
        payload = console_room_detail(client, console, room_id).json()
        assert payload["room"]["last_activity"] is None
        assert all(entry["last_activity"] is None for entry in payload["members"])
        rooms = console_rooms(client, console).json()["rooms"]
        assert [entry["room_id"] for entry in rooms] == [room_id]
        assert all(entry["last_activity"] is None for entry in rooms)


_V3_HISTORY_DATABASE = """
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
    CREATE TABLE id_counters (
        counter TEXT PRIMARY KEY CHECK (counter IN ('message_id', 'event_id')),
        last_id INTEGER NOT NULL CHECK (last_id >= 0)
    );
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


def test_v3_history_database_upgrades_to_console_v5(tmp_path):
    db_path = tmp_path / "team.sqlite3"
    owner_id = str(uuid4())
    raw = token_bytes(32)
    legacy_token = urlsafe_b64encode(raw).decode().rstrip("=")
    legacy_digest = sha256(raw).digest()
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    created_at = "2026-01-02T03:04:05Z"
    unix_created = int(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp())
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V3_HISTORY_DATABASE)
        connection.execute(
            "INSERT INTO accounts (owner_id, username, password_digest) VALUES (?, 'alice', ?)",
            (owner_id, PasswordHasher().hash(PASSWORD)),
        )
        connection.execute(
            "INSERT INTO account_sessions VALUES (?, ?, ?)",
            (legacy_digest, owner_id, int(time()) + 3600),
        )
        connection.execute(
            "INSERT INTO members VALUES (?, 'legacy-owner', 'old-harness', "
            "'old-session', 'Old role', ?)",
            (old_member, sha256(sha256(b"old").digest()).digest()),
        )
        connection.execute(
            "INSERT INTO rooms VALUES (?, 'History room', 1, 'Rules', 'Protocol', 'retained')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, old_member)
        )
        connection.execute(
            """
            INSERT INTO messages (
                message_id, room_id, sender_member_id, client_message_id, text,
                mention_member_id, created_at
            ) VALUES (41, ?, ?, 'old-1', 'Old body', NULL, ?)
            """,
            (room_id, old_member, created_at),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 41, 'message', 1)",
            (old_member,),
        )
        connection.execute(
            "INSERT INTO id_counters (counter, last_id) "
            "VALUES ('message_id', 41), ('event_id', 17)"
        )

    with live_server(db_path, legacy=False) as (client, _):
        assert bind(client, legacy_token, "sender").status_code == 200
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            purposes = dict(
                connection.execute("SELECT token_digest, purpose FROM account_sessions")
            )
        assert purposes[legacy_digest] == "enrollment"

        admin(db_path, "grant-console", "alice")
        console = console_token(client, "alice", PASSWORD)
        assert console_rooms(client, console).json() == {
            "rooms": [
                {
                    "room_id": room_id,
                    "name": "History room",
                    "member_count": 1,
                    "last_activity": {
                        "kind": "delivery",
                        "at": None,
                        "message_id": 41,
                    },
                }
            ]
        }
        timeline = console_timeline(client, console, room_id).json()
        assert timeline == {
            "activities": [
                {
                    "activity_id": 1,
                    "room_id": room_id,
                    "kind": "member_joined",
                    "at": None,
                    "message_id": None,
                    "actor_member_id": old_member,
                },
                {
                    "activity_id": 2,
                    "room_id": room_id,
                    "kind": "board_acknowledged",
                    "at": None,
                    "message_id": None,
                    "actor_member_id": old_member,
                },
                {
                    "activity_id": 3,
                    "room_id": room_id,
                    "kind": "message",
                    "at": unix_created,
                    "message_id": 41,
                    "actor_member_id": old_member,
                },
                {
                    "activity_id": 4,
                    "room_id": room_id,
                    "kind": "delivery",
                    "at": None,
                    "message_id": 41,
                    "actor_member_id": old_member,
                },
            ]
        }
        admin(db_path, "grant-transcript", "alice", room_id)
        page = console_transcript(client, console, room_id).json()
        assert page == {
            "messages": [
                {
                    "message_id": 41,
                    "room_id": room_id,
                    "sender_member_id": old_member,
                    "sender_role_description": "Old role",
                    "text": "Old body",
                    "mention_member_id": None,
                    "created_at": unix_created,
                }
            ],
            "has_more": False,
        }
        assert len(audit_rows(db_path)) == 1

        # The durable allocator continues past the pre-upgrade maximum.
        sent = post(
            client,
            "old",
            f"/rooms/{room_id}/messages",
            json={"client_message_id": "after-upgrade", "text": "New body"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["message_id"] == 42


_V3_PREVIEW_DATABASE = """
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
        expires_at INTEGER NOT NULL,
        purpose TEXT NOT NULL DEFAULT 'enrollment'
            CHECK (purpose IN ('enrollment', 'console'))
    );
    CREATE INDEX account_sessions_owner ON account_sessions (owner_id);
    CREATE TABLE operator_grants (
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        scope TEXT NOT NULL CHECK (scope IN ('console_read', 'transcript_read')),
        room_id TEXT REFERENCES rooms(room_id),
        CHECK (
            (scope = 'console_read' AND room_id IS NULL)
            OR (scope = 'transcript_read' AND room_id IS NOT NULL)
        )
    );
    CREATE UNIQUE INDEX operator_grant_identity
        ON operator_grants (owner_id, scope, ifnull(room_id, ''));
    CREATE INDEX operator_grants_owner ON operator_grants (owner_id);
"""


def test_v3_auth_preview_database_upgrades_to_console_v5_preserving_grants(tmp_path):
    """The published #5 auth preview v3 (e495544) is a supported v5 start.

    The preview carries ``purpose`` sessions and ``operator_grants`` but no
    #6 history columns. The migration applies the missing history layer and
    seeds durable IDs before the DM rebuild, then adds only the console read
    schema: the preview's purposes, sessions, grants and room data survive.
    """
    db_path = tmp_path / "team.sqlite3"
    owner_id = str(uuid4())
    console_raw = token_bytes(32)
    preview_console_token = urlsafe_b64encode(console_raw).decode().rstrip("=")
    console_digest = sha256(console_raw).digest()
    enroll_raw = token_bytes(32)
    preview_enroll_token = urlsafe_b64encode(enroll_raw).decode().rstrip("=")
    enroll_digest = sha256(enroll_raw).digest()
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    late_member = "6f6d3b93-2c2e-4d5f-9a54-6d0f4f65ab21"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V3_PREVIEW_DATABASE)
        connection.execute(
            "INSERT INTO accounts (owner_id, username, password_digest) "
            "VALUES (?, 'alice', ?)",
            (owner_id, PasswordHasher().hash(PASSWORD)),
        )
        connection.execute(
            "INSERT INTO account_sessions (token_digest, owner_id, expires_at, purpose) "
            "VALUES (?, ?, ?, 'console')",
            (console_digest, owner_id, int(time()) + 3600),
        )
        connection.execute(
            "INSERT INTO account_sessions (token_digest, owner_id, expires_at, purpose) "
            "VALUES (?, ?, ?, 'enrollment')",
            (enroll_digest, owner_id, int(time()) + 3600),
        )
        connection.execute(
            "INSERT INTO operator_grants (owner_id, scope, room_id) "
            "VALUES (?, 'console_read', NULL)",
            (owner_id,),
        )
        connection.execute(
            "INSERT INTO operator_grants (owner_id, scope, room_id) "
            "VALUES (?, 'transcript_read', ?)",
            (owner_id, room_id),
        )
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
            "INSERT INTO rooms VALUES (?, 'Preview room', 1, 'Rules', 'Protocol')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, old_member)
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 41, 1, 1)", (room_id, late_member)
        )
        connection.execute(
            "INSERT INTO messages VALUES (41, ?, ?, 'old-1', 'Preview body', NULL)",
            (room_id, old_member),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 41, 'message', 0)",
            (late_member,),
        )

    with live_server(db_path, legacy=False) as (client, _):
        # The preview's enrollment session still binds agents after the merge.
        assert bind(client, preview_enroll_token, "preview-agent").status_code == 200
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            purposes = dict(
                connection.execute("SELECT token_digest, purpose FROM account_sessions")
            )
            grants = sorted(
                connection.execute("SELECT scope, room_id FROM operator_grants")
            )
        assert purposes[console_digest] == "console"
        assert purposes[enroll_digest] == "enrollment"
        assert grants == [("console_read", None), ("transcript_read", room_id)]

        # Preserved grants authorize the console session without re-granting.
        assert console_rooms(client, preview_console_token).status_code == 200
        detail = console_room_detail(client, preview_console_token, room_id)
        assert detail.status_code == 200, detail.text
        assert detail.json()["transcript_allowed"] is True
        page = console_transcript(client, preview_console_token, room_id).json()
        assert page == {
            "messages": [
                {
                    "message_id": 41,
                    "room_id": room_id,
                    "sender_member_id": old_member,
                    "sender_role_description": "Old role",
                    "text": "Preview body",
                    "mention_member_id": None,
                    "created_at": None,
                }
            ],
            "has_more": False,
        }
        assert len(audit_rows(db_path)) == 1

        # Room data survives; the history layer defaults existing rooms and
        # the backfilled projection covers only the preview's room rows.
        history = get(client, "old", f"/rooms/{room_id}/messages").json()["messages"]
        assert [message["message_id"] for message in history] == [41]
        assert history[0]["created_at"] is None
        timeline = console_timeline(client, preview_console_token, room_id).json()
        assert timeline == {
            "activities": [
                {
                    "activity_id": 1,
                    "room_id": room_id,
                    "kind": "member_joined",
                    "at": None,
                    "message_id": None,
                    "actor_member_id": old_member,
                },
                {
                    "activity_id": 2,
                    "room_id": room_id,
                    "kind": "member_joined",
                    "at": None,
                    "message_id": None,
                    "actor_member_id": late_member,
                },
                {
                    "activity_id": 3,
                    "room_id": room_id,
                    "kind": "board_acknowledged",
                    "at": None,
                    "message_id": None,
                    "actor_member_id": late_member,
                },
                {
                    "activity_id": 4,
                    "room_id": room_id,
                    "kind": "board_acknowledged",
                    "at": None,
                    "message_id": None,
                    "actor_member_id": old_member,
                },
                {
                    "activity_id": 5,
                    "room_id": room_id,
                    "kind": "message",
                    "at": None,
                    "message_id": 41,
                    "actor_member_id": old_member,
                },
            ]
        }

        # The DM rebuild applied: the pair can message privately, and the
        # seeded allocators continue past the pre-upgrade maxima.
        dm = post(client, "old", "/dms", json={"member_id": late_member})
        assert dm.status_code == 200, dm.text
        dm_id = dm.json()["dm_id"]
        private = post(
            client,
            "old",
            f"/dms/{dm_id}/messages",
            json={"client_message_id": "old-1", "text": "same key, other family"},
        )
        assert private.status_code == 200, private.text
        assert private.json()["message_id"] == 42
        dm_events = get(client, "late", "/inbox?kinds=dm").json()["events"]
        assert [event["event_id"] for event in dm_events] == [18]
        room_events = get(client, "late", "/inbox").json()["events"]
        assert [event["event_id"] for event in room_events] == [17]


_V4_DM_DATABASE = """
    PRAGMA user_version = 4;
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
    CREATE TABLE dms (
        dm_id TEXT PRIMARY KEY,
        member_low TEXT NOT NULL REFERENCES members(member_id),
        member_high TEXT NOT NULL REFERENCES members(member_id),
        UNIQUE (member_low, member_high),
        CHECK (member_low < member_high)
    );
    CREATE TABLE messages (
        message_id INTEGER PRIMARY KEY,
        room_id TEXT REFERENCES rooms(room_id),
        dm_id TEXT REFERENCES dms(dm_id),
        sender_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_message_id TEXT NOT NULL,
        text TEXT NOT NULL,
        mention_member_id TEXT REFERENCES members(member_id),
        created_at TEXT,
        CHECK ((room_id IS NULL) <> (dm_id IS NULL))
    );
    CREATE INDEX room_history ON messages (room_id, message_id);
    CREATE INDEX dm_history ON messages (dm_id, message_id);
    CREATE UNIQUE INDEX room_message_retry ON messages (sender_member_id, client_message_id)
        WHERE room_id IS NOT NULL;
    CREATE UNIQUE INDEX dm_message_retry ON messages (dm_id, sender_member_id, client_message_id)
        WHERE dm_id IS NOT NULL;
    CREATE TABLE inbox_events (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER NOT NULL REFERENCES messages(message_id),
        kind TEXT NOT NULL CHECK (kind IN ('message', 'mention', 'dm')),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        UNIQUE (member_id, message_id)
    );
    CREATE INDEX pending_inbox ON inbox_events (member_id, event_id)
        WHERE acknowledged = 0;
    CREATE TABLE id_counters (
        counter TEXT PRIMARY KEY CHECK (counter IN ('message_id', 'event_id')),
        last_id INTEGER NOT NULL CHECK (last_id >= 0)
    );
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


def test_v4_dm_database_upgrades_to_console_v5_without_dm_activity(tmp_path):
    """The #3 DM v4 schema is a supported v5 start.

    The console layer adds purpose, grants and the read projection on top of
    the DM schema. DM rows (``messages.room_id`` is NULL) never enter the
    backfilled room activity, and the DM history stays participant-only.
    """
    db_path = tmp_path / "team.sqlite3"
    owner_id = str(uuid4())
    raw = token_bytes(32)
    legacy_token = urlsafe_b64encode(raw).decode().rstrip("=")
    legacy_digest = sha256(raw).digest()
    old_member = "c876568e-8819-40fe-a5f9-da904dcb45a3"
    late_member = "6f6d3b93-2c2e-4d5f-9a54-6d0f4f65ab21"
    room_id = "3cb9cbf2-aaf5-459b-a69f-4e256c300454"
    dm_id = "b6a4d1e2-3f4a-4b5c-8d7e-9f0a1b2c3d4e"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(_V4_DM_DATABASE)
        connection.execute(
            "INSERT INTO accounts (owner_id, username, password_digest) "
            "VALUES (?, 'alice', ?)",
            (owner_id, PasswordHasher().hash(PASSWORD)),
        )
        connection.execute(
            "INSERT INTO account_sessions VALUES (?, ?, ?)",
            (legacy_digest, owner_id, int(time()) + 3600),
        )
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
            "INSERT INTO rooms VALUES (?, 'DM room', 1, 'Rules', 'Protocol', 'retained')",
            (room_id,),
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 0, 1, 1)", (room_id, old_member)
        )
        connection.execute(
            "INSERT INTO room_members VALUES (?, ?, 41, 1, 1)", (room_id, late_member)
        )
        connection.execute(
            "INSERT INTO dms VALUES (?, ?, ?)",
            (dm_id, min(old_member, late_member), max(old_member, late_member)),
        )
        connection.execute(
            """
            INSERT INTO messages (
                message_id, room_id, dm_id, sender_member_id, client_message_id,
                text, mention_member_id, created_at
            ) VALUES (41, ?, NULL, ?, 'old-1', 'Room body', NULL, NULL)
            """,
            (room_id, old_member),
        )
        connection.execute(
            """
            INSERT INTO messages (
                message_id, room_id, dm_id, sender_member_id, client_message_id,
                text, mention_member_id, created_at
            ) VALUES (42, NULL, ?, ?, 'dm-1', 'DM secret body', NULL, NULL)
            """,
            (dm_id, old_member),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (17, ?, 41, 'message', 1)",
            (late_member,),
        )
        connection.execute(
            "INSERT INTO inbox_events VALUES (18, ?, 42, 'dm', 1)",
            (late_member,),
        )
        connection.execute(
            "INSERT INTO id_counters (counter, last_id) "
            "VALUES ('message_id', 42), ('event_id', 18)"
        )

    with live_server(db_path, legacy=False) as (client, _):
        assert bind(client, legacy_token, "sender").status_code == 200
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            purposes = dict(
                connection.execute("SELECT token_digest, purpose FROM account_sessions")
            )
        assert purposes[legacy_digest] == "enrollment"

        admin(db_path, "grant-console", "alice")
        admin(db_path, "grant-transcript", "alice", room_id)
        console = console_token(client, "alice", PASSWORD)

        # The acknowledged DM delivery (event 18, message 42) created no room
        # activity: only the room message projection appears.
        timeline = console_timeline(client, console, room_id).json()
        assert [
            (activity["kind"], activity["message_id"], activity["actor_member_id"])
            for activity in timeline["activities"]
        ] == [
            ("member_joined", None, old_member),
            ("member_joined", None, late_member),
            ("board_acknowledged", None, late_member),
            ("board_acknowledged", None, old_member),
            ("message", 41, old_member),
            ("delivery", 41, late_member),
        ]
        assert "DM secret body" not in timeline
        rooms_payload = console_rooms(client, console).json()
        assert rooms_payload["rooms"][0]["last_activity"]["message_id"] == 41
        page = console_transcript(client, console, room_id).json()
        assert [message["text"] for message in page["messages"]] == ["Room body"]
        assert page["has_more"] is False

        # The DM history stays readable only for the two participants.
        dm_history = get(client, "late", f"/dms/{dm_id}/messages").json()["messages"]
        assert [message["message_id"] for message in dm_history] == [42]
        assert dm_history[0]["text"] == "DM secret body"
        error(get(client, "sender", f"/dms/{dm_id}/messages"), 404, "not_found")

        # Durable IDs keep counting past the pre-upgrade maxima.
        room_next = post(
            client,
            "old",
            f"/rooms/{room_id}/messages",
            json={"client_message_id": "old-2", "text": "Fresh room body"},
        )
        assert room_next.status_code == 200, room_next.text
        assert room_next.json()["message_id"] == 43


def test_authorized_operator_cannot_trace_dm_text_through_console(tmp_path):
    """A fully authorized operator never traces DM text through the console.

    Even while DMs are enabled and exchanged, DM sends and acknowledgements
    create no room activity, and Timeline, roster detail, and the audited
    transcript expose only room records.
    """
    db_path = tmp_path / "team.sqlite3"
    enter_password(db_path, "create", "alice", PASSWORD)
    with live_server(db_path) as (client, _):
        enrollment = login_token(client, "alice", PASSWORD)
        assert bind(client, enrollment, "target").status_code == 200
        room_id = console_team_room(client, messages=("room body one",))
        room_path = f"/rooms/{room_id}"
        join(client, "target", room_path)
        confirm(client, "target", room_path)
        target = member(client, "target")
        sent = send(client, room_path, "key-1", text="room body two").json()
        events = get(client, "target", "/inbox").json()["events"]
        assert [event["message"]["message_id"] for event in events] == [
            sent["message_id"]
        ]
        assert (
            post(client, "target", f"/inbox/{events[0]['event_id']}/ack").status_code
            == 200
        )
        baseline = room_activity_rows(db_path)

        # DMs are enabled: the pair exchanges private text and the peer
        # acknowledges the delivery.
        dm = post(client, "sender", "/dms", json={"member_id": target})
        assert dm.status_code == 200, dm.text
        dm_id = dm.json()["dm_id"]
        secret = "dm-only secret phrase"
        private = post(
            client,
            "sender",
            f"/dms/{dm_id}/messages",
            json={"client_message_id": "dm-key-1", "text": secret},
        )
        assert private.status_code == 200, private.text
        dm_message_id = private.json()["message_id"]
        dm_events = get(client, "target", "/inbox?kinds=dm").json()["events"]
        assert [event["message"]["message_id"] for event in dm_events] == [
            dm_message_id
        ]
        for event in dm_events:
            assert (
                post(client, "target", f"/inbox/{event['event_id']}/ack").status_code
                == 200
            )

        # Neither the DM send nor the DM acknowledgement created room activity.
        assert room_activity_rows(db_path) == baseline

        admin(db_path, "grant-console", "alice")
        admin(db_path, "grant-transcript", "alice", room_id)
        console = console_token(client, "alice", PASSWORD)

        rooms_payload = console_rooms(client, console).json()
        assert (
            rooms_payload["rooms"][0]["last_activity"]["message_id"]
            == sent["message_id"]
        )
        detail = console_room_detail(client, console, room_id).json()
        assert detail["transcript_allowed"] is True

        activities = console_timeline(client, console, room_id).json()["activities"]
        assert activities
        assert all(activity["message_id"] != dm_message_id for activity in activities)
        assert [
            (activity["kind"], activity["message_id"]) for activity in activities
        ] == [
            ("member_joined", None),
            ("board_acknowledged", None),
            ("message", sent["message_id"] - 1),
            ("member_joined", None),
            ("board_acknowledged", None),
            ("message", sent["message_id"]),
            ("delivery", sent["message_id"]),
        ]

        texts = []
        message_ids = []
        page = console_transcript(client, console, room_id, limit=1).json()
        while True:
            texts.extend(message["text"] for message in page["messages"])
            message_ids.extend(message["message_id"] for message in page["messages"])
            if not page["has_more"]:
                break
            oldest = page["messages"][0]["message_id"]
            page = console_transcript(
                client, console, room_id, before=oldest, limit=1
            ).json()
        assert texts == ["room body two", "room body one"]
        assert dm_message_id not in message_ids

        for payload in (rooms_payload, detail, activities, page):
            assert secret not in json.dumps(payload)
        assert all(
            row["message_id"] != dm_message_id for row in room_activity_rows(db_path)
        )

        # Each audited transcript page left a body-free audit row only.
        assert len(audit_rows(db_path)) == 2
