"""Exercise owner CLI and live MCP sessions on a shared member binding."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from _stack import (
    Developer,
    Server,
    clean_env,
    command,
    new_secret,
    run_process,
    run_with_tty,
    tool,
    wait_for,
)
from teamup_client.ipc import attach


def manage_member(developer, action, *args):
    argv, env = command("client")
    result = run_process(
        [*argv, "members", "--config", str(developer.config_path), action, *args],
        env=clean_env() | env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


async def walk(developer):
    async with (
        developer.agent() as old,
        developer.agent() as shared,
        developer.agent(index=1) as successor,
    ):
        member = await tool(
            old,
            "register",
            name="Developer",
            description="Implements changes and coordinates handoffs with its peer.",
        )
        assert (
            await tool(shared, "register", role_description="Still the old member")
            == member
        )
        member_id = member["member_id"]
        renamed = await asyncio.to_thread(
            manage_member, developer, "rename", member_id, "Backend · Accounts"
        )
        assert renamed == {
            **member,
            "name": "Backend · Accounts",
            "role_description": "Backend · Accounts",
        }
        assert await tool(shared, "register", role_description="Ignored") == renamed
        replacement = await tool(
            successor,
            "register",
            name="QA · Accounts",
            description="Verifies account behavior and coordinates handoffs.",
        )
        assert replacement["member_id"] != member_id

        room_id = (await tool(successor, "create_room", name="Account handoff"))[
            "room_id"
        ]
        await tool(old, "join_room", room_id=room_id)
        room_members = await tool(successor, "list_room_members", room_id=room_id)
        original = next(
            row for row in room_members["members"] if row["member_id"] == member_id
        )
        room_description = "Coordinates the account handoff in this room."
        override = await asyncio.to_thread(
            manage_member,
            developer,
            "room-responsibility",
            member_id,
            room_id,
            original["membership_id"],
            "--description",
            room_description,
        )
        assert override == {"description": room_description}
        room_members = await tool(successor, "list_room_members", room_id=room_id)
        assert {row["member_id"]: row["description"] for row in room_members["members"]} == {
            member_id: room_description,
            replacement["member_id"]: replacement["description"],
        }
        assert await tool(shared, "register", role_description="Ignored") == renamed
        cleared = await asyncio.to_thread(
            manage_member,
            developer,
            "room-responsibility",
            member_id,
            room_id,
            original["membership_id"],
            "--clear",
        )
        assert cleared == {"description": None}
        room_members = await tool(successor, "list_room_members", room_id=room_id)
        assert original in room_members["members"]
        for agent in (successor, old):
            board = await tool(agent, "get_board", room_id=room_id)
            await tool(agent, "ack_board", room_id=room_id, version=board["version"])
        sent = await tool(
            shared,
            "send_message",
            room_id=room_id,
            client_message_id="handoff",
            text="QA · Accounts is taking over.",
        )

        assert await tool(old, "leave_room", room_id=room_id) == {"room_id": room_id}
        assert await tool(shared, "list_rooms") == {"rooms": []}
        await tool(shared, "join_room", room_id=room_id)
        board = await tool(shared, "get_board", room_id=room_id)
        await tool(shared, "ack_board", room_id=room_id, version=board["version"])

        retired = await asyncio.to_thread(manage_member, developer, "retire", member_id)
        assert retired == {"member_id": member_id, "retired": True}
        await tool(
            shared,
            "send_message",
            room_id=room_id,
            client_message_id="after-retirement",
            text="This member can no longer send.",
            error="member_retired",
        )
        await tool(
            old, "register", role_description="Developer", error="member_retired"
        )
        assert await tool(successor, "find_members") == {"members": [replacement]}
        assert await tool(successor, "read_messages", room_id=room_id) == {
            "messages": [{**sent, "sender_retired": True}]
        }


@pytest.fixture
def developer(ws):
    server = Server(ws, {})
    password = new_secret()
    argv, env = command("server")
    status, output = run_with_tty(
        [*argv, "accounts", "--db", str(server.db), "create", "alice"],
        [password, password],
        env,
    )
    assert status == 0, output
    server.start()

    developer = Developer(ws, "alice", server, bindings=2, credentials=False)
    argv, env = command("client")
    status, output = run_with_tty(
        [*argv, "login", "--config", str(developer.config_path), "--username", "alice"],
        [password],
        env,
    )
    assert status == 0, output

    return developer, password


def test_retirement_revokes_every_mcp_connection_using_the_same_member(developer):
    developer, _ = developer
    developer.start()
    asyncio.run(asyncio.wait_for(walk(developer), 30))


@asynccontextmanager
async def console_client(developer, password):
    argv, env = command("client")
    proc = developer.ws.spawn(
        "console",
        [*argv, "serve", "--config", str(developer.config_path), "--console"],
        env,
    )
    try:

        def endpoint():
            return next(
                (
                    line
                    for line in proc.text().splitlines()
                    if line.startswith("http://")
                ),
                None,
            )

        url = await asyncio.to_thread(wait_for, endpoint, proc, what="console address")
        origin, token = url.split("#")
        async with httpx.AsyncClient(
            base_url=origin,
            headers={"Authorization": f"Bearer {token}", "Origin": origin.rstrip("/")},
            trust_env=False,
        ) as console:
            response = await console.post(
                "/api/login", json={"username": "alice", "password": password}
            )
            response.raise_for_status()
            yield console
    finally:
        await asyncio.to_thread(proc.stop)


def test_console_departure_and_retirement_reach_mcp_and_persist_history(developer):
    developer, password = developer

    async def scenario():
        async with (
            console_client(developer, password) as console,
            developer.agent() as target,
            developer.agent(index=1) as peer,
        ):
            target_profile = await tool(
                target,
                "register",
                name="Target",
                description="Receives departure and retirement updates.",
            )
            peer_profile = await tool(
                peer,
                "register",
                name="Peer",
                description="Maintains shared room access during the handoff.",
            )
            target_id = target_profile["member_id"]
            paths = {
                name: (await tool(peer, "create_room", name=name))["room_id"]
                for name in ("Depart", "Archive", "Passive")
            }
            for room_id in paths.values():
                await tool(target, "join_room", room_id=room_id)
                for agent in (target, peer):
                    board = await tool(agent, "get_board", room_id=room_id)
                    await tool(
                        agent, "ack_board", room_id=room_id, version=board["version"]
                    )
            await tool(
                target,
                "send_message",
                room_id=paths["Archive"],
                client_message_id="archive",
                text="Keep this history",
            )
            workspace = (await console.get("/api/workspace")).json()
            session_id = next(
                item["id"]
                for item in workspace["sessions"]
                if item["member_id"] == target_id
            )
            room = next(
                item for item in workspace["rooms"] if item["id"] == paths["Depart"]
            )
            membership = next(
                item for item in room["memberships"] if item["session"] == session_id
            )
            params = {
                "session": session_id,
                "room": paths["Depart"],
                "membership_id": membership["membership_id"],
            }
            rejected = await console.post(
                "/api/sessions/leave",
                params=params,
                headers={"Origin": "https://example.org"},
            )
            assert rejected.status_code == 403

            # Attach a real local IPC lease and receive the harness driver's frames.
            messages = asyncio.Queue()

            async def receive(reader, writer):
                await reader.readline()
                frame = json.loads(await reader.readline())
                messages.put_nowait(frame["message"]["content"])
                writer.close()
                await writer.wait_closed()

            harness = developer.ws.path("claude.sock")
            socket = await asyncio.start_unix_server(receive, harness)
            binding = developer.bindings[0]
            async with (
                socket,
                attach(
                    developer.socket_path,
                    binding["binding_key"],
                    {"kind": "claude-code", "id": binding["session_key"]},
                    str(harness),
                    new_secret(),
                ),
            ):
                # A member bearer can record its own departure, while only the
                # local owner action can send a message into an attached harness.
                async with httpx.AsyncClient(
                    base_url=developer.server.url,
                    headers={"Authorization": f"Bearer {binding['member_token']}"},
                    trust_env=False,
                ) as server:
                    departure = await server.post(
                        f"/v1/rooms/{paths['Passive']}/leave?notify=true"
                    )
                    assert departure.status_code == 200
                    async with asyncio.timeout(5):
                        while (await server.get("/v1/inbox?kinds=room_left")).json()[
                            "events"
                        ]:
                            await asyncio.sleep(0.02)
                    assert messages.empty()

                for _ in range(2):
                    response = await console.post("/api/sessions/leave", params=params)
                    assert response.status_code == 200, response.text
                text = await asyncio.wait_for(messages.get(), timeout=10)
                assert paths["Depart"] in text
            recorded = (await tool(target, "notifications"))["notifications"]
            assert [(item["kind"], item["room_id"]) for item in recorded] == [
                ("room_left", paths["Passive"]),
                ("room_left", paths["Depart"]),
            ]
            assert messages.empty()
            assert not (await tool(peer, "notifications"))["notifications"]
            await tool(target, "get_board", room_id=paths["Depart"], error="not_found")

            for _ in range(2):
                response = await console.post(
                    "/api/sessions/retire", params={"session": session_id}
                )
                assert response.status_code == 200, response.text
            await tool(target, "list_rooms", error="member_retired")
            await tool(
                target, "register", role_description="Target", error="member_retired"
            )
            assert await tool(peer, "find_members") == {"members": [peer_profile]}
            result = await tool(peer, "retire_identity")
            assert result == {
                "member_id": peer_profile["member_id"],
                "role_description": peer_profile["role_description"],
                "retired": True,
            }
            assert await tool(peer, "retire_identity") == result

        async with console_client(developer, password) as restarted:
            inventory = (await restarted.get("/api/workspace")).json()
            assert not inventory["sessions"]
            assert {entry["member_id"] for entry in inventory["retired_sessions"]} == {
                target_id,
                peer_profile["member_id"],
            }
            listing = (
                await restarted.get("/api/retired", params={"session": session_id})
            ).json()
            assert listing == {
                "state": "ready",
                "conversations": [
                    {
                        "kind": "room",
                        "conversation_id": paths["Archive"],
                        "label": "Archive",
                    },
                ],
            }
            page = (
                await restarted.get(
                    "/api/retired",
                    params={
                        "session": session_id,
                        "kind": "room",
                        "conversation": paths["Archive"],
                    },
                )
            ).json()
            assert [item["text"] for item in page["messages"]] == ["Keep this history"]
            notices = (
                await restarted.get(
                    "/api/retired", params={"session": session_id, "after": 0}
                )
            ).json()["notifications"]
            assert notices == [
                {**item, "event_id": str(item["event_id"])} for item in recorded
            ]

    asyncio.run(asyncio.wait_for(scenario(), 45))


def test_transferred_leadership_controls_console_and_mcp_room_actions(developer):
    developer, password = developer

    async def exercise():
        async with (
            console_client(developer, password) as console,
            developer.agent() as creator,
            developer.agent(index=1) as sibling,
        ):
            owner = await tool(
                creator,
                "register",
                name="Creator",
                description="Creates the room and manages its membership.",
            )
            guest = await tool(
                sibling,
                "register",
                name="Sibling",
                description="Joins the room and verifies membership controls.",
            )
            room = (await tool(creator, "create_room", name="Moderation"))["room_id"]
            await tool(sibling, "join_room", room_id=room)
            snapshot = (await console.get("/api/workspace")).json()
            bindings = {session["member_id"]: session["id"] for session in snapshot["sessions"]}
            owner_session = bindings[owner["member_id"]]
            guest_session = bindings[guest["member_id"]]
            memberships = {row["session"]: row for row in snapshot["rooms"][0]["memberships"]}
            assert memberships[owner_session]["can_remove_members"] is True
            assert not memberships[guest_session].get("can_remove_members", False)
            roster = (
                await console.get("/api/room-members", params={"room": room, "session": guest_session})
            ).json()
            target = next(row for row in roster["members"] if row["id"] == guest["member_id"])
            inputs = {"room_id": room, "member_id": guest["member_id"],
                      "membership_id": target["membership_id"]}
            board = (
                await console.get("/api/room-board", params={"room": room, "session": guest_session})
            ).json()
            assert board["board"]["can_edit"] is False

            async def transfer(source, destination):
                async with asyncio.timeout(20):
                    while True:
                        ranking = await tool(source, "room_leadership_candidates", room_id=room)
                        choices = ranking["candidates"]
                        if choices:
                            break
                        await asyncio.sleep(0.1)
                [choice] = choices
                assert choice["member_id"] == destination
                receipt = await tool(
                    source, "handoff_room_leadership", room_id=room, mode="choose",
                    leadership_epoch=ranking["leadership_epoch"], member_id=destination,
                    membership_id=choice["membership_id"],
                )
                assert receipt["leader_member_id"] == destination

            await transfer(creator, guest["member_id"])
            board = (
                await console.get("/api/room-board", params={"room": room, "session": guest_session})
            ).json()
            assert board["board"]["can_edit"] is True
            published = await console.post(
                "/api/room-board",
                params={"room": room, "session": guest_session},
                json={"version": 1, "rules": "Sibling rules.", "protocol": "Sibling protocol."},
            )
            assert published.status_code == 200, published.text
            assert published.json()["board"]["version"] == 2
            await transfer(sibling, owner["member_id"])
            params = {"room": room, "member": guest["member_id"], "session": owner_session}
            removed = await console.post(
                "/api/room-members/remove", params=params,
                json={"membership_id": inputs["membership_id"]},
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["state"] == "ready"
            assert removed.json()["departure_event_id"] is not None
            room_path = {"room": room, "session": guest_session}
            assert (await console.get("/api/room", params=room_path)).json()["state"] == "not_found"
            await tool(sibling, "join_room", room_id=room)
            fresh = await tool(creator, "list_room_members", room_id=room)
            member = next(row for row in fresh["members"] if row["member_id"] == guest["member_id"])
            await tool(creator, "remove_room_member", error="membership_changed", **inputs)
            inputs["membership_id"] = member["membership_id"]
            assert (await tool(creator, "remove_room_member", **inputs))["member_id"] == guest["member_id"]
            assert await tool(sibling, "list_rooms") == {"rooms": []}
            leave = await console.post("/api/sessions/leave", params={"room": room, "session": owner_session})
            assert leave.status_code == 200
            (preview,) = (await console.get("/api/workspace")).json()["rooms"]
            assert preview["memberships"] == []
            assert preview["latest_message_id"] is None

    asyncio.run(exercise())
