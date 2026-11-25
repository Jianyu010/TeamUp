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
            (
                "search_room",
                {"room_id": "room", "q": "Hello", "after": 0, "limit": 10},
                {
                    "matches": [
                        {
                            "message_id": 1,
                            "sender_member_id": "b",
                            "created_at": "2026-11-24T00:00:00Z",
                            "snippet": "Hello there",
                        }
                    ],
                    "expired_through_message_id": 0,
                    "history_truncated": True,
                },
            ),
            ("list_rooms", {}, {"rooms": []}),
            ("notifications", {}, {"notifications": []}),
        ]
        # Unread counts the Client reports after each successful non-notice tool.
        unread = iter([0, 2, 0, 0, 0, 0, 0])
        seen = []

        async def serve(reader, writer):
            request = json.loads(await reader.readline())
            seen.append(request)
            if request["method"] == "mention_status":
                result = {"unread_mentions": next(unread)}
            else:
                calls = [item for item in seen if item["method"] != "mention_status"]
                result = requests[len(calls) - 1][2]
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
            assert "last event_id as after" in (
                tools["teamup_notifications"].description
            )
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
            assert tools["teamup_list_rooms"].inputSchema == {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            }
            assert tools.keys() == {
                "teamup_register",
                "teamup_find_members",
                "teamup_list_rooms",
                "teamup_create_room",
                "teamup_join_room",
                "teamup_leave_room",
                "teamup_retire_identity",
                "teamup_get_board",
                "teamup_ack_board",
                "teamup_send_message",
                "teamup_read_messages",
                "teamup_search_room",
                "teamup_create_task",
                "teamup_list_tasks",
                "teamup_get_task",
                "teamup_join_task",
                "teamup_report_progress",
                "teamup_finish_task",
                "teamup_task_history",
                "teamup_open_dm",
                "teamup_send_dm",
                "teamup_read_dm",
                "teamup_notifications",
            }
            search_schema = tools["teamup_search_room"].inputSchema
            assert search_schema["required"] == ["room_id", "q"]
            assert search_schema["properties"]["q"] == {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            }
            assert search_schema["properties"]["limit"] == {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
            }
            hints = []
            for method, args, result in requests:
                response = await session.call_tool(f"teamup_{method}", args)
                if result is None:
                    assert response.isError
                    assert "not_found" in response.content[0].text
                else:
                    assert not response.isError
                    assert response.structuredContent == result
                    assert json.loads(response.content[0].text) == result
                hints.append([item.text for item in response.content[1:]])
            assert hints == [
                [],
                ["TeamUp: 2 unread mentions. Call teamup_notifications to read them."],
                [],
                [],
                [],
                [],
                [],
                [],
                [],
            ]

        def ipc(method, params):
            return {
                "version": 1,
                "binding_key": key,
                "method": method,
                "params": params,
            }

        status = ipc("mention_status", {})
        assert seen == [
            ipc("register", requests[0][1]),
            status,
            ipc("send_message", requests[1][1]),
            status,
            ipc("get_board", requests[2][1]),
            ipc("open_dm", requests[3][1]),
            status,
            ipc("send_dm", requests[4][1]),
            status,
            ipc("read_dm", requests[5][1]),
            status,
            ipc("search_room", requests[6][1]),
            status,
            ipc("list_rooms", requests[7][1]),
            status,
            ipc("notifications", {}),
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
