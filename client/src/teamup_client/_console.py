"""Optional, process-bound loopback web listener for the TeamUp Client."""

import asyncio
import concurrent.futures
import hmac
import json
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from ._console_data import MAX_BEFORE, ConsoleReader
from ._protocol import REQUEST_LIMIT, decode_json, is_string, is_uuid
from .ipc import ClientError

CONSOLE_REQUEST_SECONDS = 18
WORKSPACE_RESPONSE_RESERVE_SECONDS = 0.5
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/console-history.mjs": ("console-history.mjs", "text/javascript; charset=utf-8"),
    "/console-members.js": ("console-members.js", "text/javascript; charset=utf-8"),
    "/console-tasks.js": ("console-tasks.js", "text/javascript; charset=utf-8"),
    "/console-sessions.js": ("console-sessions.js", "text/javascript; charset=utf-8"),
    "/jet-pair.png": ("jet-pair.png", "image/png"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, loop, reader, secret, **kwargs):
        self._loop = loop
        self._reader = reader
        self._secret = secret
        super().__init__(*args, **kwargs)

    def log_message(self, *_args):
        pass

    def setup(self):
        self.request.settimeout(6)
        super().setup()

    def _respond(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, document):
        self._respond(
            status, "application/json; charset=utf-8", json.dumps(document).encode()
        )

    def _origin_allowed(self, *, write=False):
        host = f"127.0.0.1:{self.server.server_address[1]}"
        origin = self.headers.get("Origin")
        return self.headers.get("Host") == host and (
            origin == f"http://{host}" or (origin is None and not write)
        )

    def _authorized(self):
        return hmac.compare_digest(
            self.headers.get("Authorization", "").encode("utf-8"), self._secret
        )

    def do_GET(self):
        if not self._origin_allowed():
            self._json(403, {"state": "forbidden"})
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._json(400, {"state": "invalid_request"})
            return
        if target.path in STATIC and not target.query:
            name, content_type = STATIC[target.path]
            self._respond(
                200,
                content_type,
                files("teamup_client").joinpath("console", name).read_bytes(),
            )
            return
        if not self._authorized():
            self._json(401, {"state": "unauthorized"})
            return
        try:
            operation = self._operation(target.path, target.query)
        except (ValueError, KeyError):
            self._json(400, {"state": "invalid_request"})
            return
        if operation is None:
            self._json(404, {"state": "not_found"})
            return
        self._run(operation)

    def do_POST(self):
        if not self._origin_allowed(write=True):
            self._json(403, {"state": "forbidden"})
            return
        if not self._authorized():
            self._json(401, {"state": "unauthorized"})
            return
        try:
            target = urlsplit(self.path)
            operation = self._write_operation(target.path, target.query)
        except (KeyError, ValueError, RecursionError, OSError):
            self._json(400, {"state": "invalid_request"})
            return
        if operation is None:
            self._json(404, {"state": "not_found"})
            return
        self._run(operation, write=True)

    def _write_operation(self, path, query):
        if path in {"/api/sessions/leave", "/api/sessions/retire"}:
            params = self._params(query)
            required = {"session", "room"} if path.endswith("/leave") else {"session"}
            optional = {"membership_id"} if path.endswith("/leave") else set()
            if not (
                required <= params.keys() <= required | optional
                and self._hex_id(params["session"], 64)
            ):
                raise ValueError
            room_id = params.get("room")
            if room_id is not None and not is_uuid(room_id):
                raise ValueError
            membership_id = params.get("membership_id")
            if membership_id is not None and not self._hex_id(membership_id, 32):
                raise ValueError
            self._task_body("empty")
            return self._reader.session_action(params["session"], room_id, membership_id)
        parts = path.split("/")
        if len(parts) != 5 or parts[1:3] != ["api", "tasks"]:
            return None
        task_id, action = parts[3:]
        if not is_uuid(task_id) or action not in {"join", "progress", "finish"}:
            raise ValueError
        params = self._params(query)
        if set(params) != {"room", "session"} or not is_uuid(params["room"]):
            raise ValueError
        data = self._task_body(action)
        return self._reader.task_action(params["session"], params["room"], task_id, action, data)

    def _task_body(self, action):
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get("Transfer-Encoding") or not 0 <= length <= REQUEST_LIMIT:
            raise ValueError
        if action != "progress":
            if length:
                raise ValueError
            return None
        if not length or self.headers.get_content_type() != "application/json":
            raise ValueError
        data = decode_json(self.rfile.read(length))
        if (
            not isinstance(data, dict)
            or set(data) != {"client_progress_id", "note"}
            or not is_string(data["client_progress_id"], 128)
            or not is_string(data["note"], 8000)
        ):
            raise ValueError
        return data

    def _run(self, operation, *, write=False):
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        try:
            data = future.result(timeout=CONSOLE_REQUEST_SECONDS)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._json(503, {"state": "unavailable"})
        except (
            ClientError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            sqlite3.Error,
            concurrent.futures.CancelledError,
        ):
            self._json(503, {"state": "unavailable"})
        else:
            status = (
                {
                    "ready": 200,
                    "unauthorized": 401,
                    "not_registered": 401,
                    "not_found": 404,
                    "board_ack_required": 409,
                    "conflict": 409,
                    "member_retired": 409,
                    "membership_changed": 409,
                    "invalid_request": 422,
                    "unavailable": 503,
                }.get(data.get("state"), 503)
                if write
                else 200
            )
            self._json(status, data)

    @staticmethod
    def _params(query):
        values = parse_qs(query, keep_blank_values=True, max_num_fields=4)
        if any(len(items) != 1 for items in values.values()):
            raise ValueError
        return {key: items[0] for key, items in values.items()}

    def _operation(self, path, query):
        params = self._params(query)
        if path == "/api/versions":
            if params:
                raise ValueError
            return self._reader.versions()
        if path == "/api/workspace" and not params:
            directory_deadline = (
                self._loop.time()
                + CONSOLE_REQUEST_SECONDS
                - WORKSPACE_RESPONSE_RESERVE_SECONDS
            )
            return self._reader.workspace(directory_deadline=directory_deadline)
        if path == "/api/retired":
            return self._retired_operation(params)
        if path == "/api/team" and set(params) == {"session"}:
            return self._reader.team(params["session"])
        if path == "/api/room-members":
            if set(params) != {"session", "room"} or not is_uuid(params["room"]):
                raise ValueError
            return self._reader.room_members(params["session"], params["room"])
        if path == "/api/room":
            if (
                not {"session", "room"}
                <= params.keys()
                <= {"session", "room", "before", "after"}
            ):
                raise ValueError
            identifier = params["room"]
            if not is_uuid(identifier):
                raise ValueError
            before = None
            after = None
            if "after" in params:
                after = self._after(params)
            if "before" in params:
                if after is not None:
                    raise ValueError
                before = int(params["before"])
                if not 1 <= before <= MAX_BEFORE:
                    raise ValueError
            return self._reader.room(params["session"], identifier, before, after=after)
        if path == "/api/tasks":
            if not {"room"} <= params.keys() <= {"room", "status", "after"}:
                raise ValueError
            status = params.get("status", "open")
            if not is_uuid(params["room"]) or status not in {"open", "finished"}:
                raise ValueError
            return self._reader.tasks(params["room"], status, self._after(params))
        if path == "/api/task-events":
            if (
                not {"session", "room", "task"}
                <= params.keys()
                <= {"session", "room", "task", "after"}
            ):
                raise ValueError
            if not is_uuid(params["room"]) or not is_uuid(params["task"]):
                raise ValueError
            return self._reader.task_events(
                params["session"], params["room"], params["task"], self._after(params)
            )
        return None

    def _retired_operation(self, params):
        session = params.get("session")
        if not self._hex_id(session, 64):
            raise ValueError
        if set(params) == {"session"}:
            return self._reader.retired_history(session)
        if set(params) == {"session", "after"}:
            return self._reader.retired_history(session, after=self._after(params))
        if not {"session", "kind", "conversation"} <= params.keys() <= {
            "session", "kind", "conversation", "before"
        }:
            raise ValueError
        if params["kind"] not in {"room", "dm"} or not is_uuid(params["conversation"]):
            raise ValueError
        before = int(params["before"]) if "before" in params else None
        if before is not None and not 1 <= before <= MAX_BEFORE:
            raise ValueError
        return self._reader.retired_history(
            session, kind=params["kind"], conversation=params["conversation"], before=before
        )

    @staticmethod
    def _hex_id(value, length):
        return bool(
            isinstance(value, str) and len(value) == length
            and all(char in "0123456789abcdef" for char in value)
        )

    @staticmethod
    def _after(params):
        after = int(params.get("after", "0"))
        if not 0 <= after < MAX_BEFORE:
            raise ValueError
        return after


@asynccontextmanager
async def serve_console(reader: ConsoleReader):
    loop = asyncio.get_running_loop()
    token = secrets.token_urlsafe(32)
    handler = partial(
        Handler, loop=loop, reader=reader, secret=f"Bearer {token}".encode()
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.15}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/#{token}"
    finally:
        await asyncio.to_thread(server.shutdown)
        await asyncio.to_thread(server.server_close)
        await asyncio.to_thread(thread.join)
