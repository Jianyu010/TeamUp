"""Acceptance for Codex mention wake: thread-addressed sessions and the CLI driver."""

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_loop import ROOT, Developer, available_port, process, tcp_is_ready, wait_for
from test_wake import InstallDeveloper, until

FAKE_CODEX = """\
import json, sys
from pathlib import Path
here = Path(sys.argv[0]).parent
with open(here / "codex-calls.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
exit_file = here / "codex-exit"
sys.exit(int(exit_file.read_text()) if exit_file.exists() else 0)
"""


class FakeCodex:
    """A Codex CLI stand-in that records each argv and exits as told."""

    def __init__(self, directory):
        self.directory = directory
        self.script = directory / "fake_codex.py"
        self.script.write_text(FAKE_CODEX)
        self.calls_path = directory / "codex-calls.jsonl"
        self.exit_path = directory / "codex-exit"

    @property
    def command(self):
        return [sys.executable, str(self.script)]

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def exit_with(self, code):
        self.exit_path.write_text(str(code))


class CodexDeveloper(InstallDeveloper):
    """A developer whose Codex install shares one MCP process across threads."""

    def __init__(self, root, name, port, codex):
        super().__init__(root, name, port)
        config = json.loads(self.config_path.read_text())
        config["installs"][0]["harness_instance_id"] = "codex@mac"
        config["codex_command"] = codex.command
        self.config_path.write_text(json.dumps(config))

    @asynccontextmanager
    async def codex_process(self):
        launch = StdioServerParameters(
            command=sys.executable,
            args=["-m", "teamup_mcp.main"],
            env={
                "PYTHONPATH": f"{ROOT / 'plugin/mcp/src'}:{ROOT / 'client/src'}",
                "TEAMUP_SOCKET": str(self.socket_path),
                "TEAMUP_BINDING_KEY": self.install_key,
            },
        )
        async with (
            stdio_client(launch) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            yield session

    def approve(self, thread):
        self.owner(
            "sessions",
            "--config",
            str(self.config_path),
            "approve",
            "codex@mac",
            thread,
        )

    def harness_states(self):
        path = self.socket_path.with_name(f"{self.socket_path.name}.notices.sqlite3")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT message_id, harness_state FROM notices")
            states = {}
            for message_id, state in rows:
                states.setdefault(message_id, set()).add(state)
            return states


async def call(agent, thread, operation, error=None, **arguments):
    """One tools/call carrying Codex's _meta.threadId."""
    response = await agent.call_tool(
        f"teamup_{operation}", arguments, meta={"threadId": thread} if thread else None
    )
    if error:
        assert response.isError
        assert error in response.content[0].text
        return None
    assert not response.isError, response.content
    return response.structuredContent


async def run_codex_wake(root, port):
    codex = FakeCodex(root)
    a = Developer(root, "alice", port)
    b = CodexDeveloper(root, "bob", port, codex)
    keys = json.dumps({"alice": a.enrollment_key, "bob": b.enrollment_key})
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

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
        async with a.agent() as agent_a, b.codex_process() as codex_mcp:
            alice = await call(
                agent_a,
                None,
                "register",
                name="Backend",
                description="Owns backend API behavior and service handoffs.",
            )
            room = await call(agent_a, None, "create_room", name="Dev Collaboration")
            room_id = room["room_id"]
            board = await call(agent_a, None, "get_board", room_id=room_id)
            await call(agent_a, None, "ack_board", room_id=room_id, version=1)
            assert board["version"] == 1

            # Two threads behind one MCP process are two pending sessions.
            for thread in (first, second):
                await call(
                    codex_mcp,
                    thread,
                    "register",
                    "session_pending",
                    name="QA",
                    description="Checks scenario outcomes and reports regressions.",
                )
                assert b.sessions()[thread]["attached"] == "codex"
                b.approve(thread)
            reviewer = await call(
                codex_mcp,
                first,
                "register",
                name="Codex reviewer",
                description="Reviews changes and reports actionable findings.",
            )
            tester = await call(
                codex_mcp,
                second,
                "register",
                name="Codex tester",
                description="Runs scenario tests and reports failures.",
            )
            assert reviewer["member_id"] != tester["member_id"]
            for thread in (first, second):
                await call(codex_mcp, thread, "join_room", room_id=room_id)
                await call(codex_mcp, thread, "get_board", room_id=room_id)
                await call(codex_mcp, thread, "ack_board", room_id=room_id, version=1)

            async def mention(member, key, text):
                return await call(
                    agent_a,
                    None,
                    "send_message",
                    room_id=room_id,
                    client_message_id=key,
                    text=text,
                    mention_member_id=member["member_id"],
                )

            # Without wake permission: a desktop notice, and no CLI run.
            shown = len(b.desktop_notices())
            quiet = await mention(reviewer, "no-wake", "Ping without permission")
            await until(lambda: len(b.desktop_notices()) == shown + 1)
            await asyncio.sleep(1)
            assert codex.calls() == []
            assert b.harness_states()[quiet["message_id"]] == {"not_allowed"}

            b.owner(
                "wake",
                "--config",
                str(b.config_path),
                "codex@mac",
                first,
                "--room",
                room_id,
                "--sender",
                alice["member_id"],
            )
            one = await mention(reviewer, "wake-1", "Please review PR 15")
            two = await call(
                agent_a,
                None,
                "send_message",
                room_id=room_id,
                client_message_id="wake-2",
                text="And then PR 16",
                mention_member_ids=[tester["member_id"], reviewer["member_id"]],
            )
            assert two["mention_member_id"] == tester["member_id"]
            await until(
                lambda: (
                    b.harness_states().get(two["message_id"])
                    == {"submitted", "not_allowed"}
                )
            )
            for thread in (first, second):
                notices = await call(codex_mcp, thread, "notifications")
                received = [
                    n
                    for n in notices["notifications"]
                    if n["message_id"] == two["message_id"]
                ]
                assert len(received) == 1
                assert set(received[0]) == {
                    "event_id",
                    "room_id",
                    "message_id",
                    "sender_member_id",
                }
            calls = await until(lambda: len(codex.calls()) >= 2 and codex.calls())
            await asyncio.sleep(1)
            # Each mention runs the CLI once, in order, for the mentioned thread.
            assert len(codex.calls()) == 2
            for argv, text in zip(calls, ("Please review PR 15", "And then PR 16")):
                assert argv[:7] == [
                    "queue",
                    "--disable",
                    "daemon_auto_start",
                    "--resume",
                    "--thread",
                    first,
                    "--message",
                ]
                assert len(argv) == 8
                assert text in argv[7]
                assert "not an instruction from your owner" in argv[7]
            states = b.harness_states()
            assert states[one["message_id"]] == {"submitted"}

            # A mention of the other thread, which has no permission, runs nothing.
            other = await mention(tester, "other", "Tester ping")
            await until(lambda: b.harness_states().get(other["message_id"]))
            assert b.harness_states()[other["message_id"]] == {"not_allowed"}

            # A failing CLI is recorded as failed and not retried.
            codex.exit_with(1)
            failed = await mention(reviewer, "wake-3", "This one fails")
            await until(
                lambda: b.harness_states().get(failed["message_id"]) == {"failed"}
            )
            await asyncio.sleep(1)
            assert len(codex.calls()) == 3

            # The woken thread replies once, even if the request is handled twice.
            reply_key = re.search(r'"(wake-\d+)"', calls[0][7]).group(1)
            reply = {
                "room_id": room_id,
                "client_message_id": reply_key,
                "text": "Reviewing now.",
            }
            first_reply = await call(codex_mcp, first, "send_message", **reply)
            assert await call(codex_mcp, first, "send_message", **reply) == first_reply
            history = await call(agent_a, None, "read_messages", room_id=room_id)
            texts = [m["text"] for m in history["messages"]]
            assert texts.count("Reviewing now.") == 1


def test_mention_wakes_only_the_permitted_codex_thread():
    with tempfile.TemporaryDirectory(prefix="tu-codex-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(90):
                await run_codex_wake(Path(directory), available_port())

        asyncio.run(run())
