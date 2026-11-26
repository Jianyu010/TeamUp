"""Optional, process-bound loopback web listener for the TeamUp Client."""

import asyncio
import concurrent.futures
import hmac
import json
import re
import secrets
import sqlite3
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from ._config import ConfigError
from ._console_data import MAX_BEFORE, ConsoleReader
from ._local import LocalError
from ._protocol import REQUEST_LIMIT, decode_json, is_integer, is_string, is_uuid
from .ipc import ClientError

CONSOLE_REQUEST_SECONDS = 18
CONSOLE_LOGIN_SECONDS = 15
WORKSPACE_RESPONSE_RESERVE_SECONDS = 0.5
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/console-history.mjs": ("console-history.mjs", "text/javascript; charset=utf-8"),
    "/console-members.js": ("console-members.js", "text/javascript; charset=utf-8"),
    "/console-board.js": ("console-board.js", "text/javascript; charset=utf-8"),
    "/console-tasks.js": ("console-tasks.js", "text/javascript; charset=utf-8"),
    "/console-sessions.js": ("console-sessions.js", "text/javascript; charset=utf-8"),
    "/jet-pair.png": ("jet-pair.png", "image/png"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


class ConsoleSession:
    def __init__(self, reader, sign_in, remember_scope):
        self.reader = reader
        self.remember_scope = remember_scope
        self._sign_in = sign_in
        self._lock = asyncio.Lock()
        self._login_task = None

    def status(self):
        return {"state": "ready" if self.reader is not None else "sign_in_required"}

    async def login(self, username, password, *, private=False, remember_scope=None):
        async with self._lock:
            if self.reader is not None:
                return self.status()
            if self._sign_in is None:
                return {"state": "unavailable"}
            if not all(
                is_string(value, limit, require_content=False)
                for value, limit in ((username, 64), (password, 128))
            ):
                return {"state": "wrong_credentials"}
            self._login_task = asyncio.current_task()
            try:
                async with asyncio.timeout(CONSOLE_LOGIN_SECONDS):
                    self.reader, authenticated_scope = await self._sign_in(
                        username, password, remember_scope
                    )
            except ClientError as error:
                if error.code in {"unauthorized", "invalid_request"}:
                    return {"state": "wrong_credentials"}
                if error.code == "rate_limited":
                    return {"state": "rate_limited"}
                return {"state": "unavailable"}
            except ConfigError:
                return {"state": "profile_unavailable"}
            except (LocalError, OSError, sqlite3.Error):
                return {"state": "unavailable"}
            finally:
                self._login_task = None
            result = self.status()
            if private:
                result["authenticated_scope"] = authenticated_scope
            return result

    async def close(self):
        self._sign_in = None
        if self._login_task is not None:
            task = self._login_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, loop, session, secret, loopback_host=None, **kwargs):
        self._loop = loop
        self._loopback_host = loopback_host
        self._session = session
        self._reader = session.reader
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
        host = self._loopback_host or f"127.0.0.1:{self.server.server_address[1]}"
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
        if target.path == "/api/login" and not target.query:
            status = self._session.status()
            if self._loopback_host is not None:
                status["remember_scope"] = self._session.remember_scope
            self._json(200, status)
            return
        if self._reader is None:
            self._json(409, {"state": "sign_in_required"})
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
            if target.path == "/api/login":
                if target.query:
                    raise ValueError
                credentials = self._action_body(required=True)
                private = self._loopback_host is not None
                fields = {"username", "password"}
                optional = {"remember_scope"} if private else set()
                if not fields <= credentials.keys() <= fields | optional:
                    raise ValueError
                if "remember_scope" in credentials and not (
                    isinstance(credentials["remember_scope"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", credentials["remember_scope"])
                ):
                    raise ValueError
                operation = self._session.login(**credentials, private=private)
            elif self._reader is None:
                self._json(409, {"state": "sign_in_required"})
                return
            else:
                operation = self._write_operation(target.path, target.query)
        except (KeyError, TypeError, ValueError, RecursionError, OSError):
            self._json(400, {"state": "invalid_request"})
            return
        if operation is None:
            self._json(404, {"state": "not_found"})
            return
        self._run(operation, write=True)

    def _write_operation(self, path, query):
        if path == "/api/wake-settings":
            body = self._action_body(required=True)
            if query or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                raise ValueError
            return self._reader.wake_settings(body["enabled"])

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
            self._action_body(required=False)
            return self._reader.session_action(params["session"], room_id, membership_id)

        if path == "/api/room-members/remove":
            params = self._params(query)
            if (
                set(params) != {"room", "session", "member"}
                or not self._hex_id(params["session"], 64)
                or not is_uuid(params["room"])
                or not is_uuid(params["member"])
            ):
                raise ValueError
            body = self._action_body(required=True)
            if set(body) != {"membership_id"} or not self._hex_id(body["membership_id"], 32):
                raise ValueError
            return self._reader.remove_room_member(
                params["session"], params["room"], params["member"], body["membership_id"],
            )

        if path == "/api/room-board":
            params = self._params(query)
            if set(params) != {"room", "session"} or not is_uuid(params["room"]):
                raise ValueError
            draft = self._action_body(required=True)
            if (
                set(draft) != {"version", "rules", "protocol"}
                or not is_integer(draft["version"], 1)
                or not is_string(draft["rules"], 8000)
                or not is_string(draft["protocol"], 8000)
            ):
                raise ValueError
            return self._reader.room_board(params["session"], params["room"], draft)

        parts = path.split("/")
        if len(parts) != 5 or parts[1:3] != ["api", "tasks"]:
            return None
        task_id, action = parts[3:]
        if not is_uuid(task_id) or action not in {"join", "progress", "finish"}:
            raise ValueError
        params = self._params(query)
        if set(params) != {"room", "session"} or not is_uuid(params["room"]):
            raise ValueError
        data = self._action_body(required=action == "progress")
        if data is not None and (
            set(data) != {"client_progress_id", "note"}
            or not is_string(data["client_progress_id"], 128)
            or not is_string(data["note"], 8000)
        ):
            raise ValueError
        return self._reader.task_action(
            params["session"], params["room"], task_id, action, data
        )

    def _action_body(self, *, required):
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get("Transfer-Encoding") or not 0 <= length <= REQUEST_LIMIT:
            raise ValueError
        if not required:
            if length:
                raise ValueError
            return None
        if not length or self.headers.get_content_type() != "application/json":
            raise ValueError
        data = decode_json(self.rfile.read(length))
        if not isinstance(data, dict):
            raise TypeError
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
                    "wrong_credentials": 401,
                    "rate_limited": 429,
                    "profile_unavailable": 409,
                    "not_registered": 401,
                    "forbidden": 403,
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
        if path == "/api/wake-settings" and not params:
            return self._reader.wake_settings()
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
        if path == "/api/room-board":
            if set(params) != {"session", "room"} or not is_uuid(params["room"]):
                raise ValueError
            return self._reader.room_board(params["session"], params["room"])
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
async def _running_listener(server):
    with server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.15}, daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(thread.join)


class _DesktopListener(ThreadingUnixStreamServer):
    daemon_threads = True


@asynccontextmanager
async def serve_console(
    reader: ConsoleReader | None, *, sign_in=None, desktop=False, remember_scope=None
):
    loop = asyncio.get_running_loop()
    token = secrets.token_urlsafe(32)
    session = ConsoleSession(reader, sign_in, remember_scope)
    handler = partial(
        Handler, loop=loop, session=session, secret=f"Bearer {token}".encode()
    )
    async with AsyncExitStack() as listeners:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        await listeners.enter_async_context(_running_listener(server))
        host = f"127.0.0.1:{server.server_port}"
        endpoint = f"http://{host}/#{token}"
        if desktop:
            # Keep the path within the Unix socket address limit on macOS.
            directory = listeners.enter_context(
                TemporaryDirectory(prefix="teamup-console-", dir="/tmp")
            )
            login_socket = Path(directory) / "login.sock"
            private = _DesktopListener(str(login_socket), partial(handler, loopback_host=host))
            await listeners.enter_async_context(_running_listener(private))
            login_socket.chmod(0o600)
            endpoint += f"\n{login_socket}"
        try:
            yield endpoint
        finally:
            await session.close()
