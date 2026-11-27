"""The public JSON and SSE transport for Collaboration."""

import asyncio
import logging
import pathlib
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Literal
from urllib.parse import unquote_to_bytes
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
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
from .gateway_observations import ERROR_CODE_KEY, GatewayObservations, GatewayTraffic
from .host_observations import HOST_METRIC_NAMES, HostObservations
from .operational_events import OperationalEvents
from .ops_metrics import ObservationSource, utc_timestamp
from .presence import Presence

_ERRORS = {
    "invalid_request": (422, "The request is invalid."),
    "unauthorized": (401, "The credential is missing or invalid."),
    "forbidden": (403, "The authenticated account lacks the required authorization."),
    "not_found": (404, "The resource was not found."),
    "board_read_required": (409, "Read the current board version first."),
    "board_ack_required": (409, "Confirm the current board version first."),
    "conflict": (409, "The request conflicts with the existing state."),
    "member_retired": (409, "This member's binding has been retired."),
    "room_archived": (409, "This room is archived."),
    "membership_changed": (409, "The current room membership has changed."),
    "invalid_mention": (422, "Mention another member of this room."),
    "invalid_peer": (422, "The DM peer must be another available member of this team."),
    "rate_limited": (429, "Too many login attempts. Try again later."),
}
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_KEEPALIVE_SECONDS = 15
_LOGGER = logging.getLogger(__name__)
_LOG_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)

_CONSOLE_STATIC_DIR = pathlib.Path(__file__).parent / "console"
_CONSOLE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; frame-src https://jianyu010.github.io"
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
_Responsibility = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=160)
]
_LongText = Annotated[
    str, StringConstraints(strict=True, max_length=8000), AfterValidator(_nonblank)
]
_MemberToken = Annotated[
    str, StringConstraints(strict=True, min_length=43, max_length=43)
]
_Identifier = Annotated[str, AfterValidator(_uuid)]
_After = Annotated[int, Query(ge=0)]
_Before = Annotated[int, Query(ge=1)]
_MEMBERSHIP_PATTERN = r"^[0-9a-f]{32}$"
_MembershipID = Annotated[str, Query(pattern=_MEMBERSHIP_PATTERN)]
_Limit = Annotated[int, Query(ge=1, le=100)]
_LogLimit = Annotated[int, Query(ge=1, le=500)]
_LogTime = Annotated[str, Query(min_length=20, max_length=32)]
_LogComponent = Literal["host", "gateway", "collaboration"]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _log_time(value: str) -> datetime:
    if _LOG_TIMESTAMP.fullmatch(value):
        try:
            return datetime.fromisoformat(value).astimezone(UTC)
        except (ValueError, OverflowError):
            pass
    raise CollaborationError("invalid_request")


_SearchText = Annotated[
    str,
    Query(min_length=1, max_length=200),
    AfterValidator(_nonblank),
]


class _BuildVersion(BaseModel):
    version: str | None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Login(_Body):
    username: Annotated[str, StringConstraints(strict=True)]
    password: Annotated[str, StringConstraints(strict=True)]
    purpose: Literal["console"] | None = None


class _NewPassword(_Body):
    password: Annotated[
        str, StringConstraints(strict=True, min_length=12, max_length=128)
    ]


class _NewAccount(_NewPassword):
    username: Annotated[
        str, StringConstraints(strict=True, min_length=3, max_length=64)
    ]


class _Enrollment(_Body):
    harness_instance_id: _ShortText
    session_key: _ShortText
    member_token: _MemberToken
    role_description: _Name | None = None
    name: _Name | None = None
    description: _Responsibility | None = None

    @field_validator("role_description", "name", "description")
    @classmethod
    def profile_text(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Profile fields must be strings when supplied")
        return value


class _RoomResponsibility(_Body):
    membership_id: Annotated[
        str, StringConstraints(strict=True, pattern=_MEMBERSHIP_PATTERN)
    ]
    description: _Responsibility | None


class _MemberRole(_Body):
    role_description: _Name


class _Room(_Body):
    name: _Name
    rules: _LongText | None = None
    protocol: _LongText | None = None
    history_policy: Literal["retained", "since_joining"] = "since_joining"

    @field_validator("rules", "protocol")
    @classmethod
    def provided_text(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Board text must be a string when provided")
        return value


class _RemoveRoomMember(_Body):
    membership_id: Annotated[
        str, StringConstraints(strict=True, pattern=_MEMBERSHIP_PATTERN)
    ]


class _ConfirmBoard(_Body):
    version: Annotated[int, Field(strict=True, gt=0)]


class _PublishBoard(_ConfirmBoard):
    rules: _LongText
    protocol: _LongText


class _Message(_Body):
    client_message_id: _ShortText
    text: _LongText
    mention_member_id: _Identifier | None = None
    mention_member_ids: Annotated[
        list[_Identifier],
        Field(min_length=1, max_length=16, json_schema_extra={"uniqueItems": True}),
    ] = Field(default_factory=list)


class _TaskCreate(_Body):
    client_task_id: _ShortText
    title: _Name
    description: _LongText | None = None
    addressed_member_ids: list[_Identifier] | None = None

    @field_validator("addressed_member_ids")
    @classmethod
    def distinct_members(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(value) > 100 or len(set(value)) != len(value)):
            raise ValueError("Address each member of this room at most once")
        return value


class _TaskProgress(_Body):
    client_progress_id: _ShortText
    note: _LongText


class _OpenDM(_Body):
    member_id: _Identifier


class _DMMessage(_Body):
    client_message_id: _ShortText
    text: _LongText


_EVENT_KINDS = frozenset(("message", "mention", "dm", "room_left"))


def _event_kinds(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ("message", "mention")
    kinds = tuple(value.split(","))
    if len(set(kinds)) != len(kinds) or not set(kinds) <= _EVENT_KINDS:
        raise CollaborationError("invalid_request")
    return kinds


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

    def __init__(
        self,
        collaboration: Collaboration,
        presence: Presence,
        *,
        task_tracer_period: float | None = None,
        host: HostObservations | None = None,
        event_log: OperationalEvents | None = None,
    ):
        self._stopping = False
        self._streams: set[asyncio.Event] = set()
        self._task_tracer_period = task_tracer_period
        observations = GatewayObservations(event_log, frozenset(_ERRORS))
        unavailable_host = ObservationSource("host", HOST_METRIC_NAMES)

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
            if event_log is not None:
                event_log.server_started()
            if self._task_tracer_period is None:
                yield
                return
            stopped = asyncio.Event()

            async def trace() -> None:
                while not stopped.is_set():
                    try:
                        await run_in_threadpool(collaboration.scan_task_reminders)
                    except Exception:
                        _LOGGER.exception("The task reminder scan failed")
                    try:
                        await asyncio.wait_for(
                            stopped.wait(), timeout=self._task_tracer_period
                        )
                    except TimeoutError:
                        pass

            scanner = asyncio.create_task(trace())
            try:
                yield
            finally:
                stopped.set()
                await scanner

        self.app = app = FastAPI(
            openapi_url=None,
            redirect_slashes=False,
            lifespan=lifespan,
        )
        app.add_middleware(GatewayTraffic, observations=observations)

        @app.exception_handler(CollaborationError)
        async def collaboration_error(
            request: Request, exc: CollaborationError
        ) -> JSONResponse:
            request.scope[ERROR_CODE_KEY] = exc.code
            return _error(exc.code)

        @app.exception_handler(RequestValidationError)
        async def validation_error(
            request: Request, _: RequestValidationError
        ) -> JSONResponse:
            request.scope[ERROR_CODE_KEY] = "invalid_request"
            return _error("invalid_request")

        @app.exception_handler(HTTPException)
        async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
            if exc.status_code == 404:
                request.scope[ERROR_CODE_KEY] = "not_found"
                return _error("not_found")
            request.scope[ERROR_CODE_KEY] = "invalid_request"
            return _error(
                "invalid_request", status_code=405 if exc.status_code == 405 else None
            )

        try:
            installed_version = version("teamup-server")
        except PackageNotFoundError:
            installed_version = None
        build_version = (
            None if installed_version == "0.0.0+unknown" else installed_version
        )

        @app.get("/v1/version")
        def server_version(response: Response) -> _BuildVersion:
            _no_store(response)
            return _BuildVersion(version=build_version)

        @app.post("/v1/auth/login", dependencies=[Depends(_require_utf8)])
        def login(body: _Login) -> dict:
            return collaboration.login(
                body.username, body.password, body.purpose or "enrollment"
            )

        @app.post("/v1/auth/console-session", dependencies=[Depends(_reject_body)])
        def issue_console_session(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return collaboration.issue_console_session(token)

        @app.get("/v1/auth/capabilities", dependencies=[Depends(_reject_body)])
        def account_capabilities(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return collaboration.account_capabilities(token)

        @app.get("/v1/console/capabilities", dependencies=[Depends(_reject_body)])
        def console_capabilities(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return collaboration.console_capabilities(token)

        @app.get("/v1/console/accounts", dependencies=[Depends(_reject_body)])
        def console_accounts(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return collaboration.console_accounts(token)

        @app.post(
            "/v1/console/accounts",
            status_code=201,
            dependencies=[Depends(_require_utf8)],
        )
        def create_account(
            token: _Bearer, body: _NewAccount, response: Response
        ) -> dict:
            _no_store(response)
            collaboration.create_account(
                body.username, body.password, operator_token=token
            )
            return {"username": body.username}

        @app.post(
            "/v1/console/accounts/{username}/password",
            dependencies=[Depends(_require_utf8)],
        )
        def reset_account_password(
            token: _Bearer, username: str, body: _NewPassword, response: Response
        ) -> dict:
            _no_store(response)
            collaboration.reset_account_password(
                username, body.password, operator_token=token
            )
            return {"username": username}

        @app.post(
            "/v1/console/accounts/{username}/disable",
            dependencies=[Depends(_reject_body)],
        )
        def disable_account(token: _Bearer, username: str, response: Response) -> dict:
            _no_store(response)
            collaboration.disable_account(username, operator_token=token)
            return {"username": username}

        @app.get("/v1/console/rooms")
        def console_rooms(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return collaboration.list_console_rooms(token)

        @app.get("/v1/console/maintenance/health")
        def maintenance_health(token: _Bearer, response: Response) -> dict:
            collaboration_snapshot = collaboration.console_maintenance_snapshot(token)
            host_snapshot = host.read() if host else unavailable_host.read(lambda: None)
            gateway_snapshot = observations.read()
            _no_store(response)
            return {
                "generated_at": utc_timestamp(),
                "sources": {
                    "host": host_snapshot,
                    "gateway": gateway_snapshot,
                    "collaboration": collaboration_snapshot,
                },
            }

        @app.get("/v1/console/maintenance/logs")
        def maintenance_logs(
            token: _Bearer,
            since: _LogTime,
            until: _LogTime,
            response: Response,
            component: _LogComponent | None = None,
            limit: _LogLimit = 100,
        ) -> dict:
            collaboration.authorize_console_logs(token)
            start, end = _log_time(since), _log_time(until)
            if not timedelta(0) < end - start <= timedelta(hours=1):
                raise CollaborationError("invalid_request")
            result: dict = (
                event_log.query(
                    since=start, until=end, component=component, limit=limit
                )
                if event_log is not None
                else {"state": "unavailable", "entries": [], "truncated": False}
            )
            collaboration.audit_console_logs(
                token,
                since=start.isoformat(),
                until=end.isoformat(),
                component=component,
                result_count=len(result["entries"]),
            )
            _no_store(response)
            return result

        @app.get("/v1/console/rooms/{room_id}")
        def console_room_detail(
            token: _Bearer, room_id: _Identifier, response: Response
        ) -> dict:
            _no_store(response)
            return collaboration.console_room_detail(token, room_id)

        @app.get("/v1/console/rooms/{room_id}/load")
        def maintenance_room_load(
            token: _Bearer, room_id: _Identifier, response: Response
        ) -> dict:
            _no_store(response)
            return collaboration.console_room_load(token, room_id)

        @app.get("/v1/console/rooms/{room_id}/timeline")
        def console_timeline(
            token: _Bearer,
            room_id: _Identifier,
            response: Response,
            after: _After = 0,
            limit: _Limit = 50,
        ) -> dict:
            _no_store(response)
            return collaboration.console_timeline(token, room_id, after, limit)

        @app.get("/v1/console/rooms/{room_id}/transcript")
        def console_transcript(
            token: _Bearer,
            room_id: _Identifier,
            response: Response,
            before: _Before | None = None,
            limit: _Limit = 50,
        ) -> dict:
            _no_store(response)
            return collaboration.console_transcript(token, room_id, before, limit)

        @app.get("/console/")
        @app.get("/dashboard/")
        def console_index() -> FileResponse:
            return _console_asset("index.html", "text/html; charset=utf-8")

        @app.get("/console/console.css")
        def console_stylesheet() -> FileResponse:
            return _console_asset("console.css", "text/css; charset=utf-8")

        @app.get("/console/console.js")
        def console_script() -> FileResponse:
            return _console_asset("console.js", "text/javascript; charset=utf-8")

        @app.get("/console/accounts.js")
        def accounts_script() -> FileResponse:
            return _console_asset("accounts.js", "text/javascript; charset=utf-8")

        @app.get("/console/maintenance.js")
        def maintenance_script() -> FileResponse:
            return _console_asset("maintenance.js", "text/javascript; charset=utf-8")

        @app.post("/v1/members", dependencies=[Depends(_require_utf8)])
        def enroll(token: _Bearer, body: _Enrollment) -> dict:
            return collaboration.enroll(token, **body.model_dump())

        @app.get("/v1/members/me")
        def get_member(token: _Bearer) -> dict:
            return collaboration.get_member(token)

        @app.get("/v1/members", dependencies=[Depends(_require_query_utf8)])
        def search_members(token: _Bearer, q: str = "") -> dict:
            return {"members": collaboration.search_members(token, q)}

        @app.get("/v1/members/me/retirement")
        def member_retirement(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return collaboration.member_retirement(token)

        @app.post("/v1/members/me/retire", dependencies=[Depends(_reject_body)])
        def retire_self(token: _Bearer) -> dict:
            return collaboration.retire_self(token)

        @app.get("/v1/members/{member_id}/archive")
        def list_retired_conversations(
            token: _Bearer, member_id: _Identifier, response: Response
        ) -> dict:
            _no_store(response)
            return collaboration.list_retired_conversations(token, member_id)

        @app.get("/v1/members/{member_id}/archive/{kind}/{conversation_id}/messages")
        def retired_history(
            token: _Bearer,
            member_id: _Identifier,
            kind: Literal["room", "dm"],
            conversation_id: _Identifier,
            response: Response,
            before: _Before | None = None,
            limit: _Limit = 50,
        ) -> dict:
            _no_store(response)
            return collaboration.retired_history(
                token, member_id, kind, conversation_id, before, limit
            )

        @app.patch("/v1/members/{member_id}", dependencies=[Depends(_require_utf8)])
        def rename_member(
            token: _Bearer, member_id: _Identifier, body: _MemberRole
        ) -> dict:
            return collaboration.rename_member(token, member_id, body.role_description)

        @app.post(
            "/v1/members/{member_id}/retire", dependencies=[Depends(_reject_body)]
        )
        def retire_member(token: _Bearer, member_id: _Identifier) -> dict:
            return collaboration.retire_member(token, member_id)

        @app.post("/v1/presence/heartbeat", dependencies=[Depends(_reject_body)])
        def heartbeat(token: _Bearer) -> dict:
            return presence.heartbeat(token)

        @app.get("/v1/presence", dependencies=[Depends(_reject_body)])
        def read_presence(token: _Bearer, response: Response) -> dict:
            _no_store(response)
            return presence.snapshot(token)

        @app.post("/v1/rooms", dependencies=[Depends(_require_utf8)])
        def create_room(token: _Bearer, body: _Room) -> dict:
            return collaboration.create_room(
                token, body.name, body.rules, body.protocol, body.history_policy
            )

        @app.get("/v1/rooms")
        def list_rooms(token: _Bearer, include_membership: bool = False) -> dict:
            return {
                "rooms": collaboration.list_rooms(
                    token, include_membership=include_membership
                )
            }

        @app.get("/v1/room-directory", dependencies=[Depends(_reject_body)])
        def room_directory(
            token: _Bearer,
            response: Response,
            after: _Identifier | None = None,
            limit: _Limit = 50,
        ) -> dict:
            _no_store(response)
            return collaboration.room_directory(token, after, limit)

        @app.get("/v1/rooms/archive", dependencies=[Depends(_reject_body)])
        def list_archived_rooms(
            token: _Bearer,
            response: Response,
            after: _Identifier | None = None,
            limit: _Limit = 50,
        ) -> dict:
            _no_store(response)
            return collaboration.list_archived_rooms(token, after, limit)

        @app.post("/v1/rooms/{room_id}/archive", dependencies=[Depends(_reject_body)])
        def archive_room(token: _Bearer, room_id: _Identifier, response: Response) -> dict:
            _no_store(response)
            return collaboration.archive_room(token, room_id)

        @app.get("/v1/rooms/{room_id}", dependencies=[Depends(_reject_body)])
        def room_detail(
            token: _Bearer, room_id: _Identifier, response: Response
        ) -> dict:
            _no_store(response)
            return collaboration.room_detail(token, room_id)

        @app.get("/v1/rooms/{room_id}/members", dependencies=[Depends(_reject_body)])
        def room_members(
            token: _Bearer,
            room_id: _Identifier,
            response: Response,
            include_membership: bool = False,
        ) -> dict:
            _no_store(response)
            return {
                "members": collaboration.room_members(
                    token, room_id, include_membership=include_membership
                ),
            }

        @app.post(
            "/v1/rooms/{room_id}/members/{member_id}/remove",
            dependencies=[Depends(_require_utf8)],
        )
        def remove_room_member(
            token: _Bearer,
            room_id: _Identifier,
            member_id: _Identifier,
            body: _RemoveRoomMember,
        ) -> dict:
            return collaboration.remove_room_member(
                token, room_id, member_id, body.membership_id
            )

        @app.patch(
            "/v1/rooms/{room_id}/members/{member_id}/responsibility",
            dependencies=[Depends(_require_utf8)],
        )
        def set_room_responsibility(
            token: _Bearer,
            room_id: _Identifier,
            member_id: _Identifier,
            body: _RoomResponsibility,
        ) -> dict:
            return collaboration.set_room_responsibility(
                token, room_id, member_id, body.membership_id, body.description
            )

        @app.post("/v1/rooms/{room_id}/join", dependencies=[Depends(_reject_body)])
        def join_room(token: _Bearer, room_id: _Identifier) -> dict:
            return collaboration.join_room(token, room_id)

        @app.post("/v1/rooms/{room_id}/leave", dependencies=[Depends(_reject_body)])
        def leave_room(
            token: _Bearer,
            room_id: _Identifier,
            notify: bool = False,
            membership_id: _MembershipID | None = None,
        ) -> dict:
            return collaboration.leave_room(
                token, room_id, notify=notify, membership_id=membership_id
            )

        @app.get("/v1/rooms/{room_id}/board/view", dependencies=[Depends(_reject_body)])
        def view_board(
            token: _Bearer, room_id: _Identifier, response: Response
        ) -> dict:
            _no_store(response)
            return collaboration.view_board(token, room_id)

        @app.post("/v1/rooms/{room_id}/board", dependencies=[Depends(_require_utf8)])
        def publish_board(
            token: _Bearer,
            room_id: _Identifier,
            body: _PublishBoard,
            response: Response,
        ) -> dict:
            _no_store(response)
            return collaboration.publish_board(token, room_id, **body.model_dump())

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
            return collaboration.send_message(
                token, room_id, **body.model_dump(exclude_unset=True)
            )

        @app.get("/v1/rooms/{room_id}/messages")
        def history(
            token: _Bearer,
            room_id: _Identifier,
            after: _After = 0,
            limit: _Limit = 50,
            before: _Before | None = None,
            membership_id: _MembershipID | None = None,
            delivery: bool = False,
        ) -> dict:
            return collaboration.history(
                token,
                room_id,
                after,
                limit,
                before,
                membership_id=membership_id,
                delivery=delivery,
            )

        @app.get(
            "/v1/rooms/{room_id}/search", dependencies=[Depends(_require_query_utf8)]
        )
        def search_room(
            token: _Bearer,
            room_id: _Identifier,
            q: _SearchText,
            after: _After = 0,
            limit: _Limit = 50,
        ) -> dict:
            return collaboration.search(token, room_id, q, after, limit)

        @app.post("/v1/rooms/{room_id}/tasks", dependencies=[Depends(_require_utf8)])
        def create_task(
            token: _Bearer, room_id: _Identifier, body: _TaskCreate
        ) -> dict:
            return collaboration.create_task(token, room_id, **body.model_dump())

        @app.get(
            "/v1/rooms/{room_id}/tasks",
            dependencies=[Depends(_require_query_utf8)],
        )
        def list_tasks(
            token: _Bearer,
            room_id: _Identifier,
            status: Literal["open", "finished"] | None = None,
            after: _After = 0,
            limit: _Limit = 50,
        ) -> dict:
            return collaboration.list_tasks(token, room_id, status, after, limit)

        @app.get(
            "/v1/rooms/{room_id}/tasks/{task_id}",
            dependencies=[Depends(_reject_body)],
        )
        def get_task(
            token: _Bearer, room_id: _Identifier, task_id: _Identifier
        ) -> dict:
            return collaboration.get_task(token, room_id, task_id)

        @app.post(
            "/v1/rooms/{room_id}/tasks/{task_id}/join",
            dependencies=[Depends(_reject_body)],
        )
        def join_task(
            token: _Bearer, room_id: _Identifier, task_id: _Identifier
        ) -> dict:
            return collaboration.join_task(token, room_id, task_id)

        @app.post(
            "/v1/rooms/{room_id}/tasks/{task_id}/progress",
            dependencies=[Depends(_require_utf8)],
        )
        def report_progress(
            token: _Bearer,
            room_id: _Identifier,
            task_id: _Identifier,
            body: _TaskProgress,
        ) -> dict:
            return collaboration.report_progress(
                token, room_id, task_id, **body.model_dump()
            )

        @app.post(
            "/v1/rooms/{room_id}/tasks/{task_id}/finish",
            dependencies=[Depends(_reject_body)],
        )
        def finish_task(
            token: _Bearer, room_id: _Identifier, task_id: _Identifier
        ) -> dict:
            return collaboration.finish_task(token, room_id, task_id)

        @app.get(
            "/v1/rooms/{room_id}/tasks/{task_id}/events",
            dependencies=[Depends(_require_query_utf8)],
        )
        def task_history(
            token: _Bearer,
            room_id: _Identifier,
            task_id: _Identifier,
            after: _After = 0,
            limit: _Limit = 50,
        ) -> dict:
            return collaboration.task_history(token, room_id, task_id, after, limit)

        @app.get(
            "/v1/task-reminders",
            dependencies=[Depends(_require_query_utf8)],
        )
        def task_reminders(
            token: _Bearer, after: _After = 0, limit: _Limit = 50
        ) -> dict:
            return {"reminders": collaboration.task_reminders(token, after, limit)}

        @app.post(
            "/v1/task-reminders/{reminder_id}/claim",
            dependencies=[Depends(_reject_body)],
        )
        def claim_task_reminder(
            token: _Bearer, reminder_id: Annotated[int, Path(gt=0)]
        ) -> dict:
            return collaboration.claim_task_reminder(token, reminder_id)

        @app.post("/v1/dms", dependencies=[Depends(_require_utf8)])
        def open_dm(token: _Bearer, body: _OpenDM) -> dict:
            return collaboration.open_dm(token, body.member_id)

        @app.post("/v1/dms/{dm_id}/messages", dependencies=[Depends(_require_utf8)])
        def send_dm(token: _Bearer, dm_id: _Identifier, body: _DMMessage) -> dict:
            return collaboration.send_dm(
                token, dm_id, body.client_message_id, body.text
            )

        @app.get("/v1/dms/{dm_id}/messages")
        def dm_history(
            token: _Bearer, dm_id: _Identifier, after: _After = 0, limit: _Limit = 50
        ) -> dict:
            return collaboration.dm_history(token, dm_id, after, limit)

        @app.get("/v1/inbox", dependencies=[Depends(_require_query_utf8)])
        def inbox(
            token: _Bearer,
            after: _After = 0,
            limit: _Limit = 50,
            kinds: str | None = None,
        ) -> dict:
            return {
                "events": collaboration.inbox(token, after, limit, _event_kinds(kinds))
            }

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
