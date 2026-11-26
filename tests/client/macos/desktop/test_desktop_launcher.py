import json
import os
import secrets
import signal
import socket
import subprocess
import sys
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlsplit

import pytest

LAUNCHER = Path(__file__).resolve().parents[4] / "client/macos/desktop/launch.py"


class LoginServer(BaseHTTPRequestHandler):
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
    with ThreadingHTTPServer(("127.0.0.1", 0), LoginServer) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def test_desktop_launcher_reuses_pending_and_active_session(login_server):
    # Keep the Client's Unix socket path short on macOS.
    with TemporaryDirectory(prefix="tu-desktop-", dir="/tmp") as temp:
        root = Path(temp)
        profile = root / "client.json"
        state = root / "state"
        client_socket = root / "client.sock"
        profile.write_text(
            json.dumps(
                {
                    "server_url": login_server,
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

            with socket.socket(socket.AF_UNIX) as sign_in:
                sign_in.settimeout(3)
                sign_in.connect(login_socket)
                body = json.dumps({"username": "alice", "password": "desktop test password"})
                host = f"127.0.0.1:{url.port}"
                sign_in.sendall((
                    f"POST /api/login HTTP/1.1\r\nHost: {host}\r\nOrigin: http://{host}\r\n"
                    f"Authorization: Bearer {url.fragment}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
                ).encode())
                with sign_in.makefile("rb") as result:
                    headers, document = result.read().split(b"\r\n\r\n", 1)
                assert headers.startswith(b"HTTP/1.0 200 ")
                assert json.loads(document) == {"state": "ready"}
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
