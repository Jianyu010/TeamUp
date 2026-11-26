import asyncio

import pytest
from _stack import Server, deploy, new_secret, tool

KEY = new_secret()


@pytest.mark.scenario(
    "NS-04", "Idempotent registration and finding members by role over HTTP"
)
def test_register_and_find_http(ws):
    server = Server(ws, {"owner": KEY}).start()
    api = server.api()
    backend = api.enroll(KEY, "Backend API", session="s1")
    frontend = api.enroll(KEY, "Frontend UI", session="s2")
    tester = api.enroll(KEY, "QA tester", session="s3")
    assert len({backend.member_id, frontend.member_id, tester.member_id}) == 3

    # Same binding and token: the original profile comes back.
    assert api.enroll(KEY, "Backend API", session="s1", token=backend.token) == backend
    # Same binding, different token, or a token on another session: conflict.
    for kwargs in ({"session": "s1"}, {"session": "s9", "token": backend.token}):
        assert (
            api.call(
                "POST",
                "/v1/members",
                KEY,
                json={
                    "harness_instance_id": "h",
                    "session_key": kwargs["session"],
                    "name": "Backend API",
                    "description": "Handles the Backend API work in this scenario.",
                    "member_token": kwargs.get("token", new_secret()),
                },
                error="conflict",
            )
            == 409
        )
    unknown_key = {
        "harness_instance_id": "h",
        "session_key": "z",
        "name": "New Member",
        "description": "Verifies that enrollment requires a known owner.",
        "member_token": new_secret(),
    }
    assert (
        api.call(
            "POST", "/v1/members", new_secret(), json=unknown_key, error="unauthorized"
        )
        == 401
    )
    bad = {
        "harness_instance_id": "h",
        "session_key": "",
        "name": " ",
        "description": "Verifies validation of enrollment input.",
        "member_token": "short",
    }
    assert (
        api.call("POST", "/v1/members", KEY, json=bad, error="invalid_request") == 422
    )

    everyone = api.call("GET", "/v1/members", backend.token)["members"]
    assert {m["member_id"] for m in everyone} == {
        backend.member_id,
        frontend.member_id,
        tester.member_id,
    }
    assert everyone == sorted(everyone, key=lambda m: m["member_id"])
    found = api.call("GET", "/v1/members", backend.token, params={"q": "frontEND"})
    assert found == {
        "members": [
            {
                "member_id": frontend.member_id,
                "role_description": "Frontend UI",
                "name": "Frontend UI",
                "description": "Handles the Frontend UI work in this scenario.",
            }
        ]
    }
    assert api.call("GET", "/v1/members", backend.token, params={"q": "nobody"}) == {
        "members": []
    }
    api.close()


@pytest.mark.scenario("NS-13", "Registration and role search through Client and MCP")
def test_register_and_find_mcp(ws):
    _, (alice, bob) = deploy(ws)

    async def run():
        async with alice.agent() as agent_a, bob.agent() as agent_b:
            a = await tool(
                agent_a,
                "register",
                name="Backend",
                description="Owns backend API behavior and service handoffs.",
            )
            assert (
                await tool(
                    agent_a,
                    "register",
                    name="Backend",
                    description="Owns backend API behavior and service handoffs.",
                )
                == a
            )
            b = await tool(
                agent_b,
                "register",
                name="Frontend",
                description="Owns interface behavior and coordinates API integration.",
            )
            assert a["member_id"] != b["member_id"]
            found = await tool(agent_a, "find_members", q="front")
            assert [m["member_id"] for m in found["members"]] == [b["member_id"]]
            both = await tool(agent_b, "find_members")
            assert {m["member_id"] for m in both["members"]} == {
                a["member_id"],
                b["member_id"],
            }

    asyncio.run(asyncio.wait_for(run(), 60))
