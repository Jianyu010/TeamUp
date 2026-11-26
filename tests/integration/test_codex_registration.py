"""Codex hooks enroll the same member used by subsequent MCP calls."""

import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from test_codex_wake import call
from test_loop import ROOT, available_port, process, tcp_is_ready, wait_for
from test_wake import InstallDeveloper


async def hook(
    developer,
    thread,
    event="SessionStart",
    *,
    command="codex-hook",
    install="codex-local",
):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "teamup_client.main",
        command,
        "--config",
        str(developer.config_path),
        "--install",
        install,
        env=os.environ | {"PYTHONPATH": str(ROOT / "client/shared/src")},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        child.communicate(
            json.dumps(
                {
                    "hook_event_name": event,
                    "session_id": thread,
                    "prompt": "private prompt sentinel",
                    "cwd": "/private/workspace/sentinel",
                    "transcript_path": "/private/transcript/sentinel",
                }
            ).encode()
        ),
        timeout=10,
    )
    assert child.returncode == 0, stderr.decode()
    assert stdout == stderr == b""


def configure(developer, **extra):
    config = json.loads(developer.config_path.read_text())
    config["installs"][0]["harness_instance_id"] = "codex-local"
    config.update(extra)
    developer.config_path.write_text(json.dumps(config))


def server(root, port, developer):
    return process(
        "teamup_server.main",
        "server/src",
        ["--db", root / "server.db", "--port", port],
        root / "server.log",
        {"TEAMUP_ENROLLMENT_KEYS": json.dumps({"operator": developer.enrollment_key})},
    )


async def check_auto_registration(root, port):
    developer = InstallDeveloper(root, "operator", port, auto_approve=True)
    legacy_thread, new_thread = str(uuid4()), str(uuid4())
    legacy_key = secrets.token_urlsafe(32)
    configure(
        developer,
        bindings=[
            {
                "harness_instance_id": "old-codex-binding",
                "session_key": legacy_thread.upper(),
                "binding_key": legacy_key,
                "member_token": secrets.token_urlsafe(32),
                "codex_install": "codex-local",
            }
        ],
    )

    # A lifecycle event while the Client is down has no effect on the turn.
    await hook(developer, new_thread)
    with server(root, port, developer) as upstream:
        wait_for(lambda: tcp_is_ready(port), upstream)
        with developer.daemon():
            async with developer.agent({"TEAMUP_BINDING_KEY": legacy_key}) as legacy:
                original = await call(
                    legacy,
                    legacy_thread,
                    "register",
                    name="Original Codex",
                    description="Maintains the existing integration.",
                )
                room = await call(
                    legacy, legacy_thread, "create_room", name="Existing room"
                )

            await asyncio.gather(
                hook(developer, new_thread, "SessionStart"),
                hook(developer, new_thread, "UserPromptSubmit"),
                hook(developer, legacy_thread),
                hook(developer, str(uuid4()), "SubagentStart"),
                hook(developer, "not-a-thread", "SessionStart"),
            )
            report = developer.sessions()
            assert set(report) == {new_thread, legacy_thread.upper()}
            legacy_session = report[legacy_thread.upper()]
            assert legacy_session["harness_instance_id"] == "old-codex-binding"
            assert legacy_session["attached"] == "codex"

            async with developer.agent({"TEAMUP_BINDING_KEY": legacy_key}) as legacy:
                # A different session sees the new member before the new thread calls MCP.
                roster = await call(
                    legacy, legacy_thread, "find_members", q=f"Codex {new_thread[-8:]}"
                )
                (automatic,) = roster["members"]
                assert automatic["name"] == f"Codex {new_thread[-8:]}"
                assert "sentinel" not in json.dumps(automatic)

        with developer.daemon():
            await hook(developer, new_thread, "UserPromptSubmit")
            async with developer.agent(
                {"TEAMUP_BINDING_KEY": developer.install_key}
            ) as mcp:
                args = {"name": "Custom name", "description": "Different profile."}
                assert (await call(mcp, new_thread, "register", **args)) == automatic
                assert (await call(mcp, legacy_thread, "register", **args)) == original
                rooms = await call(mcp, legacy_thread, "list_rooms")
                assert [item["room_id"] for item in rooms["rooms"]] == [room["room_id"]]


async def check_owner_approval(root, port):
    developer = InstallDeveloper(root, "operator", port)
    configure(developer)
    declined, approved = str(uuid4()), str(uuid4())

    with server(root, port, developer) as upstream, developer.daemon():
        wait_for(lambda: tcp_is_ready(port), upstream)
        for thread in (declined, approved):
            await hook(developer, thread)
            assert developer.sessions()[thread]["state"] == "pending"
        for action, thread in (("reject", declined), ("approve", approved)):
            developer.owner(
                "sessions",
                "--config",
                str(developer.config_path),
                action,
                "codex-local",
                thread,
            )
            await hook(developer, thread, "UserPromptSubmit")

        async with developer.agent(
            {"TEAMUP_BINDING_KEY": developer.install_key}
        ) as mcp:
            await call(mcp, declined, "list_rooms", "unauthorized")
            assert await call(mcp, approved, "list_rooms") == {"rooms": []}
            roster = await call(mcp, approved, "find_members")
            assert [member["name"] for member in roster["members"]] == [
                f"Codex {approved[-8:]}"
            ]


def test_codex_hook_registers_persistently_and_preserves_existing_identity():
    with tempfile.TemporaryDirectory(prefix="tu-reg-", dir="/tmp") as root:
        asyncio.run(check_auto_registration(Path(root), available_port()))


def test_codex_hook_respects_owner_approval_and_rejection():
    with tempfile.TemporaryDirectory(prefix="tu-reg-", dir="/tmp") as root:
        asyncio.run(check_owner_approval(Path(root), available_port()))
