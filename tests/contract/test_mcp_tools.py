"""The `teamup_*` tool names and input schemas the MCP adapter publishes.

Behaviour of the tools (IPC, mention hints) stays in tests/test_mcp_contract.py.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from conftest import ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def list_tools(socket_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teamup_mcp.main"],
        env={
            "TEAMUP_SOCKET": str(socket_path),
            "TEAMUP_BINDING_KEY": "contract-binding-key",
            "PYTHONPATH": f"{ROOT / 'plugin/mcp/src'}:{ROOT / 'client/src'}",
        },
    )
    async with (
        stdio_client(params) as (reads, writes),
        ClientSession(reads, writes) as session,
    ):
        initialized = await session.initialize()
        tools = (await session.list_tools()).tools
        return initialized.instructions, tools


def test_tool_schemas_match_golden(golden):
    with tempfile.TemporaryDirectory(prefix="tu-mcp-", dir="/tmp") as directory:
        instructions, tools = asyncio.run(list_tools(Path(directory) / "teamup.sock"))
    assert all(tool.name.startswith("teamup_") for tool in tools)
    golden(
        "mcp_tools",
        {
            "instructions": instructions,
            "tools": {
                tool.name: {
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                }
                for tool in tools
            },
        },
    )
