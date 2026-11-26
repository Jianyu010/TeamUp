import pytest
from _stack import Server, new_secret

KEY = new_secret()


@pytest.mark.scenario(
    "NS-03",
    "Liveness: unauthenticated rejection, authenticated ping, restart keeps identity",
)
def test_ping_and_restart(ws):
    server = Server(ws, {"owner": KEY}).start()
    api = server.api()
    member = api.enroll(KEY, "Backend")
    room = api.create_room(member, "ping", rules="R")
    api.read_and_ack_board(member, room)
    sent = api.send(member, room, "before restart", cid="c1")

    # No credential, malformed credential and unknown credential share one error.
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}):
        response = api.http.get("/v1/members/me", headers=headers)
        assert response.status_code == 401
        assert response.json() == {
            "error": {
                "code": "unauthorized",
                "message": "The credential is missing or invalid.",
            }
        }
    assert api.call("GET", "/v1/members/me", new_secret(), error="unauthorized") == 401
    assert api.call("GET", "/v1/nope", member.token, error="not_found") == 404
    # An enrollment key is not a member credential.
    assert api.call("GET", "/v1/members/me", KEY, error="unauthorized") == 401

    me = api.call("GET", "/v1/members/me", member.token)
    assert me == {
        "member_id": member.member_id,
        "role_description": "Backend",
        "name": "Backend",
        "description": "Handles the Backend work in this scenario.",
    }

    # SSE is the live channel; it announces readiness with a comment frame.
    with api.http.stream(
        "GET",
        "/v1/events",
        headers={
            "Authorization": f"Bearer {member.token}",
            "Accept": "text/event-stream",
        },
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        first = next(stream.iter_lines())
        assert first.startswith(":")
    assert api.call("GET", "/v1/events", member.token, error="invalid_request") == 422
    api.close()

    server.restart()
    api = server.api()
    assert api.call("GET", "/v1/members/me", member.token) == me
    # The same binding is recognised: enrollment returns the original member.
    again = api.enroll(KEY, "Backend", token=member.token)
    assert again.member_id == member.member_id
    assert api.history(member, room) == [sent]
    assert api.send(member, room, "before restart", cid="c1") == sent
    api.close()
