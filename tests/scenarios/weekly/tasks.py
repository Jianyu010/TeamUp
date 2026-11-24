"""Tasks the orchestrator hands to agents.

Each Task carries the prompt a real agent receives and a script that does the
same job through MCP for --dry-run. The two must stay equivalent: the scripts
are how the wiring is proven without spending model calls.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .agents import Spec


@dataclass
class Task:
    prompt: str
    script: Callable[..., Awaitable[None]]


# --- setup ------------------------------------------------------------------


def create_room(name: str, rules: str, protocol: str, ready: str = "") -> Task:
    steps = [
        "Call teamup_register with your role description.",
        (
            f'Call teamup_create_room with name "{name}", rules "{rules}" and '
            f'protocol "{protocol}".'
        ),
        (
            "Call teamup_get_board with the new room_id, then teamup_ack_board with "
            "the version it returns."
        ),
    ]
    if ready:
        steps.append(
            f'Send one message to the room with teamup_send_message: "{ready}". '
            "Do not mention anyone."
        )
    prompt = (
        "Task: set up the team room.\n"
        + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
        + "\nFinal answer: the room_id."
    )

    async def script(ctx):
        await ctx.call("register", role_description=ctx.spec.role)
        room = await ctx.call("create_room", name=name, rules=rules, protocol=protocol)
        board = await ctx.call("get_board", room_id=room["room_id"])
        await ctx.call("ack_board", room_id=room["room_id"], version=board["version"])
        if ready:
            await ctx.call(
                "send_message",
                room_id=room["room_id"],
                client_message_id=ctx.message_id("ready"),
                text=ready,
            )

    return Task(prompt, script)


def join_room(room_id: str, ready: str) -> Task:
    prompt = (
        f"Task: join the team room {room_id}.\n"
        "1. Call teamup_register with your role description.\n"
        f'2. Call teamup_join_room with room_id "{room_id}".\n'
        "3. Call teamup_get_board, read the rules and protocol, then call "
        "teamup_ack_board with the version it returns.\n"
        f'4. Send one message to the room with teamup_send_message: "{ready}". '
        "Do not mention anyone.\n"
        "Final answer: one line saying you joined."
    )

    async def script(ctx):
        await ctx.call("register", role_description=ctx.spec.role)
        await ctx.call("join_room", room_id=room_id)
        board = await ctx.call("get_board", room_id=room_id)
        await ctx.call("ack_board", room_id=room_id, version=board["version"])
        await ctx.call(
            "send_message",
            room_id=room_id,
            client_message_id=ctx.message_id("ready"),
            text=ready,
        )

    return Task(prompt, script)


def ask(room_id: str, targets: list[tuple[str, str]]) -> Task:
    """Post one question per (role, question), each @mentioning that role."""
    steps = [
        "Call teamup_register with your role description (repeating it is harmless).",
    ]
    for role, question in targets:
        steps.append(
            f'Call teamup_find_members with q "{role}" and take the member_id of '
            f'the member whose role contains "{role}". Then call teamup_send_message '
            f'with room_id "{room_id}", text "{question}" and mention_member_id set '
            "to that member_id."
        )
    prompt = (
        "Task: ask your teammates in the room.\n"
        + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
        + "\nThe answers arrive later as @mentions; do not wait for them. "
        "Final answer: one line saying the question was sent."
    )

    async def script(ctx):
        await ctx.call("register", role_description=ctx.spec.role)
        for role, question in targets:
            found = await ctx.call("find_members", q=role)
            [member] = [m for m in found["members"] if role in m["role_description"]]
            await ctx.call(
                "send_message",
                room_id=room_id,
                client_message_id=ctx.message_id("ask"),
                text=question,
                mention_member_id=member["member_id"],
            )

    return Task(prompt, script)


# --- wake -------------------------------------------------------------------


def wake(spec: Spec, count: int, roster: dict[str, str]) -> Task:
    if spec.mention == "sender":
        mention = "Set mention_member_id to the notice's sender_member_id."
    elif spec.mention:
        role = roster[spec.mention]
        mention = (
            f"Also set mention_member_id to the member_id of the member whose role "
            f'contains "{role}" (find it with teamup_find_members).'
        )
    else:
        mention = "Do not mention anyone (leave mention_member_id out)."
    prompt = (
        "TeamUp delivered a notice: a teammate @mentioned you "
        f"(mention number {count} for you). Handle it now.\n"
        "1. Call teamup_notifications and take the newest notice. Note its "
        "room_id, message_id and sender_member_id.\n"
        "2. Call teamup_read_messages for that room_id and read the message with "
        "that message_id.\n"
        "3. If you already posted a message in that room after that message_id, "
        "stop and say so.\n"
        "4. Otherwise send exactly ONE message to that room with teamup_send_message "
        f"that handles the mention. {spec.wake_note} {mention}\n"
        "Final answer: one line saying what you replied."
    )

    async def script(ctx):
        notices = (await ctx.call("notifications"))["notifications"]
        notice = max(notices, key=lambda item: item["event_id"])
        room_id = notice["room_id"]
        history = (await ctx.call("read_messages", room_id=room_id))["messages"]
        trigger = next(m for m in history if m["message_id"] == notice["message_id"])
        mine = await ctx.call("register", role_description=ctx.spec.role)
        if any(
            m["sender_member_id"] == mine["member_id"]
            and m["message_id"] > trigger["message_id"]
            for m in history
        ):
            return
        parts = [f"Re #{trigger['message_id']}:", *ctx.spec.facts]
        if ctx.spec.wake_note:
            parts.append(ctx.spec.wake_note)
        parts.append(f"| {trigger['text']}")
        arguments = {}
        if spec.mention == "sender":
            arguments["mention_member_id"] = notice["sender_member_id"]
        elif spec.mention:
            role = roster[spec.mention]
            found = await ctx.call("find_members", q=role)
            [member] = [m for m in found["members"] if role in m["role_description"]]
            arguments["mention_member_id"] = member["member_id"]
        await ctx.call(
            "send_message",
            room_id=room_id,
            client_message_id=ctx.message_id("reply"),
            text=" ".join(parts),
            **arguments,
        )

    return Task(prompt, script)
