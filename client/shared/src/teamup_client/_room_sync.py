"""Maintain a bounded, Server-authorized projection of a live room."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from weakref import WeakValueDictionary

from ._protocol import is_integer, is_string, is_uuid
from ._room_store import (
    CommandScope,
    RoomChange,
    RoomProjection,
    RoomScope,
    RoomStore,
    StoredCommand,
)
from ._upstream import unavailable
from .ipc import ClientError

WINDOW = 100
WINDOW_FLAGS = {"messages": "has_older", "roster": "has_more", "tasks": "has_more"}
COLLECTIONS = {"room", "board", *WINDOW_FLAGS}
METADATA = ("_sync", "coverage")
ACCESS_LOST = {
    "not_found",
    "not_registered",
    "unauthorized",
    "member_identity_changed",
    "membership_changed",
    "room_archived",
    "board_ack_required",
}

RoomRead = Callable[[str, dict | None], Awaitable[dict]]


class ConfirmedRoomBinding(Protocol):
    @property
    def member_id(self) -> str: ...

    @property
    def token(self) -> str: ...

    @property
    def server_profile(self) -> str: ...

    @property
    def account_id(self) -> str: ...


async def room_sync_generation(read: RoomRead) -> str | None:
    try:
        result = await read("/sync/capabilities", None)
    except ClientError as error:
        if error.code == "not_found":
            return None
        raise
    generation = result.get("server_generation")
    if (
        not is_integer(result.get("version"), 1, 1)
        or not isinstance(generation, str)
        or not is_string(generation)
    ):
        raise unavailable()
    return generation


@dataclass(eq=False)
class RoomAccess:
    binding: ConfirmedRoomBinding
    session_id: str
    room_id: str
    current: Callable[[ConfirmedRoomBinding], bool]
    pending: StoredCommand | None = None
    valid: bool = True

    def check(self) -> None:
        if not self.valid or not self.current(self.binding):
            raise ClientError("not_found", "Local room scope is unavailable")


@dataclass(frozen=True)
class RoomView:
    scope: RoomScope
    revision: str
    board_version: int
    messages: list[dict]
    has_older: bool
    expiry_reported: bool

    def page(self, query: dict) -> dict | None:
        # The legacy history endpoint computes its retention gap for each cursor.
        if self.expiry_reported:
            return None
        limit = query["limit"]
        if "after" in query:
            after = query["after"]
            if self.has_older and after < self.messages[0]["message_id"] - 1:
                return None
            return {
                "messages": [row for row in self.messages if row["message_id"] > after][
                    :limit
                ]
            }
        messages = [row for row in self.messages if row["message_id"] < query["before"]]
        if self.has_older and len(messages) < limit:
            return None
        return {"messages": messages[-limit:]}


def _identity(binding: ConfirmedRoomBinding) -> tuple[str, str, str]:
    return binding.server_profile, binding.account_id, binding.member_id


def _window_order(change: RoomChange) -> tuple[int, str]:
    record = change.record
    assert record is not None
    number_field = {"messages": "message_id", "tasks": "task_number"}.get(
        change.collection
    )
    if number_field is None:
        return 0, change.entity_id
    number = record[number_field]
    assert isinstance(number, int)
    return -number, change.entity_id


def _validate_message(record: dict, identifier: str) -> bool:
    targets = record.get("mention_member_ids", [])
    return (
        is_integer(record.get("message_id"), 1)
        and str(record["message_id"]) == identifier
        and is_uuid(record.get("sender_member_id"))
        and is_string(record.get("text"))
        and "mention_member_id" in record
        and (
            record["mention_member_id"] is None or is_uuid(record["mention_member_id"])
        )
        and isinstance(targets, list)
        and all(is_uuid(target) for target in targets)
    )


def _parse_change(
    value: object, scope: RoomScope, version: int, *, baseline: bool
) -> RoomChange:
    if not isinstance(value, dict):
        raise unavailable()
    collection, identifier = value.get("collection"), value.get("entity_id")
    if (
        not isinstance(collection, str)
        or collection not in COLLECTIONS
        or not isinstance(identifier, str)
        or not is_string(identifier)
    ):
        raise unavailable()
    if collection == "messages":
        valid_id = (
            identifier.isascii()
            and identifier.isdecimal()
            and not identifier.startswith("0")
        )
    elif collection in {"roster", "tasks"}:
        valid_id = is_uuid(identifier)
    else:
        valid_id = identifier == scope.room_id
    if not valid_id:
        raise unavailable()
    record = value.get("record")
    if record is None:
        if "record" not in value or baseline or collection in {"room", "board"}:
            raise unavailable()
        return RoomChange(collection, identifier, None)
    if not isinstance(record, dict):
        raise unavailable()
    if collection != "roster" and record.get("room_id") != scope.room_id:
        raise unavailable()
    if collection == "messages":
        valid = _validate_message(record, identifier)
    elif collection == "roster":
        valid = record.get("member_id") == identifier
    elif collection == "tasks":
        valid = record.get("task_id") == identifier and is_integer(
            record.get("task_number"), 1
        )
        # Task stall status is observed through the ordinary authorized task read.
        record = {key: field for key, field in record.items() if key != "stalled_since"}
    else:
        valid = collection == "room" or is_integer(
            record.get("version"), version, version
        )
    if not valid:
        raise unavailable()
    return RoomChange(collection, identifier, record)


def _metadata(projection: RoomProjection) -> dict:
    return next(
        (
            dict(row.record)
            for row in projection.records
            if (row.collection, row.entity_id) == METADATA and row.record is not None
        ),
        {},
    )


def _coverage(response: dict) -> dict[str, bool]:
    entries = response.get("coverage")
    if not isinstance(entries, dict):
        raise unavailable()
    flags = {}
    for collection, field in WINDOW_FLAGS.items():
        entry = entries.get(collection)
        if not isinstance(entry, dict) or type(entry.get(field)) is not bool:
            raise unavailable()
        flags[collection] = entry[field]
    return flags


def _page_records(
    response: dict,
    scope: RoomScope,
    version: int,
    previous: RoomProjection,
    *,
    baseline: bool,
) -> tuple[dict[tuple[str, str], RoomChange], bool]:
    values = response.get("changes")
    if not isinstance(values, list):
        raise unavailable()
    old_metadata = _metadata(previous)
    flags = _coverage(response) if baseline else dict(old_metadata["coverage"])
    rows = (
        {}
        if baseline
        else {(row.collection, row.entity_id): row for row in previous.records}
    )
    boundaries = {
        collection: max(
            _window_order(row) for row in rows.values() if row.collection == collection
        )
        for collection in WINDOW_FLAGS
        if not baseline and flags[collection]
    }
    for value in values:
        change = _parse_change(value, scope, version, baseline=baseline)
        identity = change.collection, change.entity_id
        if change.record is None:
            rows.pop(identity, None)
        elif (
            change.collection not in boundaries
            or _window_order(change) <= boundaries[change.collection]
        ):
            rows[identity] = change

    if not all((collection, scope.room_id) in rows for collection in ("room", "board")):
        raise unavailable()
    partial = False
    for collection in WINDOW_FLAGS:
        window = [row for row in rows.values() if row.collection == collection]
        if len(window) > WINDOW:
            window.sort(key=_window_order)
            for row in window[WINDOW:]:
                del rows[(row.collection, row.entity_id)]
            flags[collection] = True
        partial |= flags[collection] and len(window) < WINDOW
    if partial and baseline:
        raise unavailable()
    expiry = response.get("expired_through_message_id")
    if "expired_through_message_id" in response and (
        not is_integer(expiry, 1) or type(response.get("history_truncated")) is not bool
    ):
        raise unavailable()
    rows[METADATA] = RoomChange(
        *METADATA,
        {
            "coverage": flags,
            "board_version": version,
            "partial": partial,
            "expiry_reported": expiry is not None
            or (not baseline and old_metadata["expiry_reported"]),
        },
    )
    return rows, partial


class RoomSync:
    def __init__(
        self,
        store: RoomStore,
        confirmed_binding: Callable[[str], Awaitable[ConfirmedRoomBinding | None]],
        binding_current: Callable[[ConfirmedRoomBinding], bool],
    ):
        self._store = store
        self._confirmed_binding = confirmed_binding
        self._binding_current = binding_current
        self._locks: WeakValueDictionary[tuple[str, str, str, str], asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._active: set[RoomAccess] = set()

    @asynccontextmanager
    async def open(
        self, session: str, token: str, room_id: str
    ) -> AsyncIterator[RoomAccess]:
        binding = await self._confirmed_binding(session)
        if binding is None or binding.token != token:
            raise ClientError("not_found", "Local session is unavailable")
        lock = self._locks.setdefault((*_identity(binding), room_id), asyncio.Lock())
        async with lock:
            pending = self._store.pending_command(
                CommandScope(*_identity(binding), room_id)
            )
            access = RoomAccess(
                binding, session, room_id, self._binding_current, pending
            )
            access.check()
            self._active.add(access)
            try:
                yield access
            finally:
                self._active.remove(access)

    def invalidate(
        self, binding: ConfirmedRoomBinding, room_id: str | None = None
    ) -> tuple[RoomScope, ...]:
        identity = _identity(binding)
        for access in self._active:
            if _identity(access.binding) == identity and (
                room_id is None or access.room_id == room_id
            ):
                access.valid = False
        return self._store.invalidate_room(*identity, room_id)

    def room_missing(self, access: RoomAccess) -> None:
        access.check()
        self.invalidate(access.binding, access.room_id)
        pending = access.pending
        if (
            pending is not None
            and pending.membership_id is not None
            and pending.request is not None
            and pending.request["kind"] not in {"join_room", "leave_room"}
        ):
            self._store.block_command(pending.operation_id, redact=True)

    def access_error(
        self,
        access: RoomAccess,
        error: ClientError,
        *,
        membership_id: str | None = None,
    ) -> None:
        # A failed guard may describe an earlier operation already invalidated locally.
        if (
            error.code in ACCESS_LOST
            and access.valid
            and self._binding_current(access.binding)
        ):
            if error.code == "room_archived" or (
                error.code == "membership_changed" and membership_id is not None
            ):
                self._store.invalidate_commands(
                    *_identity(access.binding),
                    room_id=access.room_id,
                    membership_id=membership_id
                    if error.code == "membership_changed"
                    else None,
                )
            self.invalidate(access.binding, access.room_id)

    async def refresh(
        self, access: RoomAccess, selected: dict, read: RoomRead
    ) -> RoomView | None:
        generation = await room_sync_generation(read)
        if generation is None:
            return None
        membership = selected.get("membership_id")
        version = selected.get("current_board_version")
        if (
            not isinstance(membership, str)
            or not is_string(membership)
            or type(version) is not int
            or not is_integer(version, 1)
        ):
            raise unavailable()
        if not is_integer(selected.get("acked_board_version"), version, version):
            raise ClientError(
                "board_ack_required",
                "Confirm the current board before exchanging messages",
            )
        profile, account, member = _identity(access.binding)
        room = access.room_id
        scope = RoomScope(profile, generation, account, member, room, membership)
        view = await self._catch_up(access, scope, version, read)
        for previous in self._store.invalidate_room(
            profile, account, member, room, keep=scope
        ):
            if (
                previous.server_generation == generation
                and previous.membership_id != membership
            ):
                self._store.invalidate_commands(
                    profile,
                    account,
                    member,
                    room_id=room,
                    membership_id=previous.membership_id,
                )
        return view

    async def _catch_up(
        self, access: RoomAccess, scope: RoomScope, version: int, read: RoomRead
    ) -> RoomView:
        self._store.begin(scope)
        snapshot = self._store.load(scope)
        assert snapshot is not None
        metadata = _metadata(snapshot)
        cursor = (
            snapshot.cursor
            if metadata.get("board_version") == version and not metadata.get("partial")
            else None
        )
        # Each successful page consumes a bounded Server journal page or replaces the baseline.
        reset = False
        while True:
            query = {
                "expected_member_id": scope.member_id,
                "membership_id": scope.membership_id,
            }
            if cursor is not None:
                query["cursor"] = cursor
            try:
                response = await read(f"/rooms/{scope.room_id}/sync", query)
            except ClientError as error:
                if error.code != "sync_reset_required" or cursor is None or reset:
                    raise
                cursor, reset = None, True
                continue
            next_cursor, more = response.get("next_cursor"), response.get("has_more")
            if (
                response.get("server_generation") != scope.server_generation
                or response.get("member_id") != scope.member_id
                or response.get("room_id") != scope.room_id
                or response.get("membership_id") != scope.membership_id
                or not is_integer(response.get("board_version"), version, version)
                or not isinstance(next_cursor, str)
                or not is_string(next_cursor)
                or type(more) is not bool
                or (more and (cursor is None or cursor == next_cursor))
            ):
                raise unavailable()
            rows, partial = _page_records(
                response, scope, version, snapshot, baseline=cursor is None
            )
            access.check()
            revision = self._save(scope, snapshot, cursor, next_cursor, rows)
            if not more and not partial:
                state = rows[METADATA].record
                assert state is not None
                flags = state["coverage"]
                assert isinstance(flags, dict)
                messages = sorted(
                    (row for row in rows.values() if row.collection == "messages"),
                    key=_window_order,
                    reverse=True,
                )
                return RoomView(
                    scope,
                    revision,
                    version,
                    [dict(row.record) for row in messages if row.record is not None],
                    flags["messages"],
                    state["expiry_reported"] is True,
                )
            snapshot = RoomProjection(revision, next_cursor, tuple(rows.values()))
            cursor = None if partial else next_cursor

    def _save(
        self,
        scope: RoomScope,
        snapshot: RoomProjection,
        cursor: str | None,
        next_cursor: str,
        rows: dict[tuple[str, str], RoomChange],
    ) -> str:
        if cursor is None:
            revision = self._store.replace(
                scope, snapshot.revision, next_cursor, rows.values()
            )
        else:
            previous = {
                (row.collection, row.entity_id): row for row in snapshot.records
            }
            changes = [row for key, row in rows.items() if previous.get(key) != row]
            changes.extend(
                RoomChange(*key, None) for key in previous.keys() - rows.keys()
            )
            if not changes and cursor == next_cursor:
                return snapshot.revision
            revision = self._store.apply(scope, snapshot.revision, next_cursor, changes)
        if revision is None:
            raise unavailable()
        return revision

    async def revalidate_history(
        self, access: RoomAccess, view: RoomView, read: RoomRead
    ) -> None:
        refreshed = await self._catch_up(access, view.scope, view.board_version, read)
        if refreshed.revision != view.revision:
            raise unavailable()

    async def revalidate_legacy_binding(self, access: RoomAccess) -> None:
        if await self._confirmed_binding(access.session_id) != access.binding:
            raise ClientError("not_found", "Local session is unavailable")
        access.check()

    def check_view(self, access: RoomAccess, view: RoomView | None) -> None:
        access.check()
        if view is not None and not self._store.is_current(view.scope, view.revision):
            raise unavailable()
