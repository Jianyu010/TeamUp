"""Focused rooms through real Server, Client and bound MCP processes."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta

from _stack import deploy, tool


async def registered(session, name):
    return (
        await tool(session, "register", name=name, description="Verifies focused work.")
    )["member_id"]


async def acknowledge(session, room):
    board = await tool(session, "get_board", room_id=room)
    await tool(session, "ack_board", room_id=room, version=board["version"])


async def wait_for_notification(session, matches):
    async with asyncio.timeout(20):
        while True:
            feed = (await tool(session, "notifications"))["notifications"]
            found = [notice for notice in feed if matches(notice)]
            if found:
                [notice] = found
                return notice
            await asyncio.sleep(0.1)


async def ranked_candidates(session, room, expected):
    async with asyncio.timeout(20):
        while True:
            result = await tool(session, "room_leadership_candidates", room_id=room)
            if {row["member_id"] for row in result["candidates"]} == set(expected):
                assert [row["member_id"] for row in result["candidates"]] == expected
                return result
            await asyncio.sleep(0.1)


async def exercise_focused_room(creator, supervisor, reviewer):
    async with AsyncExitStack() as stack:
        owner, lead, peer = [
            await stack.enter_async_context(dev.agent())
            for dev in (creator, supervisor, reviewer)
        ]
        owner_id = await registered(owner, "Coordinator")
        lead_id = await registered(lead, "Release Lead")
        peer_id = await registered(peer, "Reviewer")
        parent = (await tool(owner, "create_room", name="Release"))["room_id"]
        await tool(lead, "join_room", room_id=parent)
        for agent in (owner, lead):
            await acknowledge(agent, parent)

        create = {
            "parent_room_id": parent,
            "client_request_id": "prep-child",
            "name": "Focused preparation",
        }
        child = await tool(owner, "create_linked_room", **create)
        room = child["room_id"]
        assert await tool(owner, "create_linked_room", **create) == child
        assert child["parent_room_id"] == parent
        assert datetime.fromisoformat(child["expires_at"]) - datetime.fromisoformat(
            child["created_at"]
        ) == timedelta(hours=5)
        directory = await tool(lead, "list_child_rooms", room_id=parent)
        assert [entry["room_id"] for entry in directory["rooms"]] == [room]
        await tool(lead, "get_board", room_id=room, error="not_found")

        found = await tool(owner, "find_members", q="Release Lead")
        assert [member["member_id"] for member in found["members"]] == [lead_id]
        invitation = {
            "room_id": room,
            "member_id": lead_id,
            "client_request_id": "lead-invitation",
        }
        delivered = await tool(owner, "invite_to_room", **invitation)
        assert await tool(owner, "invite_to_room", **invitation) == delivered
        dm = delivered["dm_id"]
        notified = await wait_for_notification(
            lead, lambda notice: notice.get("message_id") == delivered["message_id"]
        )
        assert notified["kind"] == "dm" and notified["sender_member_id"] == owner_id
        [message] = (await tool(lead, "read_dm", dm_id=dm))["messages"]
        assert message == delivered
        assert room in message["text"] and child["name"] in message["text"]
        assert "board" in message["text"].lower()
        await tool(peer, "read_dm", dm_id=dm, error="not_found")
        await tool(lead, "get_board", room_id=room, error="not_found")

        for agent in (lead, peer):
            await tool(agent, "join_room", room_id=room)
        for agent in (owner, lead, peer):
            await acknowledge(agent, room)

        work = await tool(
            owner,
            "create_task",
            room_id=room,
            client_task_id="prep-task",
            title="Verify the release configuration",
        )
        await tool(
            owner, "archive_room", room_id=parent, error="room_has_live_children"
        )

        ranking = await ranked_candidates(owner, room, [lead_id, peer_id])
        handoff = await tool(
            owner,
            "handoff_room_leadership",
            room_id=room,
            mode="auto",
            leadership_epoch=ranking["leadership_epoch"],
        )
        assert handoff["leader_member_id"] == lead_id
        assert handoff["leadership_epoch"] > ranking["leadership_epoch"]
        assert not handoff["pending"] and not handoff["left"]
        notice = await wait_for_notification(
            lead,
            lambda row: (
                row.get("kind") == "leadership_assigned" and row.get("room_id") == room
            ),
        )
        assert notice["leadership_epoch"] == handoff["leadership_epoch"]
        assert "board" in notice["text"].lower()
        await tool(owner, "archive_room", room_id=room, error="forbidden")

        await tool(lead, "archive_room", room_id=room)
        await tool(lead, "archive_room", room_id=room)
        [continuation] = (await tool(owner, "list_tasks", room_id=parent))["tasks"]
        assert continuation["title"] == work["title"]
        assert continuation["origin"] == {"room_id": room, "task_id": work["task_id"]}
        assert (
            await tool(owner, "get_task", room_id=room, task_id=work["task_id"])
        ) == work
        await tool(owner, "archive_room", room_id=parent)


def test_invite_handoff_and_child_close_over_stdio(ws):
    _, developers = deploy(ws, names=("creator", "lead", "reviewer"), notify=False)
    asyncio.run(asyncio.wait_for(exercise_focused_room(*developers), 65))
