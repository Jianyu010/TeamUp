"""Claude Code lifecycle and MCP calls share an installation member."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from test_codex_registration import hook, server
from test_loop import available_port, tcp_is_ready, tool, wait_for
from test_wake import InstallDeveloper


async def register(developer, session_id, event="SessionStart"):
    await hook(
        developer, session_id, event, command="claude-hook", install="claude-code@mac"
    )


async def check_registration(root, port, auto_approve):
    dev = InstallDeveloper(root, "operator", port, auto_approve=auto_approve)
    existing_id = str(uuid4())
    new_id = "claude-integration-session"
    label = f"Claude {hashlib.sha256(new_id.encode()).hexdigest()[:8]}"
    profile = {"name": "Existing Claude", "description": "Owns the original profile."}

    def decide(action, session_id):
        dev.owner(
            "sessions",
            "--config",
            str(dev.config_path),
            action,
            "claude-code@mac",
            session_id,
        )

    def session(session_id):
        return dev.session({"CLAUDE_CODE_SESSION_ID": session_id, "CLAUDECODE": "1"})

    await register(dev, new_id)
    with server(root, port, dev) as upstream:
        wait_for(lambda: tcp_is_ready(port), upstream)
        with dev.daemon():
            async with session(existing_id) as mcp:
                if not auto_approve:
                    await tool(mcp, "register", "session_pending", **profile)
                    decide("approve", existing_id)
                existing = await tool(mcp, "register", **profile)
                await asyncio.gather(
                    register(dev, new_id),
                    register(dev, new_id, "UserPromptSubmit"),
                    register(dev, existing_id),
                    register(dev, str(uuid4()), "SubagentStart"),
                )
                report = dev.sessions()
                assert set(report) == {existing_id, new_id}
                assert report[new_id]["attached"] is None
                if not auto_approve:
                    assert report[new_id]["state"] == "pending"
                    assert (await tool(mcp, "find_members", q=label))["members"] == []
                    declined_id = str(uuid4())
                    await register(dev, declined_id)
                    decide("reject", declined_id)
                    await register(dev, declined_id, "UserPromptSubmit")
                    async with session(declined_id) as declined:
                        await tool(declined, "list_rooms", "unauthorized")
                    decide("approve", new_id)
                    await register(dev, new_id, "UserPromptSubmit")
                (automatic,) = (await tool(mcp, "find_members", q=label))["members"]
                assert automatic["name"] == label
                assert "sentinel" not in json.dumps(automatic)
                assert await tool(mcp, "register", **profile) == existing

        with dev.daemon():
            await register(dev, new_id, "UserPromptSubmit")
            async with session(new_id) as mcp:
                assert await tool(mcp, "list_rooms") == {"rooms": []}
                assert await tool(mcp, "register", **profile) == automatic


@pytest.mark.parametrize("auto_approve", [True, False])
def test_claude_hook_enrollment_reuses_mcp_identity(auto_approve):
    with tempfile.TemporaryDirectory(prefix="tu-claude-", dir="/tmp") as root:
        asyncio.run(check_registration(Path(root), available_port(), auto_approve))
