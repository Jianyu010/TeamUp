import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from teamup_server.collaboration import Collaboration
from test_contract import (
    KEYS,
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
)
from test_server_accounts import (
    NEW_PASSWORD,
    PASSWORD,
    bind,
    login_token,
    token_from_login,
)


@pytest.fixture
def lifecycle(tmp_path):
    db_path = tmp_path / "team.db"
    store = Collaboration(db_path, KEYS)
    for name in ("alice", "bob"):
        store.create_account(name, PASSWORD)
    with api(db_path) as client:
        owner = login_token(client, "alice", PASSWORD)
        old = bind(client, owner, "target")
        assert old.status_code == 200, old.text
        peer = enroll(client, "sender", owner="bob")
        assert peer.status_code == 200, peer.text
        yield client, store, db_path, owner, old.json(), peer.json()


def test_only_the_owning_account_can_rename_or_retire(lifecycle):
    client, store, _, owner, member, _ = lifecycle
    member_id = member["member_id"]
    path = f"/v1/members/{member_id}"
    role = {"role_description": "  QA · Accounts  "}
    other = login_token(client, "bob", PASSWORD)
    console = token_from_login(
        client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": PASSWORD, "purpose": "console"},
        )
    )

    for method, url, body in (
        ("PATCH", path, {"json": role}),
        ("POST", f"{path}/retire", {}),
    ):
        for invalid in (credential("target"), console, KEYS["alice"]):
            error(
                client.request(method, url, headers=auth(invalid), **body),
                401,
                "unauthorized",
            )
        error(
            client.request(method, url, headers=auth(other), **body), 404, "not_found"
        )
        unknown = url.replace(member_id, str(uuid4()))
        error(
            client.request(method, unknown, headers=auth(owner), **body),
            404,
            "not_found",
        )

    for invalid_role in (" ", "x" * 201):
        response = client.patch(
            path, headers=auth(owner), json={"role_description": invalid_role}
        )
        error(response, 422, "invalid_request")
    error(
        client.post(
            f"{path}/retire", headers=auth(owner), json={"member_id": member_id}
        ),
        422,
        "invalid_request",
    )

    expected = {**member, **role}
    for _ in range(2):
        response = client.patch(path, headers=auth(owner), json=role)
        assert response.status_code == 200, response.text
        assert response.json() == expected
    assert get(client, "target", "/members/me").json() == expected
    assert get(client, "sender", "/members?q=accounts").json() == {
        "members": [expected]
    }
    assert get(client, "sender", "/members?q=target").json() == {"members": []}
    recovery = bind(client, owner, "target")
    assert recovery.json() == expected

    store.reset_account_password("alice", NEW_PASSWORD)
    error(client.patch(path, headers=auth(owner), json=role), 401, "unauthorized")
    error(client.post(f"{path}/retire", headers=auth(owner)), 401, "unauthorized")
    assert get(client, "target", "/members/me").json() == expected


def test_retirement_discards_pending_delivery_and_preserves_history(lifecycle):
    client, store, db_path, owner, member, peer = lifecycle
    member_id = member["member_id"]
    room_path = room(client)
    room_id = room_path.rsplit("/", 1)[-1]
    other_room = room(client)
    for path in (room_path, other_room):
        join(client, "target", path)
        for label in ("sender", "target"):
            confirm(client, label, path)

    old_mention = send(client, room_path, "mention", target=member_id).json()
    old_reply = post(
        client,
        "target",
        f"{room_path}/messages",
        json={"client_message_id": "reply", "text": "The previous member wrote this."},
    ).json()
    assert get(client, "sender", f"{room_path}/messages").json() == {
        "messages": [old_mention, old_reply]
    }
    dm_id = post(client, "sender", "/dms", json={"member_id": member_id}).json()[
        "dm_id"
    ]
    dm_path = f"/dms/{dm_id}/messages"
    dm_body = {"client_message_id": "dm", "text": "Earlier DM"}
    dm_message = post(client, "sender", dm_path, json=dm_body).json()
    pending = get(client, "target", "/inbox?kinds=mention,dm").json()["events"]
    assert [event["kind"] for event in pending] == ["mention", "dm"]

    store.grant_console_read("bob")
    store.grant_transcript_read("bob", room_id)
    console = token_from_login(
        client.post(
            "/v1/auth/login",
            json={"username": "bob", "password": PASSWORD, "purpose": "console"},
        )
    )
    retire = f"/v1/members/{member_id}/retire"
    headers = auth(credential("target")) | {"Accept": "text/event-stream"}
    with client.stream("GET", "/v1/events", headers=headers, timeout=5) as stream:
        assert stream.status_code == 200
        lines = stream.iter_lines()
        assert next(lines) == ": ready"
        assert next(lines) == ""
        with ThreadPoolExecutor(max_workers=3) as workers:
            receipts = list(
                workers.map(
                    lambda _: client.post(retire, headers=auth(owner)), range(3)
                )
            )
        assert [response.status_code for response in receipts] == [200] * 3
        assert all(
            response.json() == {"member_id": member_id, "retired": True}
            for response in receipts
        )
        assert list(lines) == []

    assert get(client, "sender", "/members?q=target").json() == {"members": []}
    for path in (room_path, other_room):
        assert get(client, "sender", f"{path}/members").json() == {
            "members": [{**peer, "acked_board_version": 1}]
        }
    assert [
        row["member_id"] for row in get(client, "sender", "/presence").json()["members"]
    ] == [peer["member_id"]]
    error(get(client, "target", "/inbox?kinds=mention,dm"), 401, "unauthorized")
    error(
        post(client, "target", f"/inbox/{pending[0]['event_id']}/ack"),
        401,
        "unauthorized",
    )
    error(
        post(
            client,
            "target",
            f"{room_path}/messages",
            json={"client_message_id": "blocked", "text": "late"},
        ),
        401,
        "unauthorized",
    )
    error(post(client, "target", f"{room_path}/join"), 401, "unauthorized")
    with closing(sqlite3.connect(db_path)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM inbox_events WHERE member_id = ?", (member_id,)
            ).fetchone()[0]
            == 0
        )

    error(send(client, room_path, "new", target=member_id), 422, "invalid_mention")
    observer = enroll(client, "observer").json()["member_id"]
    join(client, "observer", room_path)
    confirm(client, "observer", room_path)
    error(
        post(
            client,
            "sender",
            f"{room_path}/messages",
            json={
                "client_message_id": "new-many",
                "text": "hello",
                "mention_member_ids": [observer, member_id],
            },
        ),
        422,
        "invalid_mention",
    )
    assert get(client, "observer", "/inbox").json() == {"events": []}
    assert send(client, room_path, "mention", target=member_id).json() == old_mention
    expected_reply = {**old_reply, "sender_retired": True}
    assert get(client, "sender", f"{room_path}/messages").json() == {
        "messages": [old_mention, expected_reply]
    }
    transcript = client.get(
        f"/v1/console/rooms/{room_id}/transcript", headers=auth(console)
    )
    assert transcript.status_code == 200, transcript.text
    messages = transcript.json()["messages"]
    assert "sender_retired" not in messages[0]
    assert messages[1]["sender_member_id"] == member_id
    assert messages[1]["sender_role_description"] == "target"
    assert messages[1]["sender_retired"] is True

    error(
        post(client, "sender", "/dms", json={"member_id": member_id}),
        422,
        "invalid_peer",
    )
    error(
        post(
            client, "sender", dm_path, json={**dm_body, "client_message_id": "late-dm"}
        ),
        404,
        "not_found",
    )
    assert post(client, "sender", dm_path, json=dm_body).json() == dm_message
    assert get(client, "sender", dm_path).json() == {"messages": [dm_message]}


def test_retired_identity_cannot_be_reenrolled_even_after_restart(lifecycle):
    client, _, db_path, owner, member, _ = lifecycle
    path = f"/v1/members/{member['member_id']}"
    assert client.post(f"{path}/retire", headers=auth(owner)).status_code == 200
    with api(db_path) as restarted:
        error(get(restarted, "target", "/members/me"), 401, "unauthorized")
        error(bind(restarted, owner, "target"), 409, "member_retired")
        error(
            bind(restarted, owner, "target", member_token=credential("replacement")),
            409,
            "member_retired",
        )
        error(
            bind(restarted, owner, "new-session", member_token=credential("target")),
            409,
            "conflict",
        )
        error(
            restarted.patch(
                path, headers=auth(owner), json={"role_description": "new"}
            ),
            409,
            "member_retired",
        )
        successor = bind(restarted, owner, "new-session")
        assert successor.status_code == 200, successor.text
        assert successor.json()["member_id"] != member["member_id"]


@pytest.mark.parametrize("policy", ["retained", "since_joining"])
def test_leaving_a_room_ends_only_that_membership(lifecycle, policy):
    client, _, _, _, member, peer = lifecycle
    departing = room(client, history_policy=policy)
    staying = room(client)
    room_id = departing.rsplit("/", 1)[-1]
    for path in (departing, staying):
        join(client, "target", path)
        for label in ("sender", "target"):
            confirm(client, label, path)
    previous = send(
        client, departing, "before-leaving", target=member["member_id"]
    ).json()
    other = send(client, staying, "other-room", target=member["member_id"]).json()

    for _ in range(2):
        response = post(client, "target", f"{departing}/leave")
        assert response.status_code == 200, response.text
        assert response.json() == {"room_id": room_id}
    remaining = get(client, "target", "/rooms").json()["rooms"]
    assert [row["room_id"] for row in remaining] == [staying.rsplit("/", 1)[-1]]
    assert get(client, "target", "/members/me").json() == member
    error(get(client, "target", f"{departing}/messages"), 404, "not_found")
    error(
        send(client, departing, "invalid", target=member["member_id"]),
        422,
        "invalid_mention",
    )
    assert get(client, "sender", f"{departing}/members").json() == {
        "members": [{**peer, "acked_board_version": 1}]
    }
    assert get(client, "sender", f"{departing}/messages").json() == {
        "messages": [previous]
    }

    join(client, "target", departing)
    error(
        post(client, "target", f"{departing}/board/ack", json={"version": 1}),
        409,
        "board_read_required",
    )
    confirm(client, "target", departing)
    expected = [previous] if policy == "retained" else []
    assert get(client, "target", f"{departing}/messages").json() == {
        "messages": expected
    }
    pending = get(client, "target", "/inbox").json()["events"]
    assert [event["message"] for event in pending] == [other]


def test_v7_migration_preserves_bindings_and_room_messages(tmp_path):
    db_path = tmp_path / "v7.db"
    with api(db_path) as client:
        sender = enroll(client, "sender").json()
        path = room(client)
        confirm(client, "sender", path)
        message = send(client, path, "before-upgrade").json()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("ALTER TABLE members DROP COLUMN retired")
        connection.execute("PRAGMA user_version = 7")

    with api(db_path) as client:
        assert get(client, "sender", "/members/me").json() == sender
        assert get(client, "sender", f"{path}/messages").json() == {
            "messages": [message]
        }
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
