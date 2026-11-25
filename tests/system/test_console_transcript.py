"""Compare the local console's transcript with the Server's member history."""

from contextlib import closing
from datetime import datetime, timedelta

import httpx
import pytest
from _stack import Api, Developer, Server, command, new_secret, wait_for
from teamup_server.collaboration import Collaboration

PAGE_SIZE = 30


@pytest.fixture
def transcript(ws):
    server = Server(ws, {"test-owner": new_secret()})
    # The Server owner creates and revokes Carol's account through its public
    # port. The two other members keep an independent enrollment identity.
    owner = Collaboration(server.db, server.keys)
    password = new_secret()
    owner.create_account("carol", password)
    server.start()
    developer = Developer(ws, "carol", server, credentials=False, notify=False)
    binding = developer.bindings[0]
    with closing(Api(server.url, trust_env=False)) as api:
        key = server.keys["test-owner"]
        alice = api.enroll(key, "Alice", session="alice")
        bob = api.enroll(key, "Bob", session="bob")
        login = api.call(
            "POST", "/v1/auth/login", json={"username": "carol", "password": password}
        )
        carol = api.enroll(
            login["access_token"],
            "Carol",
            harness=binding["harness_instance_id"],
            session=binding["session_key"],
            token=binding["member_token"],
        )

        argv, env = command("client")
        proc = ws.spawn(
            "console",
            [*argv, "serve", "--config", str(developer.config_path), "--console"],
            env,
        )

        def console_url():
            return next(
                (
                    line
                    for line in proc.text().splitlines()
                    if line.startswith("http://")
                ),
                None,
            )

        url = wait_for(console_url, proc, what="console loopback URL")
        origin, credential = url.split("#")
        with httpx.Client(
            base_url=origin,
            headers={"Authorization": f"Bearer {credential}"},
            timeout=10,
            trust_env=False,
        ) as console:
            response = console.get("/api/workspace")
            response.raise_for_status()
            (session,) = response.json()["sessions"]
            assert session["member_id"] == carol.member_id
            yield owner, api, (alice, bob, carol), console, session["id"]


def console_page(console, session, room, **cursor):
    response = console.get(
        "/api/room", params={"session": session, "room": room, **cursor}
    )
    response.raise_for_status()
    return response.json()


def server_page(api, member, room, **cursor):
    return api.call(
        "GET",
        f"/v1/rooms/{room}/messages",
        member.token,
        params={"limit": PAGE_SIZE, **cursor},
    )


def assert_same_page(console_result, server_result, expected):
    assert console_result["state"] == "ready"
    assert server_result["messages"] == expected
    assert [(row["id"], row["text"]) for row in console_result["messages"]] == [
        (str(row["message_id"]), row["text"]) for row in expected
    ]


def test_workspace_discovers_a_teammates_room_before_joining(transcript):
    owner, api, (alice, _, carol), console, session = transcript
    assert console.get("/api/workspace").json()["rooms"] == []
    room = api.create_room(alice, "Teammate review", history_policy="retained")
    api.read_and_ack_board(alice, room)
    first = api.send(alice, room, "Only room members can read this")

    response = console.get("/api/workspace")
    response.raise_for_status()
    workspace = response.json()
    assert workspace["rooms"] == [
        {
            "id": room,
            "name": "Teammate review",
            "history_policy": "retained",
            "latest_message_id": None,
            "memberships": [],
        }
    ]
    assert workspace["directory_available"] is True
    assert console_page(console, session, room) == {"state": "not_found"}
    assert api.call("GET", "/v1/rooms", carol.token) == {"rooms": []}

    api.join(carol, room)
    (joined,) = console.get("/api/workspace").json()["rooms"]
    assert joined["id"] == room
    assert [item["session"] for item in joined["memberships"]] == [session]
    assert console_page(console, session, room)["state"] == "board_ack_required"
    api.read_and_ack_board(carol, room)
    assert console_page(console, session, room)["messages"][0]["id"] == str(
        first["message_id"]
    )

    owner.disable_account("carol")
    assert (
        api.call("GET", "/v1/room-directory", carol.token, error="unauthorized") == 401
    )
    assert console.get("/api/workspace").json()["rooms"] == []


def test_bystander_reads_the_canonical_room_transcript_across_pages(transcript):
    owner, api, (alice, bob, carol), console, session = transcript
    room = api.create_room(alice, "Review", history_policy="retained")
    api.join(bob, room)
    api.read_and_ack_board(alice, room)
    api.read_and_ack_board(bob, room)
    prejoin = api.send(
        alice, room, "Review request before Carol joined", mention=bob.member_id
    )
    api.join(carol, room)

    history_path = f"/v1/rooms/{room}/messages"
    assert api.call("GET", history_path, carol.token, error="board_ack_required") == 409
    blocked = console_page(console, session, room)
    assert blocked["state"] == "board_ack_required"
    assert blocked["messages"] == []
    api.read_and_ack_board(carol, room)

    other_room = api.create_room(alice, "Outside Carol's rooms")
    api.read_and_ack_board(alice, other_room)
    between = api.send(alice, other_room, "Other room message")
    reply = api.send(bob, room, "Reply to Alice", mention=alice.member_id)
    expected = [prejoin, reply]
    expected.extend(
        api.send(alice, room, f"Broadcast {i}") for i in range(PAGE_SIZE - 1)
    )
    expected.append(api.send(carol, room, "Carol's own message"))
    assert prejoin["message_id"] < between["message_id"] < reply["message_id"]
    assert (
        api.call(
            "GET", f"/v1/rooms/{other_room}/messages", carol.token, error="not_found"
        )
        == 404
    )
    assert console_page(console, session, other_room)["state"] == "not_found"

    latest = console_page(console, session, room)
    assert_same_page(
        latest,
        server_page(api, carol, room, before=expected[-1]["message_id"] + 1),
        expected[-PAGE_SIZE:],
    )
    assert latest["older"] == str(expected[-PAGE_SIZE]["message_id"])
    older = console_page(console, session, room, before=latest["older"])
    assert_same_page(
        older,
        server_page(api, carol, room, before=latest["older"]),
        expected[:-PAGE_SIZE],
    )
    assert older["older"] is None

    missed = api.send(alice, room, "Follow-up for Bob", mention=bob.member_id)
    broadcast = api.send(bob, room, "Follow-up to everyone")
    cursor = latest["messages"][-1]["id"]
    catchup = console_page(console, session, room, after=cursor)
    assert_same_page(
        catchup, server_page(api, carol, room, after=cursor), [missed, broadcast]
    )
    assert catchup["more_recent"] is False

    owner.disable_account("carol")
    assert (
        api.call(
            "GET",
            history_path,
            carol.token,
            params={"after": cursor},
            error="unauthorized",
        )
        == 401
    )
    revoked = console_page(console, session, room, after=cursor)
    assert revoked["state"] in {"unauthorized", "not_registered"}
    assert not revoked.get("messages")


def test_default_history_begins_at_the_readers_join_boundary(transcript):
    _, api, (alice, bob, carol), console, session = transcript
    room = api.create_room(alice, "Since joining")
    api.join(bob, room)
    api.read_and_ack_board(alice, room)
    api.read_and_ack_board(bob, room)
    prejoin = api.send(alice, room, "A pre-join mention for Bob", mention=bob.member_id)
    api.join(carol, room)
    api.read_and_ack_board(carol, room)
    postjoin = api.send(
        alice, room, "A post-join mention for Bob", mention=bob.member_id
    )

    assert api.history(alice, room) == [prejoin, postjoin]
    latest = console_page(console, session, room)
    assert_same_page(
        latest,
        server_page(api, carol, room, before=postjoin["message_id"] + 1),
        [postjoin],
    )
    before = postjoin["message_id"]
    assert_same_page(
        console_page(console, session, room, before=before),
        server_page(api, carol, room, before=before),
        [],
    )


def test_expired_transcript_reports_the_server_resume_boundary(transcript):
    owner, api, (alice, bob, carol), console, session = transcript
    room = api.create_room(alice, "Retained", history_policy="retained")
    for member in (alice, bob, carol):
        api.join(member, room)
        api.read_and_ack_board(member, room)
    expired = api.send(alice, room, "This message will expire", mention=bob.member_id)
    before_expiry = console_page(console, session, room)
    assert "expired_through_message_id" not in before_expiry
    assert "history_truncated" not in before_expiry

    age = timedelta(hours=1)
    owner.expire_due(age, now=datetime.fromisoformat(expired["created_at"]) + age)
    watermark = expired["message_id"]
    server_result = server_page(api, carol, room, after=0)
    assert server_result == {
        "messages": [],
        "expired_through_message_id": watermark,
        "history_truncated": True,
    }
    latest = console_page(console, session, room)
    assert_same_page(latest, server_result, [])
    assert latest.get("expired_through_message_id") == str(watermark)
    assert latest.get("history_truncated") is True

    kept = api.send(bob, room, "A later message")
    assert kept["message_id"] > watermark
    still_covered = console_page(console, session, room, after=watermark)
    server_result = server_page(api, carol, room, after=watermark)
    assert_same_page(still_covered, server_result, [kept])
    assert server_result["history_truncated"] is False
    assert still_covered.get("expired_through_message_id") == str(watermark)
    assert still_covered.get("history_truncated") is False
