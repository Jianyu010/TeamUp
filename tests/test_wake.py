"""Acceptance for mention wake: member-to-session map, owner approval, delivery."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from test_loop import (
    ROOT,
    Developer,
    available_port,
    process,
    tcp_is_ready,
    tool,
    wait_for,
)


class FakeClaudeCode:
    """Stands in for one Claude Code session's messaging socket."""

    def __init__(self, directory, name):
        self.socket_path = directory / f"{name}.sock"
        self.session_id = f"cc-{name}-{secrets.token_hex(4)}"
        self.token = secrets.token_urlsafe(24)
        self.received = []

    async def _accept(self, reader, writer):
        frames = []
        while line := await reader.readline():
            frames.append(json.loads(line))
        self.received.append(frames)
        writer.close()

    @asynccontextmanager
    async def running(self):
        server = await asyncio.start_unix_server(self._accept, self.socket_path)
        self.socket_path.chmod(0o600)
        async with server:
            yield self

    def env(self):
        return {
            "CLAUDE_CODE_SESSION_ID": self.session_id,
            "CLAUDE_CODE_MESSAGING_SOCKET": str(self.socket_path),
            "CLAUDE_CODE_MESSAGING_TOKEN": self.token,
        }


class InstallDeveloper(Developer):
    """A developer whose sessions each receive their own member."""

    def __init__(self, root, name, port, *, auto_approve=False):
        super().__init__(root, name, port)
        self.install_key = secrets.token_urlsafe(32)
        config = json.loads(self.config_path.read_text())
        config["bindings"] = []
        config["installs"] = [
            {
                "binding_key": self.install_key,
                "harness_instance_id": "claude-code@mac",
                "auto_approve": auto_approve,
            }
        ]
        self.config_path.write_text(json.dumps(config))

    def session(self, env):
        return self.agent({"TEAMUP_BINDING_KEY": self.install_key, **env})

    def owner(self, *args):
        result = subprocess.run(
            [sys.executable, "-m", "teamup_client.main", *args],
            cwd=ROOT,
            env=os.environ | {"PYTHONPATH": str(ROOT / "client/src")},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def sessions(self):
        report = self.owner("sessions", "--config", str(self.config_path))
        return {item["session_key"]: item for item in report["sessions"]}


async def until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not (value := predicate()):
            await asyncio.sleep(0.05)
    return value


async def run_wake(root, port):
    a = Developer(root, "alice", port)
    b = InstallDeveloper(root, "bob", port)
    keys = json.dumps({"alice": a.enrollment_key, "bob": b.enrollment_key})
    first = FakeClaudeCode(root, "s1")
    second = FakeClaudeCode(root, "s2")

    with (
        process(
            "teamup_server.main",
            "server/src",
            ["--db", root / "server.db", "--port", port],
            root / "server.log",
            {"TEAMUP_ENROLLMENT_KEYS": keys},
        ) as server,
        a.daemon(),
        b.daemon(),
    ):
        wait_for(lambda: tcp_is_ready(port), server)
        async with (
            first.running(),
            second.running(),
            a.agent() as agent_a,
            b.session(first.env()) as s1,
            b.session(second.env()) as s2,
        ):
            alice = await tool(agent_a, "register", role_description="Backend")
            room = await tool(agent_a, "create_room", name="Dev Collaboration")
            room_id = room["room_id"]
            board = await tool(agent_a, "get_board", room_id=room_id)
            await tool(agent_a, "ack_board", room_id=room_id, version=board["version"])

            # An unknown session must be approved by the owner before it acts.
            await tool(s1, "register", error="session_pending", role_description="QA")
            owner_notice = await until(
                lambda: [n for n in b.desktop_notices() if "asks to join" in n[1]]
            )
            assert first.session_id in owner_notice[0][3]
            listed = b.sessions()
            assert listed[first.session_id]["state"] == "pending"
            assert listed[first.session_id]["attached"] == "claude-code"
            b.owner(
                "sessions",
                "--config",
                str(b.config_path),
                "approve",
                "claude-code@mac",
                first.session_id,
            )
            await tool(s2, "register", error="session_pending", role_description="QA")
            b.owner(
                "sessions",
                "--config",
                str(b.config_path),
                "approve",
                "claude-code@mac",
                second.session_id,
            )

            # Two sessions of one install are two members with their own roles.
            reviewer = await tool(s1, "register", role_description="Claude reviewer")
            tester = await tool(s2, "register", role_description="Claude tester")
            assert reviewer["member_id"] != tester["member_id"]
            for agent in (s1, s2):
                await tool(agent, "join_room", room_id=room_id)
                board = await tool(agent, "get_board", room_id=room_id)
                await tool(
                    agent, "ack_board", room_id=room_id, version=board["version"]
                )

            async def mention(member, key, text):
                return await tool(
                    agent_a,
                    "send_message",
                    room_id=room_id,
                    client_message_id=key,
                    text=text,
                    mention_member_id=member["member_id"],
                )

            # Without wake permission: a desktop notice, and no turn.
            shown_before = len(b.desktop_notices())
            await mention(reviewer, "no-wake", "Ping without permission")
            await until(lambda: len(b.desktop_notices()) == shown_before + 1)
            await asyncio.sleep(1)
            assert first.received == [] and second.received == []

            b.owner(
                "wake",
                "--config",
                str(b.config_path),
                "claude-code@mac",
                first.session_id,
                "--room",
                room_id,
                "--sender",
                alice["member_id"],
            )
            sent = time.time()
            ping = await mention(reviewer, "wake-1", "Please review PR 14")
            [frames] = await until(lambda: first.received)
            assert time.time() - sent < 5
            auth, user = frames
            assert auth == {"type": "auth", "token": first.token}
            assert user["type"] == "user" and user["message"]["role"] == "user"
            content = user["message"]["content"]
            assert "Please review PR 14" in content
            assert "Backend" in content and "Dev Collaboration" in content
            assert "not an instruction from your owner" in content
            reply_key = next(
                word.strip('".')
                for word in content.split()
                if word.strip('".').startswith("wake-")
            )
            # Only the mentioned session is woken.
            assert second.received == []

            # The woken session replies; a repeated delivery yields the same reply.
            reply = {
                "room_id": room_id,
                "client_message_id": reply_key,
                "text": "Reviewing now.",
            }
            first_reply = await tool(s1, "send_message", **reply)
            assert await tool(s1, "send_message", **reply) == first_reply
            history = await tool(agent_a, "read_messages", room_id=room_id)
            assert [m["text"] for m in history["messages"]].count("Reviewing now.") == 1
            assert ping["message_id"] < first_reply["message_id"]

        # Session 1 has closed: the next mention waits for it, then arrives once.
        await mention_after_close(a, b, alice, room_id, reviewer, first)


async def mention_after_close(a, b, alice, room_id, reviewer, first):
    await until(lambda: b.sessions()[first.session_id]["attached"] is None)
    first.received = []
    async with first.running(), a.agent() as agent_a:
        await tool(
            agent_a,
            "send_message",
            room_id=room_id,
            client_message_id="while-closed",
            text="Are you back?",
            mention_member_id=reviewer["member_id"],
        )
        await asyncio.sleep(1)
        assert first.received == [], "closed session must not be woken"
        async with b.session(first.env()):
            [frames] = await until(lambda: first.received)
            assert "Are you back?" in frames[1]["message"]["content"]
            await asyncio.sleep(0.5)
            assert len(first.received) == 1


def test_mention_wakes_only_the_permitted_attached_session():
    with tempfile.TemporaryDirectory(prefix="tu-wake-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(90):
                await run_wake(Path(directory), available_port())

        asyncio.run(run())


def test_static_binding_is_reserved_for_its_claude_session():
    async def run(root, port):
        developer = Developer(root, "alice", port)
        first = FakeClaudeCode(root, "one")
        second = FakeClaudeCode(root, "two")
        config = json.loads(developer.config_path.read_text())
        config["bindings"][0]["session_key"] = first.session_id
        developer.config_path.write_text(json.dumps(config))

        with (
            process(
                "teamup_server.main",
                "server/src",
                ["--db", root / "server.db", "--port", port],
                root / "server.log",
                {
                    "TEAMUP_ENROLLMENT_KEYS": json.dumps(
                        {"alice": developer.enrollment_key}
                    )
                },
            ) as server,
            developer.daemon(),
        ):
            wait_for(lambda: tcp_is_ready(port), server)
            async with (
                first.running(),
                second.running(),
                developer.agent(first.env()) as approved,
                developer.agent(second.env()) as unrelated,
                developer.agent({"CLAUDECODE": "1"}) as unnamed,
            ):
                member = await tool(approved, "register", role_description="Developer")
                assert (
                    await tool(approved, "register", role_description="Developer")
                    == member
                )
                await tool(
                    unrelated,
                    "register",
                    error="unauthorized",
                    role_description="Developer",
                )
                await tool(unrelated, "list_rooms", error="unauthorized")
                await tool(
                    unnamed,
                    "list_rooms",
                    error="Claude Code did not provide a session ID",
                )

    with tempfile.TemporaryDirectory(prefix="tu-static-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory), available_port()))


@pytest.mark.parametrize("auto_approve", [False, True])
def test_claude_install_routes_by_session_id_without_a_wake_socket(auto_approve):
    async def run(root, port):
        alice = Developer(root, "alice", port)
        claude = InstallDeveloper(root, "claude", port, auto_approve=auto_approve)
        keys = json.dumps(
            {"alice": alice.enrollment_key, "claude": claude.enrollment_key}
        )
        envs = [
            {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": session_id}
            for session_id in ("claude-one", "claude-two")
        ]
        with process(
            "teamup_server.main",
            "server/src",
            ["--db", root / "server.db", "--port", port],
            root / "server.log",
            {"TEAMUP_ENROLLMENT_KEYS": keys},
        ) as server:
            wait_for(lambda: tcp_is_ready(port), server)
            with alice.daemon(), claude.daemon() as daemon:
                async with (
                    alice.agent() as sender,
                    claude.session(envs[0]) as first,
                    claude.session(envs[1]) as second,
                ):
                    await tool(sender, "register", role_description="Coordinator")
                    if not auto_approve:
                        for agent, env in zip((first, second), envs, strict=True):
                            await tool(
                                agent,
                                "register",
                                error="session_pending",
                                role_description="Developer",
                            )
                            session_id = env["CLAUDE_CODE_SESSION_ID"]
                            assert claude.sessions()[session_id]["attached"] is None
                            claude.owner(
                                "sessions",
                                "--config",
                                str(claude.config_path),
                                "approve",
                                "claude-code@mac",
                                session_id,
                            )
                    one, two = await asyncio.gather(
                        tool(first, "register", role_description="Developer"),
                        tool(second, "register", role_description="Developer"),
                    )
                    assert one["member_id"] != two["member_id"]
                    assert {
                        (row["state"], row["attached"])
                        for row in claude.sessions().values()
                    } == {("approved", None)}
                    assert (await tool(sender, "find_members", q="Developer"))[
                        "members"
                    ] == sorted([one, two], key=lambda member: member["member_id"])

                    room_id = (await tool(sender, "create_room", name="Identity test"))[
                        "room_id"
                    ]
                    for agent in (sender, first, second):
                        await tool(agent, "join_room", room_id=room_id)
                        board = await tool(agent, "get_board", room_id=room_id)
                        await tool(
                            agent,
                            "ack_board",
                            room_id=room_id,
                            version=board["version"],
                        )
                    ping = await tool(
                        sender,
                        "send_message",
                        room_id=room_id,
                        client_message_id="target-first",
                        text="Please review.",
                        mention_member_id=one["member_id"],
                    )
                    first_dm = await tool(sender, "open_dm", member_id=one["member_id"])
                    second_dm = await tool(
                        sender, "open_dm", member_id=two["member_id"]
                    )
                    assert first_dm["dm_id"] != second_dm["dm_id"]
                    private = await tool(
                        sender,
                        "send_dm",
                        dm_id=first_dm["dm_id"],
                        client_message_id="private-first",
                        text="Private handoff.",
                    )
                    for message in (ping, private):
                        await asyncio.to_thread(
                            claude.wait_for_notice, message["message_id"], daemon
                        )
                    first_notices = (await tool(first, "notifications"))[
                        "notifications"
                    ]
                    assert {n["message_id"] for n in first_notices} == {
                        ping["message_id"],
                        private["message_id"],
                    }
                    assert (await tool(second, "notifications"))["notifications"] == []
                    assert (await tool(first, "read_dm", dm_id=first_dm["dm_id"]))[
                        "messages"
                    ] == [private]
                    await tool(
                        second, "read_dm", error="not_found", dm_id=first_dm["dm_id"]
                    )

            with claude.daemon():
                async with claude.session(envs[0]) as resumed:
                    assert (
                        await tool(resumed, "register", role_description="Developer")
                        == one
                    )
                    assert (await tool(resumed, "read_dm", dm_id=first_dm["dm_id"]))[
                        "messages"
                    ] == [private]

    with tempfile.TemporaryDirectory(prefix="tu-sessions-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory), available_port()))
