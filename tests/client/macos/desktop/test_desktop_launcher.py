import hashlib
import json
import os
import runpy
import secrets
import signal
import socket
import subprocess
import sys
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlsplit

import pytest

LAUNCHER = Path(__file__).resolve().parents[4] / "client/macos/desktop/launch.py"


class LoginServer(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        credentials = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/auth/login"
        assert credentials == {"username": "alice", "password": "desktop test password"}
        token = secrets.token_urlsafe(32)
        payload = json.dumps({"access_token": token, "expires_at": 1900000000}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def login_server():
    with TemporaryDirectory(prefix="tu-ssh-", dir="/tmp") as temp:
        private = Path(temp) / "private"
        private.mkdir(mode=0o700)
        socket_path = private / "ssh.sock"
        with ThreadingUnixStreamServer(str(socket_path), LoginServer) as server:
            socket_path.chmod(0o600)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            try:
                yield "http://127.0.0.1:1", socket_path
            finally:
                server.shutdown()
                thread.join()


def test_desktop_launcher_reuses_pending_and_active_session(login_server, monkeypatch):
    server_url, server_socket = login_server
    scope = hashlib.sha256(f"{server_url}\0{server_socket}".encode()).hexdigest()
    # Keep the Client's Unix socket path short on macOS.
    with TemporaryDirectory(prefix="tu-desktop-", dir="/tmp") as temp:
        root = Path(temp)
        profile = root / "client.json"
        state = root / "state"
        client_socket = root / "client.sock"
        profile.write_text(
            json.dumps(
                {
                    "server_url": server_url,
                    "server_socket": str(server_socket),
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
            assert not client_socket.exists()
            assert launch() == address
            url, login_socket = address.splitlines()
            assert Path(login_socket).is_socket()
            assert Path(login_socket).stat().st_mode & 0o077 == 0
            url = urlsplit(url)
            assert url.hostname == "127.0.0.1"
            assert len(url.fragment) == 43
            endpoint = next(state.glob("*/console.url"))
            assert endpoint.stat().st_mode & 0o077 == 0
            launcher = runpy.run_path(str(LAUNCHER))
            ensure_console = launcher["ensure_console"]
            with monkeypatch.context() as failing_probe:
                failing_probe.setitem(
                    ensure_console.__globals__, "console_state", lambda _address: None
                )
                with pytest.raises(launcher["LaunchError"], match="still waiting for sign-in"):
                    ensure_console(profile, client, state)
            assert endpoint.read_text().strip() == address
            assert launch() == address
            connection = HTTPConnection(url.hostname, url.port, timeout=3)
            try:
                connection.request(
                    "GET",
                    "/api/login",
                    headers={
                        "Authorization": f"Bearer {url.fragment}",
                    },
                )
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read()) == {"state": "sign_in_required"}
            finally:
                connection.close()

            def private_login(credentials=None):
                host = f"127.0.0.1:{url.port}"
                method = "GET" if credentials is None else "POST"
                body = "" if credentials is None else json.dumps(credentials)
                with socket.socket(socket.AF_UNIX) as sign_in:
                    sign_in.settimeout(3)
                    sign_in.connect(login_socket)
                    sign_in.sendall((
                        f"{method} /api/login HTTP/1.1\r\nHost: {host}\r\n"
                        f"Origin: http://{host}\r\nAuthorization: Bearer {url.fragment}\r\n"
                        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                        f"Connection: close\r\n\r\n{body}"
                    ).encode())
                    with sign_in.makefile("rb") as result:
                        headers, document = result.read().split(b"\r\n\r\n", 1)
                return int(headers.split(b" ", 2)[1]), json.loads(document)

            assert private_login() == (200, {
                "state": "sign_in_required", "remember_scope": scope
            })
            credentials = {"username": "alice", "password": "desktop test password"}
            original = profile.read_bytes()
            assert private_login({**credentials, "remember_scope": "a" * 64}) == (
                409, {"state": "profile_unavailable"}
            )
            assert profile.read_bytes() == original
            assert not client_socket.exists()
            server_socket.chmod(0o766)
            assert private_login({**credentials, "remember_scope": scope}) == (
                503, {"state": "unavailable"}
            )
            server_socket.chmod(0o600)
            shared_parent = server_socket.parent.parent
            shared_parent.chmod(0o777)
            try:
                assert private_login({**credentials, "remember_scope": scope}) == (
                    503, {"state": "unavailable"}
                )
            finally:
                shared_parent.chmod(0o700)
            assert private_login({**credentials, "remember_scope": scope}) == (200, {
                "state": "ready", "authenticated_scope": scope
            })
            assert private_login(credentials) == (200, {"state": "ready"})
            assert client_socket.is_socket()
            assert launch() == address
        finally:
            pidfile = next(state.glob("*/client.pid"), None)
            if pidfile is not None:
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = monotonic() + 5
                while client_socket.exists() and monotonic() < deadline:
                    sleep(0.05)
