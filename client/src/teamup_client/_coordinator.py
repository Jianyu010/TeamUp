import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial

from ._config import Binding, owner_key
from ._desktop import PREVIEW_LIMIT, TITLE_LIMIT, clean
from ._harness import owned_socket, wake_prompt
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
    "mention_status": Operation(),
}
LOOKUP_TIMEOUT = 2
HARNESS_KINDS = ("claude-code", "codex")
SESSION_KEY = partial(is_string, limit=128)
TOKEN = partial(is_string, limit=4096)


def _uuids(value):
    return (
        isinstance(value, list)
        and len(value) <= 100
        and all(is_uuid(item) for item in value)
    )


IDENTITY = {"harness_instance_id": SESSION_KEY, "session_key": SESSION_KEY}
OWNER_OPERATIONS = {
    "sessions": Operation(),
    "decide_session": Operation(
        required={
            **IDENTITY,
            "state": lambda value: value in ("approved", "rejected"),
        }
    ),
    "set_wake": Operation(required={**IDENTITY, "rooms": _uuids, "senders": _uuids}),
}


@dataclass(frozen=True, eq=False)
class Lease:
    """Where a live session can be reached. Held in memory only."""

    kind: str
    address: str
    token: str | None = field(default=None, repr=False)


@dataclass
class Session:
    binding: Binding
    scope: str
    inbox_changed: asyncio.Event = field(default_factory=asyncio.Event)
    registered: asyncio.Event = field(default_factory=asyncio.Event)
    notices_changed: asyncio.Event = field(default_factory=asyncio.Event)
    harness_changed: asyncio.Event = field(default_factory=asyncio.Event)
    workers: list = field(default_factory=list)

    @property
    def identity(self):
        return self.binding.harness_instance_id, self.binding.session_key


def _valid(operation, params):
    return (
        operation.required.keys() <= params.keys()
        and params.keys() <= operation.required.keys() | operation.optional.keys()
        and all(
            validator(params[name])
            for name, validator in (operation.required | operation.optional).items()
            if name in params
        )
    )


def valid_session(value):
    return (
        value is None
        or isinstance(value, dict)
        and set(value) == {"id", "kind"}
        and SESSION_KEY(value["id"])
        and value["kind"] in HARNESS_KINDS
    )


class Coordinator:
    def __init__(self, config, upstream, notices, notice_output, notifier, drivers):
        self._server_url = config.server_url
        self._registration_token = config.account_token or config.enrollment_key
        self._legacy_enrollment = config.enrollment_key is not None
        self._owner_key = owner_key(config)
        self._upstream = upstream
        self._notices = notices
        self._notice_output = notice_output
        self._notifier = notifier
        self._drivers = drivers
        self._static = {
            binding.binding_key: (binding.harness_instance_id, binding.session_key)
            for binding in config.bindings
        }
        self._installs = {install.binding_key: install for install in config.installs}
        self._install_harnesses = {
            install.harness_instance_id for install in config.installs
        }
        self._leases = {}
        self._watching = False
        self._sessions = {}
        for binding in config.bindings:
            self._add_session(binding)
        for row in notices.sessions():
            if row["state"] == "approved" and (
                row["harness_instance_id"] in self._install_harnesses
            ):
                self._add_session(self._session_binding(row))

    # Identity ---------------------------------------------------------------

    @staticmethod
    def _session_binding(row):
        return Binding(
            binding_key="",
            member_token=row["member_token"],
            harness_instance_id=row["harness_instance_id"],
            session_key=row["session_key"],
        )

    def _add_session(self, binding):
        session = Session(binding, binding_scope(self._server_url, binding))
        self._sessions[session.identity] = session
        if self._watching:
            self._start(session)
        return session

    def _identity(self, key, session_ref):
        if key in self._static:
            return self._static[key], None
        install = self._installs.get(key)
        if install is None:
            raise ClientError("unauthorized", "Unknown local binding")
        if session_ref is None:
            raise ClientError("invalid_request", "This binding needs a session")
        return (install.harness_instance_id, session_ref["id"]), install

    async def _resolve(self, key, session_ref):
        identity, install = self._identity(key, session_ref)
        if session_ref is not None and session_ref["kind"] == "codex":
            # Codex sessions are addressed by thread and have no live connection.
            self._set_lease(identity, Lease("codex", session_ref["id"]))
        if install is None:
            return self._sessions[identity]
        row = self._notices.session(*identity)
        if row is None:
            state = "approved" if install.auto_approve else "pending"
            row = self._notices.add_session(*identity, secrets.token_urlsafe(32), state)
            if row["state"] == "pending":
                await self._notifier.show(
                    "A new agent session asks to join TeamUp",
                    identity[0],
                    "Approve: teamup-client sessions --config <path> approve "
                    f"{identity[0]} {identity[1]}",
                )
        if row["state"] == "rejected":
            raise ClientError("unauthorized", "The owner declined this session")
        if row["state"] == "pending":
            raise ClientError(
                "session_pending",
                "Ask the owner to run: teamup-client sessions --config <path> "
                f"approve {identity[0]} {identity[1]}",
            )
        return self._sessions.get(identity) or self._add_session(
            self._session_binding(row)
        )

    # Leases -----------------------------------------------------------------

    def _set_lease(self, identity, lease):
        current = self._leases.get(identity)
        if (
            current is not None
            and (current.kind, current.address)
            == (
                lease.kind,
                lease.address,
            )
            and current.token == lease.token
        ):
            return current
        self._leases[identity] = lease
        if session := self._sessions.get(identity):
            session.harness_changed.set()
        return lease

    def attach(self, key, session_ref, params):
        """Record a live Claude Code session. Returns a release callback."""
        if not (
            session_ref is not None
            and session_ref["kind"] == "claude-code"
            and set(params) == {"address", "token"}
            and TOKEN(params["token"])
            and owned_socket(params["address"])
        ):
            raise ClientError("invalid_request", "Invalid session attachment")
        identity, _ = self._identity(key, session_ref)
        lease = self._set_lease(
            identity, Lease("claude-code", params["address"], params["token"])
        )

        def release():
            if self._leases.get(identity) is lease:
                del self._leases[identity]

        return release

    # Calls ------------------------------------------------------------------

    async def execute(self, key, method, params, session_ref=None):
        if self._owner_key is not None and secrets.compare_digest(
            key.encode(), self._owner_key.encode()
        ):
            return self._owner(method, params)
        operation = OPERATIONS.get(method)
        if operation is None or not _valid(operation, params):
            self._identity(key, session_ref)
            raise ClientError("invalid_request", "Invalid method or parameters")
        session = await self._resolve(key, session_ref)
        binding = session.binding

        if method == "notifications":
            notices = self._notices.page(
                session.scope, params.get("after", 0), params.get("limit", 50)
            )
            self._notices.mark_delivered(
                session.scope, [notice["event_id"] for notice in notices]
            )
            return {"notifications": notices}
        if method == "mention_status":
            return {"unread_mentions": self._notices.unread(session.scope)}
        if method == "register":
            result = await self._register(binding, operation, params)
            session.registered.set()
            self._remember_members(session.scope, [result])
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
        elif method in ("create_room", "join_room"):
            self._remember_room(session.scope, result)
        elif method == "find_members" and isinstance(result.get("members"), list):
            self._remember_members(session.scope, result["members"])
        return result

    def _owner(self, method, params):
        operation = OWNER_OPERATIONS.get(method)
        if operation is None or not _valid(operation, params):
            raise ClientError("invalid_request", "Invalid owner method or parameters")
        if method == "sessions":
            return {"sessions": self._session_report()}
        identity = (params["harness_instance_id"], params["session_key"])
        if method == "decide_session":
            if identity[0] not in self._install_harnesses or not (
                self._notices.decide_session(*identity, params["state"])
            ):
                raise ClientError("not_found", "Unknown session")
            session = self._sessions.get(identity)
            if params["state"] == "approved" and session is None:
                self._add_session(
                    self._session_binding(
                        {
                            **self._notices.session(*identity),
                            "harness_instance_id": identity[0],
                            "session_key": identity[1],
                        }
                    )
                )
            elif params["state"] == "rejected" and session is not None:
                self._stop(self._sessions.pop(identity))
            return {"state": params["state"]}
        if identity not in self._sessions:
            raise ClientError("not_found", "Unknown or unapproved session")
        self._notices.set_wake(*identity, params["rooms"], params["senders"])
        self._sessions[identity].harness_changed.set()
        return {"rooms": sorted(params["rooms"]), "senders": sorted(params["senders"])}

    def _session_report(self):
        identities = {identity: "approved" for identity in self._static.values()} | {
            (row["harness_instance_id"], row["session_key"]): row["state"]
            for row in self._notices.sessions()
            if row["harness_instance_id"] in self._install_harnesses
        }
        report = []
        for identity, state in sorted(identities.items()):
            lease = self._leases.get(identity)
            report.append(
                {
                    "harness_instance_id": identity[0],
                    "session_key": identity[1],
                    "static": identity in self._static.values(),
                    "state": state,
                    "attached": lease.kind if lease else None,
                    "wake": self._notices.wake(*identity),
                }
            )
        return report

    def _remember_room(self, scope, room):
        if is_uuid(room.get("room_id")) and ROLE(room.get("name")):
            self._notices.remember_room(scope, room["room_id"], room["name"])

    def _remember_members(self, scope, members):
        self._notices.remember_members(
            scope,
            [
                member
                for member in members
                if isinstance(member, dict)
                and is_uuid(member.get("member_id"))
                and ROLE(member.get("role_description"))
            ],
        )

    async def _register(self, binding, operation, params):
        if not self._legacy_enrollment:
            try:
                return await self._upstream.request(
                    "GET", "/members/me", binding.member_token
                )
            except ClientError as exc:
                if exc.code != "not_registered":
                    raise
        if self._registration_token is None:
            raise ClientError(
                "unauthorized", "Run teamup-client login to register this binding"
            )
        return await self._upstream.request(
            operation.verb,
            operation.route,
            self._registration_token,
            body={
                **params,
                "harness_instance_id": binding.harness_instance_id,
                "session_key": binding.session_key,
                "member_token": binding.member_token,
            },
            member_auth=False,
        )

    # Workers ----------------------------------------------------------------

    def _start(self, session):
        session.workers = [
            asyncio.create_task(worker(session.binding, session))
            for worker in (self._watch, self._announce, self._deliver)
        ]

    @staticmethod
    def _stop(session):
        for worker in session.workers:
            worker.cancel()

    @asynccontextmanager
    async def watching_inboxes(self):
        self._watching = True
        for session in self._sessions.values():
            self._start(session)
        try:
            yield
        finally:
            self._watching = False
            workers = [
                worker
                for session in self._sessions.values()
                for worker in session.workers
            ]
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
                await self._drain(
                    binding.member_token,
                    session.scope,
                    (session.notices_changed, session.harness_changed),
                )
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

    async def _drain(self, token, scope, notices_changed):
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
                    text = event["message"].get("text")
                    preview = clean(text, PREVIEW_LIMIT) if BOARD_TEXT(text) else ""
                    if self._notices.record(scope, notice, preview):
                        self._notice_output.emit(notice)
                        for changed in notices_changed:
                            changed.set()
                await self._upstream.request("POST", f"/inbox/{event_id}/ack", token)
                after = event_id
            if len(events) < limit:
                return

    async def _announce(self, binding, session):
        """Show one desktop notice per recorded mention, in event order."""
        scope = session.scope
        while True:
            try:
                self._notices.abandon_unsettled(scope)
                while True:
                    session.notices_changed.clear()
                    while notice := self._notices.next_pending(scope):
                        title, subtitle, body = await self._describe(
                            binding, scope, notice
                        )
                        # Persist the attempt first so a crash never repeats it.
                        self._notices.begin_attempt(scope, notice["event_id"])
                        shown = await self._notifier.show(title, subtitle, body)
                        self._notices.settle(scope, notice["event_id"], shown)
                    await session.notices_changed.wait()
            except sqlite3.Error:
                await asyncio.sleep(1)

    async def _describe(self, binding, scope, notice):
        role, room = await self._names(binding, scope, notice)
        title = clean(f"{role} mentioned you", TITLE_LIMIT)
        return title, clean(room, TITLE_LIMIT), notice["preview"] or "New mention"

    async def _names(self, binding, scope, notice):
        sender = notice["sender_member_id"]
        role = self._notices.member_role(scope, sender)
        if role is None:
            try:
                async with asyncio.timeout(LOOKUP_TIMEOUT):
                    page = await self._upstream.request(
                        "GET", "/members", binding.member_token, query={"q": ""}
                    )
                if isinstance(page, dict) and isinstance(page.get("members"), list):
                    self._remember_members(scope, page["members"])
                    role = self._notices.member_role(scope, sender)
            except (ClientError, OSError, OverflowError, TimeoutError):
                pass
        room = self._notices.room_name(scope, notice["room_id"])
        return role or "A teammate", room or f"Room {notice['room_id'][:8]}"

    async def _deliver(self, binding, session):
        """Hand allowed mentions to the attached session, one at a time, in order."""
        scope = session.scope
        while True:
            try:
                self._notices.abandon_submitting(scope)
                while True:
                    session.harness_changed.clear()
                    blocked = False
                    for notice in self._notices.undecided(scope):
                        if not self._wake_allowed(session.identity, notice):
                            state = "not_allowed"
                        elif blocked or self._leases.get(session.identity) is None:
                            # Keep event order: later mentions wait behind this one.
                            state, blocked = "waiting", True
                        else:
                            state = await self._hand_off(binding, session, notice)
                        self._notices.set_harness_state(
                            scope, notice["event_id"], state
                        )
                    await session.harness_changed.wait()
            except sqlite3.Error:
                await asyncio.sleep(1)

    def _wake_allowed(self, identity, notice):
        allowed = self._notices.wake(*identity)
        return bool(
            allowed
            and notice["room_id"] in allowed["rooms"]
            and (
                not allowed["senders"]
                or notice["sender_member_id"] in allowed["senders"]
            )
        )

    async def _hand_off(self, binding, session, notice):
        lease = self._leases.get(session.identity)
        driver = self._drivers.get(lease.kind)
        if driver is None:
            return "failed"
        room = notice["room_id"]
        try:
            # Recheck access as this member before anything reaches the session.
            page = await self._upstream.request(
                "GET",
                f"/rooms/{room}/messages",
                binding.member_token,
                query={"after": notice["message_id"] - 1, "limit": 1},
            )
            board = await self._upstream.request(
                "GET", f"/rooms/{room}/board", binding.member_token
            )
        except (ClientError, OSError, OverflowError):
            return "failed"
        messages = page.get("messages") if isinstance(page, dict) else None
        message = next(
            (
                item
                for item in messages or []
                if isinstance(item, dict)
                and item.get("message_id") == notice["message_id"]
                and BOARD_TEXT(item.get("text"))
            ),
            None,
        )
        if message is None or not (
            isinstance(board, dict)
            and is_integer(board.get("version"), 1)
            and isinstance(board.get("rules"), str)
            and isinstance(board.get("protocol"), str)
        ):
            return "failed"
        role, room_name = await self._names(binding, session.scope, notice)
        prompt = wake_prompt(notice, role, room_name, message, board)
        self._notices.set_harness_state(session.scope, notice["event_id"], "submitting")
        submitted = await driver(lease.address, lease.token, prompt)
        if submitted is None:
            return "unknown"
        return "submitted" if submitted else "failed"


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
