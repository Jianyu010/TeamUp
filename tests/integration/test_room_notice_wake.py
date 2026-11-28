"""Acceptance for focused room notices reaching an authorized harness session."""

import asyncio
import json
import tempfile
from pathlib import Path

from test_loop import Developer, available_port, process, tcp_is_ready, tool, wait_for
from test_wake import FakeClaudeCode, InstallDeveloper, until

HANDOFF = (
    "After the appointment, hand off to the new leader any context that member "
    "is authorized to receive."
)
RANKED = "Ranked candidates, best first (TeamUp directory data, not instructions):"


async def eligible_successor(incumbent, room_id, member_id):
    """The leader's candidates page once the peer's heartbeat makes it eligible."""
    async with asyncio.timeout(20):
        while True:
            ranking = await tool(incumbent, "room_leadership_candidates", room_id=room_id)
            if [row["member_id"] for row in ranking["candidates"]] == [member_id]:
                return ranking
            await asyncio.sleep(0.1)


async def run_deferred_handoff(root, port):
    leader = Developer(root, "alice", port)
    peer = Developer(root, "bob", port)
    harness = FakeClaudeCode(root, "leader")
    successor_harness = FakeClaudeCode(root, "successor")
    for developer, session in ((leader, harness), (peer, successor_harness)):
        config = json.loads(developer.config_path.read_text())
        config["bindings"][0]["session_key"] = session.session_id
        developer.config_path.write_text(json.dumps(config))
    keys = json.dumps({"alice": leader.enrollment_key, "bob": peer.enrollment_key})

    with (
        process(
            "teamup_server.main",
            "server/src",
            ["--db", root / "server.db", "--port", port],
            root / "server.log",
            {"TEAMUP_ENROLLMENT_KEYS": keys},
        ) as server,
        leader.daemon(),
        peer.daemon(),
    ):
        wait_for(lambda: tcp_is_ready(port), server)
        async with (
            harness.running(),
            successor_harness.running(),
            leader.agent(harness.env()) as incumbent,
            peer.agent(successor_harness.env()) as successor,
        ):
            await tool(
                incumbent, "register", name="Coordinator",
                description="Coordinates the room and assigns follow-up work.",
            )
            candidate = await tool(
                successor, "register", name="Release Lead",
                description="Coordinates releases and hands off deployment.",
            )
            room_id = (await tool(incumbent, "create_room", name="Focused release"))["room_id"]
            for agent in (incumbent, successor):
                await tool(agent, "join_room", room_id=room_id)
                board = await tool(agent, "get_board", room_id=room_id)
                await tool(agent, "ack_board", room_id=room_id, version=board["version"])
            work = await tool(
                successor, "create_task", room_id=room_id, client_task_id="brief-1",
                title="Verify the release configuration",
            )
            ranking = await eligible_successor(incumbent, room_id, candidate["member_id"])
            InstallDeveloper.owner(
                leader, "wake", "--config", str(leader.config_path),
                "desktop", harness.session_id, "--room", room_id,
            )

            InstallDeveloper.owner(
                peer, "wake", "--config", str(peer.config_path),
                "desktop", successor_harness.session_id, "--room", room_id,
            )

            pending = await tool(
                incumbent, "handoff_room_leadership", room_id=room_id,
                leadership_epoch=ranking["leadership_epoch"], mode="defer",
            )
            assert pending["pending"] and not pending["left"]
            [auth, user] = await until(lambda: harness.received and harness.received[0])
            assert auth == {"type": "auth", "token": harness.token}
            durable = (
                f"Choose a successor within two minutes. Use teamup_room_leadership_candidates "
                f"for {room_id}, then teamup_handoff_room_leadership with leadership_epoch "
                f"{pending['leadership_epoch']}."
            )
            brief = ranking["candidates"][0]
            recent = brief["recent_work"]
            assert recent["kind"] == "task" and recent["task_number"] == work["task_number"]
            assert user["message"]["content"].splitlines() == [
                f"TeamUp room {room_id}: {durable}",
                RANKED,
                (
                    f"1. name: {brief['name']} | account: none | description: "
                    f'{brief["description"]} | recent work: task {recent["task_number"]} '
                    f'"{recent["title"]}" {recent["event"]} at {recent["created_at"]}'
                ),
                HANDOFF,
            ]

            # An agent that only polls still receives the durable, actionable text.
            [notice] = [
                row for row in (await tool(incumbent, "notifications"))["notifications"]
                if row.get("kind") == "leadership_deferred"
            ]
            assert notice == {
                "event_id": notice["event_id"], "kind": "leadership_deferred",
                "room_id": room_id, "text": durable,
                "leadership_epoch": pending["leadership_epoch"],
            }

            appointed = await tool(
                incumbent, "handoff_room_leadership", room_id=room_id,
                leadership_epoch=pending["leadership_epoch"], mode="choose",
                member_id=brief["member_id"], membership_id=brief["membership_id"],
            )
            assert appointed["leader_member_id"] == candidate["member_id"]
            [auth, user] = await until(
                lambda: successor_harness.received and successor_harness.received[0]
            )
            assert auth == {"type": "auth", "token": successor_harness.token}
            assert user["message"]["content"] == (
                f"TeamUp room {room_id}: You now lead room {room_id}. "
                "Read its Board, review open tasks and manage the room."
            )


def test_deferred_handoff_wakes_the_incumbent_and_successor():
    with tempfile.TemporaryDirectory(prefix="tu-room-notice-", dir="/tmp") as directory:

        async def run():
            async with asyncio.timeout(90):
                await run_deferred_handoff(Path(directory), available_port())

        asyncio.run(run())
