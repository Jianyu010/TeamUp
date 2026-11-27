import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
# Stands in for osascript: records each desktop notice instead of showing it.
RECORDER = (
    "import json, sys, time\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as log:\n"
    "    log.write(json.dumps([time.time(), *sys.argv[2:]]) + '\\n')\n"
)


def wait_for(predicate, child=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child and child.poll() is not None:
            raise AssertionError(
                f"{child.args[2]} exited with status {child.returncode}"
            )
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for TeamUp readiness or notification")


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def tcp_is_ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


@contextmanager
def process(module, source, args, log, env=None):
    with log.open("ab") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", module, *map(str, args)],
            cwd=ROOT,
            env=os.environ | {"PYTHONPATH": str(ROOT / source)} | (env or {}),
            stdout=output,
            stderr=output,
        )
        try:
            yield child
        except BaseException:
            sys.stderr.write(log.read_text(errors="replace"))
            raise
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


class Developer:
    def __init__(self, root, name, port):
        directory = root / name
        directory.mkdir(mode=0o700)
        self.config_path = directory / "client.json"
        self.socket_path = directory / "client.sock"
        self.log = directory / "client.log"
        self.desktop = directory / "desktop.jsonl"
        self.enrollment_key = secrets.token_urlsafe(32)
        self.binding_key = secrets.token_urlsafe(32)
        self.config_path.touch(mode=0o600)
        self.config_path.write_text(
            json.dumps(
                {
                    "server_url": f"http://127.0.0.1:{port}",
                    "socket_path": str(self.socket_path),
                    "enrollment_key": self.enrollment_key,
                    "notify_command": [
                        sys.executable,
                        "-c",
                        RECORDER,
                        str(self.desktop),
                    ],
                    "bindings": [
                        {
                            "harness_instance_id": "desktop",
                            "session_key": "test-agent",
                            "binding_key": self.binding_key,
                            "member_token": secrets.token_urlsafe(32),
                        }
                    ],
                }
            )
        )

    @contextmanager
    def daemon(self):
        with process(
            "teamup_client.main",
            "client/shared/src",
            ["serve", "--config", self.config_path],
            self.log,
        ) as child:
            wait_for(self.socket_path.is_socket, child)
            yield child

    @asynccontextmanager
    async def agent(self, env=None):
        launch = StdioServerParameters(
            command=sys.executable,
            args=["-m", "teamup_mcp.main"],
            env={
                "PYTHONPATH": f"{ROOT / 'plugin/mcp/src'}:{ROOT / 'client/shared/src'}",
                "TEAMUP_SOCKET": str(self.socket_path),
                "TEAMUP_BINDING_KEY": self.binding_key,
                **(env or {}),
            },
        )
        async with (
            stdio_client(launch) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            yield session

    def desktop_notices(self):
        if not self.desktop.exists():
            return []
        return [json.loads(line) for line in self.desktop.read_text().splitlines()]

    def wait_for_notice(self, message_id, child):
        def find():
            if not self.log.exists():
                return None
            for line in self.log.read_text(errors="replace").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("message_id") == message_id:
                    return value
            return None

        return wait_for(find, child)


async def tool(agent, operation, error=None, **arguments):
    response = await agent.call_tool(f"teamup_{operation}", arguments)
    if error:
        assert response.isError
        assert error in response.content[0].text
        return None
    assert not response.isError, response.content
    return response.structuredContent


async def run_exchange(root, port):
    a = Developer(root, "alice", port)
    b = Developer(root, "bob", port)
    keys = json.dumps({"alice": a.enrollment_key, "bob": b.enrollment_key})

    with (
        process(
            "teamup_server.main",
            "server/src",
            ["--db", root / "server.db", "--port", port],
            root / "server.log",
            {"TEAMUP_ENROLLMENT_KEYS": keys},
        ) as server,
        a.daemon(),
    ):
        wait_for(lambda: tcp_is_ready(port), server)
        async with a.agent() as agent_a:
            alice = await tool(
                agent_a,
                "register",
                name="Backend",
                description="Owns backend API behavior and service handoffs.",
            )
            assert (
                await tool(
                    agent_a,
                    "register",
                    name="Backend",
                    description="Owns backend API behavior and service handoffs.",
                )
                == alice
            )
            room = await tool(
                agent_a,
                "create_room",
                name="API compatibility",
                rules="Share test data",
            )
            room_id = room["room_id"]
            await tool(
                agent_a,
                "ack_board",
                error="invalid_request",
                room_id=room_id,
                version=True,
            )
            await tool(
                agent_a,
                "ack_board",
                error="board_read_required",
                room_id=room_id,
                version=1,
            )
            board = await tool(agent_a, "get_board", room_id=room_id)
            await tool(agent_a, "ack_board", room_id=room_id, version=board["version"])
            await tool(
                agent_a,
                "send_message",
                error="invalid_request",
                room_id=room_id,
                client_message_id="spoof",
                text="test",
                sender_member_id=alice["member_id"],
            )

            with b.daemon() as bob_daemon:
                async with b.agent() as agent_b:
                    bob = await tool(
                        agent_b,
                        "register",
                        name="Frontend",
                        description="Owns interface behavior and coordinates API integration.",
                    )
                    assert alice["member_id"] != bob["member_id"]
                    assert (await tool(agent_a, "find_members", q="FRONT"))[
                        "members"
                    ] == [bob]
                    await tool(agent_b, "get_board", error="not_found", room_id=room_id)
                    await tool(agent_b, "join_room", room_id=room_id)
                    await tool(
                        agent_b,
                        "read_messages",
                        error="board_ack_required",
                        room_id=room_id,
                    )
                    assert (await tool(agent_b, "list_rooms"))["rooms"] == [
                        {
                            "room_id": room_id,
                            "name": "API compatibility",
                            "acked_board_version": None,
                            "current_board_version": 1,
                            "latest_message_id": None,
                        }
                    ]
                    board = await tool(agent_b, "get_board", room_id=room_id)
                    assert board["rules"] == "Share test data"
                    await tool(
                        agent_b, "ack_board", room_id=room_id, version=board["version"]
                    )
                    await tool(
                        agent_b,
                        "send_message",
                        room_id=room_id,
                        client_message_id="ready",
                        text="Frontend is ready.",
                    )
                messages = (await tool(agent_a, "read_messages", room_id=room_id))[
                    "messages"
                ]
                assert [item["text"] for item in messages] == ["Frontend is ready."]

                ping = {
                    "room_id": room_id,
                    "client_message_id": "live-ping",
                    "text": "Can you review the response shape?",
                    "mention_member_id": bob["member_id"],
                }
                sent = time.time()
                live = await tool(agent_a, "send_message", **ping)
                assert await tool(agent_a, "send_message", **ping) == live
                notice = await asyncio.to_thread(
                    b.wait_for_notice, live["message_id"], bob_daemon
                )
                assert notice["sender_member_id"] == alice["member_id"]
                assert "text" not in notice
                # Bob's session is idle and has no wake permission: the owner
                # still gets one desktop notice within 5 s and no turn starts.
                [shown] = await asyncio.to_thread(
                    wait_for, b.desktop_notices, bob_daemon
                )
                assert shown[0] - sent < 5
                assert shown[1:] == [
                    "Backend mentioned you",
                    "API compatibility",
                    "Can you review the response shape?",
                ]
                async with b.agent() as agent_b:
                    # Any tool result carries the unread hint until notices are read.
                    response = await agent_b.call_tool(
                        "teamup_get_board", {"room_id": room_id}
                    )
                    assert response.content[-1].text == (
                        "TeamUp: 1 unread mention. "
                        "Call teamup_notifications to read them."
                    )
                    assert (await tool(agent_b, "notifications"))["notifications"] == [
                        notice
                    ]
                    response = await agent_b.call_tool(
                        "teamup_get_board", {"room_id": room_id}
                    )
                    assert len(response.content) == 1
                    messages = (await tool(agent_b, "read_messages", room_id=room_id))[
                        "messages"
                    ]
                    assert [item["text"] for item in messages] == [
                        "Frontend is ready.",
                        "Can you review the response shape?",
                    ]
                    assert (await tool(agent_b, "list_rooms"))["rooms"] == [
                        {
                            "room_id": room_id,
                            "name": "API compatibility",
                            "acked_board_version": 1,
                            "current_board_version": 1,
                            "latest_message_id": live["message_id"],
                        }
                    ]

            offline = await tool(
                agent_a,
                "send_message",
                **(
                    ping
                    | {
                        "client_message_id": "offline-ping",
                        "text": "Check this when you get back.",
                    }
                ),
            )
            assert (await tool(agent_a, "list_rooms"))["rooms"] == [
                {
                    "room_id": room_id,
                    "name": "API compatibility",
                    "acked_board_version": 1,
                    "current_board_version": 1,
                    "latest_message_id": offline["message_id"],
                }
            ]
            with b.daemon() as bob_daemon:
                backfill = await asyncio.to_thread(
                    b.wait_for_notice, offline["message_id"], bob_daemon
                )
                # Reconnecting delivers the offline mention exactly once.
                await asyncio.to_thread(
                    wait_for, lambda: len(b.desktop_notices()) == 2, bob_daemon
                )
                assert b.desktop_notices()[1][3] == "Check this when you get back."
                async with b.agent() as agent_b:
                    assert (await tool(agent_b, "notifications"))["notifications"] == [
                        notice,
                        backfill,
                    ]
            with b.daemon():
                await asyncio.sleep(1)
            assert len(b.desktop_notices()) == 2


def test_two_agent_mcp_loop_and_offline_notification():
    with tempfile.TemporaryDirectory(prefix="tu-loop-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(60):
                await run_exchange(Path(directory), available_port())

        asyncio.run(run())


async def run_dm_exchange(root, port):
    a = Developer(root, "dm-alice", port)
    b = Developer(root, "dm-bob", port)
    keys = json.dumps({"alice": a.enrollment_key, "bob": b.enrollment_key})

    with (
        process(
            "teamup_server.main",
            "server/src",
            ["--db", root / "dm-server.db", "--port", port],
            root / "dm-server.log",
            {"TEAMUP_ENROLLMENT_KEYS": keys},
        ) as server,
        a.daemon() as alice_daemon,
    ):
        wait_for(lambda: tcp_is_ready(port), server)
        async with a.agent() as agent_a:
            alice = await tool(
                agent_a,
                "register",
                name="Backend",
                description="Owns backend API behavior and service handoffs.",
            )
            with b.daemon() as bob_daemon:
                async with b.agent() as agent_b:
                    bob = await tool(
                        agent_b,
                        "register",
                        name="Frontend",
                        description="Owns interface behavior and coordinates API integration.",
                    )
                    assert (await tool(agent_a, "find_members", q="FRONT"))[
                        "members"
                    ] == [bob]

                    dm = await tool(agent_a, "open_dm", member_id=bob["member_id"])
                    assert dm == {
                        "dm_id": dm["dm_id"],
                        "peer_member_id": bob["member_id"],
                    }
                    sent = await tool(
                        agent_a,
                        "send_dm",
                        dm_id=dm["dm_id"],
                        client_message_id="dm-1",
                        text="Private handoff for the adapter.",
                    )
                    assert set(sent) == {
                        "message_id",
                        "dm_id",
                        "sender_member_id",
                        "client_message_id",
                        "text",
                        "created_at",
                    }
                    retry = await tool(
                        agent_a,
                        "send_dm",
                        dm_id=dm["dm_id"],
                        client_message_id="dm-1",
                        text="Private handoff for the adapter.",
                    )
                    assert retry == sent
                    await tool(
                        agent_a,
                        "send_dm",
                        error="conflict",
                        dm_id=dm["dm_id"],
                        client_message_id="dm-1",
                        text="Changed intent",
                    )

                    notice = await asyncio.to_thread(
                        b.wait_for_notice, sent["message_id"], bob_daemon
                    )
                    assert notice == {
                        "event_id": notice["event_id"],
                        "kind": "dm",
                        "dm_id": dm["dm_id"],
                        "message_id": sent["message_id"],
                        "sender_member_id": alice["member_id"],
                    }
                    assert "text" not in notice
                    # A DM notice never becomes a room mention: no desktop
                    # reminder and no unread-mention hint on later tool calls.
                    await asyncio.sleep(1)
                    assert b.desktop_notices() == []
                    response = await agent_b.call_tool("teamup_find_members", {})
                    assert len(response.content) == 1
                    assert (await tool(agent_b, "notifications"))["notifications"] == [
                        notice
                    ]
                    assert await tool(
                        agent_b, "open_dm", member_id=alice["member_id"]
                    ) == {"dm_id": dm["dm_id"], "peer_member_id": alice["member_id"]}
                    assert await tool(agent_b, "read_dm", dm_id=dm["dm_id"]) == {
                        "messages": [sent]
                    }

                    reply = await tool(
                        agent_b,
                        "send_dm",
                        dm_id=dm["dm_id"],
                        client_message_id="dm-b1",
                        text="Received, continuing privately.",
                    )
                    alice_notice = await asyncio.to_thread(
                        a.wait_for_notice, reply["message_id"], alice_daemon
                    )
                    assert alice_notice["kind"] == "dm"
                    assert alice_notice["dm_id"] == dm["dm_id"]
                    assert alice_notice["message_id"] == reply["message_id"]
                    assert alice_notice["sender_member_id"] == bob["member_id"]

            with b.daemon():
                async with b.agent() as agent_b:
                    notifications = (await tool(agent_b, "notifications"))[
                        "notifications"
                    ]
                    assert notifications == [notice]
                    recovered = await tool(agent_b, "read_dm", dm_id=dm["dm_id"])
                    assert recovered == {"messages": [sent, reply]}

        server_log = (root / "dm-server.log").read_text(errors="replace")
        assert "Private handoff for the adapter." not in server_log
        assert "Received, continuing privately." not in server_log


def test_dm_mcp_exchange_notices_and_recovery():
    with tempfile.TemporaryDirectory(prefix="tu-dm-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(60):
                await run_dm_exchange(Path(directory), available_port())

        asyncio.run(run())


async def run_focused_room_exchange(root, port):
    alice = Developer(root, "alice", port)
    bob = Developer(root, "bob", port)
    keys = json.dumps({"alice": alice.enrollment_key, "bob": bob.enrollment_key})
    with (
        process(
            "teamup_server.main",
            "server/src",
            ["--db", root / "server.db", "--port", port],
            root / "server.log",
            {"TEAMUP_ENROLLMENT_KEYS": keys},
        ) as server,
        alice.daemon(),
        bob.daemon(),
    ):
        wait_for(lambda: tcp_is_ready(port), server)
        async with alice.agent() as leader, bob.agent() as peer:
            await tool(leader, "register", name="Build Lead",
                       description="Coordinates the build and team handoffs.")
            bob_member = await tool(peer, "register", name="Test Lead",
                                    description="Coordinates test coverage.")
            bob_id = bob_member["member_id"]
            parent = await tool(leader, "create_room", name="Release")
            parent_id = parent["room_id"]
            invite = {
                "room_id": parent_id,
                "member_id": bob_id,
                "client_request_id": "release-invitation",
            }
            receipt = await tool(leader, "invite_to_room", **invite)
            assert await tool(leader, "invite_to_room", **invite) == receipt
            assert await tool(peer, "list_rooms") == {"rooms": []}
            dm = (await tool(peer, "read_dm", dm_id=receipt["dm_id"]))["messages"]
            assert len(dm) == 1 and parent_id in dm[0]["text"]
            await tool(peer, "join_room", room_id=parent_id)

            create_child = {
                "parent_room_id": parent_id,
                "name": "Focused verification",
                "client_request_id": "verification-room",
                "rules": "Verify release behavior",
                "history_policy": "retained",
            }
            child = await tool(leader, "create_linked_room", **create_child)
            assert await tool(leader, "create_linked_room", **create_child) == child
            assert child["parent_room_id"] == parent_id
            child_id = child["room_id"]
            child_page = await tool(peer, "list_child_rooms", room_id=parent_id, limit=1)
            assert [row["room_id"] for row in child_page["rooms"]] == [child_id]
            assert (await tool(peer, "list_rooms"))["rooms"][0]["room_id"] == parent_id
            await tool(
                leader, "archive_room", error="room_has_live_children", room_id=parent_id
            )
            await tool(
                leader,
                "extend_room_lease",
                error="invalid_request",
                room_id=child_id,
                seconds=True,
                expected_expires_at=child["expires_at"],
            )

            async with asyncio.timeout(20):
                while True:
                    ranking = await tool(leader, "room_leadership_candidates", room_id=parent_id)
                    candidate = next(
                        (row for row in ranking["candidates"] if row["member_id"] == bob_id),
                        None,
                    )
                    if candidate:
                        break
                    await asyncio.sleep(0.1)
            handoff = await tool(
                leader,
                "handoff_room_leadership",
                room_id=parent_id,
                leadership_epoch=ranking["leadership_epoch"],
                mode="choose",
                member_id=bob_id,
                membership_id=candidate["membership_id"],
                leave=True,
            )
            assert handoff["left"] and not handoff["pending"]
            assert handoff["leader_member_id"] == bob_id
            assert handoff["leadership_epoch"] > ranking["leadership_epoch"]
            await tool(
                peer,
                "confirm_room_leadership",
                error="conflict",
                room_id=parent_id,
                leadership_epoch=ranking["leadership_epoch"],
            )


def test_focused_room_mcp_exchange():
    with tempfile.TemporaryDirectory(prefix="tu-focus-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(60):
                await run_focused_room_exchange(Path(directory), available_port())

        asyncio.run(run())
