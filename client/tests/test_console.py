import asyncio

import httpx
from teamup_client import _console_data
from teamup_client._console import serve_console
from teamup_client._console_data import ConsoleReader, ConsoleSession
from teamup_client.ipc import ClientError

ROOM = "247629c4-8e54-4d0e-8b25-7a8c43bf85b2"
BACKEND = "93e57f02-b2d7-4145-a968-9f0b60319c71"
REVIEWER = "a4a4610b-42b2-45d3-9002-c4ee89fe3427"


class Upstream:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, path, token, *, query=None):
        self.calls.append((method, path, token, query))
        if path == "/members/me":
            if token == "backend-secret":
                return {"member_id": BACKEND, "role_description": "Backend"}
            return {"member_id": REVIEWER, "role_description": "Reviewer"}
        if path == "/members":
            return {"members": [{"member_id": BACKEND, "role_description": "Backend"}]}
        if path == "/rooms":
            return {
                "rooms": [
                    {
                        "room_id": ROOM,
                        "name": "Compatibility",
                        "acked_board_version": 2 if token == "backend-secret" else None,
                        "current_board_version": 2,
                        "latest_message_id": 15,
                    }
                ],
            }
        if path == f"/rooms/{ROOM}/tasks":
            if token == "reviewer-secret":
                raise ClientError("board_ack_required", "Current board needed")
            return {
                "tasks": [{"participant_member_ids": [BACKEND]}],
                "next_after": None,
            }
        if path == f"/rooms/{ROOM}/messages":
            if token == "reviewer-secret":
                raise ClientError(
                    "board_ack_required", "Read and acknowledge the board"
                )
            return {
                "messages": [
                    {
                        "message_id": 15,
                        "sender_member_id": BACKEND,
                        "text": "Ready for review",
                        "mention_member_id": None,
                        "created_at": "2026-11-25T00:00:00Z",
                    }
                ]
            }
        raise AssertionError(path)


def sessions():
    return (
        ConsoleSession("primary", "codex", "api", "backend-secret"),
        ConsoleSession("review", "claude-code", "review", "reviewer-secret"),
    )


def test_workspace_and_room_reads_keep_session_scope_and_board_ack():
    async def scenario():
        request = Upstream()
        reader = ConsoleReader(sessions, request)
        workspace = await reader.workspace()
        assert [room["id"] for room in workspace["rooms"]] == [ROOM]
        assert {item["id"] for item in workspace["sessions"]} == {"primary", "review"}
        assert {item["session"] for item in workspace["rooms"][0]["memberships"]} == {
            "primary",
            "review",
        }
        status = {
            item["session"]: item["active_task"]
            for item in workspace["rooms"][0]["memberships"]
        }
        assert status == {"primary": True, "review": None}
        assert "-secret" not in str(workspace)
        room = await reader.room("primary", ROOM)
        assert [message["text"] for message in room["messages"]] == ["Ready for review"]
        assert room["board"] == {"current": 2, "acknowledged": 2}
        assert (
            "GET",
            f"/rooms/{ROOM}/messages",
            "backend-secret",
            {"before": 16, "limit": 30},
        ) in request.calls
        reviewer = await reader.room("review", ROOM)
        assert reviewer["state"] == "board_ack_required"
        assert reviewer["messages"] == []
        assert all(not path.endswith("/board") for _, path, _, _ in request.calls)

    asyncio.run(scenario())


def test_in_flight_history_is_discarded_when_local_approval_is_removed():
    async def scenario():
        active = list(sessions())
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path.endswith("/messages"):
                active.clear()
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(lambda: tuple(active), request).room(
            "primary", ROOM
        )
        assert result == {"state": "not_found"}

    asyncio.run(scenario())


def test_loopback_listener_requires_its_credential_and_serves_bundled_assets():
    async def scenario():
        async with serve_console(ConsoleReader(sessions, Upstream())) as url:
            origin, token = url.split("#")
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(), trust_env=False
            ) as client:
                home = await client.get(origin)
                assert home.status_code == 200
                assert "TeamUp" in home.text
                assert token not in home.text
                assert (await client.get(origin + "console.js")).status_code == 200
                logo = await client.get(origin + "jet-pair.png")
                assert logo.headers["content-type"] == "image/png"
                assert logo.content.startswith(b"\x89PNG\r\n\x1a\n")
                assert (await client.get(origin + "api/workspace")).status_code == 401
                auth = {"Authorization": "Bearer " + token}
                snapshot = await client.get(origin + "api/workspace", headers=auth)
                assert snapshot.status_code == 200
                assert snapshot.json()["rooms"][0]["id"] == ROOM
                assert snapshot.headers["cache-control"] == "no-store"
                bad_host = await client.get(
                    origin + "api/workspace", headers={**auth, "Host": "other.test"}
                )
                bad_origin = await client.get(
                    origin + "api/workspace",
                    headers={**auth, "Origin": "http://other.test"},
                )
                assert bad_host.status_code == bad_origin.status_code == 403
                assert (
                    await client.get(origin + "api/room", headers=auth)
                ).status_code == 400

    asyncio.run(scenario())


def test_task_actions_require_same_origin_and_keep_session_scope():
    task = "2f47cb81-36dc-42eb-8736-8f51a303455a"

    async def scenario():
        requests = []

        async def upstream(method, path, token, *, query=None, body=None):
            requests.append((method, path, token, query, body))
            if method == "POST" and token == "reviewer-secret":
                raise ClientError("board_ack_required", "Current board needed")
            if method == "GET":
                return {
                    "tasks": [{"task_id": task, "task_number": 1, "version": 1}],
                    "next_after": None,
                }
            return {"task_id": task, "task_number": 1, "status": "open"}

        async with serve_console(ConsoleReader(sessions, upstream)) as url:
            origin, token = url.split("#")
            async with httpx.AsyncClient(trust_env=False) as client:
                auth = {"Authorization": "Bearer " + token}
                endpoint = origin + f"api/tasks/{task}/join"
                params = {"session": "review", "room": ROOM}
                rejected = await client.post(endpoint, headers=auth, params=params)
                assert rejected.status_code == 403
                assert requests == []
                auth["Origin"] = origin.rstrip("/")
                rejected = await client.post(endpoint, headers=auth, params=params)
                assert rejected.status_code == 409
                assert rejected.json()["state"] == "board_ack_required"
                assert requests[-1][2] == "reviewer-secret"
                params["session"] = "primary"
                taken = await client.post(endpoint, headers=auth, params=params)
                assert (
                    taken.status_code == 200 and taken.json()["task"]["task_id"] == task
                )
                assert requests[-1] == (
                    "POST",
                    f"/rooms/{ROOM}/tasks/{task}/join",
                    "backend-secret",
                    None,
                    None,
                )
                page = await client.get(
                    origin + "api/tasks",
                    headers=auth,
                    params={"room": ROOM, "status": "open"},
                )
                assert page.json()["tasks"] == [
                    {
                        "task_id": task,
                        "task_number": "1",
                        "version": 1,
                        "visible_to": ["primary", "review"],
                    }
                ]
                assert "-secret" not in page.text

    asyncio.run(scenario())


def test_slow_task_observation_keeps_the_workspace_available(monkeypatch):
    monkeypatch.setattr(_console_data, "TASK_OBSERVATION_SECONDS", 0.02)

    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path.endswith("/tasks"):
                await asyncio.Event().wait()
            return await upstream(method, path, token, query=query)

        result = await asyncio.wait_for(ConsoleReader(sessions, request).workspace(), 1)
        assert [room["id"] for room in result["rooms"]] == [ROOM]
        assert all(
            membership["active_task"] is None
            for membership in result["rooms"][0]["memberships"]
        )

    asyncio.run(scenario())
