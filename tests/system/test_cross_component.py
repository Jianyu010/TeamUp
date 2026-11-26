"""3 · Linux ST · cross-component: Server, two Client daemons and MCP sessions.

Walks register, create room, join, board read and ack, a message with an
@mention, the live notice on the other Client, and the inbox ack, using only
public entry points.
"""

import asyncio

from _stack import deploy, tool, wait_for


async def walk(server, alice, bob):
    api = server.api()
    async with alice.agent() as a, bob.agent() as b:
        responsibility = "Reviews database changes and coordinates backend releases."
        a_member = await tool(
            a, "register", name="Backend Lead", description=responsibility
        )
        assert a_member["name"] == "Backend Lead"
        assert a_member["description"] == responsibility
        b_member = await tool(
            b,
            "register",
            name="Frontend",
            description="Owns interface behavior and coordinates API integration.",
        )
        assert a_member["member_id"] != b_member["member_id"]

        room = (await tool(a, "create_room", name="Cross", rules="Be kind"))["room_id"]
        board = await tool(a, "get_board", room_id=room)
        await tool(a, "ack_board", room_id=room, version=board["version"])

        await tool(b, "join_room", room_id=room)
        roster = (await tool(b, "list_room_members", room_id=room))["members"]
        backend = next(
            entry for entry in roster if entry["member_id"] == a_member["member_id"]
        )
        assert backend["description"] == responsibility
        board = await tool(b, "get_board", room_id=room)
        await tool(b, "ack_board", room_id=room, version=board["version"])

        sent = await tool(
            a,
            "send_message",
            room_id=room,
            client_message_id="cross-1",
            text="Please review.",
            mention_member_id=b_member["member_id"],
        )
        notice = await asyncio.to_thread(bob.wait_for_notice, sent["message_id"])
        assert notice["sender_member_id"] == a_member["member_id"]
        assert all(n["message_id"] != sent["message_id"] for n in alice.notices())

        got = (await tool(b, "notifications"))["notifications"]
        assert [n["message_id"] for n in got] == [sent["message_id"]]
        history = (await tool(b, "read_messages", room_id=room))["messages"]
        assert [m["text"] for m in history] == ["Please review."]

    # The Client acked the mention on the Server after delivering it, so the
    # member's inbox is empty and a repeated ack is idempotent.
    token = bob.bindings[0]["member_token"]
    try:
        # The ack is a separate request after the notice, so wait for it to land.
        wait_for(
            lambda: api.call("GET", "/v1/inbox", token)["events"] == [],
            timeout=10,
            what="the Client to ack the mention",
        )
        event_id = notice["event_id"]
        ack = api.call("POST", f"/v1/inbox/{event_id}/ack", token)
        assert ack == {"event_id": event_id}
    finally:
        api.close()


def test_register_room_message_notice_ack(ws):
    server, (alice, bob) = deploy(ws)
    asyncio.run(asyncio.wait_for(walk(server, alice, bob), 50))
