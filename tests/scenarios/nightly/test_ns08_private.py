import asyncio

import pytest
from _stack import deploy, tool


async def wait_for_dm_notices(agent, message_id):
    async with asyncio.timeout(20):
        while True:
            notifications = (await tool(agent, "notifications"))["notifications"]
            notices = [notice for notice in notifications if notice.get("kind") == "dm"]
            if any(notice["message_id"] == message_id for notice in notices):
                return notices
            await asyncio.sleep(0.1)


async def run(alice, bob, carol):
    async with alice.agent() as a, bob.agent() as b, carol.agent() as c:
        a_member = await tool(
            a,
            "register",
            name="Backend",
            description="Owns backend API behavior and service handoffs.",
        )
        b_member = await tool(
            b,
            "register",
            name="Frontend",
            description="Owns interface behavior and coordinates API integration.",
        )
        c_member = await tool(
            c,
            "register",
            name="Bystander",
            description="Verifies that private conversations remain out of scope.",
        )

        opened = await tool(a, "open_dm", member_id=b_member["member_id"])
        dm_id = opened["dm_id"]
        assert opened["peer_member_id"] == b_member["member_id"]

        question = "Which fields does /orders return?"
        first = await tool(
            a,
            "send_dm",
            dm_id=dm_id,
            client_message_id="q1",
            text=question,
        )
        bob_notices = await wait_for_dm_notices(b, first["message_id"])

        reopened = await tool(b, "open_dm", member_id=a_member["member_id"])
        assert reopened["dm_id"] == dm_id
        assert reopened["peer_member_id"] == a_member["member_id"]

        answer = "id, total, items."
        reply = await tool(
            b,
            "send_dm",
            dm_id=dm_id,
            client_message_id="r1",
            text=answer,
        )
        alice_notices = await wait_for_dm_notices(a, reply["message_id"])
        assert [first["text"], reply["text"]] == [question, answer]

        for agent, notices, received, sender in (
            (a, alice_notices, reply, b_member),
            (b, bob_notices, first, a_member),
        ):
            assert len(notices) == 1
            notice = notices[0]
            assert notice["dm_id"] == dm_id
            assert notice["message_id"] == received["message_id"]
            assert notice["sender_member_id"] == sender["member_id"]
            assert "text" not in notice

            history = await tool(agent, "read_dm", dm_id=dm_id)
            assert history["messages"] == [first, reply]

        await tool(c, "read_dm", error="not_found", dm_id=dm_id)
        await tool(
            c,
            "send_dm",
            error="not_found",
            dm_id=dm_id,
            client_message_id="x",
            text="hi",
        )
        # A later DM confirms Carol's notification feed has caught up.
        separate_dm = await tool(a, "open_dm", member_id=c_member["member_id"])
        control = await tool(
            a,
            "send_dm",
            dm_id=separate_dm["dm_id"],
            client_message_id="control",
            text="This message is for Carol.",
        )
        carol_notices = await wait_for_dm_notices(c, control["message_id"])
        assert [notice["message_id"] for notice in carol_notices] == [
            control["message_id"]
        ]


@pytest.mark.scenario("NS-08", "Two-member direct message conversation")
def test_two_member_conversation(ws):
    _, (alice, bob, carol) = deploy(ws, names=("alice", "bob", "carol"))
    asyncio.run(asyncio.wait_for(run(alice, bob, carol), 90))
