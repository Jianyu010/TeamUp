import asyncio

import pytest
from _stack import Server, deploy, new_secret, tool

KEY = new_secret()


@pytest.mark.scenario(
    "NS-06", "Board read/ack gate: versions, error codes, messages blocked until ack"
)
def test_board_gate_http(ws):
    server = Server(ws, {"owner": KEY}).start()
    api = server.api()
    alice = api.enroll(KEY, "Backend", session="a")
    bob = api.enroll(KEY, "Frontend", session="b")
    room = api.create_room(
        alice, "gated", rules="Share test data", protocol="Ack first"
    )
    api.join(bob, room)

    # Joining alone does not unlock the room.
    for method, path, body in (
        ("GET", f"/v1/rooms/{room}/messages", None),
        (
            "POST",
            f"/v1/rooms/{room}/messages",
            {"client_message_id": "c", "text": "hi"},
        ),
    ):
        status = api.call(
            method, path, bob.token, json=body, error="board_ack_required"
        )
        assert status == 409
    ack = f"/v1/rooms/{room}/board/ack"
    # Acknowledging a version that was never read.
    assert (
        api.call(
            "POST", ack, bob.token, json={"version": 1}, error="board_read_required"
        )
        == 409
    )
    assert (
        api.call(
            "POST", ack, bob.token, json={"version": True}, error="invalid_request"
        )
        == 422
    )
    assert (
        api.call("POST", ack, bob.token, json={"version": 0}, error="invalid_request")
        == 422
    )
    assert api.call("POST", ack, bob.token, json={}, error="invalid_request") == 422

    board = api.call("GET", f"/v1/rooms/{room}/board", bob.token)
    assert board == {
        "room_id": room,
        "version": 1,
        "rules": "Share test data",
        "protocol": "Ack first",
    }
    # Read but not acknowledged: still blocked. A wrong version conflicts.
    assert (
        api.call(
            "GET", f"/v1/rooms/{room}/messages", bob.token, error="board_ack_required"
        )
        == 409
    )
    assert (
        api.call("POST", ack, bob.token, json={"version": 2}, error="conflict") == 409
    )
    assert api.call("POST", ack, bob.token, json={"version": 1}) == {"version": 1}
    # Acknowledging twice is fine.
    assert api.call("POST", ack, bob.token, json={"version": 1}) == {"version": 1}

    # Bob's acknowledgement does not unlock Alice.
    assert (
        api.call(
            "POST",
            f"/v1/rooms/{room}/messages",
            alice.token,
            json={"client_message_id": "c", "text": "hi"},
            error="board_ack_required",
        )
        == 409
    )
    api.read_and_ack_board(alice, room)
    api.send(bob, room, "board acknowledged")
    assert [m["text"] for m in api.history(alice, room)] == ["board acknowledged"]
    api.close()


@pytest.mark.scenario(
    "NS-14", "Board tools through Client and MCP carry the same error codes"
)
def test_board_gate_mcp(ws):
    _, (alice, bob) = deploy(ws)

    async def run():
        async with alice.agent() as a, bob.agent() as b:
            await tool(a, "register", role_description="Backend")
            await tool(b, "register", role_description="Frontend")
            room = (await tool(a, "create_room", name="Gate", rules="Share test data"))[
                "room_id"
            ]
            await tool(
                a, "ack_board", error="invalid_request", room_id=room, version=True
            )
            await tool(
                a, "ack_board", error="board_read_required", room_id=room, version=1
            )
            await tool(a, "read_messages", error="board_ack_required", room_id=room)
            board = await tool(a, "get_board", room_id=room)
            assert board["rules"] == "Share test data" and board["version"] == 1
            await tool(a, "ack_board", room_id=room, version=board["version"])
            await tool(b, "read_messages", error="not_found", room_id=room)
            await tool(b, "join_room", room_id=room)
            await tool(
                b,
                "send_message",
                error="board_ack_required",
                room_id=room,
                client_message_id="x",
                text="too early",
            )
            board = await tool(b, "get_board", room_id=room)
            await tool(b, "ack_board", room_id=room, version=board["version"])
            await tool(
                b, "send_message", room_id=room, client_message_id="x", text="ok"
            )
            texts = [
                m["text"]
                for m in (await tool(a, "read_messages", room_id=room))["messages"]
            ]
            assert texts == ["ok"]

    asyncio.run(asyncio.wait_for(run(), 60))
