import asyncio
import json
import os
from contextlib import suppress
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from teamup_client.ipc import ClientError, attach, call

ROOM = {"room_id": {"type": "string", "format": "uuid"}}
TEXT = {"type": "string", "minLength": 1, "maxLength": 8_000}
NAME = {"type": "string", "minLength": 1, "maxLength": 200}
QUERY = {"type": "string", "minLength": 1, "maxLength": 200}
HISTORY_POLICY = {"type": "string", "enum": ["retained", "since_joining"]}
PAGE = {
    "after": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
}
HISTORY = PAGE | {"before": {"type": "integer", "minimum": 1}}


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
        "list_rooms",
        "List rooms this member created or joined, with the acknowledged and "
        "current board versions and the latest message ID.",
    ),
    tool(
        "create_room",
        "Create and join a team-visible task room for three or more agents. "
        "The room has a required board.",
        {"name": NAME},
        {"rules": TEXT, "protocol": TEXT, "history_policy": HISTORY_POLICY},
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
    tool("read_messages", "Read room messages after a message ID.", ROOM, HISTORY),
    tool(
        "search_room",
        "Search readable room messages for a case-insensitive substring.",
        ROOM | {"q": QUERY},
        PAGE,
    ),
    tool(
        "notifications",
        "List received mentions after an event ID. Open room history for their text.",
        optional=PAGE,
    ),
]


def claude_code_session(environ=os.environ) -> dict | None:
    """Claude Code gives the MCP servers it launches its own session's inbox."""
    session_id = environ.get("CLAUDE_CODE_SESSION_ID")
    address = environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    token = environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
    if session_id and address and token:
        return {
            "id": session_id,
            "kind": "claude-code",
            "address": address,
            "token": token,
        }
    return None


def codex_session(meta: Any) -> dict | None:
    """Codex names the calling thread in each tools/call _meta."""
    extra = getattr(meta, "model_extra", None) or {}
    thread = extra.get("threadId")
    if isinstance(thread, str) and thread:
        return {"id": thread, "kind": "codex"}
    return None


async def hold_attachment(socket_path: str, binding_key: str, live: dict) -> None:
    """Keep this session reachable by the Client; reattach after Client restarts."""
    session = {"id": live["id"], "kind": live["kind"]}
    delay = 0.5
    while True:
        try:
            async with attach(
                socket_path, binding_key, session, live["address"], live["token"]
            ) as closed:
                delay = 0.5
                await closed.wait()
        except ClientError:
            pass
        await asyncio.sleep(delay)
        delay = min(delay * 2, 10)


async def unread_hint(
    socket_path: str, binding_key: str, session: dict | None = None
) -> str | None:
    """Best-effort reminder so an active agent notices mentions without polling."""
    try:
        status = await call(
            socket_path, binding_key, "mention_status", {}, session=session
        )
    except ClientError:
        return None
    count = status.get("unread_mentions")
    if type(count) is not int or count < 1:
        return None
    noun = "mention" if count == 1 else "mentions"
    return f"TeamUp: {count} unread {noun}. Call teamup_notifications to read them."


def create_mcp(socket_path: str, binding_key: str, live: dict | None = None) -> Server:
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
    ) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
        # Client validates the exact IPC values and returns the published error code.
        try:
            if name not in methods:
                raise ClientError("invalid_request", "Unknown TeamUp tool")
            session = (
                {"id": live["id"], "kind": live["kind"]}
                if live
                else codex_session(server.request_context.meta)
            )
            result = await call(
                socket_path, binding_key, methods[name], arguments, session=session
            )
            content = [TextContent(type="text", text=json.dumps(result, indent=2))]
            if name != "teamup_notifications" and (
                hint := await unread_hint(socket_path, binding_key, session)
            ):
                content.append(TextContent(type="text", text=hint))
            return content, result
        except ClientError as error:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(type="text", text=f"{error.code}: {error.message}")
                ],
            )

    return server


async def serve(socket_path: str, binding_key: str) -> None:
    live = claude_code_session()
    server = create_mcp(socket_path, binding_key, live)
    holder = (
        asyncio.create_task(hold_attachment(socket_path, binding_key, live))
        if live
        else None
    )
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        if holder is not None:
            holder.cancel()
            with suppress(asyncio.CancelledError):
                await holder


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
