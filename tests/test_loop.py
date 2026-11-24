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

ROOT = Path(__file__).resolve().parents[1]


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
        self.enrollment_key = secrets.token_urlsafe(32)
        self.binding_key = secrets.token_urlsafe(32)
        self.config_path.touch(mode=0o600)
        self.config_path.write_text(
            json.dumps(
                {
                    "server_url": f"http://127.0.0.1:{port}",
                    "socket_path": str(self.socket_path),
                    "enrollment_key": self.enrollment_key,
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
            "client/src",
            ["serve", "--config", self.config_path],
            self.log,
        ) as child:
            wait_for(self.socket_path.is_socket, child)
            yield child

    @asynccontextmanager
    async def agent(self):
        launch = StdioServerParameters(
            command=sys.executable,
            args=["-m", "teamup_mcp.main"],
            env={
                "PYTHONPATH": f"{ROOT / 'plugin/mcp/src'}:{ROOT / 'client/src'}",
                "TEAMUP_SOCKET": str(self.socket_path),
                "TEAMUP_BINDING_KEY": self.binding_key,
            },
        )
        async with (
            stdio_client(launch) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            yield session

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
            alice = await tool(agent_a, "register", role_description="Backend")
            assert await tool(agent_a, "register", role_description="Backend") == alice
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
                    bob = await tool(agent_b, "register", role_description="Frontend")
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
                live = await tool(agent_a, "send_message", **ping)
                assert await tool(agent_a, "send_message", **ping) == live
                notice = await asyncio.to_thread(
                    b.wait_for_notice, live["message_id"], bob_daemon
                )
                assert notice["sender_member_id"] == alice["member_id"]
                assert "text" not in notice
                async with b.agent() as agent_b:
                    assert (await tool(agent_b, "notifications"))["notifications"] == [
                        notice
                    ]
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
                async with b.agent() as agent_b:
                    assert (await tool(agent_b, "notifications"))["notifications"] == [
                        notice,
                        backfill,
                    ]


def test_two_agent_mcp_loop_and_offline_notification():
    with tempfile.TemporaryDirectory(prefix="tu-loop-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(60):
                await run_exchange(Path(directory), available_port())

        asyncio.run(run())
