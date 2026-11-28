import asyncio
from dataclasses import dataclass, replace

import pytest
from teamup_client._console_data import ConsoleReader, ConsoleSession
from teamup_client._room_store import CommandScope, RoomScope, open_room_store
from teamup_client.ipc import ClientError

ROOM = "350050b8-8d5b-4a3e-9226-84aa8bb406b1"
MEMBER = "280babda-a01e-40ac-9f82-14a1c2b658fc"
MEMBERSHIP = "1234abcd" * 4
GENERATION = "first-generation"
SYNC = f"/rooms/{ROOM}/sync"
HISTORY = f"/rooms/{ROOM}/messages"
SCOPE = RoomScope("profile", GENERATION, "account", MEMBER, ROOM, MEMBERSHIP)


@dataclass(frozen=True)
class Binding:
    session_id: str = "console-id"
    member_id: str = MEMBER
    token: str = "member-token"
    server_profile: str = "profile"
    account_id: str = "account"
    revision: int = 1


def message(identifier):
    return {
        "message_id": identifier,
        "room_id": ROOM,
        "sender_member_id": MEMBER,
        "client_message_id": str(identifier),
        "text": f"Message {identifier}",
        "mention_member_id": None,
        "created_at": None,
    }


def change(collection, identifier, record):
    return {"collection": collection, "entity_id": str(identifier), "record": record}


class Server:
    def __init__(self):
        self.calls = []
        self.binding = Binding()
        self.messages = [message(1), message(2)]
        self.sender = "Reviewer"
        self.membership = MEMBERSHIP
        self.generation = GENERATION
        self.baseline_cursor = "baseline"
        self.board = 2
        self.acknowledged = 2
        self.response = None
        self.sync_error = None
        self.capability_error = None
        self.start = None
        self.proceed = None
        self.retired = False
        self.archive_during_history = False
        self.room_listed = True

    async def confirmed_binding(self, identity):
        return self.binding if self.binding.session_id == identity else None

    def binding_current(self, binding):
        return self.binding == binding

    def sessions(self):
        b = self.binding
        return (ConsoleSession(b.session_id, "codex", "review", b.token, b.revision),)

    def reader(self, store):
        from teamup_client._room_sync import RoomSync

        sync = RoomSync(store, self.confirmed_binding, self.binding_current)
        return ConsoleReader(self.sessions, self.request, room_sync=sync)

    def baseline(self):
        return {
            "has_more": False,
            "next_cursor": self.baseline_cursor,
            "changes": [
                change("room", ROOM, {"room_id": ROOM}),
                change("board", ROOM, {"room_id": ROOM, "version": self.board}),
                *(
                    change("messages", row["message_id"], row)
                    for row in self.messages[-100:]
                ),
            ],
            "coverage": {
                "room": {"has_more": False},
                "board": {"has_more": False},
                "roster": {"has_more": False},
                "tasks": {"has_more": False},
                "messages": {"has_older": len(self.messages) > 100},
            },
        }

    async def request(self, method, path, token, *, query=None):
        self.calls.append((path, query))
        assert method == "GET"
        assert token == self.binding.token
        if path == "/rooms":
            if not self.room_listed:
                return {"rooms": []}
            return {
                "rooms": [
                    {
                        "room_id": ROOM,
                        "membership_id": self.membership,
                        "current_board_version": self.board,
                        "acked_board_version": self.acknowledged,
                        "latest_message_id": self.messages[-1]["message_id"]
                        if self.messages
                        else None,
                    }
                ]
            }
        if path == "/sync/capabilities":
            if self.capability_error:
                raise ClientError(self.capability_error, "Capabilities unavailable")
            return {"version": 1, "server_generation": self.generation}
        if path == SYNC:
            if self.start:
                self.start.set()
                await self.proceed.wait()
            if self.sync_error:
                raise ClientError(self.sync_error, "Sync unavailable")
            if query.get("cursor") == "pruned":
                raise ClientError("sync_reset_required", "New baseline needed")
            response = self.response if query.get("cursor") else self.baseline()
            if callable(response):
                response = response(query["cursor"])
            return {
                "member_id": MEMBER,
                "room_id": ROOM,
                "membership_id": self.membership,
                "server_generation": self.generation,
                "board_version": self.board,
                **(
                    response
                    or {
                        "has_more": False,
                        "next_cursor": query["cursor"],
                        "changes": [],
                    }
                ),
            }
        if path == "/members":
            return {
                "members": []
                if self.retired
                else [
                    {
                        "member_id": MEMBER,
                        "role_description": self.sender,
                    }
                ]
            }
        if path == HISTORY:
            if self.archive_during_history:
                self.sync_error = "room_archived"
            rows = [
                {**item, **({"sender_retired": True} if self.retired else {})}
                for item in self.messages
            ]
            if "before" in query:
                return {
                    "messages": [r for r in rows if r["message_id"] < query["before"]][
                        -30:
                    ]
                }
            return {
                "messages": [r for r in rows if r["message_id"] > query["after"]][:30]
            }
        raise AssertionError(path)


@pytest.fixture
def socket_path(tmp_path):
    directory = tmp_path / "profile"
    directory.mkdir(mode=0o700)
    return directory / "client.sock"


def test_console_reopens_and_catches_up_multiple_deltas_without_history(socket_path):
    async def scenario():
        server = Server()
        with open_room_store(socket_path) as store:
            result = await server.reader(store).room(server.binding.session_id, ROOM)
            assert [row["id"] for row in result["messages"]] == ["1", "2"]
        server.sender = "Updated reviewer"
        server.messages = [message(2), message(3)]
        server.response = lambda cursor: {
            "baseline": {"has_more": True, "next_cursor": "empty-page", "changes": []},
            "empty-page": {
                "has_more": False,
                "next_cursor": "updated",
                "changes": [
                    change("messages", 1, None),
                    change("messages", 3, message(3)),
                ],
            },
        }[cursor]
        with open_room_store(socket_path) as store:
            result = await server.reader(store).room(server.binding.session_id, ROOM)
            assert [row["id"] for row in result["messages"]] == ["2", "3"]
            assert {row["sender"] for row in result["messages"]} == {"Updated reviewer"}
            assert store.load(SCOPE).cursor == "updated"
        assert not any(path == HISTORY for path, _ in server.calls)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code", ["board_ack_required", "membership_changed", "room_archived", "unavailable"]
)
def test_revalidation_error_keeps_the_cached_bodies_private(socket_path, code):
    async def scenario():
        server = Server()
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            assert (await reader.room(server.binding.session_id, ROOM))[
                "state"
            ] == "ready"
            server.sync_error = code
            result = await reader.room(server.binding.session_id, ROOM)
            assert result["state"] == code
            assert not result.get("messages")
            snapshot = store.load(SCOPE)
            if code == "unavailable":
                assert snapshot.cursor == "baseline"
            else:
                assert snapshot is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("member_id", "wrong-member"),
        ("membership_id", "wrong-membership"),
        ("room_id", "wrong-room"),
        ("board_version", 1),
        ("server_generation", "changed"),
    ],
)
def test_invalid_delta_never_releases_or_advances_projection(socket_path, field, value):
    async def scenario():
        server = Server()
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            await reader.room(server.binding.session_id, ROOM)
            before = store.load(SCOPE)
            server.response = {
                "has_more": False,
                "next_cursor": "bad",
                field: value,
                "changes": [change("messages", 3, message(3))],
            }
            result = await reader.room(server.binding.session_id, ROOM)
            assert result["state"] != "ready"
            assert not result.get("messages")
            assert store.load(SCOPE) == before

    asyncio.run(scenario())


def test_delayed_page_cannot_reappear_under_new_local_binding_revision(socket_path):
    async def scenario():
        server = Server()
        server.start, server.proceed = asyncio.Event(), asyncio.Event()
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            pending = asyncio.create_task(reader.room(server.binding.session_id, ROOM))
            await asyncio.wait_for(server.start.wait(), timeout=1)
            server.binding = replace(
                server.binding, revision=2, account_id="other-account"
            )
            server.proceed.set()
            result = await pending
            assert result == {"state": "not_found"}
            state = store.load(SCOPE)
            assert state is None or not state.records

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "membership", "blocked"),
    [("send_message", MEMBERSHIP, True), ("join_room", None, False)],
)
def test_missing_room_fences_joined_outbox_without_a_projection(
    socket_path, kind, membership, blocked
):
    async def scenario():
        server = Server()
        server.room_listed = False
        scope = CommandScope("profile", "account", MEMBER, ROOM)
        with open_room_store(socket_path) as store:
            command = store.stage_command(
                scope,
                GENERATION,
                membership,
                None,
                "intent",
                {
                    "operation_id": "operation",
                    "kind": kind,
                    "parameters": {"text": "Draft"},
                },
            )
            assert command is not None and store.load(SCOPE) is None
            result = await server.reader(store).room(server.binding.session_id, ROOM)
            assert result == {"state": "not_found"}
            stored = store.find_command(scope, kind, None, "intent")
            assert stored is not None
            assert stored.blocked is blocked
            assert (stored.request is None) is blocked

    asyncio.run(scenario())


def test_paging_outside_cache_and_retired_sender_keep_legacy_history_shape(socket_path):
    async def scenario():
        server = Server()
        server.messages = [message(identifier) for identifier in range(1, 104)]
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            result = await reader.room(server.binding.session_id, ROOM, before=8)
            assert [row["id"] for row in result["messages"]] == [
                str(i) for i in range(1, 8)
            ]
            server.retired = True
            result = await reader.room(server.binding.session_id, ROOM, after=102)
            assert result["messages"][0]["sender"] == f"Retired member ({MEMBER[:8]})"
            assert [query for path, query in server.calls if path == HISTORY] == [
                {"before": 8, "limit": 30, "membership_id": MEMBERSHIP},
                {"after": 102, "limit": 30, "membership_id": MEMBERSHIP},
            ]

    asyncio.run(scenario())


def test_only_unsupported_capability_falls_back_to_online_history(socket_path):
    async def scenario():
        server = Server()
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            server.capability_error = "not_found"
            result = await reader.room(server.binding.session_id, ROOM)
            assert result["state"] == "ready"
            assert [row["id"] for row in result["messages"]] == ["1", "2"]
            assert store.load(SCOPE) is None
            server.capability_error = "unavailable"
            result = await reader.room(server.binding.session_id, ROOM)
            assert result["state"] == "unavailable"
            assert not result.get("messages")
            assert sum(path == HISTORY for path, _ in server.calls) == 1

    asyncio.run(scenario())


def test_bounded_window_refills_when_an_old_upsert_cannot_fill_the_gap(socket_path):
    async def scenario():
        server = Server()
        server.messages = [message(identifier) for identifier in range(1, 106)]
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            await reader.room(server.binding.session_id, ROOM)
            server.messages = [row for row in server.messages if row["message_id"] != 6]
            server.baseline_cursor = "refilled"
            server.response = {
                "has_more": False,
                "next_cursor": "removed",
                "changes": [
                    change("messages", 6, None),
                    change("messages", 1, message(1)),
                ],
            }
            result = await reader.room(server.binding.session_id, ROOM)
            assert result["state"] == "ready"
            projection = store.load(SCOPE)
            assert projection.cursor == "refilled"
            cached = {
                int(row.entity_id)
                for row in projection.records
                if row.collection == "messages"
            }
            assert cached == {5, *range(7, 106)}

    asyncio.run(scenario())


def test_pruned_cursor_rebuilds_from_authorized_baseline(socket_path):
    async def scenario():
        server = Server()
        server.baseline_cursor = "pruned"
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            await reader.room(server.binding.session_id, ROOM)
            server.baseline_cursor = "fresh"
            server.messages = [message(3)]
            result = await reader.room(server.binding.session_id, ROOM)
            assert [row["id"] for row in result["messages"]] == ["3"]
            assert store.load(SCOPE).cursor == "fresh"

    asyncio.run(scenario())


def test_legacy_page_does_not_reopen_archived_live_view(socket_path):
    async def scenario():
        server = Server()
        server.messages = [message(identifier) for identifier in range(1, 105)]
        server.archive_during_history = True
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            result = await reader.room(server.binding.session_id, ROOM, before=3)
            assert result["state"] == "room_archived"
            assert result["messages"] == []
            assert store.load(SCOPE) is None

    asyncio.run(scenario())


def test_malformed_change_does_not_commit_other_changes_or_cursor(socket_path):
    async def scenario():
        server = Server()
        with open_room_store(socket_path) as store:
            reader = server.reader(store)
            await reader.room(server.binding.session_id, ROOM)
            before = store.load(SCOPE)
            server.response = {
                "has_more": False,
                "next_cursor": "malformed",
                "changes": [
                    change("messages", 3, message(3)),
                    change("messages", "not-a-message-id", None),
                ],
            }
            result = await reader.room(server.binding.session_id, ROOM)
            assert result["state"] == "unavailable"
            assert result["messages"] == []
            assert store.load(SCOPE) == before

    asyncio.run(scenario())
