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
DM = {"dm_id": {"type": "string", "format": "uuid"}}
MESSAGE_KEY = {"type": "string", "minLength": 1, "maxLength": 128}
TEXT = {"type": "string", "minLength": 1, "maxLength": 8_000}
NAME = {"type": "string", "minLength": 1, "maxLength": 200}
QUERY = {"type": "string", "minLength": 1, "maxLength": 200}
HISTORY_POLICY = {"type": "string", "enum": ["retained", "since_joining"]}
PAGE = {
    "after": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
}
HISTORY = PAGE | {"before": {"type": "integer", "minimum": 1}}
RETRY = {"type": "string", "minLength": 1, "maxLength": 128}
DESCRIPTION = {"type": ["string", "null"], "minLength": 1, "maxLength": 8_000}
TASK_STATUS = {"type": "string", "enum": ["open", "finished"]}
ADDRESSED = {
    "type": "array",
    "items": {"type": "string", "format": "uuid"},
    "maxItems": 100,
    "uniqueItems": True,
}
TASK = ROOM | {"task_id": {"type": "string", "format": "uuid"}}


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
        "retire_identity",
        "Permanently retire your bound TeamUp identity and leave its rooms. "
        "Earlier conversation history remains available to authorized readers.",
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
    tool("leave_room", "Leave a room as this member. You can rejoin later.", ROOM),
    tool(
        "list_room_members",
        "List current room members with the membership IDs for their current joins.",
        ROOM,
    ),
    tool(
        "remove_room_member",
        "Remove a member from a room you created and are still joined to. "
        "Use the target's membership ID from the roster. Reuse it on retry.",
        ROOM | {
            "member_id": {"type": "string", "format": "uuid"},
            "membership_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        },
    ),
    tool("get_board", "Read the room's current rules, protocol and version.", ROOM),
    tool(
        "ack_board",
        "Acknowledge a board version you have just read.",
        ROOM | {"version": {"type": "integer", "minimum": 1}},
    ),
    tool(
        "send_message",
        "Send a room message. Optionally notify one member with mention_member_id "
        "or up to 16 with mention_member_ids. Preserve the target order on retry.",
        ROOM
        | {
            "client_message_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "text": TEXT,
        },
        {
            "mention_member_id": {"type": ["string", "null"], "format": "uuid"},
            "mention_member_ids": {**ADDRESSED, "minItems": 1, "maxItems": 16},
        },
    ),
    tool("read_messages", "Read room messages after a message ID.", ROOM, HISTORY),
    tool(
        "search_room",
        "Search readable room messages for a case-insensitive substring.",
        ROOM | {"q": QUERY},
        PAGE,
    ),
    tool(
        "create_task",
        "Create an explicit room task after reading and acknowledging the current "
        "board. Use a new client_task_id and reuse it when retrying this task.",
        ROOM | {"client_task_id": RETRY, "title": NAME},
        {"description": DESCRIPTION, "addressed_member_ids": ADDRESSED},
    ),
    tool(
        "list_tasks",
        "List authorized room tasks after a task number, optionally filtered by "
        "status. Read and acknowledge a stale board first.",
        ROOM,
        PAGE | {"status": TASK_STATUS},
    ),
    tool("get_task", "Read one authorized room task.", TASK),
    tool(
        "join_task",
        "Join an open unaddressed task or an open task addressed to you.",
        TASK,
    ),
    tool(
        "report_progress",
        "Record progress on a task you participate in. Use a new "
        "client_progress_id and reuse it when retrying this update.",
        TASK | {"client_progress_id": RETRY, "note": TEXT},
    ),
    tool(
        "finish_task",
        "Finish a task you created or participate in. Finishing is terminal.",
        TASK,
    ),
    tool(
        "task_history",
        "Read a task's created, joined, progress and finished events after an "
        "event number.",
        TASK,
        PAGE,
    ),
    tool(
        "open_dm",
        "Open the private DM with another member. "
        "Every open returns the same conversation for the pair.",
        {"member_id": {"type": "string", "format": "uuid"}},
    ),
    tool(
        "send_dm",
        "Send a private DM message to the peer. There is no mention field; "
        "keep the same client_message_id when retrying a message.",
        DM | {"client_message_id": MESSAGE_KEY, "text": TEXT},
    ),
    tool(
        "read_dm",
        "Read DM messages after a message ID. "
        "The response may report that older DM history expired.",
        DM,
        PAGE,
    ),
    tool(
        "notifications",
        "List received mentions, DM notices, and owner-requested room departures after an event ID. "
        "Open room or DM history for message text. Rows stay after delivery, "
        "so a full first page can hold only older rows: when a page returns "
        "limit rows, pass its last event_id as after until a shorter page "
        "returns.",
        optional=PAGE,
    ),
]


def claude_code_session(environ=os.environ) -> dict[str, str] | None:
    session_id = environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    return {"id": session_id, "kind": "claude-code"}


def codex_session(meta: Any) -> dict | None:
    """Codex names the calling thread in each tools/call _meta."""
    extra = getattr(meta, "model_extra", None) or {}
    thread = extra.get("threadId")
    if isinstance(thread, str) and thread:
        return {"id": thread, "kind": "codex"}
    return None


async def hold_attachment(
    socket_path: str,
    binding_key: str,
    session: dict[str, str],
    address: str,
    token: str,
) -> None:
    """Keep this session reachable by the Client; reattach after Client restarts."""
    delay = 0.5
    while True:
        try:
            async with attach(
                socket_path, binding_key, session, address, token
            ) as closed:
                delay = 0.5
                await closed.wait()
        except ClientError as exc:
            if exc.code == "member_retired":
                return
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
    unread = []
    for field, singular, plural in (
        ("unread_mentions", "mention", "mentions"),
        ("unread_room_departures", "room departure", "room departures"),
    ):
        count = status.get(field)
        if type(count) is int and count > 0:
            unread.append(f"{count} unread {singular if count == 1 else plural}")
    if not unread:
        return None
    return f"TeamUp: {' and '.join(unread)}. Call teamup_notifications to read them."


def create_mcp(
    socket_path: str,
    binding_key: str,
    claude: dict[str, str] | None = None,
    *,
    require_claude_session: bool = False,
) -> Server:
    server = Server(
        "TeamUp",
        instructions=(
            "Use a DM for two agents. Use a shared room for three or more agents "
            "who need the same context. If DM tools are unavailable, ask for an "
            "approved private handoff. "
            "Register the approved local session with a specific role and "
            "responsibility from the task, or ask the user; keep the returned "
            "member ID and existing approval requirements. Role descriptions can repeat. "
            "Confirm the intended member ID if role search finds multiple matches. Short examples: "
            "'Backend · Accounts API', 'Frontend · Accounts screen', 'QA · Accounts "
            "fixtures'. Avoid bare titles like 'Developer' and do not invent a "
            "teammate's role. Before creating a room, write rules and protocol for "
            "its purpose, expected roles, progress, handoff, and completion, and "
            "preserve requirements the user gave explicitly. After joining or "
            "creating a room, read and acknowledge its current board. Reuse "
            "teamup_list_rooms for joined-room discovery. Use teamup_leave_room "
            "to end membership in one room and teamup_retire_identity to permanently "
            "retire the current TeamUp identity. Give a short room "
            "responsibility introduction only when needed, prefer board role "
            "choices, do not repeat it on rejoin, and do not invent a persistent "
            "room role. Turn a natural request to assign work into an explicit room "
            "task: read and acknowledge the current board, then call "
            "teamup_create_task with a fresh client_task_id and reuse it on retry. "
            "Use teamup_join_task to take eligible open work, report progress with "
            "teamup_report_progress using a fresh client_progress_id reused on "
            "retry, and finish with teamup_finish_task. Read the current room board "
            "and task history with teamup_list_tasks, teamup_get_task and "
            "teamup_task_history; keep ordinary chat and mentions unchanged. "
            "Open a pair DM with the teammate's member ID; teamup_open_dm returns "
            "the same conversation on every repeat. "
            "To request someone's attention, pass their member ID as mention_member_id. "
            "Use mention_member_ids for an ordered list of room recipients. "
            "Use a fresh client_message_id for each new message and keep it on retry. "
            "Keep DM text between the two participants and out of logs and rooms. "
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
            if require_claude_session and claude is None:
                raise ClientError(
                    "invalid_request",
                    "Claude Code did not provide a session ID. Reconnect TeamUp from a "
                    "local Claude Code version that provides CLAUDE_CODE_SESSION_ID.",
                )
            session = claude or codex_session(server.request_context.meta)
            result = await call(
                socket_path, binding_key, methods[name], arguments, session=session
            )
            content = [TextContent(type="text", text=json.dumps(result, indent=2))]
            if name not in {"teamup_notifications", "teamup_retire_identity"} and (
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
    claude = claude_code_session()
    address = os.getenv("CLAUDE_CODE_MESSAGING_SOCKET")
    token = os.getenv("CLAUDE_CODE_MESSAGING_TOKEN")
    server = create_mcp(
        socket_path,
        binding_key,
        claude,
        require_claude_session=bool(os.getenv("CLAUDECODE") or address or token),
    )
    holder = (
        asyncio.create_task(
            hold_attachment(socket_path, binding_key, claude, address, token)
        )
        if claude and address and token
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
