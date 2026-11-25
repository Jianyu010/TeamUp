import json
import os
import signal
import socket
import subprocess
import sys
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urlsplit

LAUNCHER = Path(__file__).resolve().parents[1] / "desktop" / "launch.py"


def test_desktop_launcher_reuses_client_through_private_socketpair():
    # Keep the Client's Unix socket path short on macOS.
    with TemporaryDirectory(prefix="tu-desktop-", dir="/tmp") as temp:
        root = Path(temp)
        profile = root / "client.json"
        state = root / "state"
        client_socket = root / "client.sock"
        profile.write_text(
            json.dumps(
                {
                    "server_url": "http://127.0.0.1:1",
                    "socket_path": str(client_socket),
                    "bindings": [],
                }
            )
        )
        profile.chmod(0o600)
        client = Path(sys.executable).with_name("teamup-client")
        command = [
            sys.executable,
            "-I",
            str(LAUNCHER),
            "--client",
            str(client),
            "--config",
            str(profile),
            "--state-dir",
            str(state),
        ]

        def launch():
            # Node uses a socketpair for execFile stdout on Unix.
            reader, writer = socket.socketpair()
            with reader, writer:
                result = subprocess.run(
                    command,
                    stdout=writer,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
                return reader.recv(256).decode().strip()

        try:
            address = launch()
            assert launch() == address
            url = urlsplit(address)
            assert url.hostname == "127.0.0.1"
            assert len(url.fragment) == 43
            endpoint = next(state.glob("*/console.url"))
            assert endpoint.stat().st_mode & 0o077 == 0
            connection = HTTPConnection(url.hostname, url.port, timeout=3)
            try:
                connection.request(
                    "GET",
                    "/api/workspace",
                    headers={
                        "Authorization": f"Bearer {url.fragment}",
                    },
                )
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read()) == {
                    "sessions": [],
                    "rooms": [],
                    "retired_sessions": [],
                }
            finally:
                connection.close()
        finally:
            pidfile = next(state.glob("*/client.pid"), None)
            if pidfile is not None and client_socket.exists():
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
                deadline = monotonic() + 5
                while client_socket.exists() and monotonic() < deadline:
                    sleep(0.05)
