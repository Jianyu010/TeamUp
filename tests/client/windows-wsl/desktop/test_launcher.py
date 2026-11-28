import json
import os
import runpy
import secrets
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlsplit

import pytest

LAUNCHER = Path(__file__).resolve().parents[4] / "client/windows-wsl/desktop/launch.py"


def test_desktop_launcher_reuses_client_through_private_socketpair():
    # Keep the WSL Client's Unix socket path short.
    with TemporaryDirectory(prefix="tu-win-") as temp:
        root = Path(temp)
        profile = root / "client.json"
        state = root / "state"
        client_socket = root / "client.sock"
        profile.write_text(
            json.dumps(
                {
                    "server_url": "http://127.0.0.1:1",
                    "enrollment_key": secrets.token_urlsafe(32),
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
                logs = "\n".join(
                    path.read_text() for path in state.glob("*/client.log")
                )
                assert result.returncode == 0, result.stderr + logs
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
                    "directory_available": False,
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


class LoginServer(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        assert urlsplit(self.path).path == "/v1/auth/capabilities"
        self.reply(200, {"admin": False})

    def do_POST(self):
        credentials = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/auth/login"
        valid = credentials == {
            "username": "alice",
            "password": "desktop test password",
        }
        data = (
            {"access_token": secrets.token_urlsafe(32), "expires_at": 1900000000}
            if valid
            else {"error": {"code": "unauthorized"}}
        )
        self.reply(200 if valid else 401, data)

    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_launcher_reuses_pending_sign_in_and_activates_the_same_client(monkeypatch):
    with (
        TemporaryDirectory(prefix="tu-win-login-") as temp,
        ThreadingHTTPServer(("127.0.0.1", 0), LoginServer) as server,
    ):
        root = Path(temp)
        profile = root / "client.json"
        client_socket = root / "client.sock"
        state = root / "state"
        profile.write_text(
            json.dumps(
                {
                    "server_url": f"http://127.0.0.1:{server.server_port}",
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
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()

        def launch():
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
            assert not client_socket.exists()
            pidfile = next(state.glob("*/client.pid"))
            pid = pidfile.read_text()
            assert launch() == address
            assert pidfile.read_text() == pid
            endpoint = urlsplit(address)

            def request(path, credentials=None):
                connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
                try:
                    connection.request(
                        "GET" if credentials is None else "POST",
                        path,
                        body=None if credentials is None else json.dumps(credentials),
                        headers={
                            "Authorization": f"Bearer {endpoint.fragment}",
                            "Origin": f"http://127.0.0.1:{endpoint.port}",
                            "Content-Type": "application/json",
                        },
                    )
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            assert request("/api/login") == (200, {"state": "sign_in_required"})
            assert request("/api/workspace") == (409, {"state": "sign_in_required"})
            original = profile.read_bytes()
            assert request(
                "/api/login", {"username": "alice", "password": "wrong"}
            ) == (401, {"state": "wrong_credentials"})
            assert profile.read_bytes() == original
            assert not client_socket.exists()

            launcher = runpy.run_path(str(LAUNCHER))
            ensure_console = launcher["ensure_console"]
            with monkeypatch.context() as failing_probe:
                failing_probe.setitem(
                    ensure_console.__globals__, "console_state", lambda _: None
                )
                with pytest.raises(
                    launcher["LaunchError"], match="still waiting for sign-in"
                ):
                    ensure_console(profile, client, state)
            assert pidfile.read_text() == pid
            assert launch() == address

            assert request(
                "/api/login", {"username": "alice", "password": "desktop test password"}
            ) == (200, {"state": "ready"})
            assert client_socket.is_socket()
            assert request("/api/workspace")[0] == 200
            assert launch() == address
            assert pidfile.read_text() == pid
        finally:
            pidfile = next(state.glob("*/client.pid"), None)
            if pidfile is not None:
                with suppress(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)
                deadline = monotonic() + 5
                while client_socket.exists() and monotonic() < deadline:
                    sleep(0.05)
            server.shutdown()
            thread.join()
