"""Room archive checks through the Server's member API."""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from _stack import Server, new_secret
from teamup_server.main import create_app


@pytest.fixture
def archive_api(ws):
    keys = {"creator": new_secret(), "other": new_secret()}
    server = Server(ws, keys).start()
    with closing(server.api()) as api:
        creator = api.enroll(keys["creator"], "Creator", session="creator")
        peer = api.enroll(keys["other"], "Peer", session="peer")
        outsider = api.enroll(keys["other"], "Outsider", session="outsider")
        yield api, creator, peer, outsider


@pytest.mark.scenario("NS-17A", "Room archive creator authorization and retries")
def test_creator_authorization_and_retries(archive_api):
    api, creator, peer, outsider = archive_api
    room = api.create_room(creator, "Complete the release")
    api.join(peer, room)
    path = f"/v1/rooms/{room}/archive"

    for token, status, code in (
        (new_secret(), 401, "unauthorized"),
        (outsider.token, 404, "not_found"),
        (peer.token, 403, "forbidden"),
    ):
        assert api.call("POST", path, token, error=code) == status
        assert api.call("GET", "/v1/rooms/archive", creator.token)["rooms"] == []

    receipt = api.call("POST", path, creator.token)
    assert receipt["room_id"] == room
    assert receipt["name"] == "Complete the release"
    assert receipt["archive_reason"] == "goal_reached"
    assert datetime.fromisoformat(receipt["archived_at"]).utcoffset() == timedelta()
    assert api.call("POST", path, creator.token) == receipt
    assert api.call("POST", path, peer.token, error="forbidden") == 403


@pytest.mark.scenario("NS-17B", "Archived rooms stop live operations and delivery")
def test_archive_freezes_live_operations_and_delivery(archive_api):
    api, creator, peer, outsider = archive_api
    room = api.create_room(creator, "Close delivery")
    api.join(peer, room)
    for member in (creator, peer):
        api.read_and_ack_board(member, room)
    path = f"/v1/rooms/{room}"
    chat = api.send(creator, room, "Pending mention", mention=peer.member_id)
    [event] = api.call("GET", "/v1/inbox", peer.token, params={"kind": "mention"})[
        "events"
    ]
    assert event["message"]["message_id"] == chat["message_id"]
    task = api.call(
        "POST",
        f"{path}/tasks",
        creator.token,
        json={"title": "Pending task", "client_task_id": "pending"},
    )
    assert [
        r["room_id"]
        for r in api.call("GET", "/v1/room-directory", outsider.token)["rooms"]
    ] == [room]
    assert [
        r["room_id"] for r in api.call("GET", "/v1/rooms", peer.token)["rooms"]
    ] == [room]

    api.call("POST", f"{path}/archive", creator.token)
    assert api.call("GET", "/v1/rooms", peer.token)["rooms"] == []
    assert api.call("GET", "/v1/room-directory", outsider.token)["rooms"] == []
    assert api.call("GET", "/v1/inbox", peer.token)["events"] == []
    assert (
        api.call(
            "POST", f"/v1/inbox/{event['event_id']}/ack", peer.token, error="not_found"
        )
        == 404
    )
    for method, suffix, request in (
        ("POST", "join", {}),
        ("POST", "leave", {}),
        ("POST", "messages", {"json": {"client_message_id": "late", "text": "late"}}),
        (
            "POST",
            "board",
            {"json": {"version": 1, "rules": "late", "protocol": "late"}},
        ),
        ("POST", f"tasks/{task['task_id']}/finish", {}),
        ("GET", "messages", {"params": {"delivery": "true"}}),
    ):
        assert (
            api.call(
                method,
                f"{path}/{suffix}",
                creator.token,
                error="room_archived",
                **request,
            )
            == 409
        )
    assert api.call("POST", f"{path}/join", outsider.token, error="not_found") == 404


@pytest.mark.scenario(
    "NS-17C", "Archived history, closing roster and member-scoped pages"
)
def test_archived_reads_and_pagination(archive_api):
    api, creator, peer, outsider = archive_api
    room = api.create_room(creator, "Saved history", rules="Keep the transcript")
    path = f"/v1/rooms/{room}"
    api.join(peer, room)
    board = api.read_and_ack_board(creator, room)
    api.read_and_ack_board(peer, room)
    chat = api.send(creator, room, "Find this in the archive")
    task = api.call(
        "POST",
        f"{path}/tasks",
        creator.token,
        json={"title": "Read after closing", "client_task_id": "archive-read"},
    )

    receipt = api.call("POST", f"{path}/archive", creator.token)
    second = api.create_room(creator, "Second archived room")
    second_receipt = api.call("POST", f"/v1/rooms/{second}/archive", creator.token)
    expected = sorted([receipt, second_receipt], key=lambda row: row["room_id"])
    first_page = api.call(
        "GET", "/v1/rooms/archive", creator.token, params={"limit": 1}
    )
    assert first_page == {"rooms": expected[:1], "next_after": expected[0]["room_id"]}
    assert api.call(
        "GET",
        "/v1/rooms/archive",
        creator.token,
        params={"limit": 1, "after": first_page["next_after"]},
    ) == {"rooms": expected[1:], "next_after": None}
    assert api.call("GET", "/v1/rooms/archive", peer.token)["rooms"] == [receipt]
    assert api.call("GET", "/v1/rooms/archive", outsider.token)["rooms"] == []
    assert api.call("GET", f"{path}/messages", outsider.token, error="not_found") == 404

    roster = api.call(
        "GET", f"{path}/members", creator.token, params={"include_membership": "true"}
    )
    assert {row["member_id"] for row in roster["members"]} == {
        creator.member_id,
        peer.member_id,
    }
    assert all("membership_id" not in row for row in roster["members"])
    api.call("POST", "/v1/members/me/retire", peer.token)
    assert api.call("GET", f"{path}/members", creator.token) == roster
    assert api.call("GET", f"{path}/board/view", creator.token) == {
        **board,
        "can_edit": False,
    }
    assert api.history(creator, room) == [chat]
    [match] = api.call(
        "GET", f"{path}/search", creator.token, params={"q": "Find this"}
    )["matches"]
    assert match["message_id"] == chat["message_id"]
    assert api.call("GET", f"{path}/tasks/{task['task_id']}", creator.token) == task


@pytest.mark.scenario(
    "NS-17D", "Inactivity follows committed chat time and the startup sweep"
)
def test_archive_inactivity_at_exact_deadline(ws):
    async def run():
        key, token, peer_token = (new_secret() for _ in range(3))
        start = datetime(2026, 1, 1, tzinfo=UTC)
        now = start
        app = create_app(ws.path("inactivity.db"), {"owner": key}, clock=lambda: now)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:

            async def call(
                method, path, *, credential=token, body=None, rejected=False
            ):
                response = await client.request(
                    method,
                    path,
                    headers={"Authorization": f"Bearer {credential}"},
                    json=body,
                )
                result = response.json()
                if rejected:
                    assert response.status_code == 409, result
                    assert result["error"]["code"] == "room_archived"
                    return None
                assert response.status_code == 200, result
                return result

            async def swept(expected):
                # The app's real lifespan starts the production maintenance worker.
                async with app.router.lifespan_context(app):
                    async with asyncio.timeout(5):
                        while True:
                            archives = (await call("GET", "/v1/rooms/archive"))["rooms"]
                            archived = {row["room_id"]: row for row in archives}
                            if set(archived) == set(expected):
                                assert all(
                                    row["archive_reason"] == "inactive"
                                    for row in archives
                                )
                                return archived
                            await asyncio.sleep(0.02)

            for identity, credential in (("creator", token), ("peer", peer_token)):
                await call(
                    "POST",
                    "/v1/members",
                    credential=key,
                    body={
                        "harness_instance_id": "inactivity",
                        "session_key": identity,
                        "name": identity,
                        "description": "Checks archive inactivity.",
                        "member_token": credential,
                    },
                )
            empty, active = [
                (await call("POST", "/v1/rooms", body={"name": name}))["room_id"]
                for name in ("empty", "with-chat")
            ]
            now = start + timedelta(hours=23)
            await call("POST", f"/v1/rooms/{empty}/join", credential=peer_token)
            await call(
                "POST",
                f"/v1/rooms/{empty}/board",
                body={"version": 1, "rules": "New rules", "protocol": "New protocol"},
            )
            for room in (empty, active):
                board = await call("GET", f"/v1/rooms/{room}/board")
                await call(
                    "POST",
                    f"/v1/rooms/{room}/board/ack",
                    body={"version": board["version"]},
                )
            await call(
                "POST",
                f"/v1/rooms/{empty}/tasks",
                body={
                    "client_task_id": "inactivity-task",
                    "title": "Archive ignores task activity",
                },
            )
            chat_url = f"/v1/rooms/{active}/messages"
            first_chat = {"client_message_id": "only-chat", "text": "Refresh once"}
            original = await call("POST", chat_url, body=first_chat)

            now = start + timedelta(hours=24)
            await call(
                "POST",
                f"/v1/rooms/{empty}/messages",
                body=first_chat | {"client_message_id": "late-empty"},
                rejected=True,
            )
            archives = await swept({empty})
            assert datetime.fromisoformat(archives[empty]["archived_at"]) == now
            assert [
                room["room_id"] for room in (await call("GET", "/v1/rooms"))["rooms"]
            ] == [active]

            now = start + timedelta(hours=46)
            assert await call("POST", chat_url, body=first_chat) == original
            now = start + timedelta(hours=47)
            await call(
                "POST",
                chat_url,
                body=first_chat | {"client_message_id": "late"},
                rejected=True,
            )
            archives = await swept({empty, active})
            assert datetime.fromisoformat(archives[active]["archived_at"]) == now
            assert await call("POST", f"/v1/rooms/{active}/archive") == archives[active]
            assert (await call("GET", chat_url))["messages"] == [original]

    asyncio.run(asyncio.wait_for(run(), 25))
