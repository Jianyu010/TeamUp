"""Shared fixtures for the nightly scenarios and the performance benchmarks.

Everything here talks to TeamUp only through its published entry points: the
`teamup-server` and `teamup-client` command lines, the Client IPC socket, the
`teamup-mcp` stdio tools and the Server's HTTP API. Nothing imports a private
module or opens the Server database.
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
CLIENT_PACKAGE = "client/shared"
SOURCES = {
    "server": ("teamup_server.main", "server/src"),
    "client": ("teamup_client.main", f"{CLIENT_PACKAGE}/src"),
    "mcp": ("teamup_mcp.main", "plugin/mcp/src"),
}
SCRIPTS = {"server": "teamup-server", "client": "teamup-client", "mcp": "teamup-mcp"}
# Stands in for osascript: records each desktop notice instead of showing it.
RECORDER = (
    "import json, sys, time\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as log:\n"
    "    log.write(json.dumps([time.time(), *sys.argv[2:]]) + '\\n')\n"
)


# Generic helpers -------------------------------------------------------------


def wait_for(predicate, proc=None, timeout=20, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and not proc.alive():
            raise AssertionError(f"{proc.name} exited with status {proc.returncode()}")
        if value := predicate():
            return value
        time.sleep(0.03)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what}")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def tcp_ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def run_process(argv, **kwargs):
    """Run a command to completion; callers inspect ``returncode`` themselves."""
    return subprocess.run(argv, check=False, **kwargs)


def new_secret():
    """A 43-character base64url secret, the shape of every TeamUp credential."""
    return secrets.token_urlsafe(32)


def command(tool, bin_dir=None):
    """The argv prefix and extra env for a TeamUp tool, from source or installed."""
    if bin_dir is not None:
        return [str(Path(bin_dir) / SCRIPTS[tool])], {}
    module, source = SOURCES[tool]
    paths = [ROOT / source]
    if tool == "mcp":
        paths.append(ROOT / SOURCES["client"][1])
    return [sys.executable, "-m", module], {
        "PYTHONPATH": os.pathsep.join(map(str, paths))
    }


def clean_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


# Installation ----------------------------------------------------------------


def uv():
    found = shutil.which("uv") or os.environ.get("UV")
    assert found, "uv is required for the installation scenario"
    return found


def create_venv(venv):
    """Create an empty venv with the running interpreter; return its python."""
    venv = Path(venv)
    created = run_process(
        [uv(), "venv", "--python", sys.executable, str(venv)],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=120,
    )
    assert created.returncode == 0, created.stderr[-1500:]
    return venv / "bin" / "python"


def pip_install(venv, requirements, *options):
    """``uv pip install`` into ``venv``; requirements are paths or wheel files."""
    installed = run_process(
        [uv(), "pip", "install", "--python", str(Path(venv) / "bin" / "python")]
        + [*options, *map(str, requirements)],
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=600,
        cwd=ROOT,
    )
    assert installed.returncode == 0, installed.stderr[-1500:]


def build_wheels(out_dir, packages=("server", CLIENT_PACKAGE, "plugin/mcp")):
    """Build one wheel per package directory; return {package: wheel path}."""
    out_dir = Path(out_dir)
    wheels = {}
    for package in packages:
        target = out_dir / package.replace("/", "-")
        built = run_process(
            [uv(), "build", "--wheel", "--out-dir", str(target), str(ROOT / package)],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=300,
            cwd=ROOT,
        )
        assert built.returncode == 0, built.stderr[-1500:]
        [wheels[package]] = target.glob("*.whl")
    return wheels


def install_venv(venv, packages):
    """Create an empty venv with the running interpreter and install local paths.

    ``UV_DEFAULT_INDEX`` / ``UV_INDEX_URL`` are inherited from the environment.
    Returns the venv's bin directory.
    """
    create_venv(venv)
    pip_install(venv, [ROOT / package for package in packages])
    return Path(venv) / "bin"


# Processes and workspace -----------------------------------------------------


class Proc:
    def __init__(self, name, argv, log, env=None, cwd=ROOT):
        self.name = name
        self.log = Path(log)
        self._output = self.log.open("ab")
        self.child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=clean_env() | (env or {}),
            stdout=self._output,
            stderr=self._output,
        )

    def alive(self):
        return self.child.poll() is None

    def returncode(self):
        return self.child.poll()

    def stop(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
        self._output.close()

    def text(self):
        return self.log.read_text(errors="replace") if self.log.exists() else ""


class Workspace:
    """A short /tmp directory plus every process started inside it."""

    def __init__(self, prefix="tu-"):
        # Unix socket paths are length-limited, so stay under /tmp.
        base = "/tmp" if Path("/tmp").is_dir() else None
        self.root = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        self.procs = []
        self._count = 0

    def path(self, name):
        return self.root / name

    def spawn(self, name, argv, env=None):
        self._count += 1
        proc = Proc(name, argv, self.root / f"{self._count}-{name}.log", env)
        self.procs.append(proc)
        return proc

    def logs(self, limit=3000):
        parts = []
        for proc in self.procs:
            parts.append(f"--- {proc.name} ---\n{proc.text()[-limit:]}")
        return "\n".join(parts)

    def close(self):
        for proc in reversed(self.procs):
            with suppress(Exception):
                proc.stop()
        shutil.rmtree(self.root, ignore_errors=True)


# Server ----------------------------------------------------------------------


def make_certificate(directory):
    """A self-signed loopback certificate, or None when openssl is unavailable."""
    if shutil.which("openssl") is None:
        return None
    cert, key = Path(directory) / "server.pem", Path(directory) / "server.key"
    result = run_process(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0 or not cert.exists():
        return None
    key.chmod(0o600)
    return cert, key


class Server:
    def __init__(self, ws, keys, *, tls=None, bin_dir=None, db=None, port=None):
        self.ws = ws
        self.keys = keys
        self.tls = tls
        self.bin_dir = bin_dir
        self.db = Path(db) if db else ws.path("server.db")
        self.port = port or free_port()
        self.proc = None

    @property
    def url(self):
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    @property
    def verify(self):
        return ssl.create_default_context(cafile=self.tls[0]) if self.tls else True

    def start(self):
        argv, env = command("server", self.bin_dir)
        argv += ["--db", str(self.db), "--port", str(self.port)]
        if self.tls:
            argv += ["--tls-cert", str(self.tls[0]), "--tls-key", str(self.tls[1])]
        env["TEAMUP_ENROLLMENT_KEYS"] = json.dumps(self.keys)
        self.proc = self.ws.spawn("server", argv, env)
        wait_for(lambda: tcp_ready(self.port), self.proc, what="server port")
        return self

    def stop(self):
        if self.proc:
            self.proc.stop()
            self.proc = None

    def restart(self):
        self.stop()
        return self.start()

    def api(self):
        return Api(self.url, self.verify)


# HTTP API --------------------------------------------------------------------


class ApiError(AssertionError):
    def __init__(self, status, code, message):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code = status, code


@dataclass
class Member:
    member_id: str
    token: str
    role: str


class Api:
    """The Server's public HTTP API, with documented error shapes enforced."""

    def __init__(self, base, verify=True, **client_options):
        self.base = base
        self.http = httpx.Client(
            base_url=base, verify=verify, timeout=30, **client_options
        )

    def close(self):
        self.http.close()

    def raw(self, method, path, token=None, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.http.request(method, path, headers=headers, **kwargs)

    def call(self, method, path, token=None, error=None, **kwargs):
        """Return the JSON body; with ``error``, assert that code and return its status."""
        response = self.raw(method, path, token, **kwargs)
        body = response.json()
        if error:
            assert body.get("error", {}).get("code") == error, (
                response.status_code,
                body,
            )
            assert set(body["error"]) == {"code", "message"}, body
            return response.status_code
        if response.status_code != 200:
            err = body.get("error", {})
            raise ApiError(response.status_code, err.get("code"), err.get("message"))
        return body

    def enroll(self, key, role, *, harness="h", session="s", token=None):
        token = token or new_secret()
        body = self.call(
            "POST",
            "/v1/members",
            key,
            json={
                "harness_instance_id": harness,
                "session_key": session,
                "name": role,
                "description": f"Handles the {role} work in this scenario.",
                "member_token": token,
            },
        )
        return Member(body["member_id"], token, body["role_description"])

    def create_room(self, member, name="room", **board):
        return self.call(
            "POST", "/v1/rooms", member.token, json={"name": name, **board}
        )["room_id"]

    def join(self, member, room_id):
        return self.call("POST", f"/v1/rooms/{room_id}/join", member.token)

    def read_and_ack_board(self, member, room_id):
        board = self.call("GET", f"/v1/rooms/{room_id}/board", member.token)
        self.call(
            "POST",
            f"/v1/rooms/{room_id}/board/ack",
            member.token,
            json={"version": board["version"]},
        )
        return board

    def send(self, member, room_id, text, cid=None, mention=None):
        body = {"client_message_id": cid or new_secret()[:16], "text": text}
        if mention:
            body["mention_member_id"] = mention
        return self.call(
            "POST", f"/v1/rooms/{room_id}/messages", member.token, json=body
        )

    def history(self, member, room_id, after=0, limit=100):
        return self.call(
            "GET",
            f"/v1/rooms/{room_id}/messages",
            member.token,
            params={"after": after, "limit": limit},
        )["messages"]


# Terminal prompts ------------------------------------------------------------


def run_with_tty(argv, answers, env=None, timeout=60):
    """Run a command whose passwords are read from a terminal, answering prompts.

    ``answers`` is a list of strings sent one per "assword" prompt. Returns
    (exit_status, output). The password never appears in argv or the environment.
    """
    import pty
    import select

    pid, fd = pty.fork()
    if pid == 0:  # child: the pty is its controlling terminal
        os.execvpe(argv[0], argv, clean_env() | (env or {}))
    output, pending = b"", list(answers)
    deadline = time.monotonic() + timeout
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                output += chunk
                if pending and chunk.rstrip().endswith(b":") and b"assword" in chunk:
                    os.write(fd, pending.pop(0).encode() + b"\n")
        _, raw = os.waitpid(pid, 0)
        status = os.waitstatus_to_exitcode(raw)
    finally:
        os.close(fd)
    return status, output.decode(errors="replace")


# Client daemon and MCP -------------------------------------------------------


class Developer:
    """One developer machine: a private directory, config and Client daemon."""

    def __init__(
        self,
        ws,
        name,
        server,
        *,
        bindings=1,
        bin_dir=None,
        notify=True,
        credentials=True,
    ):
        self.ws, self.name, self.server, self.bin_dir = ws, name, server, bin_dir
        self.directory = ws.path(name)
        self.directory.mkdir(mode=0o700)
        self.config_path = self.directory / "client.json"
        self.socket_path = self.directory / "client.sock"
        self.desktop = self.directory / "desktop.jsonl"
        self.enrollment_key = new_secret()
        self.bindings = [self._init_binding(i) for i in range(bindings)]
        self.proc = None
        config = {
            "server_url": server.url,
            "socket_path": str(self.socket_path),
            "bindings": self.bindings,
        }
        if credentials:
            config["enrollment_key"] = self.enrollment_key
        if server.tls:
            shutil.copy(server.tls[0], self.directory / "ca.pem")
            config["ca_file"] = "ca.pem"
        # An empty command disables desktop notices without touching osascript.
        config["notify_command"] = (
            [sys.executable, "-c", RECORDER, str(self.desktop)] if notify else []
        )
        self.config_path.touch(mode=0o600)
        self.config_path.write_text(json.dumps(config))

    def _init_binding(self, index):
        argv, env = command("client", self.bin_dir)
        out = subprocess.run(
            [*argv, "init-binding"],
            capture_output=True,
            text=True,
            env=clean_env() | env,
            cwd=ROOT,
            timeout=60,
            check=True,
        ).stdout
        binding = json.loads(out)
        assert set(binding) >= {"binding_key", "member_token"}
        binding["harness_instance_id"] = f"{self.name}-harness"
        binding["session_key"] = f"{self.name}-session-{index}"
        return binding

    @property
    def binding_key(self):
        return self.bindings[0]["binding_key"]

    @property
    def log(self):
        return self.proc.log

    def start(self):
        argv, env = command("client", self.bin_dir)
        argv += ["serve", "--config", str(self.config_path)]
        self.proc = self.ws.spawn(f"client-{self.name}", argv, env)
        wait_for(self.socket_path.is_socket, self.proc, what="client socket")
        return self

    def stop(self):
        if self.proc:
            self.proc.stop()
            self.proc = None

    @asynccontextmanager
    async def agent(self, index=0):
        argv, env = command("mcp", self.bin_dir)
        launch = StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env=clean_env()
            | env
            | {
                "TEAMUP_SOCKET": str(self.socket_path),
                "TEAMUP_BINDING_KEY": self.bindings[index]["binding_key"],
            },
        )
        async with (
            stdio_client(launch) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            yield session

    def desktop_notices(self):
        if not self.desktop.exists():
            return []
        return [json.loads(x) for x in self.desktop.read_text().splitlines()]

    def notices(self):
        """Live notices the Client printed on stderr, as dicts."""
        found = []
        for line in self.log.read_text(errors="replace").splitlines():
            with suppress(json.JSONDecodeError):
                value = json.loads(line)
                if isinstance(value, dict) and "message_id" in value:
                    found.append(value)
        return found

    def wait_for_notice(self, message_id, timeout=20):
        def find():
            return next(
                (n for n in self.notices() if n["message_id"] == message_id), None
            )

        return wait_for(find, self.proc, timeout, f"notice for message {message_id}")


class LogTail:
    """Follows a Client log on a thread and stamps each notice when first seen."""

    def __init__(self, path):
        self.path = Path(path)
        self.seen = {}
        self._offset = 0
        self._partial = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            self.poll()
            time.sleep(0.0002)

    def poll(self):
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        if not chunk:
            return
        stamp = time.perf_counter()
        self._offset += len(chunk)
        *lines, self._partial = (self._partial + chunk).split(b"\n")
        for line in lines:
            with suppress(ValueError):
                value = json.loads(line)
                if isinstance(value, dict) and "message_id" in value:
                    self.seen.setdefault(value["message_id"], stamp)

    async def wait(self, message_id, timeout=20):
        deadline = time.monotonic() + timeout
        while message_id not in self.seen:
            if time.monotonic() > deadline:
                raise AssertionError(f"no notice for message {message_id}")
            await asyncio.sleep(0.0005)
        return self.seen[message_id]


def deploy(
    ws, names=("alice", "bob"), *, bin_dir=None, tls=None, start=True, **options
):
    """Start a Server and one Client daemon per developer, like the quickstart."""
    server = Server(ws, {}, tls=tls, bin_dir=bin_dir)
    devs = [Developer(ws, n, server, bin_dir=bin_dir, **options) for n in names]
    server.keys = {dev.name: dev.enrollment_key for dev in devs}
    if start:
        server.start()
        for dev in devs:
            dev.start()
    return server, devs


async def tool(agent, operation, error=None, **arguments):
    """Call ``teamup_<operation>``; with ``error`` assert the failure code."""
    response = await agent.call_tool(f"teamup_{operation}", arguments)
    if error:
        assert response.isError, response.content
        assert error in response.content[0].text, response.content[0].text
        return None
    assert not response.isError, response.content
    return response.structuredContent


async def agent_tool_names(agent):
    return {item.name for item in (await agent.list_tools()).tools}
