"""Exercise owner CLI and live MCP sessions on a shared member binding."""

import asyncio
import json

from _stack import (
    Developer,
    Server,
    clean_env,
    command,
    new_secret,
    run_process,
    run_with_tty,
    tool,
)


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
        member = await tool(old, "register", role_description="Developer")
        assert (
            await tool(shared, "register", role_description="Still the old member")
            == member
        )
        member_id = member["member_id"]
        renamed = await asyncio.to_thread(
            manage_member, developer, "rename", member_id, "Backend · Accounts"
        )
        assert renamed == {
            "member_id": member_id,
            "role_description": "Backend · Accounts",
        }
        assert await tool(shared, "register", role_description="Ignored") == renamed
        replacement = await tool(
            successor, "register", role_description="QA · Accounts"
        )
        assert replacement["member_id"] != member_id

        room_id = (await tool(successor, "create_room", name="Account handoff"))[
            "room_id"
        ]
        await tool(old, "join_room", room_id=room_id)
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
            error="not_registered",
        )
        await tool(
            old, "register", role_description="Developer", error="member_retired"
        )
        assert await tool(successor, "find_members") == {"members": [replacement]}
        assert await tool(successor, "read_messages", room_id=room_id) == {
            "messages": [{**sent, "sender_retired": True}]
        }


def test_retirement_revokes_every_mcp_connection_using_the_same_member(ws):
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
    developer.start()
    asyncio.run(asyncio.wait_for(walk(developer), 30))
