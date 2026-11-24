import asyncio

import pytest
from _stack import agent_tool_names, deploy, tool

DM_WORDS = ("dm", "direct", "private", "conversation")


async def run(alice, bob, carol, record):
    async with alice.agent() as a, bob.agent() as b, carol.agent() as c:
        names = await agent_tool_names(a)
        dm_tools = sorted(n for n in names if any(w in n.lower() for w in DM_WORDS))
        if dm_tools:
            pytest.skip(f"DM tools detected {dm_tools}; extend NS-08 to cover them")
        record(
            "detail",
            "DM is not merged on this branch: ran as a two-member room",
        )
        a_member = await tool(a, "register", role_description="Backend")
        b_member = await tool(b, "register", role_description="Frontend")
        c_member = await tool(c, "register", role_description="Bystander")

        room = (await tool(a, "create_room", name="alice+bob", rules="Private pair"))[
            "room_id"
        ]
        await tool(
            a,
            "ack_board",
            room_id=room,
            version=(await tool(a, "get_board", room_id=room))["version"],
        )
        await tool(b, "join_room", room_id=room)
        await tool(
            b,
            "ack_board",
            room_id=room,
            version=(await tool(b, "get_board", room_id=room))["version"],
        )

        first = await tool(
            a,
            "send_message",
            room_id=room,
            client_message_id="q1",
            text="Which fields does /orders return?",
            mention_member_id=b_member["member_id"],
        )
        await asyncio.to_thread(bob.wait_for_notice, first["message_id"])
        reply = await tool(
            b,
            "send_message",
            room_id=room,
            client_message_id="r1",
            text="id, total, items.",
            mention_member_id=a_member["member_id"],
        )
        await asyncio.to_thread(alice.wait_for_notice, reply["message_id"])

        # Each side reads the full conversation in order.
        for agent in (a, b):
            texts = [
                m["text"]
                for m in (await tool(agent, "read_messages", room_id=room))["messages"]
            ]
            assert texts == ["Which fields does /orders return?", "id, total, items."]
        # A third member can neither read nor write the pair's room, nor be mentioned.
        await tool(c, "read_messages", error="not_found", room_id=room)
        await tool(
            c,
            "send_message",
            error="not_found",
            room_id=room,
            client_message_id="x",
            text="hi",
        )
        await tool(
            a,
            "send_message",
            error="invalid_mention",
            room_id=room,
            client_message_id="q2",
            text="adding you",
            mention_member_id=c_member["member_id"],
        )
        assert carol.notices() == [] and carol.desktop_notices() == []


@pytest.mark.scenario(
    "NS-08", "Two-member private conversation (room fallback until DM lands)"
)
def test_two_member_conversation(ws, record_property):
    _, (alice, bob, carol) = deploy(ws, names=("alice", "bob", "carol"))
    asyncio.run(asyncio.wait_for(run(alice, bob, carol, record_property), 90))
