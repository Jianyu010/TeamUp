import asyncio
import json
import time

import pytest
from _stack import (
    ROOT,
    Developer,
    Server,
    clean_env,
    command,
    new_secret,
    run_process,
    run_with_tty,
    tool,
)

PASSWORD = "correct horse battery " + new_secret()[:8]


def server_cli(server, *args):
    argv, env = command("server")
    return [*argv, "accounts", "--db", str(server.db), *args], env


def client_cli(*args):
    argv, env = command("client")
    return [*argv, *args], env


@pytest.mark.scenario(
    "NS-10", "Account login, agent enrollment under an account, and disabling it"
)
def test_account_login_and_disable(ws):
    server = Server(ws, {})
    argv, env = server_cli(server, "create", "alice")
    status, output = run_with_tty(argv, [PASSWORD, PASSWORD], env)
    assert status == 0 and "create succeeded" in output, output
    assert PASSWORD not in output  # prompts do not echo the password
    # A second create of the same name is refused; weak passwords too.
    argv, env = server_cli(server, "create", "alice")
    status, output = run_with_tty(argv, [PASSWORD, PASSWORD], env)
    assert status != 0 and "already exists" in output
    argv, env = server_cli(server, "create", "weakling")
    status, output = run_with_tty(argv, ["short", "short"], env)
    assert status != 0 and "12" in output
    server.start()
    api = server.api()

    # Login: wrong password, unknown user and disabled-looking failures look identical.
    bad = {"username": "alice", "password": "not the password"}
    ghost = {"username": "ghost", "password": PASSWORD}
    for body in (bad, ghost):
        assert (
            api.call("POST", "/v1/auth/login", json=body, error="unauthorized") == 401
        )
    login = api.call(
        "POST", "/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert login["expires_at"] > time.time() + 7 * 3600
    human = login["access_token"]
    assert len(human) == 43

    # A human token can enroll agents but is not a member credential.
    agent = api.enroll(human, "Backend", session="agent-1")
    assert api.call("GET", "/v1/members/me", human, error="unauthorized") == 401
    assert (
        api.call("GET", "/v1/members/me", agent.token)["member_id"] == agent.member_id
    )
    # The member credential cannot enroll more agents.
    assert (
        api.call(
            "POST",
            "/v1/members",
            agent.token,
            json={
                "harness_instance_id": "h",
                "session_key": "x",
                "role_description": "y",
                "member_token": new_secret(),
            },
            error="unauthorized",
        )
        == 401
    )

    # The Client path: `teamup-client login` stores the token, the daemon enrolls.
    dev = Developer(ws, "alice", server, credentials=False)
    argv, env = client_cli(
        "login", "--config", str(dev.config_path), "--username", "alice"
    )
    status, output = run_with_tty(argv, [PASSWORD], env)
    assert status == 0 and "login saved" in output, output
    assert PASSWORD not in dev.config_path.read_text()
    assert "account_token" in json.loads(dev.config_path.read_text())
    dev.start()

    async def register():
        async with dev.agent() as a:
            member = await tool(a, "register", role_description="Frontend")
            room = (await tool(a, "create_room", name="mine", rules="R"))["room_id"]
            return member, room

    member, room = asyncio.run(asyncio.wait_for(register(), 60))
    token = dev.bindings[0]["member_token"]
    assert api.call("GET", "/v1/members/me", token)["member_id"] == member["member_id"]

    # Open a live stream, then disable the account.
    stream = api.http.stream(
        "GET",
        "/v1/events",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
    )
    response = stream.__enter__()
    lines = response.iter_lines()
    assert next(lines).startswith(":")
    argv, env = server_cli(server, "disable", "alice")
    out = run_process(
        argv,
        capture_output=True,
        text=True,
        env=clean_env() | env,
        cwd=ROOT,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    argv, env = server_cli(server, "list")
    listed = run_process(
        argv,
        capture_output=True,
        text=True,
        env=clean_env() | env,
        cwd=ROOT,
        timeout=30,
    )
    assert "alice\tdisabled" in listed.stdout

    # New business requests fail at once for the account's agents and for login.
    for member_token in (token, agent.token):
        assert (
            api.call("GET", "/v1/members/me", member_token, error="unauthorized") == 401
        )
        assert api.call("GET", "/v1/inbox", member_token, error="unauthorized") == 401
    assert (
        api.call(
            "POST",
            f"/v1/rooms/{room}/messages",
            token,
            json={"client_message_id": "x", "text": "still here?"},
            error="unauthorized",
        )
        == 401
    )
    assert (
        api.call(
            "POST",
            "/v1/auth/login",
            json={"username": "alice", "password": PASSWORD},
            error="unauthorized",
        )
        == 401
    )
    assert (
        api.call(
            "POST",
            "/v1/members",
            human,
            json={
                "harness_instance_id": "h",
                "session_key": "late",
                "role_description": "z",
                "member_token": new_secret(),
            },
            error="unauthorized",
        )
        == 401
    )

    # The open stream ends by the next keepalive (at most 15 s).
    started = time.monotonic()
    for _ in lines:
        if time.monotonic() - started > 25:
            raise AssertionError(
                "SSE stream stayed open after the account was disabled"
            )
    assert time.monotonic() - started < 25
    stream.__exit__(None, None, None)

    # Through the Client the failure surfaces as a clear error, not a hang.
    async def after():
        async with dev.agent() as a:
            response = await a.call_tool("teamup_create_room", {"name": "after"})
            # The Client may say unauthorized or that the binding is no longer registered.
            assert response.isError
            assert response.content[0].text.split(":")[0] in (
                "unauthorized",
                "not_registered",
            )

    asyncio.run(asyncio.wait_for(after(), 30))
    api.close()
