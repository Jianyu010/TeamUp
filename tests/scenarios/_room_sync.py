"""Exercise room sync over the public Server and Client interfaces."""

import json
import threading
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

from tests.scenarios._scenario_browser import (
    account_cli,
    console_listener,
    console_login,
)
from tests.scenarios._stack import Api, Developer, Server, new_secret, wait_for

COMMANDS = "/v1/room-commands"


class LoopbackRelay:
    """Keep Client HTTP online while deterministically withholding all SSE hints."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.client = httpx.Client(base_url=upstream.url, timeout=8, trust_env=False)
        self.online = threading.Event()
        self.online.set()
        self._closing = threading.Event()
        self._calls = []
        self._lock = threading.Lock()
        self._pause = None
        self._member_override = None
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                # A stopping Client can reset an idle keep-alive connection.
                with suppress(ConnectionResetError):
                    super().handle()

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                relay.forward(self)

            do_POST = do_GET

        self.listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.listener.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.listener.server_port}"

    @property
    def tls(self):
        return None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._closing.set()
        self.listener.shutdown()
        self.listener.server_close()
        self.thread.join()
        self.client.close()

    def calls(self, path, method="GET"):
        with self._lock:
            return [
                call
                for call in self._calls
                if (call.path, call.method) == (path, method)
            ]

    @contextmanager
    def report_member_as(self, confirmed_id, replacement_id):
        """Substitute the member ID in an otherwise authentic profile reply."""
        assert self._member_override is None
        self._member_override = confirmed_id, replacement_id
        try:
            yield
        finally:
            self._member_override = None

    @contextmanager
    def pause_exchange(self, path, method="GET", *, before=False, drop=False):
        """Hold one request before forwarding or one reply after the Server responds."""
        pause = ExchangePause(method, path, before, drop)
        with self._lock:
            assert self._pause is None, "A reply is already held"
            self._pause = pause
        try:
            yield pause
        finally:
            pause.release.set()
            with self._lock:
                self._pause = None

    def forward(self, handler):
        request = urlsplit(handler.path)
        params = parse_qs(request.query)
        query = {key: values[0] for key, values in params.items()}
        if not self.online.is_set():
            # Closing before any response bytes simulates a transport outage.
            self.record(Exchange(handler.command, request.path, query, None))
            handler.close_connection = True
            return
        if request.path == "/v1/events":
            self.record(Exchange(handler.command, request.path, query, None))
            self.idle_events(handler)
            return
        headers = {
            name: handler.headers[name]
            for name in ("Authorization", "Content-Type")
            if name in handler.headers
        }
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length) if length else None
        if not self._release_exchange(handler.command, request.path, before=True):
            handler.close_connection = True
            return
        try:
            response = self.client.request(
                handler.command,
                request.path,
                params=params,
                headers=headers,
                content=body,
            )
        except httpx.TransportError:
            handler.close_connection = True
            return
        operation_id = None
        if handler.command == "POST" and request.path == COMMANDS and body:
            operation_id = json.loads(body)["operation_id"]
        content = response.content
        overridden = None
        if (
            (replacement := self._member_override) is not None
            and handler.command == "GET"
            and request.path == "/v1/members/me"
            and response.status_code == 200
        ):
            profile = response.json()
            if profile["member_id"] == replacement[0]:
                overridden = replacement[1]
                content = json.dumps({**profile, "member_id": overridden}).encode()
        self.record(
            Exchange(
                handler.command,
                request.path,
                query,
                response.status_code,
                operation_id,
                overridden,
            )
        )
        if not self._release_exchange(handler.command, request.path, before=False):
            handler.close_connection = True
            return
        with suppress(BrokenPipeError, ConnectionResetError):
            handler.send_response(response.status_code)
            for name in ("Content-Type", "Cache-Control", "Retry-After"):
                if name in response.headers:
                    handler.send_header(name, response.headers[name])
            handler.send_header("Content-Length", str(len(content)))
            handler.end_headers()
            handler.wfile.write(content)

    def record(self, call):
        with self._lock:
            self._calls.append(call)

    def idle_events(self, handler):
        # Keep the subscription healthy while discarding every real Server hint.
        def chunk(data):
            handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            handler.wfile.flush()

        handler.close_connection = True
        with suppress(BrokenPipeError, ConnectionResetError):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            chunk(b"event: ready\ndata: {}\n\n")
            while not self._closing.wait(0.25) and self.online.is_set():
                chunk(b": keepalive\n\n")
            handler.wfile.write(b"0\r\n\r\n")

    def _release_exchange(self, method, path, *, before):
        with self._lock:
            pause = self._pause
            if (
                not pause
                or pause.claimed
                or (pause.method, pause.path, pause.before) != (method, path, before)
            ):
                return True
            pause.claimed = True
        pause.reached.set()
        return pause.release.wait(8) and not pause.drop


@dataclass
class Exchange:
    method: str
    path: str
    query: dict[str, str]
    status: int | None
    operation_id: str | None = None
    overridden_member_id: str | None = None


@dataclass
class ExchangePause:
    method: str
    path: str
    before: bool
    drop: bool
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False


class RoomSyncStack:
    account = "reader"

    def __init__(self, ws, relay, api, enrollment_key, password):
        self.ws, self.relay, self.api = ws, relay, api
        self.password = password
        self.bin_dir = relay.upstream.bin_dir
        self.writer = api.enroll(enrollment_key, "Legacy writer", session="writer")
        self.first_reader = self.profile("warm")
        self.binding = self.first_reader.bindings[0]
        reader_key = api.call(
            "POST",
            "/v1/auth/login",
            json={"username": self.account, "password": password},
        )["access_token"]
        self.reader = api.enroll(
            reader_key,
            "Reader",
            harness=self.binding["harness_instance_id"],
            session=self.binding["session_key"],
            token=self.binding["member_token"],
        )

    def profile(self, name, *, same_binding=False):
        developer = Developer(
            self.ws,
            name,
            self.relay,
            bin_dir=self.bin_dir,
            credentials=False,
            notify=False,
        )
        if same_binding:
            # A fresh profile starts from the published binding configuration only.
            developer.bindings[:] = [self.binding.copy()]
            config = json.loads(developer.config_path.read_text())
            config["bindings"] = developer.bindings
            developer.config_path.write_text(json.dumps(config))
        return developer

    @contextmanager
    def console(self, developer, *, sign_in=True):
        with console_listener(developer) as (console, _):
            if sign_in:
                console_login(console, self.account, self.password)
            yield console

    def create_room(self, **fields):
        room = self.api.create_room(self.writer, **fields)
        self.api.join(self.reader, room)
        for member in (self.writer, self.reader):
            self.api.read_and_ack_board(member, room)
        return room


@contextmanager
def room_sync_stack(ws, bin_dir=None):
    password, key = new_secret(), new_secret()
    server = Server(ws, {"writer": key}, bin_dir=bin_dir)
    account_cli(server, "create", RoomSyncStack.account, password=password)
    server.start()
    with (
        LoopbackRelay(server) as relay,
        closing(Api(server.url, trust_env=False)) as api,
    ):
        yield RoomSyncStack(ws, relay, api, key, password)


def console_session_id(console, member):
    def ready():
        response = console.get("/api/workspace")
        response.raise_for_status()
        return next(
            (
                row["id"]
                for row in response.json()["sessions"]
                if row["state"] == "ready" and row["member_id"] == member.member_id
            ),
            None,
        )

    return wait_for(ready, timeout=8, what="authorized Console session")


def console_room(console, session, room, *, archive=False, **params):
    path = "/api/room-archive" if archive else "/api/room"
    response = console.get(path, params={"session": session, "room": room, **params})
    response.raise_for_status()
    return response.json()


def membership_id(api, member, room):
    rooms = api.call(
        "GET", "/v1/rooms", member.token, params={"include_membership": "true"}
    )["rooms"]
    return next(entry["membership_id"] for entry in rooms if entry["room_id"] == room)


def sync_page(api, member, room, membership, cursor=None, **kwargs):
    params = {"expected_member_id": member.member_id, "membership_id": membership}
    if cursor is not None:
        params["cursor"] = cursor
    return api.call(
        "GET", f"/v1/rooms/{room}/sync", member.token, params=params, **kwargs
    )


def change_records(page, collection):
    return [
        item["record"] for item in page["changes"] if item["collection"] == collection
    ]


def transcript(view):
    assert view["state"] == "ready", view
    return [(row["id"], row["text"]) for row in view["messages"]]


def command_for(api, member, room, kind, **parameters):
    generation = api.call("GET", "/v1/sync/capabilities", member.token)[
        "server_generation"
    ]
    return {
        "operation_id": str(uuid4()),
        "expected_member_id": member.member_id,
        "server_generation": generation,
        "kind": kind,
        "room_id": room,
        "parameters": parameters,
    }
