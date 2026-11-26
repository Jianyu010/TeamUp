import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import partial

from ._config import Binding, owner_key
from ._console_data import ConsoleSession
from ._desktop import PREVIEW_LIMIT, TITLE_LIMIT, clean
from ._harness import owned_socket, wake_prompt
from ._notices import binding_scope
from ._protocol import is_integer, is_string, is_uuid
from ._task_notices import drain_task_reminders
from ._upstream import unavailable
from .ipc import ClientError

ROLE = partial(is_string, limit=200)
BOARD_TEXT = partial(is_string, limit=8_000)

MESSAGE_KEY = partial(is_string, limit=128)
SEARCH = partial(is_string, require_content=False)
QUERY = partial(is_string, limit=200)
AFTER = partial(is_integer, minimum=0)
BEFORE = partial(is_integer, minimum=1)
POSITIVE = partial(is_integer, minimum=1)
LIMIT = partial(is_integer, minimum=1, maximum=100)
PAGE = {"after": AFTER, "limit": LIMIT}
HISTORY = {"after": AFTER, "before": BEFORE, "limit": LIMIT}
ROOM = {"room_id": is_uuid}
TASK = {**ROOM, "task_id": is_uuid}
DM = {"dm_id": is_uuid}
HISTORY_POLICIES = ("retained", "since_joining")
TASK_STATUSES = ("open", "finished")
PRESENCE_INTERVAL = 15


def _profile_text(value, limit):
    return is_string(value, limit) and value == value.strip() and value.isprintable()


def _member_name(value):
    return _profile_text(value, 200) and len(value.split()) <= 3

def _membership_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _history_policy(value):
    return value in HISTORY_POLICIES


def _task_status(value):
    return value in TASK_STATUSES


def _mention(value):
    return value is None or is_uuid(value)


def _optional_text(value):
    return value is None or BOARD_TEXT(value)


def _addresses(value, *, minimum=0, maximum=100):
    return (
        isinstance(value, list)
        and minimum <= len(value) <= maximum
        and all(is_uuid(item) for item in value)
        and len({item.lower() for item in value}) == len(value)
    )


@dataclass(frozen=True)
class Operation:
    verb: str = ""
    route: str = ""
    required: dict = field(default_factory=dict)
    optional: dict = field(default_factory=dict)


OPERATIONS = {
    "register": Operation(
        "POST",
        "/members",
        optional={
            "name": _member_name,
            "description": partial(_profile_text, limit=160),
            "role_description": ROLE,
        },
    ),
    "retire_identity": Operation("POST", "/members/me/retire"),
    "find_members": Operation("GET", "/members", optional={"q": SEARCH}),
    "list_rooms": Operation("GET", "/rooms"),
    "create_room": Operation(
        "POST",
        "/rooms",
        {"name": ROLE},
        {
            "rules": BOARD_TEXT,
            "protocol": BOARD_TEXT,
            "history_policy": _history_policy,
        },
    ),
    "join_room": Operation("POST", "/rooms/{room_id}/join", ROOM),
    "leave_room": Operation("POST", "/rooms/{room_id}/leave", ROOM),
    "list_room_members": Operation("GET", "/rooms/{room_id}/members", ROOM),
    "remove_room_member": Operation(
        "POST",
        "/rooms/{room_id}/members/{member_id}/remove",
        {**ROOM, "member_id": is_uuid, "membership_id": _membership_id},
    ),
    "get_board": Operation("GET", "/rooms/{room_id}/board", ROOM),
    "ack_board": Operation(
        "POST", "/rooms/{room_id}/board/ack", {**ROOM, "version": POSITIVE}
    ),
    "send_message": Operation(
        "POST",
        "/rooms/{room_id}/messages",
        {**ROOM, "client_message_id": MESSAGE_KEY, "text": BOARD_TEXT},
        {
            "mention_member_id": _mention,
            "mention_member_ids": partial(_addresses, minimum=1, maximum=16),
        },
    ),
    "read_messages": Operation("GET", "/rooms/{room_id}/messages", ROOM, HISTORY),
    "search_room": Operation(
        "GET", "/rooms/{room_id}/search", {**ROOM, "q": QUERY}, PAGE
    ),
    "create_task": Operation(
        "POST",
        "/rooms/{room_id}/tasks",
        {**ROOM, "client_task_id": MESSAGE_KEY, "title": ROLE},
        {"description": _optional_text, "addressed_member_ids": _addresses},
    ),
    "list_tasks": Operation(
        "GET",
        "/rooms/{room_id}/tasks",
        ROOM,
        {**PAGE, "status": _task_status},
    ),
    "get_task": Operation("GET", "/rooms/{room_id}/tasks/{task_id}", TASK),
    "join_task": Operation("POST", "/rooms/{room_id}/tasks/{task_id}/join", TASK),
    "report_progress": Operation(
        "POST",
        "/rooms/{room_id}/tasks/{task_id}/progress",
        {**TASK, "client_progress_id": MESSAGE_KEY, "note": BOARD_TEXT},
    ),
    "finish_task": Operation("POST", "/rooms/{room_id}/tasks/{task_id}/finish", TASK),
    "task_history": Operation(
        "GET", "/rooms/{room_id}/tasks/{task_id}/events", TASK, PAGE
    ),
    "open_dm": Operation("POST", "/dms", {"member_id": is_uuid}),
    "send_dm": Operation(
        "POST",
        "/dms/{dm_id}/messages",
        {**DM, "client_message_id": MESSAGE_KEY, "text": BOARD_TEXT},
    ),
    "read_dm": Operation("GET", "/dms/{dm_id}/messages", DM, PAGE),
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
    "leave_session_room": Operation(required={**IDENTITY, **ROOM}),
    "retire_session": Operation(required=IDENTITY),
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
    presence_ready: asyncio.Event = field(default_factory=asyncio.Event)
    notices_changed: asyncio.Event = field(default_factory=asyncio.Event)
    harness_changed: asyncio.Event = field(default_factory=asyncio.Event)
    workers: list = field(default_factory=list)

    @property
    def identity(self):
        return self.binding.harness_instance_id, self.binding.session_key


def _valid(operation, params):
    if params.get("mention_member_id") is not None and "mention_member_ids" in params:
        return False
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
    def __init__(
        self, config, upstream, notices, notice_output, notifier, drivers, *,
        signed_in_console=False,
    ):
        self._server_url = config.server_url
        self._signed_in_console = signed_in_console and config.account_token is not None
        self._account_token = config.account_token
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
        self._codex_static = {
            (binding.codex_install, binding.session_key.lower()): self._static[binding.binding_key]
            for binding in config.bindings
            if binding.codex_install is not None
        }
        self._installs = {install.binding_key: install for install in config.installs}
        self._install_harnesses = {
            install.harness_instance_id for install in config.installs
        }
        self._leases = {}
        self._watching = False
        self._sessions = {}
        self._retired = notices.retired_sessions(self._server_url)
        self._pending_retirements: dict[tuple[str, str], tuple[Session, dict]] = {}
        for binding in config.bindings:
            if (binding.harness_instance_id, binding.session_key) not in self._retired:
                self._add_session(binding)
        for row in notices.sessions():
            identity = (row["harness_instance_id"], row["session_key"])
            if (
                row["state"] == "approved"
                and identity[0] in self._install_harnesses
                and identity not in self._retired
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
        if identity := self._static.get(key):
            if (
                session_ref is not None
                and session_ref["kind"] == "claude-code"
                and session_ref["id"] != identity[1]
            ):
                raise ClientError(
                    "unauthorized",
                    "This static binding belongs to a different Claude Code session. "
                    "Configure a per-session install with teamup-client init-install.",
                )
            return identity, None
        install = self._installs.get(key)
        if install is None:
            raise ClientError("unauthorized", "Unknown local binding")
        if session_ref is None:
            raise ClientError(
                "invalid_request", "This install needs a session ID from its harness"
            )
        identity = (install.harness_instance_id, session_ref["id"])
        if (
            session_ref["kind"] == "codex"
            and (static := self._codex_static.get((identity[0], identity[1].lower())))
            and identity not in self._retired
            and self._notices.session(*identity) is None
        ):
            return static, None
        return identity, install

    async def _resolve(self, identity, install, session_ref):
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
        if identity in self._retired or identity in self._pending_retirements:
            raise self._retired_error()
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
            return await self._owner(method, params)
        operation = OPERATIONS.get(method)
        if operation is None or not _valid(operation, params):
            self._identity(key, session_ref)
            raise ClientError("invalid_request", "Invalid method or parameters")
        if method == "register" and params.keys() not in (
            {"name", "description"}, {"role_description"}
        ):
            self._identity(key, session_ref)
            raise ClientError(
                "invalid_request",
                "Provide a name of up to three words and a one-line description.",
            )
        identity, install = self._identity(key, session_ref)
        if retired := self._retired.get(identity):
            if method == "retire_identity":
                return self._retirement_receipt(retired)
            raise self._retired_error()
        if pending := self._pending_retirements.get(identity):
            session, _ = pending
            if method == "retire_identity":
                return await self._retire(session)
            await self._sync_retirement(session)
            raise self._retired_error()
        session = await self._resolve(identity, install, session_ref)
        binding = session.binding
        if method == "retire_identity":
            return await self._retire(session)

        if method == "notifications":
            notices = self._notices.page(
                session.scope, params.get("after", 0), params.get("limit", 50)
            )
            self._notices.mark_delivered(
                session.scope, [notice["event_id"] for notice in notices]
            )
            return {"notifications": notices}
        if method == "mention_status":
            counts = {"unread_mentions": self._notices.unread(session.scope)}
            if departures := self._notices.unread_departures(session.scope):
                counts["unread_room_departures"] = departures
            return counts
        if method == "register":
            try:
                result = await self._register(binding, operation, params)
            except ClientError as exc:
                if exc.code == "member_retired":
                    await self._sync_retirement(session)
                raise
            session.registered.set()
            session.presence_ready.set()
            self._remember_members(session.scope, [result])
            return result

        path = operation.route.format(**params)
        fields = {
            name: value
            for name, value in params.items()
            if f"{{{name}}}" not in operation.route
        }
        if method == "list_room_members":
            fields["include_membership"] = "true"
        try:
            if operation.verb == "GET":
                result = await self._upstream.request(
                    operation.verb, path, binding.member_token, query=fields
                )
            else:
                result = await self._upstream.request(
                    operation.verb, path, binding.member_token, body=fields or None
                )
        except ClientError as exc:
            if exc.code == "not_registered" and await self._sync_retirement(session):
                raise self._retired_error() from None
            raise
        if identity in self._retired or identity in self._pending_retirements:
            raise self._retired_error()
        if method == "ack_board":
            session.inbox_changed.set()
        elif method in ("create_room", "join_room"):
            self._remember_room(session.scope, result)
        elif method == "find_members" and isinstance(result.get("members"), list):
            self._remember_members(session.scope, result["members"])
        return result

    async def _owner(self, method, params):
        operation = OWNER_OPERATIONS.get(method)
        if operation is None or not _valid(operation, params):
            raise ClientError("invalid_request", "Invalid owner method or parameters")
        if method == "sessions":
            return {"sessions": self._session_report()}
        identity = (params["harness_instance_id"], params["session_key"])
        if retired := self._retired.get(identity):
            if method == "retire_session":
                return self._retirement_receipt(retired)
            raise self._retired_error()
        if identity in self._pending_retirements and method != "retire_session":
            raise self._retired_error()
        if method in {"leave_session_room", "retire_session"}:
            return await self._session_action(identity, params.get("room_id"))
        if method == "decide_session":
            if identity[0] not in self._install_harnesses or not (
                self._notices.decide_session(*identity, params["state"])
            ):
                raise ClientError("not_found", "Unknown session")
            session = self._sessions.get(identity)
            if params["state"] == "approved" and session is None:
                row = self._notices.session(*identity)
                self._add_session(self._session_binding(row))
            elif params["state"] == "rejected" and session is not None:
                self._stop(self._sessions.pop(identity))
            return {"state": params["state"]}
        if identity not in self._sessions:
            raise ClientError("not_found", "Unknown or unapproved session")
        self._notices.set_wake(*identity, params["rooms"], params["senders"])
        self._sessions[identity].harness_changed.set()
        return {"rooms": sorted(params["rooms"]), "senders": sorted(params["senders"])}

    @staticmethod
    def _retired_error():
        return ClientError("member_retired", "This TeamUp identity has been retired")

    @staticmethod
    def _retirement_receipt(profile):
        return {
            "member_id": profile["member_id"],
            "role_description": profile["role_description"],
            "retired": True,
        }

    def _archive_session(self, session, receipt):
        if not (
            receipt.get("retired") is True
            and is_uuid(receipt.get("member_id"))
            and ROLE(receipt.get("role_description"))
        ):
            raise unavailable()
        identity = session.identity
        if identity not in self._retired:
            lease = self._leases.get(identity)
            harness = lease.kind if lease else identity[0]
            self._pending_retirements[identity] = session, receipt
            self._notices.retire_session(
                self._server_url, identity, session.scope, harness, receipt
            )
            del self._pending_retirements[identity]
            self._leases.pop(identity, None)
            self._retired[identity] = {
                **self._retirement_receipt(receipt),
                "scope": session.scope,
                "harness": harness,
            }
            if active := self._sessions.pop(identity, None):
                self._stop(active)
        return self._retirement_receipt(receipt)

    async def _sync_retirement(self, session):
        if session.identity in self._retired:
            return True
        try:
            pending = self._pending_retirements.get(session.identity)
            if pending is None:
                receipt = await self._upstream.request(
                    "GET", "/members/me/retirement", session.binding.member_token
                )
            else:
                _, receipt = pending
            self._archive_session(session, receipt)
        except ClientError:
            return False
        except sqlite3.Error:
            # The Server confirmed retirement. The watcher can retry the local archive.
            return True
        return True

    async def _retire(self, session):
        pending = self._pending_retirements.get(session.identity)
        if pending is None:
            receipt = await self._upstream.request(
                "POST", "/members/me/retire", session.binding.member_token
            )
        else:
            _, receipt = pending
        try:
            return self._archive_session(session, receipt)
        except sqlite3.Error:
            raise unavailable() from None

    async def _session_action(self, identity, room_id, membership_id=None):
        if pending := self._pending_retirements.get(identity):
            if room_id is not None:
                raise self._retired_error()
            session, _ = pending
            return await self._retire(session)
        session = self._sessions.get(identity)
        if session is None:
            raise ClientError("not_found", "Unknown or unapproved session")
        if room_id is None:
            return await self._retire(session)
        if membership_id is None:
            listing = await self._upstream.request(
                "GET", "/rooms", session.binding.member_token,
                query={"include_membership": "true"},
            )
            if self._sessions.get(identity) is not session:
                raise ClientError("not_found", "Unknown or unapproved session")
            room = next(
                (room for room in listing["rooms"] if room["room_id"] == room_id), None
            )
            if room is None:
                return {"room_id": room_id}
            membership_id = room.get("membership_id")
        query = {"notify": "true"}
        if membership_id is not None:
            query["membership_id"] = membership_id
        result = await self._upstream.request(
            "POST", f"/rooms/{room_id}/leave", session.binding.member_token, query=query
        )
        event_id = result.get("departure_event_id")
        if is_integer(event_id, 1, 2**63 - 1):
            self._record_departure(session.scope, event_id, room_id)
            if self._sessions.get(identity) is session:
                job = asyncio.create_task(self._deliver_departure(session, room_id))
                session.workers.append(job)
                job.add_done_callback(session.workers.remove)
        session.inbox_changed.set()
        return {"room_id": room_id}

    @staticmethod
    def _console_id(identity):
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def console_retired_sessions(self) -> list[dict]:
        return [
            {
                "id": self._console_id(identity),
                "label": identity[1],
                "harness": item["harness"],
                "member_id": item["member_id"],
                "role_description": item["role_description"],
            }
            for identity, item in sorted(self._retired.items())
        ]

    async def console_session_action(
        self, session_id: str, room_id: str | None, membership_id: str | None
    ) -> dict:
        if room_id is None:
            for identity, profile in self._retired.items():
                if self._console_id(identity) == session_id:
                    return {"state": "ready", **self._retirement_receipt(profile)}
        for identity in self._sessions.keys() | self._pending_retirements.keys():
            if self._console_id(identity) == session_id:
                result = await self._session_action(identity, room_id, membership_id)
                return {"state": "ready", **result}
        raise ClientError("not_found", "Unknown or unapproved session")

    async def console_retired_history(
        self,
        session_id: str,
        *,
        after: int | None = None,
        kind: str | None = None,
        conversation: str | None = None,
        before: int | None = None,
    ) -> dict:
        record = next(
            (row for identity, row in self._retired.items() if self._console_id(identity) == session_id),
            None,
        )
        if record is None:
            raise ClientError("not_found", "Unknown retired session")
        page_size = 30
        if after is not None:
            page = self._notices.page(record["scope"], after, page_size + 1)
            return {
                "state": "ready",
                "notifications": [
                    {name: str(value) if name in {"event_id", "message_id"} else value
                     for name, value in item.items()}
                    for item in page[:page_size]
                ],
                "next_after": str(page[page_size - 1]["event_id"]) if len(page) > page_size else None,
            }
        if self._account_token is None:
            raise ClientError("unauthorized", "Sign in to view archived conversations")
        path = f'/members/{record["member_id"]}/archive'
        if conversation is None:
            listing = await self._upstream.request(
                "GET", path, self._account_token, member_auth=False
            )
            return {"state": "ready", "conversations": listing["conversations"]}
        query = {"limit": page_size + 1}
        if before is not None:
            query["before"] = before
        result = await self._upstream.request(
            "GET", f"{path}/{kind}/{conversation}/messages", self._account_token,
            query=query, member_auth=False,
        )
        rows = result["messages"]
        page = rows[-page_size:]
        return {
            "state": "ready",
            "messages": [
                {**item, "message_id": str(item["message_id"])}
                for item in page
            ],
            "older": str(page[0]["message_id"]) if len(rows) > page_size else None,
        }

    async def console_wake_settings(self, enabled: bool | None = None) -> dict:
        if not self._signed_in_console:
            return {"state": "unavailable"}
        if enabled is None:
            enabled = self._notices.console_wake_enabled()
        else:
            self._notices.set_console_wake_enabled(enabled)
            for session in self._sessions.values():
                session.harness_changed.set()
        return {"state": "ready", "enabled": enabled}

    def console_sessions(self) -> tuple[ConsoleSession, ...]:
        """Expose approved sessions to the Client-owned read projection."""
        return tuple(
            ConsoleSession(
                self._console_id(identity),
                self._leases[identity].kind
                if identity in self._leases
                else identity[0],
                identity[1],
                session.binding.member_token,
            )
            for identity, session in sorted(self._sessions.items())
            if identity not in self._pending_retirements
        )

    def _session_report(self):
        identities = {identity: "approved" for identity in self._static.values()} | {
            (row["harness_instance_id"], row["session_key"]): row["state"]
            for row in self._notices.sessions()
            if row["harness_instance_id"] in self._install_harnesses
        }
        report = []
        identities.update(dict.fromkeys(self._retired, "retired"))
        identities.update(dict.fromkeys(self._pending_retirements, "retired"))
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
            for worker in (
                self._watch,
                self._announce,
                self._deliver,
                self._publish_presence,
            )
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
                session.presence_ready.set()
                async with self._upstream.events(binding.member_token) as lines:
                    await self._drain_with_hints(binding, session, lines)
                delay = 0.25
            except ClientError as exc:
                if exc.code == "not_registered":
                    await self._sync_retirement(session)
                    if session.identity in self._retired:
                        return
            except (OSError, OverflowError, sqlite3.Error):
                pass
            try:
                async with asyncio.timeout(delay):
                    await session.registered.wait()
                    delay = 0.25
            except TimeoutError:
                delay = min(delay * 2, 5)
            session.registered.clear()

    async def _publish_presence(self, binding, session):
        ready = session.presence_ready
        loop = asyncio.get_running_loop()
        await ready.wait()
        while True:
            ready.clear()
            renew_at = loop.time() + PRESENCE_INTERVAL
            try:
                await self._upstream.request(
                    "POST", "/presence/heartbeat", binding.member_token
                )
            except ClientError as exc:
                if exc.code in ("not_registered", "unauthorized"):
                    # A new successful member check or registration must rearm it.
                    await ready.wait()
                    continue
            except OSError:
                pass
            await asyncio.sleep(max(0, renew_at - loop.time()))

    async def _drain_with_hints(self, binding, session, lines):
        wake = session.inbox_changed
        reminder_wake = asyncio.Event()
        reader = asyncio.create_task(self._read_hints(lines, wake, reminder_wake))
        reminders = asyncio.create_task(
            self._watch_task_reminders(binding, reminder_wake)
        )
        try:
            while True:
                wake.clear()
                await self._drain(binding.member_token, session)
                if reader.done():
                    await reader
                    return
                await wake.wait()
        finally:
            reader.cancel()
            reminders.cancel()
            await asyncio.gather(reader, reminders, return_exceptions=True)

    async def _watch_task_reminders(self, binding, wake):
        while True:
            wake.clear()
            try:
                if not await drain_task_reminders(
                    self._upstream.request, binding.member_token, self._notifier.show
                ):
                    return
            except (ClientError, OSError, OverflowError):
                with suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=5)
            else:
                await wake.wait()

    @staticmethod
    async def _read_hints(lines, *wakes):
        event = ""
        data = False
        try:
            async for line in lines:
                if not line:
                    if event == "inbox_changed" and data:
                        for wake in wakes:
                            wake.set()
                    event, data = "", False
                elif line.startswith("event:"):
                    event = line.removeprefix("event:").removeprefix(" ")
                elif line.startswith("data:"):
                    data = True
        finally:
            for wake in wakes:
                wake.set()

    async def _drain(self, token, session):
        scope = session.scope
        after = 0
        limit = 100
        kinds = "message,mention,dm,room_left"
        while True:
            try:
                page = await self._upstream.request(
                    "GET", "/inbox", token, query={"after": after, "limit": limit, "kinds": kinds}
                )
            except ClientError as exc:
                if exc.code == "invalid_request" and kinds.endswith(",room_left"):
                    # Servers from before the lifecycle upgrade still serve these kinds.
                    kinds = "message,mention,dm"
                    continue
                raise
            events = page.get("events")
            if not isinstance(events, list) or len(events) > limit:
                raise unavailable()
            for event in events:
                if (
                    not isinstance(event, dict)
                    or not is_integer(event.get("event_id"), after + 1, 2**63 - 1)
                    or event.get("kind") not in ("message", "mention", "dm", "room_left")
                ):
                    raise unavailable()
                event_id = event["event_id"]
                if event["kind"] == "room_left":
                    room_id = event.get("room_id")
                    if not is_uuid(room_id):
                        raise unavailable()
                    self._record_departure(scope, event_id, room_id)
                elif event["kind"] == "dm":
                    # A DM notice is typed and body-free: no desktop or wake
                    # pipeline, and no session authorization change.
                    notice = _dm_notice(event)
                    if self._notices.record(scope, notice):
                        self._notice_output.emit(notice)
                elif event["kind"] == "mention":
                    notice = _notice(event)
                    text = event["message"].get("text")
                    preview = clean(text, PREVIEW_LIMIT) if BOARD_TEXT(text) else ""
                    if self._notices.record(scope, notice, preview):
                        self._notice_output.emit(notice)
                        session.notices_changed.set()
                        session.harness_changed.set()
                try:
                    await self._upstream.request(
                        "POST", f"/inbox/{event_id}/ack", token
                    )
                except ClientError as exc:
                    # An expired event is already gone; keep draining the rest.
                    if exc.code != "not_found":
                        raise
                after = event_id
            if len(events) < limit:
                return

    def _record_departure(self, scope, event_id, room_id):
        if self._notices.record_departure(scope, event_id, room_id):
            self._notice_output.emit(
                {"kind": "room_left", "event_id": event_id, "room_id": room_id}
            )

    async def _deliver_departure(self, session, room_id):
        lease = self._leases.get(session.identity)
        if lease is None:
            return
        driver = self._drivers.get("codex-notice" if lease.kind == "codex" else lease.kind)
        if driver is not None:
            await driver(
                lease.address, lease.token,
                f"TeamUp: Your owner removed this session from room {room_id}.",
            )

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
                    pending = self._notices.undecided(scope)
                    blocked = False
                    for notice in pending:
                        if not self._wake_allowed(session, notice):
                            state = "not_allowed"
                        elif blocked or self._leases.get(session.identity) is None:
                            state = "waiting"
                        else:
                            state = await self._hand_off(binding, session, notice)
                        if state == "waiting":
                            # Keep event order: later mentions wait behind this one.
                            blocked = True
                        self._notices.set_harness_state(
                            scope, notice["event_id"], state
                        )
                    if blocked or not pending:
                        await session.harness_changed.wait()
            except sqlite3.Error:
                await asyncio.sleep(1)

    def _wake_allowed(self, session, notice):
        identity = session.identity
        if self._sessions.get(identity) is not session or identity in self._pending_retirements:
            return False
        allowed = self._notices.wake(*identity)
        if allowed is None:
            return self._signed_in_console and self._notices.console_wake_enabled()
        return notice["room_id"] in allowed["rooms"] and (
            not allowed["senders"] or notice["sender_member_id"] in allowed["senders"]
        )

    async def _hand_off(self, binding, session, notice):
        lease = self._leases.get(session.identity)
        driver = self._drivers.get(lease.kind)
        if driver is None:
            return "failed"
        room = notice["room_id"]
        role, room_name = await self._names(binding, session.scope, notice)
        try:
            # Read the message and acknowledged board under the same membership.
            page = await self._upstream.request(
                "GET",
                f"/rooms/{room}/messages",
                binding.member_token,
                query={"after": notice["message_id"] - 1, "limit": 1, "delivery": "true"},
            )
            board = page.get("delivery_board")
            if board is None:
                # Servers without the lifecycle API still use the separate board read.
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
        if not self._wake_allowed(session, notice):
            return "not_allowed"
        if self._leases.get(session.identity) is not lease:
            return "waiting"
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


def _dm_notice(event):
    message = event.get("message")
    if (
        not isinstance(message, dict)
        or not is_uuid(message.get("dm_id"))
        or not is_integer(message.get("message_id"), 1, 2**63 - 1)
        or not is_uuid(message.get("sender_member_id"))
    ):
        raise unavailable()
    return {
        "event_id": event["event_id"],
        "kind": "dm",
        "dm_id": message["dm_id"],
        "message_id": message["message_id"],
        "sender_member_id": message["sender_member_id"],
    }
