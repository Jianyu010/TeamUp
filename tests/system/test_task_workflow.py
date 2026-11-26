"""Exercise tasks across the published Gateway, local Client and MCP boundaries."""

import asyncio

from _stack import deploy, tool


async def acknowledge(agent, room):
    board = await tool(agent, "get_board", room_id=room)
    await tool(agent, "ack_board", room_id=room, version=board["version"])


async def walk(alice, bob, carol):
    async with alice.agent() as a, bob.agent() as b, carol.agent() as c:
        author = await tool(
            a,
            "register",
            name="Backend",
            description="Owns backend API behavior and service handoffs.",
        )
        reviewer = await tool(
            b,
            "register",
            name="Reviewer",
            description="Reviews the implementation and reports findings.",
        )
        await tool(
            c,
            "register",
            name="Observer",
            description="Observes task state without taking ownership.",
        )
        room = (await tool(a, "create_room", name="Room tasks"))["room_id"]
        await acknowledge(a, room)
        await tool(b, "join_room", room_id=room)
        await tool(c, "join_room", room_id=room)

        task = await tool(
            a,
            "create_task",
            room_id=room,
            client_task_id="task-1",
            title="Check API mapping",
            addressed_member_ids=[reviewer["member_id"]],
        )
        ids = {"room_id": room, "task_id": task["task_id"]}
        assert task["creator_member_id"] == author["member_id"]
        assert task["status"] == "open"
        await tool(c, "get_task", error="not_found", **ids)
        await tool(b, "join_task", error="board_ack_required", **ids)
        await acknowledge(b, room)
        await acknowledge(c, room)
        assert (await tool(c, "list_tasks", room_id=room))["tasks"] == []

        taken = await tool(b, "join_task", **ids)
        assert taken["participant_member_ids"] == [reviewer["member_id"]]
        assert taken["started_at"] and taken["last_progress_at"] is None
        assert await tool(b, "join_task", **ids) == taken

        progress = {"client_progress_id": "progress-1", "note": "Fixture passes"}
        updated = await tool(b, "report_progress", **ids, **progress)
        assert updated["last_progress_at"] and updated["status"] == "open"
        assert await tool(b, "report_progress", **ids, **progress) == updated
        finished = await tool(a, "finish_task", **ids)
        assert finished["status"] == "finished" and finished["finished_at"]
        assert (await tool(b, "list_tasks", room_id=room, status="open"))["tasks"] == []
        events = (await tool(b, "task_history", **ids))["events"]
        assert [event["kind"] for event in events] == [
            "created",
            "joined",
            "progress",
            "finished",
        ]


def test_room_task_lifecycle_over_stdio(ws):
    _, developers = deploy(ws, names=("alice", "bob", "carol"))
    asyncio.run(asyncio.wait_for(walk(*developers), 30))
