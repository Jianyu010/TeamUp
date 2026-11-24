import asyncio
import errno
import json
import os
import pty
import re
import secrets
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from test_loop import (
    ROOT,
    Developer,
    available_port,
    process,
    tcp_is_ready,
    tool,
    wait_for,
)


def password_command(args, password, prompts=1, env=None):
    master, slave = pty.openpty()
    child = subprocess.Popen(
        args,
        cwd=ROOT,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=os.environ | (env or {}),
        start_new_session=True,
    )
    os.close(slave)
    buffer = b""
    output = b""
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                if child.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output += chunk
            buffer += chunk
            if prompts and re.search(rb"(?i)password[^\r\n]{0,60}[:?] *$", buffer):
                os.write(master, password.encode() + b"\n")
                buffer = b""
                prompts -= 1
        status = child.wait(timeout=2)
        assert password.encode() not in output, "Password appeared in CLI output"
        assert status == 0, output.decode(errors="replace")
        assert prompts == 0, output.decode(errors="replace")
        return output.decode(errors="replace")
    finally:
        os.close(master)
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def login(developer, username, password):
    return password_command(
        [
            sys.executable,
            "-m",
            "teamup_client.main",
            "login",
            "--config",
            str(developer.config_path),
            "--username",
            username,
        ],
        password,
        env={"PYTHONPATH": str(ROOT / "client/src")},
    )


async def account_exchange(root, port, admin, prefix="test"):
    a = Developer(root, "alice", port)
    b = Developer(root, "bob", port)
    usernames = [f"{prefix}-alice", f"{prefix}-bob"]
    for developer, username in zip((a, b), usernames, strict=True):
        config = json.loads(developer.config_path.read_text())
        config.pop("enrollment_key")
        developer.config_path.write_text(json.dumps(config))
        password = secrets.token_urlsafe(24)
        admin("create", username, password)
        login(developer, username, password)
        assert "account_token" in json.loads(developer.config_path.read_text())

    with a.daemon(), b.daemon() as b_daemon:
        async with a.agent() as agent_a, b.agent() as agent_b:
            alice = await tool(
                agent_a, "register", role_description="Account smoke backend"
            )
            bob = await tool(
                agent_b, "register", role_description="Account smoke frontend"
            )
            assert alice["member_id"] != bob["member_id"]
            room = await tool(agent_a, "create_room", name="Account login smoke")
            room_id = room["room_id"]
            await tool(agent_b, "join_room", room_id=room_id)
            for agent in (agent_a, agent_b):
                board = await tool(agent, "get_board", room_id=room_id)
                await tool(
                    agent, "ack_board", room_id=room_id, version=board["version"]
                )
            ping = await tool(
                agent_a,
                "send_message",
                room_id=room_id,
                client_message_id="account-smoke-ping",
                text="Can you review this test response?",
                mention_member_id=bob["member_id"],
            )
            notice = await asyncio.to_thread(
                b.wait_for_notice, ping["message_id"], b_daemon
            )
            assert notice["sender_member_id"] == alice["member_id"]
            await tool(
                agent_b,
                "send_message",
                room_id=room_id,
                client_message_id="account-smoke-reply",
                text="Reviewed the test response.",
            )
            messages = (await tool(agent_a, "read_messages", room_id=room_id))[
                "messages"
            ]
            assert [message["text"] for message in messages] == [
                "Can you review this test response?",
                "Reviewed the test response.",
            ]
            admin("disable", usernames[0])
            config = json.loads(a.config_path.read_text())
            bind = config["bindings"][0]
            async with httpx.AsyncClient(
                base_url=config["server_url"], trust_env=False
            ) as api:
                member = {"Authorization": f"Bearer {bind['member_token']}"}
                assert (await api.get("/v1/inbox", headers=member)).status_code == 401
                assert (
                    await api.post(
                        f"/v1/rooms/{room_id}/messages",
                        headers=member,
                        json={"client_message_id": "blocked", "text": "Denied"},
                    )
                ).status_code == 401
                human = {"Authorization": f"Bearer {config['account_token']}"}
                assert (
                    await api.post(
                        "/v1/members",
                        headers=human,
                        json={
                            "harness_instance_id": bind["harness_instance_id"],
                            "session_key": bind["session_key"],
                            "member_token": bind["member_token"],
                            "role_description": "Denied",
                        },
                    )
                ).status_code == 401
            assert (await tool(agent_b, "read_messages", room_id=room_id))[
                "messages"
            ] == messages
    return usernames


def test_two_login_accounts_exchange_and_disable_over_public_entry_points():
    with tempfile.TemporaryDirectory(prefix="tu-acct-", dir="/tmp") as directory:
        root = Path(directory)
        port = available_port()
        database = root / "server.db"

        def admin(operation, username, password=None):
            args = [
                sys.executable,
                "-m",
                "teamup_server.main",
                "accounts",
                "--db",
                str(database),
                operation,
                username,
            ]
            env = {"PYTHONPATH": str(ROOT / "server/src")}
            if password:
                password_command(args, password, prompts=2, env=env)
            else:
                subprocess.run(
                    args,
                    cwd=ROOT,
                    env=os.environ | env,
                    check=True,
                    capture_output=True,
                    timeout=10,
                )

        with process(
            "teamup_server.main",
            "server/src",
            ["--db", database, "--port", port],
            root / "server.log",
        ) as server:
            wait_for(lambda: tcp_is_ready(port), server)
            asyncio.run(account_exchange(root, port, admin))
