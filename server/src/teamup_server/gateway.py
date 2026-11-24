"""The public JSON and SSE transport for Collaboration."""

import asyncio
import pathlib
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Annotated, Literal
from urllib.parse import unquote_to_bytes
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.types import Send

from .collaboration import Collaboration, CollaborationError, ObserveInbox

_ERRORS = {
    "invalid_request": (422, "The request is invalid."),
    "unauthorized": (401, "The credential is missing or invalid."),
    "forbidden": (403, "The authenticated account lacks the required authorization."),
    "not_found": (404, "The resource was not found."),
    "board_read_required": (409, "Read the current board version first."),
    "board_ack_required": (409, "Confirm the current board version first."),
    "conflict": (409, "The request conflicts with the existing state."),
    "invalid_mention": (422, "Mention another member of this room."),
    "rate_limited": (429, "Too many login attempts. Try again later."),
}
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_KEEPALIVE_SECONDS = 15
_CONSOLE_STATIC_DIR = pathlib.Path(__file__).parent / "console"
_CONSOLE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text is blank")
    value.encode("utf-8")
    return value


def _uuid(value: str) -> str:
    if _UUID.fullmatch(value) is None:
        raise ValueError("Expected a standard UUID")
    return str(UUID(value))


_ShortText = Annotated[
    str, StringConstraints(strict=True, max_length=128), AfterValidator(_nonblank)
]
_Name = Annotated[
    str, StringConstraints(strict=True, max_length=200), AfterValidator(_nonblank)
]
_LongText = Annotated[
    str, StringConstraints(strict=True, max_length=8000), AfterValidator(_nonblank)
]
_MemberToken = Annotated[
    str, StringConstraints(strict=True, min_length=43, max_length=43)
]
_Identifier = Annotated[str, AfterValidator(_uuid)]
_After = Annotated[int, Query(ge=0)]
_Limit = Annotated[int, Query(ge=1, le=100)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Login(_Body):
    username: Annotated[str, StringConstraints(strict=True)]
    password: Annotated[str, StringConstraints(strict=True)]
    purpose: Literal["console"] | None = None


class _Enrollment(_Body):
    harness_instance_id: _ShortText
    session_key: _ShortText
    role_description: _Name
    member_token: _MemberToken


class _Room(_Body):
    name: _Name
    rules: _LongText | None = None
    protocol: _LongText | None = None

    @field_validator("rules", "protocol")
    @classmethod
    def provided_text(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Board text must be a string when provided")
        return value


class _ConfirmBoard(_Body):
    version: Annotated[int, Field(strict=True, gt=0)]


class _Message(_Body):
    client_message_id: _ShortText
    text: _LongText
    mention_member_id: _Identifier | None = None


def _error(code: str, *, status_code: int | None = None) -> JSONResponse:
    status, message = _ERRORS[code]
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code or status
    )


async def _reject_body(request: Request) -> None:
    if await request.body():
        raise CollaborationError("invalid_request")


async def _require_utf8(request: Request) -> None:
    try:
        body = (await request.body()).decode("utf-8")
    except UnicodeDecodeError:
        raise CollaborationError("invalid_request") from None
    if "\x00" in body:
        raise CollaborationError("invalid_request")


async def _require_query_utf8(request: Request) -> None:
    try:
        unquote_to_bytes(request.scope["query_string"]).decode("utf-8")
    except UnicodeDecodeError:
        raise CollaborationError("invalid_request") from None


def _bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise CollaborationError("unauthorized")
    return parts[1]


_Bearer = Annotated[str, Depends(_bearer)]


def _console_asset(name: str, media_type: str) -> FileResponse:
    return FileResponse(
        _CONSOLE_STATIC_DIR / name, media_type=media_type, headers=_CONSOLE_HEADERS
    )


class _InboxStream(StreamingResponse):
    def __init__(self, frames: AsyncGenerator[str, None]):
        self._frames = frames
        super().__init__(
            frames,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def stream_response(self, send: Send) -> None:
        async with aclosing(self._frames):
            await super().stream_response(send)


class Gateway:
    """Maps HTTP requests to Collaboration and owns active streaming connections."""

    def __init__(self, collaboration: Collaboration):
        self._stopping = False
        self._streams: set[asyncio.Event] = set()
        self.app = app = FastAPI(openapi_url=None, redirect_slashes=False)

        @app.exception_handler(CollaborationError)
        async def collaboration_error(
            _: Request, exc: CollaborationError
        ) -> JSONResponse:
            return _error(exc.code)

        @app.exception_handler(RequestValidationError)
        async def validation_error(
            _: Request, __: RequestValidationError
        ) -> JSONResponse:
            return _error("invalid_request")

        @app.exception_handler(HTTPException)
        async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
            if exc.status_code == 404:
                return _error("not_found")
            return _error(
                "invalid_request", status_code=405 if exc.status_code == 405 else None
            )

        @app.post("/v1/auth/login", dependencies=[Depends(_require_utf8)])
        def login(body: _Login) -> dict:
            return collaboration.login(
                body.username, body.password, body.purpose or "enrollment"
            )

        @app.get("/v1/console/rooms")
        def console_rooms(token: _Bearer) -> dict:
            return collaboration.list_console_rooms(token)

        @app.get("/console/")
        def console_index() -> FileResponse:
            return _console_asset("index.html", "text/html; charset=utf-8")

        @app.get("/console/console.css")
        def console_stylesheet() -> FileResponse:
            return _console_asset("console.css", "text/css; charset=utf-8")

        @app.get("/console/console.js")
        def console_script() -> FileResponse:
            return _console_asset("console.js", "text/javascript; charset=utf-8")

        @app.post("/v1/members", dependencies=[Depends(_require_utf8)])
        def enroll(token: _Bearer, body: _Enrollment) -> dict:
            return collaboration.enroll(token, **body.model_dump())

        @app.get("/v1/members/me")
        def get_member(token: _Bearer) -> dict:
            return collaboration.get_member(token)

        @app.get("/v1/members", dependencies=[Depends(_require_query_utf8)])
        def search_members(token: _Bearer, q: str = "") -> dict:
            return {"members": collaboration.search_members(token, q)}

        @app.post("/v1/rooms", dependencies=[Depends(_require_utf8)])
        def create_room(token: _Bearer, body: _Room) -> dict:
            return collaboration.create_room(
                token, body.name, body.rules, body.protocol
            )

        @app.post("/v1/rooms/{room_id}/join", dependencies=[Depends(_reject_body)])
        def join_room(token: _Bearer, room_id: _Identifier) -> dict:
            return collaboration.join_room(token, room_id)

        @app.get("/v1/rooms/{room_id}/board", dependencies=[Depends(_reject_body)])
        def read_board(token: _Bearer, room_id: _Identifier) -> dict:
            return collaboration.read_board(token, room_id)

        @app.post(
            "/v1/rooms/{room_id}/board/ack", dependencies=[Depends(_require_utf8)]
        )
        def confirm_board(
            token: _Bearer, room_id: _Identifier, body: _ConfirmBoard
        ) -> dict:
            collaboration.confirm_board(token, room_id, body.version)
            return {"version": body.version}

        @app.post("/v1/rooms/{room_id}/messages", dependencies=[Depends(_require_utf8)])
        def send_message(token: _Bearer, room_id: _Identifier, body: _Message) -> dict:
            return collaboration.send_message(token, room_id, **body.model_dump())

        @app.get("/v1/rooms/{room_id}/messages")
        def history(
            token: _Bearer, room_id: _Identifier, after: _After = 0, limit: _Limit = 50
        ) -> dict:
            return {"messages": collaboration.history(token, room_id, after, limit)}

        @app.get("/v1/inbox")
        def inbox(token: _Bearer, after: _After = 0, limit: _Limit = 50) -> dict:
            return {"events": collaboration.inbox(token, after, limit)}

        @app.post("/v1/inbox/{event_id}/ack", dependencies=[Depends(_reject_body)])
        def acknowledge_event(
            token: _Bearer, event_id: Annotated[int, Path(gt=0)]
        ) -> dict:
            collaboration.acknowledge_event(token, event_id)
            return {"event_id": event_id}

        @app.get("/v1/events")
        async def events(
            token: _Bearer, accept: Annotated[str, Header()] = ""
        ) -> StreamingResponse:
            accepted = {
                entry.split(";", 1)[0].strip().lower() for entry in accept.split(",")
            }
            if "text/event-stream" not in accepted:
                raise CollaborationError("invalid_request")
            observe = await run_in_threadpool(collaboration.inbox_changes, token)
            return _InboxStream(self._event_frames(collaboration, token, observe))

    def stop_streams(self) -> None:
        """Complete open SSE responses before the HTTP server drains its requests."""
        self._stopping = True
        for changed in self._streams:
            changed.set()

    async def _event_frames(
        self, collaboration: Collaboration, token: str, observe: ObserveInbox
    ) -> AsyncGenerator[str, None]:
        changed = asyncio.Event()
        loop = asyncio.get_running_loop()

        def signal() -> None:
            loop.call_soon_threadsafe(changed.set)

        self._streams.add(changed)
        try:
            with observe(signal):
                await run_in_threadpool(collaboration.get_member, token)
                yield ": ready\n\n"
                while not self._stopping:
                    frame = "event: inbox_changed\ndata: {}\n\n"
                    try:
                        await asyncio.wait_for(
                            changed.wait(), timeout=_KEEPALIVE_SECONDS
                        )
                    except TimeoutError:
                        frame = ": keepalive\n\n"
                    else:
                        changed.clear()
                    if self._stopping:
                        return
                    await run_in_threadpool(collaboration.get_member, token)
                    yield frame
        except CollaborationError as exc:
            if exc.code != "unauthorized":
                raise
        finally:
            self._streams.remove(changed)
