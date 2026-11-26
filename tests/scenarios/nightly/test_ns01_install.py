import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from _stack import (
    CLIENT_PACKAGE,
    ROOT,
    agent_tool_names,
    clean_env,
    install_venv,
    run_process,
)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def run(bin_dir, script, *args, **kwargs):
    return run_process(
        [str(Path(bin_dir) / script), *args],
        capture_output=True,
        text=True,
        env=clean_env(),
        cwd="/",
        timeout=60,
        **kwargs,
    )


def imported_from(bin_dir, module):
    """Where the installed interpreter imports ``module`` from."""
    out = subprocess.run(
        [
            str(Path(bin_dir) / "python"),
            "-c",
            f"import {module}; print({module}.__file__)",
        ],
        capture_output=True,
        text=True,
        env=clean_env(),
        cwd="/",
        timeout=60,
        check=True,
    )
    return Path(out.stdout.strip())


async def mcp_tools(bin_dir):
    params = StdioServerParameters(
        command=str(Path(bin_dir) / "teamup-mcp"),
        env=clean_env()
        | {"TEAMUP_SOCKET": "/tmp/tu-no-such.sock", "TEAMUP_BINDING_KEY": "x" * 43},
    )
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        return await agent_tool_names(session)


@pytest.mark.scenario(
    "NS-01", "Each package installs into an empty venv and its console script runs"
)
def test_install_each_package(workspace_venv):
    root = Path(tempfile.mkdtemp(prefix="tu-pkg-", dir="/tmp"))
    try:
        server = install_venv(root / "server", ["server"])
        client = install_venv(root / "client", [CLIENT_PACKAGE])
        mcp = install_venv(root / "mcp", ["plugin/mcp", CLIENT_PACKAGE])

        # Packages must come from the venv, not from the checkout.
        for bin_dir, module in (
            (server, "teamup_server"),
            (client, "teamup_client"),
            (mcp, "teamup_mcp"),
            (workspace_venv, "teamup_server"),
        ):
            assert ROOT not in imported_from(bin_dir, module).parents, module
            assert bin_dir.parent in imported_from(bin_dir, module).parents

        for bin_dir in (server, workspace_venv):
            help_ = run(bin_dir, "teamup-server", "--help")
            assert help_.returncode == 0 and "--db" in help_.stdout
            accounts = run(bin_dir, "teamup-server", "accounts", "--help")
            assert accounts.returncode == 0 and "create" in accounts.stdout
            # --db is required: a clean usage error, not a traceback.
            bare = run(bin_dir, "teamup-server")
            assert bare.returncode == 2 and "Traceback" not in bare.stderr

        for bin_dir in (client, workspace_venv):
            help_ = run(bin_dir, "teamup-client", "--help")
            assert help_.returncode == 0 and "serve" in help_.stdout
            binding = run(bin_dir, "teamup-client", "init-binding")
            assert binding.returncode == 0 and "binding_key" in binding.stdout

        # teamup-mcp refuses to start without its approved-session environment.
        bare = run_process(
            [str(mcp / "teamup-mcp")],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        assert bare.returncode != 0 and "TEAMUP_SOCKET" in bare.stderr

        # With the environment it speaks MCP over stdio and lists the TeamUp tools.
        for bin_dir in (mcp, workspace_venv):
            names = asyncio.run(asyncio.wait_for(mcp_tools(bin_dir), 60))
            assert {
                "teamup_register",
                "teamup_send_message",
                "teamup_get_board",
            } <= names
    finally:
        shutil.rmtree(root, ignore_errors=True)
