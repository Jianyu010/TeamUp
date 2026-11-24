import pytest
from _stack import Server, new_secret

KEY = new_secret()


@pytest.mark.scenario("NS-05", "Room creation, join, and refusal of non-members")
def test_room_create_join_refuse(ws):
    server = Server(ws, {"owner": KEY}).start()
    api = server.api()
    alice = api.enroll(KEY, "Backend", session="a")
    bob = api.enroll(KEY, "Frontend", session="b")
    carol = api.enroll(KEY, "Outsider", session="c")

    created = api.call("POST", "/v1/rooms", alice.token, json={"name": "API compat"})
    room = created["room_id"]
    assert created["name"] == "API compat"
    # A room always has a board: version 1 with default text.
    board = api.call("GET", f"/v1/rooms/{room}/board", alice.token)
    assert board["version"] == 1 and board["room_id"] == room

    # Before joining, the room is invisible and unusable.
    for method, path, body in (
        ("GET", f"/v1/rooms/{room}/board", None),
        ("GET", f"/v1/rooms/{room}/messages", None),
        (
            "POST",
            f"/v1/rooms/{room}/messages",
            {"client_message_id": "x", "text": "hi"},
        ),
        ("POST", f"/v1/rooms/{room}/board/ack", {"version": 1}),
    ):
        status = api.call(method, path, bob.token, json=body, error="not_found")
        assert status == 404, path

    assert api.call("POST", f"/v1/rooms/{room}/join", bob.token) == created
    # Joining again is harmless and keeps the original join position.
    assert api.call("POST", f"/v1/rooms/{room}/join", bob.token) == created
    api.read_and_ack_board(alice, room)
    api.read_and_ack_board(bob, room)
    api.send(alice, room, "hello bob")
    assert [m["text"] for m in api.history(bob, room)] == ["hello bob"]

    # The outsider stays out even after the room is busy.
    assert api.call("GET", f"/v1/rooms/{room}/messages", carol.token, error="not_found")
    assert api.call(
        "POST",
        f"/v1/rooms/{room}/messages",
        carol.token,
        json={"client_message_id": "x", "text": "let me in"},
        error="not_found",
    )
    # Mentioning a non-member of the room is invalid.
    assert (
        api.call(
            "POST",
            f"/v1/rooms/{room}/messages",
            alice.token,
            json={
                "client_message_id": "m1",
                "text": "psst",
                "mention_member_id": carol.member_id,
            },
            error="invalid_mention",
        )
        == 422
    )

    # Unknown rooms, malformed ids and bad input.
    missing = "00000000-0000-4000-8000-000000000000"
    assert (
        api.call("POST", f"/v1/rooms/{missing}/join", bob.token, error="not_found")
        == 404
    )
    assert (
        api.call(
            "POST", "/v1/rooms/not-a-uuid/join", bob.token, error="invalid_request"
        )
        == 422
    )
    for body in (
        {"name": "  "},
        {"name": "x", "rules": ""},
        {"name": "x", "extra": 1},
        {},
    ):
        assert (
            api.call(
                "POST", "/v1/rooms", alice.token, json=body, error="invalid_request"
            )
            == 422
        )
    assert (
        api.call(
            "POST", "/v1/rooms", new_secret(), json={"name": "x"}, error="unauthorized"
        )
        == 401
    )
    api.close()
