"""Server, Client daemons and MCP sessions for one scenario.

Self-contained on purpose: the weekly suite must not share fixtures with the
nightly suite. Everything goes through public entry points: the server and
client command lines, the Client socket and the stdio MCP server.
"""

import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[3]
# Stands in for osascript: records each desktop notice instead of showing it.
# The orchestrator reads this file to know when to wake an agent.
RECORDER = (
    "import json, sys, time\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as log:\n"
    "    log.write(json.dumps([time.time(), *sys.argv[2:]]) + '\\n')\n"
)


class StackError(RuntimeError):
    pass


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def tcp_is_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


async def wait_until(predicate, what: str, timeout: float = 20, child=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            raise StackError(f"{what}: process exited with status {child.returncode}")
        if value := predicate():
            return value
        await asyncio.sleep(0.05)
    raise StackError(f"Timed out waiting for {what}")


class Daemon:
    """One long-running TeamUp process (server or client) with a log file."""

    def __init__(self, module: str, source: str, args, log: Path, env=None):
        self.module, self.source, self.args = module, source, [*map(str, args)]
        self.log, self.env = log, env or {}
        self.child: subprocess.Popen | None = None

    def start(self) -> subprocess.Popen:
        paths = [str(ROOT / self.source)]
        if existing := os.environ.get("PYTHONPATH"):
            paths.append(existing)
        env = os.environ | {"PYTHONPATH": os.pathsep.join(paths)} | self.env
        with self.log.open("ab") as output:
            self.child = subprocess.Popen(
                [sys.executable, "-m", self.module, *self.args],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
            )
        return self.child

    def stop(self) -> None:
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def tail(self, lines: int = 20) -> str:
        if not self.log.exists():
            return ""
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])


class Developer:
    """One agent's private Client: config, socket, binding key, notice recorder."""

    def __init__(self, root: Path, name: str, port: int):
        directory = root / name
        directory.mkdir(mode=0o700)
        self.name = name
        self.config_path = directory / "client.json"
        self.socket_path = directory / "client.sock"
        self.desktop = directory / "desktop.jsonl"
        self.enrollment_key = secrets.token_urlsafe(32)
        self.binding_key = secrets.token_urlsafe(32)
        self.member_token = secrets.token_urlsafe(32)
        self.config_path.touch(mode=0o600)
        self.config_path.write_text(
            json.dumps(
                {
                    "server_url": f"http://127.0.0.1:{port}",
                    "socket_path": str(self.socket_path),
                    "enrollment_key": self.enrollment_key,
                    "notify_command": [
                        sys.executable,
                        "-c",
                        RECORDER,
                        str(self.desktop),
                    ],
                    "bindings": [
                        {
                            "harness_instance_id": "weekly",
                            "session_key": name,
                            "binding_key": self.binding_key,
                            "member_token": self.member_token,
                        }
                    ],
                }
            )
        )
        self.client = Daemon(
            "teamup_client.main",
            "client/shared/src",
            ["serve", "--config", self.config_path],
            directory / "client.log",
        )

    @property
    def secrets(self) -> list[str]:
        return [self.enrollment_key, self.binding_key, self.member_token]

    async def start_client(self) -> None:
        child = self.client.start()
        await wait_until(
            self.socket_path.is_socket, f"{self.name} client socket", child=child
        )

    def stop_client(self) -> None:
        self.client.stop()
        self.socket_path.unlink(missing_ok=True)

    def notices(self) -> list[list]:
        """Desktop notices the Client has handed to the recorder, oldest first."""
        if not self.desktop.exists():
            return []
        lines = self.desktop.read_text().splitlines()
        return [json.loads(line) for line in lines if line.endswith("]")]

    def mcp_environment(self) -> dict[str, str]:
        return {
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT / "plugin/mcp/src"), str(ROOT / "client/shared/src")]
            ),
            "TEAMUP_SOCKET": str(self.socket_path),
            "TEAMUP_BINDING_KEY": self.binding_key,
        }

    @asynccontextmanager
    async def session(self):
        """A direct stdio MCP session, as a harness would open it."""
        launch = StdioServerParameters(
            command=sys.executable,
            args=["-m", "teamup_mcp.main"],
            env=self.mcp_environment(),
        )
        async with (
            stdio_client(launch) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            yield session


async def call(session, operation: str, **arguments):
    """Call one TeamUp MCP tool. Returns (is_error, structured result or text)."""
    response = await session.call_tool(f"teamup_{operation}", arguments)
    if response.isError:
        return True, response.content[0].text
    return False, response.structuredContent


class Stack:
    """A loopback Server plus one Developer per agent, in a private temp dir."""

    def __init__(self, names: list[str]):
        self.root = Path(
            tempfile.mkdtemp(
                prefix="tuw-", dir=os.environ.get("TEAMUP_SCENARIO_TMPDIR", "/tmp")
            )
        )
        self.root.chmod(0o700)
        self.port = available_port()
        self.developers = {
            name: Developer(self.root, name, self.port) for name in names
        }
        keys = {name: dev.enrollment_key for name, dev in self.developers.items()}
        self.server = Daemon(
            "teamup_server.main",
            "server/src",
            ["--db", self.root / "server.db", "--port", self.port],
            self.root / "server.log",
            {"TEAMUP_ENROLLMENT_KEYS": json.dumps(keys)},
        )

    @property
    def secrets(self) -> list[str]:
        return [secret for dev in self.developers.values() for secret in dev.secrets]

    async def start(self) -> None:
        child = self.server.start()
        await wait_until(lambda: tcp_is_ready(self.port), "server port", child=child)
        for dev in self.developers.values():
            await dev.start_client()

    def stop(self) -> None:
        for dev in self.developers.values():
            dev.stop_client()
        self.server.stop()
        shutil.rmtree(self.root, ignore_errors=True)
