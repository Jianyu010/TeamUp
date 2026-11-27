"""Tasks the orchestrator hands to agents.

Each Task carries the prompt a real agent receives and a script that does the
same job through MCP for --dry-run. The two must stay equivalent: the scripts
are how the wiring is proven without spending model calls.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .agents import Spec


@dataclass
class Task:
    prompt: str
    script: Callable[..., Awaitable[None]]


# --- setup ------------------------------------------------------------------


def register() -> Task:
    async def script(ctx):
        await ctx.call("register", **ctx.spec.profile)

    return Task(
        "Register with TeamUp using your assigned name and description.", script
    )


def create_room(name: str, rules: str, protocol: str, ready: str = "") -> Task:
    steps = [
        "Call teamup_register with your assigned name and description.",
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
        await ctx.call("register", **ctx.spec.profile)
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
        "1. Call teamup_register with your assigned name and description.\n"
        f'2. Call teamup_join_room with room_id "{room_id}".\n'
        "3. Call teamup_get_board, read the rules and protocol, then call "
        "teamup_ack_board with the version it returns.\n"
        f'4. Send one message to the room with teamup_send_message: "{ready}". '
        "Do not mention anyone.\n"
        "Final answer: one line saying you joined."
    )

    async def script(ctx):
        await ctx.call("register", **ctx.spec.profile)
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
        "Call teamup_register with your assigned name and description (repeating it is harmless).",
    ]
    for role, question in targets:
        steps.append(
            f'Call teamup_find_members with q "{role}" and take the member_id of '
            f'the member whose name contains "{role}". Then call teamup_send_message '
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
        await ctx.call("register", **ctx.spec.profile)
        for role, question in targets:
            found = await ctx.call("find_members", q=role)
            [member] = [m for m in found["members"] if role in m["name"]]
            await ctx.call(
                "send_message",
                room_id=room_id,
                client_message_id=ctx.message_id("ask"),
                text=question,
                mention_member_id=member["member_id"],
            )

    return Task(prompt, script)


# --- direct messages --------------------------------------------------------


def ask_dm(role: str, question: str) -> Task:
    prompt = (
        "Register with TeamUp using your assigned name and description. "
        f'Use teamup_find_members to find "{role}" and teamup_open_dm with that '
        f'member_id. In that DM, send exactly one message with teamup_send_dm: "{question}" '
        "Use a new client_message_id. End your turn after sending."
    )

    async def script(ctx):
        await ctx.call("register", **ctx.spec.profile)
        found = await ctx.call("find_members", q=role)
        [peer] = [member for member in found["members"] if member["name"] == role]
        dm = await ctx.call("open_dm", member_id=peer["member_id"])
        await ctx.call(
            "send_dm",
            dm_id=dm["dm_id"],
            text=question,
            client_message_id=ctx.message_id("dm-question"),
        )

    return Task(prompt, script)


def answer_dm(dm_id: str) -> Task:
    prompt = (
        f"Read DM {dm_id} with teamup_read_dm. Reply to the question with one "
        "teamup_send_dm in the same conversation. Copy the exact request code "
        "from the question and give the port you know, as plain digits. "
        "Use a new client_message_id."
    )

    async def script(ctx):
        history = await ctx.call("read_dm", dm_id=dm_id)
        request = re.search(r"\breq-[0-9a-f]{8}\b", history["messages"][0]["text"])
        if request is None:
            raise ValueError("DM question lacks a request code")
        await ctx.call(
            "send_dm",
            dm_id=dm_id,
            text=f"{request[0]}: {ctx.spec.facts[0]}",
            client_message_id=ctx.message_id("dm-reply"),
        )

    return Task(prompt, script)


def confirm_dm(dm_id: str) -> Task:
    prompt = (
        f"Read the reply in DM {dm_id} with teamup_read_dm. Use teamup_send_dm to "
        "send one confirmation in the same conversation that includes the port "
        "from your teammate's reply as plain digits. Use a new client_message_id."
    )

    async def script(ctx):
        history = await ctx.call("read_dm", dm_id=dm_id)
        reply = history["messages"][-1]
        port = re.search(r"\b[0-9]{5}\b", reply["text"])
        if port is None:
            raise ValueError("DM reply lacks a five-digit port")
        await ctx.call(
            "send_dm",
            dm_id=dm_id,
            text=f"I will use port {port[0]}.",
            client_message_id=ctx.message_id("dm-confirm"),
        )

    return Task(prompt, script)


# --- room tasks -------------------------------------------------------------


def assign_release_check(room_id: str, title: str, assignment: dict[str, str]) -> Task:
    description = (
        "Tester records the release smoke test fixture in task progress. "
        "Reviewer checks that progress and finishes the task."
    )
    prompt = (
        f'In room {room_id}, assign a task titled "{title}" to Tester and Reviewer. '
        "Get their member IDs with teamup_list_room_members. Use teamup_create_task "
        "with both IDs in addressed_member_ids and a new client_task_id. "
        f'Use this description: "{description}" '
        f'Set assignment.closing_criteria to "{assignment["closing_criteria"]}" and '
        f'assignment.eta to "{assignment["eta"]}". '
        "Creating the assignment sends its own notification. End your turn after creating it."
    )

    async def script(ctx):
        roster = (await ctx.call("list_room_members", room_id=room_id))["members"]
        member_ids = {member["name"]: member["member_id"] for member in roster}
        await ctx.call(
            "create_task",
            room_id=room_id,
            title=title,
            description=description,
            assignment=assignment,
            addressed_member_ids=[member_ids["Tester"], member_ids["Reviewer"]],
            client_task_id=ctx.message_id("release-task"),
        )

    return Task(prompt, script)


def read_assignment_prompt(room_id: str) -> str:
    return (
        "Use teamup_notifications to find the task assignment mention. "
        f"Read its room message with teamup_read_messages in room {room_id}. "
        "Use the task number from that message to find the task with teamup_list_tasks. "
        "Read the task's closing criteria."
    )


async def read_assignment(ctx, room_id: str) -> dict:
    notices = (await ctx.call("notifications"))["notifications"]
    [notice] = [row for row in notices if row.get("room_id") == room_id]
    messages = (await ctx.call("read_messages", room_id=room_id))["messages"]
    [message] = [row for row in messages if row["message_id"] == notice["message_id"]]
    number = re.search(r"Task #([0-9]+) assigned", message["text"])
    if number is None:
        raise ValueError("Assignment notice lacks a task number")
    records = (await ctx.call("list_tasks", room_id=room_id))["tasks"]
    [task] = [record for record in records if record["task_number"] == int(number[1])]
    return {"room_id": room_id, "task_id": task["task_id"]}


def record_release_check(room_id: str) -> Task:
    prompt = (
        f"{read_assignment_prompt(room_id)} "
        "Use teamup_join_task to accept the task. Record your private smoke-test result in "
        "one teamup_report_progress, preserving the exact fixture ID and using a new "
        "client_progress_id. Leave the task open for the reviewer."
    )

    async def script(ctx):
        args = await read_assignment(ctx, room_id)
        await ctx.call("join_task", **args)
        await ctx.call(
            "report_progress",
            **args,
            note=ctx.spec.facts[0],
            client_progress_id=ctx.message_id("release-evidence"),
        )

    return Task(prompt, script)


def review_release_check(room_id: str) -> Task:
    prompt = (
        f"{read_assignment_prompt(room_id)} "
        "Use teamup_task_history to read the tester's progress. If the smoke test "
        "passed, use teamup_join_task, then record one note with teamup_report_progress "
        "confirming the result and copying the exact fixture ID from the tester's note. "
        "Use a new client_progress_id. Then close the task with teamup_finish_task."
    )

    async def script(ctx):
        args = await read_assignment(ctx, room_id)
        history = (await ctx.call("task_history", **args))["events"]
        [note] = [event["note"] for event in history if event["kind"] == "progress"]
        fixture = re.search(r"\bfixture-[0-9a-f]{8}\b", note)
        if fixture is None or "passed" not in note:
            raise ValueError("Task progress lacks a passing fixture result")
        await ctx.call("join_task", **args)
        await ctx.call(
            "report_progress",
            **args,
            note=f"Reviewed the passing smoke test for {fixture[0]}.",
            client_progress_id=ctx.message_id("release-review"),
        )
        await ctx.call("finish_task", **args)

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
        mine = await ctx.call("register", **ctx.spec.profile)
        if any(
            m["sender_member_id"] == mine["member_id"]
            and m["message_id"] > trigger["message_id"]
            for m in history
        ):
            return
        parts = [f"Re msg-{trigger['message_id']}:", *ctx.spec.facts]
        if ctx.spec.wake_note:
            parts.append(ctx.spec.wake_note)
        parts.append(f"| {trigger['text']}")
        arguments = {}
        if spec.mention == "sender":
            arguments["mention_member_id"] = notice["sender_member_id"]
        elif spec.mention:
            role = roster[spec.mention]
            found = await ctx.call("find_members", q=role)
            [member] = [m for m in found["members"] if role in m["name"]]
            arguments["mention_member_id"] = member["member_id"]
        await ctx.call(
            "send_message",
            room_id=room_id,
            client_message_id=ctx.message_id("reply"),
            text=" ".join(parts),
            **arguments,
        )

    return Task(prompt, script)
