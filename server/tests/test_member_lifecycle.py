import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
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


@pytest.mark.parametrize("version", [7, 8])
def test_migration_preserves_bindings_and_room_messages(tmp_path, version):
    db_path = tmp_path / f"v{version}.db"
    with api(db_path) as client:
        sender = enroll(client, "sender").json()
        path = room(client)
        confirm(client, "sender", path)
        message = send(client, path, "before-upgrade").json()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DROP TABLE retired_room_access")
        connection.execute("DROP TABLE room_departures")
        connection.execute("ALTER TABLE room_members DROP COLUMN membership_id")
        if version == 7:
            connection.execute("ALTER TABLE members DROP COLUMN retired")
        connection.execute(f"PRAGMA user_version = {version}")

    with api(db_path) as client:
        assert get(client, "sender", "/members/me").json() == sender
        assert get(client, "sender", f"{path}/messages").json() == {
            "messages": [message]
        }
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 10


def test_self_retirement_can_be_retried_without_restoring_access(lifecycle):
    client, store, db_path, owner, member, _ = lifecycle
    path = "/members/me/retirement"
    error(get(client, "target", path), 404, "not_found")
    error(get(client, "unregistered", path), 401, "unauthorized")
    error(
        client.post("/v1/members/me/retire", headers=auth(owner)),
        401,
        "unauthorized",
    )
    expected = {**member, "retired": True}
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(lambda _: post(client, "target", "/members/me/retire"), range(3))
        )
    assert [item.status_code for item in responses] == [200] * 3
    assert [item.json() for item in responses] == [expected] * 3
    with api(db_path) as restarted:
        assert get(restarted, "target", path).json() == expected
        error(get(restarted, "target", "/members/me"), 401, "unauthorized")
        error(bind(restarted, owner, "target"), 409, "member_retired")
        store.disable_account("alice")
        error(post(restarted, "target", "/members/me/retire"), 401, "unauthorized")
        error(get(restarted, "target", path), 401, "unauthorized")


def test_owner_departure_notice_is_durable_and_recipient_scoped(lifecycle):
    client, _, db_path, _, member, _ = lifecycle
    path = room(client)
    room_id = path.rsplit("/", 1)[-1]
    join(client, "target", path)
    confirm(client, "sender", path)
    confirm(client, "target", path)
    send(client, path, "pending", target=member["member_id"])
    joined = get(client, "target", "/rooms?include_membership=true").json()["rooms"][0]
    membership = joined["membership_id"]
    url = f"{path}/leave?notify=true&membership_id={membership}"
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: post(client, "target", url), range(3)))
    assert [item.status_code for item in responses] == [200] * 3
    receipts = [item.json() for item in responses]
    assert all(receipt["room_id"] == room_id for receipt in receipts)
    assert sum(receipt["departure_event_id"] is not None for receipt in receipts) == 1
    error(get(client, "target", f"{path}/members"), 404, "not_found")
    join(client, "target", path)
    confirm(client, "target", path)
    error(post(client, "target", url), 409, "membership_changed")
    error(
        get(client, "target", f"{path}/messages?membership_id={membership}"),
        409,
        "membership_changed",
    )
    assert get(client, "target", f"{path}/members").status_code == 200

    with api(db_path) as restarted:
        assert get(restarted, "target", "/inbox").json() == {"events": []}
        assert get(restarted, "sender", "/inbox?kinds=room_left").json() == {
            "events": []
        }
        events = get(restarted, "target", "/inbox?kinds=mention,room_left").json()[
            "events"
        ]
        assert len(events) == 1
        notice = events[0]
        event_id = notice["event_id"]
        assert notice == {"event_id": event_id, "kind": "room_left", "room_id": room_id}
        ack = f"/inbox/{event_id}/ack"
        error(post(restarted, "sender", ack), 404, "not_found")
        assert post(restarted, "target", ack).status_code == 200
        assert post(restarted, "target", ack).status_code == 200
        assert get(restarted, "target", "/inbox?kinds=room_left").json() == {
            "events": []
        }


def test_owner_archive_reads_only_pre_retirement_authorized_history(lifecycle):
    client, store, db_path, owner, member, peer = lifecycle
    path = room(client)
    blocked = room(client)
    left = room(client)
    for candidate in (path, blocked, left):
        confirm(client, "sender", candidate)
        send(client, candidate, "before-joining")
        join(client, "target", candidate)
    confirm(client, "target", path)
    confirm(client, "target", left)
    post(client, "target", f"{left}/leave")
    before = send(client, path, "first").json()
    newest = send(client, path, "second").json()
    dm_id = post(
        client, "target", "/dms", json={"member_id": peer["member_id"]}
    ).json()["dm_id"]
    dm = post(
        client,
        "target",
        f"/dms/{dm_id}/messages",
        json={"client_message_id": "dm", "text": "Earlier DM"},
    ).json()
    archive = f"/v1/members/{member['member_id']}/archive"
    owner_headers = auth(owner)
    error(client.get(archive, headers=owner_headers), 404, "not_found")
    assert post(client, "target", "/members/me/retire").status_code == 200
    send(client, path, "after-retirement")
    with api(db_path) as restarted:
        result = restarted.get(archive, headers=owner_headers).json()
        assert {
            (item["kind"], item["conversation_id"]) for item in result["conversations"]
        } == {
            ("room", path.rsplit("/", 1)[-1]),
            ("dm", dm_id),
        }
        other = login_token(restarted, "bob", PASSWORD)
        error(restarted.get(archive, headers=auth(other)), 404, "not_found")
        error(
            restarted.get(archive, headers=auth(credential("target"))),
            401,
            "unauthorized",
        )
        for missing in (blocked, left):
            error(
                restarted.get(
                    f"{archive}/room/{missing.rsplit('/', 1)[-1]}/messages",
                    headers=owner_headers,
                ),
                404,
                "not_found",
            )
        transcript = f"{archive}/room/{path.rsplit('/', 1)[-1]}/messages"
        assert restarted.get(f"{transcript}?limit=1", headers=owner_headers).json()[
            "messages"
        ] == [newest]
        assert restarted.get(
            f"{transcript}?before={newest['message_id']}", headers=owner_headers
        ).json()["messages"] == [before]
        dms = restarted.get(
            f"{archive}/dm/{dm_id}/messages", headers=owner_headers
        ).json()
        assert dms["messages"] == [{**dm, "sender_retired": True}]
        age = timedelta(hours=1)
        store.expire_due(age, now=datetime.fromisoformat(newest["created_at"]) + age)
        assert restarted.get(transcript, headers=owner_headers).json()["messages"] == []
        assert restarted.get(
            f"{archive}/dm/{dm_id}/messages", headers=owner_headers
        ).json()["messages"] == [{**dm, "sender_retired": True}]
        store.expire_due_dms(datetime.fromisoformat(dm["created_at"]))
        assert (
            restarted.get(
                f"{archive}/dm/{dm_id}/messages", headers=owner_headers
            ).json()["messages"]
            == []
        )


def test_retained_room_delivery_cannot_reuse_the_previous_membership(lifecycle):
    client, _, _, _, target, _ = lifecycle
    path = room(client, history_policy="retained")
    join(client, "target", path)
    for label in ("sender", "target"):
        confirm(client, label, path)
    sent = send(client, path, "pending", target=target["member_id"]).json()
    delivery = get(client, "target", f"{path}/messages?delivery=true").json()
    assert delivery["messages"] == [sent]
    assert delivery["delivery_board"]["version"] == 1
    assert delivery["delivery_board"]["room_id"] == path.rsplit("/", 1)[-1]
    post(client, "target", f"{path}/leave")
    join(client, "target", path)
    confirm(client, "target", path)
    assert get(client, "target", f"{path}/messages").json()["messages"] == [sent]
    assert (
        get(client, "target", f"{path}/messages?delivery=true").json()["messages"] == []
    )
