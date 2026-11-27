"""WE-xx scenarios. Outcomes are judged from TeamUp state, never by a model."""

import asyncio
import itertools
import random
import re
from dataclasses import dataclass
from uuid import uuid4

from . import tasks
from .agents import Spec
from .run import Failure, Run

RULES = "Keep messages short. Answer every @mention in the room."
PROTOCOL = "Read and acknowledge this board. Reply in the room, not elsewhere."


@dataclass
class Scenario:
    id: str
    title: str
    agents: int
    max_turns: int
    max_seconds: int
    run: object = None  # async (options) -> detail
    skip: str = ""


def mention_of(messages, sender, target):
    return [
        m
        for m in messages
        if m["sender_member_id"] == sender and m["mention_member_id"] == target
    ]


def roster_text(messages, names):
    return "; ".join(
        f"msg-{m['message_id']} {names.get(m['sender_member_id'], '?')}"
        f"->{names.get(m['mention_member_id'], '-')}: {m['text'][:60]}"
        for m in messages
    )


# --- WE-01 ------------------------------------------------------------------


async def we01(options) -> str:
    port = str(random.Random().randint(20000, 60000))
    request = f"req-{uuid4().hex[:8]}"
    question = f"Which port does the frontend dev server listen on? Request {request}."
    specs = [
        Spec("A", "Backend"),
        Spec(
            "B",
            "Frontend",
            facts=[f"The frontend dev server listens on port {port}."],
        ),
    ]
    async with Run("WE-01", specs, options) as run:
        await run.turn("B", tasks.register())
        b_id = run.member_id("B")
        await run.turn("A", tasks.ask_dm("Frontend", question))
        a_id = run.member_id("A")
        dm_id = run.dm_id()
        messages = await run.dm_messages(dm_id, "A")
        if [(m["sender_member_id"], m["text"]) for m in messages] != [(a_id, question)]:
            raise Failure(
                f"A did not send the expected DM question. Errors: {run.turn_errors()}"
            )

        # A DM notice is local and does not start a turn.
        await run.turn("B", tasks.answer_dm(dm_id), kind="nudge")
        reply = await run.dm_messages(dm_id, "A")
        if (
            len(reply) != 2
            or reply[:1] != messages
            or reply[1]["sender_member_id"] != b_id
            or reply[1]["message_id"] <= messages[0]["message_id"]
            or request not in reply[1]["text"]
            or not re.search(rf"\b{port}\b", reply[1]["text"])
        ):
            raise Failure(
                "B did not reply with the request code and its private port. "
                f"Errors: {run.turn_errors()}"
            )

        await run.turn("A", tasks.confirm_dm(dm_id), kind="nudge")
        final = await run.dm_messages(dm_id, "B")
        if (
            len(final) != 3
            or final[:2] != reply
            or final[2]["sender_member_id"] != a_id
            or final[2]["message_id"] <= reply[1]["message_id"]
            or not re.search(rf"\b{port}\b", final[2]["text"])
        ):
            raise Failure(
                f"A did not confirm B's port in the DM. Errors: {run.turn_errors()}"
            )
        return "DM history shows A's question, B's private port, and A's confirmation"


# --- WE-02 ------------------------------------------------------------------


async def we02(options) -> str:
    specs = [
        Spec(
            "A",
            "Backend",
            facts=["The users API returns HTTP 404 for an unknown user."],
            wake_note="Answer with the facts you know.",
        ),
        Spec(
            "B",
            "Frontend",
            facts=["The web client uses React 19."],
            wake_note="Answer with the facts you know.",
        ),
        Spec("C", "Tester"),
    ]
    async with Run("WE-02", specs, options) as run:
        await run.phase(
            (
                "A",
                tasks.create_room(
                    "Release check", RULES, PROTOCOL, "Backend is here. Board read."
                ),
            )
        )
        room = run.room_id()
        await run.phase(
            ("B", tasks.join_room(room, "Frontend is here. Board read.")),
            ("C", tasks.join_room(room, "Tester is here. Board read.")),
        )
        await run.phase(
            (
                "C",
                tasks.ask(
                    room,
                    [
                        (
                            "Backend",
                            "Which HTTP status does the users API return for an unknown user?",
                        ),
                        ("Frontend", "Which React version does the web client use?"),
                    ],
                ),
            )
        )
        ids = {name: run.member_id(name) for name in specs_names(specs)}
        names = {value: key for key, value in ids.items()}

        async def done():
            messages = await run.messages(room, "A")
            for name in ids:
                if not await run.has_acked(name, room):
                    return False, f"{name} has not acked the current board"
            posted = {m["sender_member_id"] for m in messages}
            if missing := [n for n, i in ids.items() if i not in posted]:
                return False, f"{missing} have not posted"
            for target in ("A", "B"):
                asks = mention_of(messages, ids["C"], ids[target])
                if not asks:
                    return False, f"C has not mentioned {target}"
                if not [
                    m
                    for m in messages
                    if m["sender_member_id"] == ids[target]
                    and m["message_id"] > asks[0]["message_id"]
                ]:
                    return False, f"{target} has not replied to C's mention"
            return True, ""

        try:
            await run.drain(done)
        except Failure as failure:
            messages = await run.messages(room, "A")
            raise Failure(
                f"{failure}. Room: {roster_text(messages, names)}. "
                f"Errors: {run.turn_errors()}"
            ) from None
        return "all three acked the board, each posted, A and B answered C's mentions"


def specs_names(specs):
    return [spec.name for spec in specs]


# --- WE-03 ------------------------------------------------------------------


async def we03(options) -> str:
    port = str(random.Random().randint(20000, 60000))
    specs = [
        Spec("A", "Backend"),
        Spec(
            "B",
            "Frontend",
            facts=[f"The frontend dev server listens on port {port}."],
            wake_note="Answer the question with the facts you know.",
        ),
    ]
    async with Run("WE-03", specs, options) as run:
        stack = run.stack
        await run.phase(("A", tasks.create_room("Offline check", RULES, PROTOCOL)))
        room = run.room_id()
        await run.phase(("B", tasks.join_room(room, "Frontend is here.")))
        b_dev = stack.developers["B"]
        quiet = len(b_dev.notices())
        b_dev.stop_client()
        await run.phase(
            (
                "A",
                tasks.ask(
                    room,
                    [
                        (
                            "Frontend",
                            "Which port does the frontend dev server listen on?",
                        )
                    ],
                ),
            )
        )
        a_id, b_id = run.member_id("A"), run.member_id("B")
        asks = mention_of(await run.messages(room, "A"), a_id, b_id)
        if not asks:
            raise Failure(f"A did not mention B. Errors: {run.turn_errors()}")
        await asyncio.sleep(2)
        if len(b_dev.notices()) != quiet:
            raise Failure("B's stopped Client still recorded a notice")
        await b_dev.start_client()

        async def done():
            messages = await run.messages(room, "A")
            replies = [
                m
                for m in messages
                if m["sender_member_id"] == b_id
                and m["message_id"] > asks[0]["message_id"]
            ]
            return bool(replies), "B has not replied to the backfilled mention"

        try:
            await run.drain(done, watch=["B"])
        except Failure as failure:
            raise Failure(f"{failure}. Errors: {run.turn_errors()}") from None
        # Exactly once: nothing else may arrive or be answered afterwards.
        await asyncio.sleep(options["settle"])
        messages = await run.messages(room, "A")
        replies = [
            m
            for m in messages
            if m["sender_member_id"] == b_id and m["message_id"] > asks[0]["message_id"]
        ]
        notices = len(b_dev.notices()) - quiet
        wakes = [t for t in run.turns if t.agent == "B" and t.kind == "wake"]
        if len(replies) != 1 or notices != 1 or len(wakes) != 1:
            raise Failure(
                f"expected one notice, one wake and one reply; got {notices} notices, "
                f"{len(wakes)} wakes, {len(replies)} replies"
            )
        if port not in replies[0]["text"]:
            raise Failure(f"B's reply lacks the port: {replies[0]['text'][:120]}")
        return (
            "B's Client was stopped during the mention, backfilled once, B replied once"
        )


# --- WE-05 ------------------------------------------------------------------

CHAIN = ["Frontend", "Tester", "Docs", "Ops"]
STATUS = {
    "Backend": "API v2 is merged.",
    "Frontend": "Login page is done.",
    "Tester": "12 tests pass, 1 flaky.",
    "Docs": "Changelog is drafted.",
    "Ops": "Staging deploy is green.",
}


async def we05(options) -> str:
    # Hand-off chain: Backend -> Frontend -> Tester -> Docs -> Ops.
    specs = [Spec("Backend", "Backend")]
    for index, role in enumerate(CHAIN):
        following = CHAIN[index + 1] if index + 1 < len(CHAIN) else None
        specs.append(
            Spec(
                role,
                role,
                facts=[f"Status: {STATUS[role]}"],
                mention=following,
                wake_note="Post your status from the facts you know"
                + (f", then hand off to {following}." if following else "."),
            )
        )
    specs[0].facts = [f"Status: {STATUS['Backend']}"]
    async with Run("WE-05", specs, options) as run:
        await run.phase(("Backend", tasks.create_room("Stand-up", RULES, PROTOCOL)))
        room = run.room_id()
        await run.phase(
            *[(role, tasks.join_room(room, f"{role} is here.")) for role in CHAIN]
        )
        await run.phase(
            (
                "Backend",
                tasks.ask(
                    room,
                    [
                        (
                            "Frontend",
                            (
                                f"Stand-up. Backend status: {STATUS['Backend']} "
                                "Frontend, post your status next and hand off to Tester."
                            ),
                        )
                    ],
                ),
            )
        )
        ids = {spec.name: run.member_id(spec.name) for spec in specs}
        names = {value: key for key, value in ids.items()}
        edges = [("Backend", "Frontend"), *itertools.pairwise(CHAIN)]

        async def done():
            messages = await run.messages(room, "Backend")
            for sender, target in edges:
                if not mention_of(messages, ids[sender], ids[target]):
                    return False, f"{sender} has not handed off to {target}"
            posted = {m["sender_member_id"] for m in messages}
            if missing := [n for n, i in ids.items() if i not in posted]:
                return False, f"{missing} have not posted"
            handoff = mention_of(messages, ids["Docs"], ids["Ops"])[0]["message_id"]
            if not any(
                m["sender_member_id"] == ids["Ops"] and m["message_id"] > handoff
                for m in messages
            ):
                return False, "Ops has not posted after the Docs hand-off"
            return True, ""

        try:
            await run.drain(done)
        except Failure as failure:
            messages = await run.messages(room, "Backend")
            raise Failure(
                f"{failure}. Room: {roster_text(messages, names)}. "
                f"Errors: {run.turn_errors()}"
            ) from None
        return (
            "five agents posted status and handed off Backend>Frontend>Tester>Docs>Ops"
        )


# --- WE-06 ------------------------------------------------------------------


async def we06(options) -> str:
    title = "Verify the release fixture"
    fixture = f"fixture-{uuid4().hex[:8]}"
    assignment = {
        "closing_criteria": "Confirm the smoke-test result and exact fixture ID.",
        "eta": "15 minutes after assignment",
    }
    specs = [
        Spec("A", "Release Lead"),
        Spec("B", "Tester", facts=[f"The release smoke test passed on {fixture}."]),
        Spec("C", "Reviewer"),
        Spec("D", "Observer"),
    ]
    async with Run("WE-06", specs, options) as run:
        await run.turn(
            "A",
            tasks.create_room(
                "Release verification",
                "Keep release evidence in the task history.",
                "Read and acknowledge the board. Record task work as task progress.",
            ),
        )
        room = run.room_id()
        await run.phase(
            ("B", tasks.join_room(room, "Tester is here.")),
            ("C", tasks.join_room(room, "Reviewer is here.")),
            ("D", tasks.join_room(room, "Observer is here.")),
        )
        a_id, b_id, c_id = (run.member_id(name) for name in ("A", "B", "C"))
        contributors = {b_id, c_id}
        before = await run.messages(room, "D")
        created = await run.turn(
            "A", tasks.assign_release_check(room, title, assignment)
        )
        records = (await run.read("A", "list_tasks", room_id=room))["tasks"]
        if (
            len(records) != 1
            or records[0]["title"] != title
            or records[0]["creator_member_id"] != a_id
            or not records[0]["description"]
            or set(records[0]["addressed_member_ids"]) != contributors
            or records[0]["participant_member_ids"]
            or records[0]["status"] != "open"
            or records[0].get("assignment") != assignment
        ):
            raise Failure(
                f"A did not create the addressed release task. Errors: {run.turn_errors()}"
            )
        opened = records[0]
        task_args = {"room_id": room, "task_id": opened["task_id"]}
        requests = [
            call.input
            for call in created.calls
            if call.name == "teamup_create_task" and not call.error
        ]
        if not requests:
            raise Failure("A did not create the assignment through MCP")
        if await run.read("A", "create_task", **requests[0]) != opened:
            raise Failure("Retry did not return the existing assignment")

        messages = await run.messages(room, "D")
        if messages[: len(before)] != before or len(messages) != len(before) + 1:
            raise Failure("Assignment and its retry must add exactly one room message")
        alert = messages[-1]
        if (
            alert["sender_member_id"] != a_id
            or set(alert.get("mention_member_ids", [])) != contributors
            or not re.search(rf"\bTask #{opened['task_number']}\b", alert["text"])
            or any(
                value in alert["text"]
                for value in (title, opened["description"], *assignment.values())
            )
        ):
            raise Failure(
                "Assignment mention must direct only assignees to the task number"
            )
        if (await run.read("D", "list_tasks", room_id=room))["tasks"]:
            raise Failure("A room observer could list a private assignment")
        try:
            await run.read("D", "get_task", **task_args)
        except Failure as error:
            if "not_found" not in str(error):
                raise
        else:
            raise Failure("A room observer could read a private assignment by ID")

        async def await_assignment_notice(recipient: str) -> None:
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:
                notices = (await run.read(recipient, "notifications"))["notifications"]
                if notices:
                    if (
                        len(notices) == 1
                        and notices[0].get("room_id") == room
                        and notices[0]["message_id"] == alert["message_id"]
                        and notices[0]["sender_member_id"] == a_id
                    ):
                        return
                    raise Failure(f"{recipient} received unexpected notices: {notices}")
                await asyncio.sleep(0.2)
            raise Failure(f"{recipient} did not receive the assignment mention")

        await asyncio.gather(*(await_assignment_notice(name) for name in ("B", "C")))
        for name in ("A", "D"):
            if (await run.read(name, "notifications"))["notifications"]:
                raise Failure(
                    f"{name} received a notification for another member's assignment"
                )

        await run.turn("B", tasks.record_release_check(room), kind="wake")
        evidence = (await run.read("A", "task_history", **task_args))["events"]
        if not any(
            e["kind"] == "progress"
            and e["actor_member_id"] == b_id
            and fixture in (e["note"] or "")
            for e in evidence
        ):
            raise Failure(
                f"B did not record the release evidence. Errors: {run.turn_errors()}"
            )

        await run.turn("C", tasks.review_release_check(room), kind="wake")
        task = await run.read("A", "get_task", **task_args)
        history = (await run.read("A", "task_history", **task_args))["events"]
        expected = [
            ("created", a_id),
            ("joined", b_id),
            ("progress", b_id),
            ("joined", c_id),
            ("progress", c_id),
            ("finished", c_id),
        ]
        if (
            task["status"] != "finished"
            or set(task["participant_member_ids"]) != contributors
            or [(e["kind"], e["actor_member_id"]) for e in history] != expected
            or fixture not in (history[4]["note"] or "")
        ):
            raise Failure(
                "Durable task history does not show the expected handoff and finish. "
                f"Errors: {run.turn_errors()}"
            )
        return "Both assignees received one directed mention and completed the private task handoff"


SCENARIOS = [
    Scenario("WE-01", "Two-agent DM exchange", 2, 4, 600, we01),
    Scenario("WE-02", "Three-agent room with @mentions", 3, 14, 720, we02),
    Scenario("WE-03", "@mention while the Client is offline", 2, 10, 600, we03),
    Scenario(
        "WE-04",
        "Board change mid-conversation",
        3,
        0,
        0,
        skip="Covered by the model-free Board mid-conversation check NS-15.04.",
    ),
    Scenario("WE-05", "Five-agent stand-up hand-off chain", 5, 18, 900, we05),
    Scenario("WE-06", "Directed room task assignment and handoff", 4, 7, 900, we06),
]
