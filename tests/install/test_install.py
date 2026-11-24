"""3 · Install: each package builds to a wheel and works from a fresh venv.

Only public surfaces are used: the console scripts and what the venv's own
interpreter imports.
"""

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from _stack import (
    ROOT,
    agent_tool_names,
    build_wheels,
    clean_env,
    create_venv,
    pip_install,
    run_process,
)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.fixture(scope="module")
def wheels():
    root = Path(tempfile.mkdtemp(prefix="tu-whl-", dir="/tmp"))
    try:
        yield build_wheels(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def venvs(wheels):
    root = Path(tempfile.mkdtemp(prefix="tu-inst-", dir="/tmp"))
    try:
        out = {}
        for name, packages in {
            "server": ["server"],
            "client": ["client"],
            "mcp": ["plugin/mcp", "client"],
        }.items():
            create_venv(root / name)
            pip_install(root / name, [wheels[p] for p in packages])
            out[name] = root / name / "bin"
        yield out
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(bin_dir, script, *args):
    return run_process(
        [str(bin_dir / script), *args],
        capture_output=True,
        text=True,
        env=clean_env(),
        cwd="/",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def imported_from(bin_dir, module):
    out = subprocess.run(
        [str(bin_dir / "python"), "-c", f"import {module}; print({module}.__file__)"],
        capture_output=True,
        text=True,
        env=clean_env(),
        cwd="/",
        timeout=60,
        check=True,
    )
    return Path(out.stdout.strip())


def test_wheels_are_built(wheels):
    assert {p.suffix for p in wheels.values()} == {".whl"}
    assert {p.name.split("-")[0] for p in wheels.values()} == {
        "teamup_server",
        "teamup_client",
        "teamup_mcp",
    }


@pytest.mark.parametrize(
    ("venv", "module"),
    [("server", "teamup_server"), ("client", "teamup_client"), ("mcp", "teamup_mcp")],
)
def test_imports_resolve_from_the_venv(venvs, venv, module):
    where = imported_from(venvs[venv], module)
    assert venvs[venv].parent in where.parents
    assert ROOT not in where.parents


def test_server_script(venvs):
    help_ = run(venvs["server"], "teamup-server", "--help")
    assert help_.returncode == 0 and "--db" in help_.stdout
    bare = run(venvs["server"], "teamup-server")
    assert bare.returncode == 2 and "Traceback" not in bare.stderr


def test_client_script(venvs):
    help_ = run(venvs["client"], "teamup-client", "--help")
    assert help_.returncode == 0 and "serve" in help_.stdout
    binding = run(venvs["client"], "teamup-client", "init-binding")
    assert binding.returncode == 0 and "binding_key" in binding.stdout


def test_mcp_script(venvs):
    bare = run(venvs["mcp"], "teamup-mcp")
    assert bare.returncode != 0 and "TEAMUP_SOCKET" in bare.stderr

    async def tools():
        params = StdioServerParameters(
            command=str(venvs["mcp"] / "teamup-mcp"),
            env=clean_env()
            | {"TEAMUP_SOCKET": "/tmp/tu-no-such.sock", "TEAMUP_BINDING_KEY": "x" * 43},
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            return await agent_tool_names(session)

    names = asyncio.run(asyncio.wait_for(tools(), 60))
    assert {"teamup_register", "teamup_send_message", "teamup_get_board"} <= names
