import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial

from ._notices import binding_scope
from ._protocol import is_integer, is_string, is_uuid
from ._upstream import unavailable
from .ipc import ClientError

ROLE = partial(is_string, limit=200)
BOARD_TEXT = partial(is_string, limit=8_000)
MESSAGE_KEY = partial(is_string, limit=128)
SEARCH = partial(is_string, require_content=False)
AFTER = partial(is_integer, minimum=0)
POSITIVE = partial(is_integer, minimum=1)
LIMIT = partial(is_integer, minimum=1, maximum=100)
PAGE = {"after": AFTER, "limit": LIMIT}
ROOM = {"room_id": is_uuid}


def _mention(value):
    return value is None or is_uuid(value)


@dataclass(frozen=True)
class Operation:
    verb: str = ""
    route: str = ""
    required: dict = field(default_factory=dict)
    optional: dict = field(default_factory=dict)


OPERATIONS = {
    "register": Operation("POST", "/members", {"role_description": ROLE}),
    "find_members": Operation("GET", "/members", optional={"q": SEARCH}),
    "create_room": Operation(
        "POST", "/rooms", {"name": ROLE}, {"rules": BOARD_TEXT, "protocol": BOARD_TEXT}
    ),
    "join_room": Operation("POST", "/rooms/{room_id}/join", ROOM),
    "get_board": Operation("GET", "/rooms/{room_id}/board", ROOM),
    "ack_board": Operation(
        "POST", "/rooms/{room_id}/board/ack", {**ROOM, "version": POSITIVE}
    ),
    "send_message": Operation(
        "POST",
        "/rooms/{room_id}/messages",
        {**ROOM, "client_message_id": MESSAGE_KEY, "text": BOARD_TEXT},
        {"mention_member_id": _mention},
    ),
    "read_messages": Operation("GET", "/rooms/{room_id}/messages", ROOM, PAGE),
    "notifications": Operation(optional=PAGE),
}


@dataclass
class Session:
    scope: str
    inbox_changed: asyncio.Event = field(default_factory=asyncio.Event)
    registered: asyncio.Event = field(default_factory=asyncio.Event)


class Coordinator:
    def __init__(self, config, upstream, notices, notice_output):
        self._enrollment = config.enrollment_key
        self._upstream = upstream
        self._notices = notices
        self._notice_output = notice_output
        self._bindings = {binding.binding_key: binding for binding in config.bindings}
        self._sessions = {
            binding.binding_key: Session(binding_scope(config.server_url, binding))
            for binding in config.bindings
        }

    async def execute(self, key, method, params):
        binding = self._bindings.get(key)
        if binding is None:
            raise ClientError("unauthorized", "Unknown local binding")
        operation = OPERATIONS.get(method)
        if operation is None or not (
            operation.required.keys() <= params.keys()
            and params.keys() <= operation.required.keys() | operation.optional.keys()
            and all(
                validator(params[name])
                for name, validator in (operation.required | operation.optional).items()
                if name in params
            )
        ):
            raise ClientError("invalid_request", "Invalid method or parameters")
        session = self._sessions[key]

        if method == "notifications":
            notices = self._notices.page(
                session.scope, params.get("after", 0), params.get("limit", 50)
            )
            return {"notifications": notices}
        if method == "register":
            body = {
                **params,
                "harness_instance_id": binding.harness_instance_id,
                "session_key": binding.session_key,
                "member_token": binding.member_token,
            }
            result = await self._upstream.request(
                operation.verb,
                operation.route,
                self._enrollment,
                body=body,
                member_auth=False,
            )
            session.registered.set()
            return result

        path = operation.route.format(**params)
        fields = {name: value for name, value in params.items() if name != "room_id"}
        if operation.verb == "GET":
            result = await self._upstream.request(
                operation.verb, path, binding.member_token, query=fields
            )
        else:
            result = await self._upstream.request(
                operation.verb, path, binding.member_token, body=fields or None
            )
        if method == "ack_board":
            session.inbox_changed.set()
        return result

    @asynccontextmanager
    async def watching_inboxes(self):
        workers = [
            asyncio.create_task(self._watch(binding, self._sessions[key]))
            for key, binding in self._bindings.items()
        ]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _watch(self, binding, session):
        delay = 0.25
        while True:
            try:
                await self._upstream.request("GET", "/members/me", binding.member_token)
                async with self._upstream.events(binding.member_token) as lines:
                    await self._drain_with_hints(binding, session, lines)
                delay = 0.25
            except (ClientError, OSError, OverflowError, sqlite3.Error):
                pass
            try:
                async with asyncio.timeout(delay):
                    await session.registered.wait()
                    delay = 0.25
            except TimeoutError:
                delay = min(delay * 2, 5)
            session.registered.clear()

    async def _drain_with_hints(self, binding, session, lines):
        wake = session.inbox_changed
        reader = asyncio.create_task(self._read_hints(lines, wake))
        try:
            while True:
                wake.clear()
                await self._drain(binding.member_token, session.scope)
                if reader.done():
                    await reader
                    return
                await wake.wait()
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @staticmethod
    async def _read_hints(lines, wake):
        event = ""
        data = False
        try:
            async for line in lines:
                if not line:
                    if event == "inbox_changed" and data:
                        wake.set()
                    event, data = "", False
                elif line.startswith("event:"):
                    event = line.removeprefix("event:").removeprefix(" ")
                elif line.startswith("data:"):
                    data = True
        finally:
            wake.set()

    async def _drain(self, token, scope):
        after = 0
        limit = 100
        while True:
            page = await self._upstream.request(
                "GET", "/inbox", token, query={"after": after, "limit": limit}
            )
            events = page.get("events")
            if not isinstance(events, list) or len(events) > limit:
                raise unavailable()
            for event in events:
                if (
                    not isinstance(event, dict)
                    or not is_integer(event.get("event_id"), after + 1, 2**63 - 1)
                    or event.get("kind") not in ("message", "mention")
                ):
                    raise unavailable()
                event_id = event["event_id"]
                if event["kind"] == "mention":
                    notice = _notice(event)
                    if self._notices.record(scope, notice):
                        self._notice_output.emit(notice)
                await self._upstream.request("POST", f"/inbox/{event_id}/ack", token)
                after = event_id
            if len(events) < limit:
                return


def _notice(event):
    message = event.get("message")
    if (
        not isinstance(message, dict)
        or not is_uuid(message.get("room_id"))
        or not is_integer(message.get("message_id"), 1, 2**63 - 1)
        or not is_uuid(message.get("sender_member_id"))
    ):
        raise unavailable()
    return {
        "event_id": event["event_id"],
        "room_id": message["room_id"],
        "message_id": message["message_id"],
        "sender_member_id": message["sender_member_id"],
    }
