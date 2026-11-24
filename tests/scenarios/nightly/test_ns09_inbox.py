import pytest
from _stack import Server, new_secret

KEY = new_secret()


def stream_headers(member):
    return {"Authorization": f"Bearer {member.token}", "Accept": "text/event-stream"}


@pytest.mark.scenario(
    "NS-09", "Inbox events, acks, cursors and SSE wake-ups over the public API"
)
def test_inbox_and_sse(ws):
    server = Server(ws, {"owner": KEY}).start()
    api = server.api()
    alice, bob, carol, dave = (
        api.enroll(KEY, name, session=name)
        for name in ("alice", "bob", "carol", "dave")
    )
    room = api.create_room(alice, "inbox", rules="R")
    for member in (bob, carol, dave):
        api.join(member, room)
    for member in (alice, bob, carol):
        api.read_and_ack_board(member, room)

    def inbox(member, **params):
        return api.call("GET", "/v1/inbox", member.token, params=params)["events"]

    assert inbox(bob) == []
    # A live stream is told to look again when something arrives.
    with api.http.stream("GET", "/v1/events", headers=stream_headers(bob)) as stream:
        lines = stream.iter_lines()
        assert next(lines).startswith(":")
        m1 = api.send(alice, room, "no mention", cid="m1")
        m2 = api.send(alice, room, "bob please look", cid="m2", mention=bob.member_id)
        frame = next(line for line in lines if line.startswith("event:"))
        assert frame == "event: inbox_changed"

    kinds = [(e["kind"], e["message"]["message_id"]) for e in inbox(bob)]
    assert kinds == [("message", m1["message_id"]), ("mention", m2["message_id"])]
    assert [e["kind"] for e in inbox(carol)] == ["message", "message"]
    assert inbox(alice) == []  # senders read their own messages from history
    event_ids = [e["event_id"] for e in inbox(bob)]
    assert event_ids == sorted(event_ids)

    # Cursors and limits.
    assert [e["event_id"] for e in inbox(bob, after=event_ids[0])] == event_ids[1:]
    assert len(inbox(bob, limit=1)) == 1
    assert api.call(
        "GET", "/v1/inbox", bob.token, params={"limit": 101}, error="invalid_request"
    )

    # Acks are idempotent, personal and remove the event.
    ack = f"/v1/inbox/{event_ids[0]}/ack"
    assert api.call("POST", ack, bob.token) == {"event_id": event_ids[0]}
    assert api.call("POST", ack, bob.token) == {"event_id": event_ids[0]}
    assert [e["event_id"] for e in inbox(bob)] == event_ids[1:]
    assert len(inbox(carol)) == 2
    assert api.call("POST", ack, carol.token, error="not_found") == 404
    assert api.call("POST", "/v1/inbox/999999/ack", bob.token, error="not_found") == 404
    assert (
        api.call("POST", f"/v1/inbox/{event_ids[1]}/ack", dave.token, error="not_found")
        == 404
    )

    # Dave joined but has not acknowledged the board: nothing is released yet.
    assert inbox(dave) == []
    api.read_and_ack_board(dave, room)
    assert len(inbox(dave)) == 2
    api.close()
