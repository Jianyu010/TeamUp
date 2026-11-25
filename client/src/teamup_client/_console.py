"""Optional, process-bound loopback web listener for the TeamUp Client."""

import asyncio
import concurrent.futures
import hmac
import json
import secrets
import threading
from contextlib import asynccontextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from ._console_data import MAX_BEFORE, ConsoleReader
from ._protocol import is_uuid
from .ipc import ClientError

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
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

    def do_GET(self):
        host = f"127.0.0.1:{self.server.server_address[1]}"
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != host or origin not in (None, f"http://{host}"):
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
        if not hmac.compare_digest(
            self.headers.get("Authorization", "").encode("utf-8"), self._secret
        ):
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
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        try:
            data = future.result(timeout=18)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._json(503, {"state": "unavailable"})
        except (
            ClientError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            concurrent.futures.CancelledError,
        ):
            self._json(503, {"state": "unavailable"})
        else:
            self._json(200, data)

    def _operation(self, path, query):
        params = parse_qs(query, keep_blank_values=True, max_num_fields=3)
        if any(len(values) != 1 for values in params.values()):
            raise ValueError
        if path == "/api/workspace" and not params:
            return self._reader.workspace()
        if path == "/api/team" and set(params) == {"session"}:
            return self._reader.team(params["session"][0])
        if path == "/api/room":
            if (
                not {"session", "room"}
                <= params.keys()
                <= {"session", "room", "before", "after"}
            ):
                raise ValueError
            identifier = params["room"][0]
            if not is_uuid(identifier):
                raise ValueError
            before = None
            after = None
            if "after" in params:
                after = int(params["after"][0])
                if not 0 <= after < MAX_BEFORE:
                    raise ValueError
            if "before" in params:
                if after is not None:
                    raise ValueError
                before = int(params["before"][0])
                if not 1 <= before <= MAX_BEFORE:
                    raise ValueError
            return self._reader.room(
                params["session"][0], identifier, before, after=after
            )
        return None


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
