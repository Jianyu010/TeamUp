"""Archive and retrieve a room through the bound MCP agents."""

import asyncio

from _stack import deploy, tool


async def archive_room(alice, bob):
    async with alice.agent() as creator, bob.agent() as peer:
        first = await tool(
            creator,
            "register",
            name="Creator",
            description="Creates and closes the room.",
        )
        second = await tool(
            peer, "register", name="Peer", description="Checks archived room access."
        )
        room = (await tool(creator, "create_room", name="Archived team room"))[
            "room_id"
        ]
        await tool(peer, "join_room", room_id=room)
        for agent in (creator, peer):
            board = await tool(agent, "get_board", room_id=room)
            await tool(agent, "ack_board", room_id=room, version=board["version"])
        await tool(
            creator,
            "send_message",
            room_id=room,
            text="Still here",
            client_message_id="first",
        )
        await tool(peer, "archive_room", room_id=room, error="forbidden")
        archived = await tool(creator, "archive_room", room_id=room)
        assert (await tool(peer, "list_rooms"))["rooms"] == []
        assert (await tool(peer, "list_archived_rooms"))["rooms"] == [archived]
        members = (await tool(peer, "list_room_members", room_id=room))["members"]
        assert {member["member_id"] for member in members} == {
            first["member_id"],
            second["member_id"],
        }
        messages = (await tool(peer, "read_messages", room_id=room))["messages"]
        assert [message["text"] for message in messages] == ["Still here"]


def test_room_archive_over_stdio(ws):
    _, developers = deploy(ws, names=("alice", "bob"))
    asyncio.run(asyncio.wait_for(archive_room(*developers), 30))
