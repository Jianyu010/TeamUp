import asyncio
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from teamup_client.ipc import ClientError, call

ROOM = {"room_id": {"type": "string", "format": "uuid"}}
TEXT = {"type": "string", "minLength": 1, "maxLength": 8_000}
NAME = {"type": "string", "minLength": 1, "maxLength": 200}
PAGE = {
    "after": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
}


def tool(
    name: str,
    description: str,
    required: dict[str, dict] | None = None,
    optional: dict[str, dict] | None = None,
) -> Tool:
    required_fields = required or {}
    return Tool(
        name=f"teamup_{name}",
        description=description,
        inputSchema={
            "type": "object",
            "properties": required_fields | (optional or {}),
            "required": list(required_fields),
            "additionalProperties": False,
        },
    )


TOOLS = [
    tool(
        "register",
        "Register this owner-approved session or recover its existing member ID.",
        {"role_description": NAME},
    ),
    tool(
        "find_members",
        "Find teammates by a substring of their role description.",
        optional={"q": {"type": "string", "default": ""}},
    ),
    tool(
        "create_room",
        "Create and join a team-visible task room for three or more agents. "
        "The room has a required board.",
        {"name": NAME},
        {"rules": TEXT, "protocol": TEXT},
    ),
    tool("join_room", "Join a team-visible room.", ROOM),
    tool("get_board", "Read the room's current rules, protocol and version.", ROOM),
    tool(
        "ack_board",
        "Acknowledge a board version you have just read.",
        ROOM | {"version": {"type": "integer", "minimum": 1}},
    ),
    tool(
        "send_message",
        "Send a room message and optionally notify one other member.",
        ROOM
        | {
            "client_message_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "text": TEXT,
        },
        {"mention_member_id": {"type": ["string", "null"], "format": "uuid"}},
    ),
    tool("read_messages", "Read room messages after a message ID.", ROOM, PAGE),
    tool(
        "notifications",
        "List received mentions after an event ID. Open room history for their text.",
        optional=PAGE,
    ),
]


def create_mcp(socket_path: str, binding_key: str) -> Server:
    server = Server(
        "TeamUp",
        instructions=(
            "Use a DM for two agents. Use a shared room for three or more agents "
            "who need the same context. If DM tools are unavailable, ask for an "
            "approved private handoff. "
            "Register the approved local session, then find teammates by role. "
            "After joining or creating a room, read and acknowledge its current board. "
            "To request someone's attention, pass their member ID as mention_member_id. "
            "Use a fresh client_message_id for each new message and keep it on retry. "
            "Follow local sharing permissions."
        ),
    )
    methods = {entry.name: entry.name.removeprefix("teamup_") for entry in TOOLS}

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool(validate_input=False)
    async def invoke(
        name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | CallToolResult:
        # Client validates the exact IPC values and returns the published error code.
        try:
            if name not in methods:
                raise ClientError("invalid_request", "Unknown TeamUp tool")
            return await call(socket_path, binding_key, methods[name], arguments)
        except ClientError as error:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(type="text", text=f"{error.code}: {error.message}")
                ],
            )

    return server


async def serve(socket_path: str, binding_key: str) -> None:
    server = create_mcp(socket_path, binding_key)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    socket_path = os.environ.get("TEAMUP_SOCKET")
    binding_key = os.environ.get("TEAMUP_BINDING_KEY")
    if not socket_path or not binding_key:
        raise SystemExit(
            "Set TEAMUP_SOCKET and TEAMUP_BINDING_KEY for this approved session."
        )
    asyncio.run(serve(socket_path, binding_key))


if __name__ == "__main__":
    main()
