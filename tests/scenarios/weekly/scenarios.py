"""WE-xx scenarios. Outcomes are judged from TeamUp state, never by a model."""

import asyncio
import itertools
import random
from dataclasses import dataclass

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
    specs = [
        Spec("A", "Backend", mention=None),
        Spec(
            "B",
            "Frontend",
            facts=[f"The frontend dev server listens on port {port}."],
            mention="sender",
            wake_note="Answer the question with the facts you know.",
        ),
    ]
    async with Run("WE-01", specs, options) as run:
        await run.phase(
            ("A", tasks.create_room("Port check", RULES, PROTOCOL)),
        )
        room = run.room_id()
        await run.phase(("B", tasks.join_room(room, "Frontend is here.")))
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
        names = {a_id: "A", b_id: "B"}

        async def done():
            messages = await run.messages(room, "A")
            asks = mention_of(messages, a_id, b_id)
            replies = mention_of(messages, b_id, a_id)
            if not asks:
                return False, "A has not asked B with a mention"
            replies = [r for r in replies if r["message_id"] > asks[0]["message_id"]]
            if not replies:
                return False, "B has not replied to A with a mention"
            final = [
                m
                for m in messages
                if m["sender_member_id"] == a_id
                and m["message_id"] > replies[0]["message_id"]
                and port in m["text"]
            ]
            if not final:
                return False, f"A has not posted port {port} after B's reply"
            return True, ""

        try:
            await run.drain(done)
        except Failure as failure:
            messages = await run.messages(room, "A")
            raise Failure(
                f"{failure}. Room: {roster_text(messages, names)}. "
                f"Errors: {run.turn_errors()}"
            ) from None
        return (
            "two-member room (no DM tool in this build): A asked B, B replied with a "
            f"mention, A posted port {port}"
        )


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


# --- WE-04 ------------------------------------------------------------------


async def we04(options) -> str:
    raise Failure("skipped scenario was run")


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


SCENARIOS = [
    Scenario("WE-01", "Two-agent direct exchange", 2, 10, 600, we01),
    Scenario("WE-02", "Three-agent room with @mentions", 3, 14, 720, we02),
    Scenario("WE-03", "@mention while the Client is offline", 2, 10, 600, we03),
    Scenario(
        "WE-04",
        "Board change mid-conversation",
        3,
        0,
        0,
        we04,
        skip="Server and MCP have no board-update operation; boards are fixed at "
        "version 1 (only get_board and ack_board exist).",
    ),
    Scenario("WE-05", "Five-agent stand-up hand-off chain", 5, 18, 900, we05),
]
