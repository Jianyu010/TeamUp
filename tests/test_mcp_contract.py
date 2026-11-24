import asyncio
import json
import secrets
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def test_stdio_passes_approved_binding_and_surfaces_ipc_error():
    async def run(socket_path):
        key = secrets.token_urlsafe(32)
        requests = [
            (
                "register",
                {"role_description": "Frontend"},
                {"member_id": "a", "role_description": "Frontend"},
            ),
            (
                "send_message",
                {
                    "room_id": "room",
                    "client_message_id": "x",
                    "text": "Hello",
                    "mention_member_id": "b",
                },
                {"message_id": 1},
            ),
            ("get_board", {"room_id": "room"}, None),
            (
                "open_dm",
                {"member_id": "b"},
                {"dm_id": "d", "peer_member_id": "b"},
            ),
            (
                "send_dm",
                {"dm_id": "d", "client_message_id": "x", "text": "Hello"},
                {"message_id": 2, "dm_id": "d"},
            ),
            (
                "read_dm",
                {"dm_id": "d", "after": 0, "limit": 10},
                {
                    "messages": [],
                    "expired_through_message_id": 0,
                    "history_truncated": True,
                },
            ),
        ]
        seen = []

        async def serve(reader, writer):
            request = json.loads(await reader.readline())
            index = len(seen)
            seen.append(request)
            result = requests[index][2]
            response = (
                {"result": result}
                if result is not None
                else {"error": {"code": "not_found", "message": "Room is unavailable"}}
            )
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(serve, socket_path)
        socket_path.chmod(0o600)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "teamup_mcp.main"],
            env={
                "TEAMUP_SOCKET": str(socket_path),
                "TEAMUP_BINDING_KEY": key,
                "PYTHONPATH": f"{ROOT / 'plugin/mcp/src'}:{ROOT / 'client/src'}",
            },
        )
        async with (
            server,
            stdio_client(params) as (reads, writes),
            ClientSession(reads, writes) as session,
        ):
            initialized = await session.initialize()
            instructions = (initialized.instructions or "").lower()
            assert "use a dm for two agents" in instructions
            assert "room for three or more" in instructions
            assert "if dm tools are unavailable" in instructions
            assert "private handoff" in instructions
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert "three or more" in tools["teamup_create_room"].description
            assert "retry" in tools["teamup_send_dm"].description
            assert "same conversation" in tools["teamup_open_dm"].description
            assert tools["teamup_open_dm"].inputSchema == {
                "type": "object",
                "properties": {"member_id": {"type": "string", "format": "uuid"}},
                "required": ["member_id"],
                "additionalProperties": False,
            }
            assert tools["teamup_send_dm"].inputSchema == {
                "type": "object",
                "properties": {
                    "dm_id": {"type": "string", "format": "uuid"},
                    "client_message_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "text": {"type": "string", "minLength": 1, "maxLength": 8_000},
                },
                "required": ["dm_id", "client_message_id", "text"],
                "additionalProperties": False,
            }
            assert tools["teamup_read_dm"].inputSchema == {
                "type": "object",
                "properties": {
                    "dm_id": {"type": "string", "format": "uuid"},
                    "after": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                },
                "required": ["dm_id"],
                "additionalProperties": False,
            }
            assert tools.keys() == {
                "teamup_register",
                "teamup_find_members",
                "teamup_create_room",
                "teamup_join_room",
                "teamup_get_board",
                "teamup_ack_board",
                "teamup_send_message",
                "teamup_read_messages",
                "teamup_open_dm",
                "teamup_send_dm",
                "teamup_read_dm",
                "teamup_notifications",
            }
            for method, args, result in requests:
                response = await session.call_tool(f"teamup_{method}", args)
                if result is None:
                    assert response.isError
                    assert "not_found" in response.content[0].text
                else:
                    assert not response.isError
                    assert response.structuredContent == result

        assert seen == [
            {"version": 1, "binding_key": key, "method": method, "params": args}
            for method, args, _ in requests
        ]

    with tempfile.TemporaryDirectory(prefix="tu-mcp-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory) / "teamup.sock"))


def test_collaboration_skill_covers_dm_handoff_and_privacy():
    skill = (ROOT / "plugin/skills/teamup-collaboration/SKILL.md").read_text()
    lowered = skill.lower()
    assert "teamup_open_dm" in lowered
    assert "teamup_send_dm" in lowered
    assert "teamup_read_dm" in lowered
    assert "same conversation" in lowered
    assert "older client release" in lowered
    assert "do not use a room as a substitute" in lowered
    assert "logs" in lowered
    assert "wake automatically" in lowered
