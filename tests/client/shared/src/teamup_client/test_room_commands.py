import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from uuid import uuid4

import pytest
from teamup_client._room_store import open_room_store
from teamup_client.ipc import ClientError

ROOM = "00000000-0000-4000-8000-000000000001"
MEMBER = "00000000-0000-4000-8000-000000000002"
TASK = "00000000-0000-4000-8000-000000000003"
PARAMS = {"room_id": ROOM, "client_message_id": "send-1", "text": "Private draft"}


@dataclass(frozen=True)
class Binding:
    server_profile: str = "profile"
    account_id: str = "account"
    member_id: str = MEMBER
    token: str = "token"


class Server:
    def __init__(self):
        self.valid_binding = True
        self.generation = "generation-1"
        self.membership = "1" * 32
        self.version = 5
        self.membership_revision = 1
        self.commands = ["send_message", "create_task", "acknowledge_board"]
        self.attempts = []
        self.receipts = {}
        self.messages = []
        self.hydrations = []
        self.fail_after_commit = False
        self.fail_before_commit = False
        self.deny = False
        self.denial_code = "forbidden"
        self.revoke_after_commit = False
        self.restore_on_hydration = False
        self.task = {"room_id": ROOM, "task_id": TASK, "title": "Private task"}

    async def request(self, method, path, token, *, query=None, body=None):
        assert token == "token"
        if (method, path) == ("GET", "/sync/capabilities"):
            if self.commands is None:
                raise ClientError("not_found", "old Server")
            return {
                "version": 1,
                "server_generation": self.generation,
                "commands": self.commands,
            }
        if (method, path) == ("GET", f"/rooms/{ROOM}/membership-state"):
            assert query == {"expected_member_id": MEMBER}
            return {
                "server_generation": self.generation,
                "member_id": MEMBER,
                "room_id": ROOM,
                "membership_id": self.membership,
                "membership_revision": self.membership_revision,
            }
        if (method, path) == ("GET", "/rooms"):
            assert query == {"include_membership": "true"}
            return {
                "rooms": [
                    {
                        "room_id": ROOM,
                        "membership_id": self.membership,
                        "current_board_version": self.version,
                        "acked_board_version": self.version,
                    }
                ]
            }
        if method == "GET":
            self.hydrations.append((path, query))
            if path == f"/rooms/{ROOM}":
                return {"room_id": ROOM, "name": "Recovery"}
            if path == f"/rooms/{ROOM}/messages":
                if self.restore_on_hydration:
                    self.generation = "restored"
                return {
                    "messages": [
                        row
                        for row in self.messages
                        if row["message_id"] > query["after"]
                    ][: query["limit"]]
                }
            assert path == f"/rooms/{ROOM}/tasks/{TASK}"
            return self.task
        assert (method, path) == ("POST", "/room-commands")
        self.attempts.append(body)
        if self.fail_before_commit:
            self.fail_before_commit = False
            raise ClientError("unavailable", "request dropped")
        if self.deny:
            raise ClientError(self.denial_code, "server denial")
        operation = body["operation_id"]
        if operation not in self.receipts:
            kind = body["kind"]
            receipt = {"kind": kind, "room_id": ROOM}
            if kind == "send_message":
                fields = body["parameters"]
                message_id = len(self.messages) + 1
                self.messages.append(
                    {
                        "room_id": ROOM,
                        "message_id": message_id,
                        "sender_member_id": MEMBER,
                        "client_message_id": fields["client_message_id"],
                        "text": fields["text"],
                        "mention_member_id": None,
                        "created_at": None,
                    }
                )
                receipt["message_id"] = message_id
            elif kind in {"create_task", "record_task_progress"}:
                receipt["task_id"] = TASK
            elif kind == "join_room":
                assert (
                    body["parameters"]["membership_revision"]
                    == self.membership_revision
                )
                if self.membership is None:
                    self.membership = "3" * 32
                    self.membership_revision += 1
                receipt["membership_id"] = self.membership
            elif kind == "leave_room":
                assert body["parameters"]["membership_id"] == self.membership
                self.membership = None
                self.membership_revision += 1
                receipt["departure_event_id"] = 71
            else:
                receipt["version"] = body["parameters"]["version"]
            self.receipts[operation] = {
                "operation_id": operation,
                "server_generation": self.generation,
                "receipt": receipt,
            }
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise ClientError("unavailable", "response lost")
        if self.revoke_after_commit:
            self.valid_binding = False
        return self.receipts[operation]

    def client(self, store):
        from teamup_client._room_commands import RoomCommands

        return RoomCommands(store, self.request, lambda _binding: self.valid_binding)


async def send(client, **overrides):
    return await client.execute(Binding(), "send_message", {**PARAMS, **overrides})


def test_lost_response_replays_immutable_send_after_restart(tmp_path):
    async def scenario():
        socket_path = tmp_path / "client.sock"
        server = Server()
        server.fail_after_commit = True
        with open_room_store(socket_path) as store:
            with pytest.raises(ClientError) as lost:
                await send(server.client(store))
            assert lost.value.code == "command_outcome_unknown"
        assert not server.hydrations
        with open_room_store(socket_path) as store:
            client = server.client(store)
            result = await send(client)
            assert result == server.messages[0]
            assert await send(client) == result
        assert server.attempts == [server.attempts[0]] * 2
        assert server.attempts[0]["parameters"] == {
            "membership_id": "1" * 32,
            "expected_board_version": 5,
            "client_message_id": "send-1",
            "text": "Private draft",
        }
        assert len(server.messages) == 1
        assert server.hydrations[0] == (
            f"/rooms/{ROOM}/messages",
            {"after": 0, "limit": 1, "membership_id": "1" * 32},
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["generation", "membership", "commands", "deny"])
def test_ambiguous_retry_keeps_unknown_when_replay_is_blocked(tmp_path, changed):
    async def scenario():
        socket_path = tmp_path / "client.sock"
        server = Server()
        server.fail_after_commit = True
        with (
            open_room_store(socket_path) as store,
            pytest.raises(ClientError, match="outcome"),
        ):
            await send(server.client(store))
        setattr(
            server,
            changed,
            {
                "generation": "generation-2",
                "membership": "2" * 32,
                "commands": None,
                "deny": True,
            }[changed],
        )
        with open_room_store(socket_path) as store:
            client = server.client(store)
            with pytest.raises(ClientError) as blocked:
                await send(client)
            assert blocked.value.code == "command_outcome_unknown"
            with pytest.raises(ClientError) as still_blocked:
                await send(client)
            assert still_blocked.value.code == "command_outcome_unknown"
        assert len(server.attempts) == (2 if changed == "deny" else 1)
        assert not server.hydrations

    asyncio.run(scenario())


def test_definitive_first_denial_allows_a_new_operation(tmp_path):
    async def scenario():
        server = Server()
        server.deny = True
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            with pytest.raises(ClientError) as rejected:
                await send(client)
            assert rejected.value.code == "forbidden"
            server.deny = False
            assert (await send(client))["text"] == "Private draft"
        assert len(server.attempts) == 2
        assert server.attempts[0]["operation_id"] != server.attempts[1]["operation_id"]

    asyncio.run(scenario())


def test_pending_send_orders_actor_room_and_conflicts_on_changed_payload(tmp_path):
    async def scenario():
        server = Server()
        server.fail_after_commit = True
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            with pytest.raises(ClientError, match="outcome"):
                await send(client)
            with pytest.raises(ClientError) as waiting:
                await send(client, client_message_id="send-2")
            assert waiting.value.code == "command_pending"
            with pytest.raises(ClientError) as conflict:
                await send(client, text="Different draft")
            assert conflict.value.code == "conflict"
            assert len(server.attempts) == 1
            await send(client)
            await send(client, client_message_id="send-2")
        assert [row["client_message_id"] for row in server.messages] == [
            "send-1",
            "send-2",
        ]

    asyncio.run(scenario())


def test_capability_falls_back_only_for_an_unaccepted_intent(tmp_path):
    async def scenario():
        server = Server()
        server.commands = None
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            assert await send(client) is None
            assert not server.attempts
            assert not server.hydrations

    asyncio.run(scenario())


def test_task_hydrates_the_current_authorized_result(tmp_path):
    async def scenario():
        server = Server()
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            result = await client.execute(
                Binding(),
                "create_task",
                {"room_id": ROOM, "client_task_id": "task-1", "title": "Private task"},
            )
            assert result == server.task
        assert server.attempts[0]["kind"] == "create_task"
        assert server.hydrations == [(f"/rooms/{ROOM}/tasks/{TASK}", None)]

    asyncio.run(scenario())


def test_acknowledgment_keeps_the_board_version_the_member_read(tmp_path):
    async def scenario():
        server = Server()
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            result = await client.execute(
                Binding(), "ack_board", {"room_id": ROOM, "version": 3}
            )
            assert result == {"version": 3}
        assert server.attempts[0]["parameters"] == {
            "membership_id": "1" * 32,
            "version": 3,
        }

    asyncio.run(scenario())


def test_console_uses_durable_task_command_without_legacy_write(tmp_path):
    from teamup_client._console_data import ConsoleReader, ConsoleSession

    async def scenario():
        server = Server()
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            server.commands.append("record_task_progress")

            class Lifecycle:
                async def room_command(self, identity, method, params):
                    assert identity == "session"
                    assert method == "report_progress"
                    return await client.execute(Binding(), method, params)

            console = ConsoleReader(
                lambda: (ConsoleSession("session", "codex", "Agents", "token"),),
                server.request,
                Lifecycle(),
            )
            result = await console.task_action(
                "session",
                ROOM,
                TASK,
                "progress",
                {"client_progress_id": "progress-1", "note": "Checked"},
            )
            assert result == {"state": "ready", "task": server.task}

    asyncio.run(scenario())


def test_late_receipt_cannot_release_row_after_local_revocation(tmp_path):
    async def scenario():
        server = Server()
        server.revoke_after_commit = True
        socket_path = tmp_path / "client.sock"
        with open_room_store(socket_path) as store:
            with pytest.raises(ClientError) as hidden:
                await send(server.client(store))
            assert hidden.value.code == "not_found"
        assert not server.hydrations
        server.valid_binding = True
        server.revoke_after_commit = False
        with open_room_store(socket_path) as store:
            assert await send(server.client(store)) == server.messages[0]
        assert len(server.attempts) == 1

    asyncio.run(scenario())


def test_reconnect_reconciles_receipt_without_hydrating_private_result(tmp_path):
    async def scenario():
        server = Server()
        server.fail_after_commit = True
        socket_path = tmp_path / "client.sock"
        with (
            open_room_store(socket_path) as store,
            pytest.raises(ClientError, match="outcome"),
        ):
            await send(server.client(store))
        with open_room_store(socket_path) as store:
            client = server.client(store)
            await client.retry_pending(Binding())
            await client.retry_pending(Binding())
            assert not server.hydrations
            assert server.attempts == [server.attempts[0]] * 2
            assert await send(client) == server.messages[0]
            assert len(server.attempts) == 2

    asyncio.run(scenario())


def test_ambiguous_background_retry_runs_only_once_across_restarts(tmp_path):
    async def scenario():
        server = Server()
        server.fail_after_commit = True
        socket_path = tmp_path / "client.sock"
        with (
            open_room_store(socket_path) as store,
            pytest.raises(ClientError, match="outcome"),
        ):
            await send(server.client(store))
        server.fail_after_commit = True
        with open_room_store(socket_path) as store:
            await server.client(store).retry_pending(Binding())
        with open_room_store(socket_path) as store:
            client = server.client(store)
            await client.retry_pending(Binding())
            assert server.attempts == [server.attempts[0]] * 2
            assert await send(client) == server.messages[0]
        assert server.attempts == [server.attempts[0]] * 3

    asyncio.run(scenario())


def test_hydration_does_not_release_a_message_after_generation_changes(tmp_path):
    async def scenario():
        server = Server()
        server.restore_on_hydration = True
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            with pytest.raises(ClientError) as committed:
                await send(client)
            assert committed.value.code == "command_result_unavailable"
            with pytest.raises(ClientError) as retry:
                await send(client)
            assert retry.value.code == "command_result_unavailable"
        assert len(server.messages) == 1
        assert len(server.attempts) == 1
        assert len(server.hydrations) == 1

    asyncio.run(scenario())


def test_concurrent_room_writes_wait_for_the_previous_command(tmp_path):
    async def scenario():
        from teamup_client._room_commands import RoomCommands

        server = Server()
        first_submitted = asyncio.Event()
        second_started = asyncio.Event()

        async def request(method, path, token, *, query=None, body=None):
            if method == "POST" and body["parameters"]["client_message_id"] == "send-1":
                first_submitted.set()
                await second_started.wait()
                assert not server.attempts
            return await server.request(method, path, token, query=query, body=body)

        with open_room_store(tmp_path / "client.sock") as store:
            client = RoomCommands(store, request, lambda binding: True)

            async def run_second():
                await first_submitted.wait()
                second_started.set()
                return await send(client, client_message_id="send-2")

            first, second = await asyncio.gather(send(client), run_second())
        assert first["message_id"] == 1
        assert second["message_id"] == 2

    asyncio.run(scenario())


def test_lost_leave_response_can_be_recovered_after_projection_is_hidden(tmp_path):
    async def scenario():
        server = Server()
        server.commands.append("leave_room")
        server.fail_after_commit = True
        params = {"room_id": ROOM, "notify": True, "membership_id": "1" * 32}
        with open_room_store(tmp_path / "client.sock") as store:
            client = server.client(store)
            with pytest.raises(ClientError, match="outcome"):
                await client.execute(Binding(), "leave_room", params)
            store.invalidate_commands(
                "profile",
                "account",
                MEMBER,
                room_id=ROOM,
                membership_id="1" * 32,
                keep_self_leave=True,
            )
            result = await client.execute(Binding(), "leave_room", params)
        assert result == {"room_id": ROOM, "departure_event_id": 71}
        assert server.membership is None
        assert server.attempts == [server.attempts[0]] * 2

    asyncio.run(scenario())


def test_late_receipt_records_commit_but_does_not_reopen_a_blocked_scope(tmp_path):
    async def scenario():
        from teamup_client._room_commands import RoomCommands

        server = Server()
        with open_room_store(tmp_path / "client.sock") as store:

            async def request(method, path, token, *, query=None, body=None):
                response = await server.request(
                    method, path, token, query=query, body=body
                )
                if method == "POST":
                    store.invalidate_commands(
                        "profile", "account", MEMBER, room_id=ROOM
                    )
                return response

            client = RoomCommands(store, request, lambda binding: True)
            for _ in range(2):
                with pytest.raises(ClientError) as rejected:
                    await send(client)
                assert rejected.value.code == "command_result_unavailable"
        assert not server.hydrations
        assert len(server.attempts) == 1

    asyncio.run(scenario())


def test_board_rejection_keeps_editable_draft_until_access_is_lost(tmp_path):
    async def scenario():
        server = Server()
        server.deny = True
        server.denial_code = "board_ack_required"
        socket_path = tmp_path / "client.sock"
        database = socket_path.with_name("client.sock.rooms.sqlite3")
        with open_room_store(socket_path) as store:
            client = server.client(store)
            with pytest.raises(ClientError) as rejected:
                await send(client)
            assert rejected.value.code == "board_ack_required"
            assert b"Private draft" in database.read_bytes()
            client.invalidate_room(
                "profile", "account", MEMBER, ROOM, membership_id="1" * 32
            )
            assert b"Private draft" not in database.read_bytes()

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["join_room", "leave_room"])
def test_explicit_request_id_can_start_a_new_membership_intent(tmp_path, method):
    async def scenario():
        server = Server()
        server.commands.append(method)
        if method == "join_room":
            server.membership = None
        server.fail_before_commit = True
        socket_path = tmp_path / "client.sock"
        params = {"room_id": ROOM}
        with open_room_store(socket_path) as store:
            client = server.client(store)
            with pytest.raises(ClientError, match="outcome"):
                await client.execute(Binding(), method, params)
            client.invalidate_room("profile", "account", MEMBER, ROOM)
            with pytest.raises(ClientError, match="outcome"):
                await client.execute(Binding(), method, params)
        assert len(server.attempts) == 1
        server.membership_revision += 2
        if method == "leave_room":
            server.membership = "2" * 32
        with open_room_store(socket_path) as store:
            client = server.client(store)
            with pytest.raises(ClientError, match="outcome"):
                await client.execute(Binding(), method, params)
            assert len(server.attempts) == 1
            new_intent = {**params, "client_request_id": "fresh-intent"}
            server.fail_after_commit = True
            with pytest.raises(ClientError, match="outcome"):
                await client.execute(Binding(), method, new_intent)
            result = await client.execute(Binding(), method, new_intent)
            with pytest.raises(ClientError, match="outcome"):
                await client.execute(Binding(), method, params)
        assert result["room_id"] == ROOM
        old, fresh, replay = server.attempts
        assert old["operation_id"] != fresh["operation_id"]
        assert fresh == replay
        assert "client_request_id" not in fresh["parameters"]
        with closing(
            sqlite3.connect(socket_path.with_name("client.sock.rooms.sqlite3"))
        ) as db:
            assert db.execute(
                "SELECT blocked, attempts, receipt FROM room_commands WHERE operation_id = ?",
                (old["operation_id"],),
            ).fetchone() == (1, 1, None)

    asyncio.run(scenario())


def test_stale_capability_read_cannot_redact_another_rooms_new_intent(tmp_path):
    async def scenario():
        from teamup_client._room_commands import RoomCommands
        from teamup_client._room_store import CommandScope

        server = Server()
        capability_read = asyncio.Event()
        deliver_capability = asyncio.Event()
        other_room = "00000000-0000-4000-8000-000000000010"

        async def request(method, path, token, *, query=None, body=None):
            result = await server.request(method, path, token, query=query, body=body)
            if path == "/sync/capabilities":
                capability_read.set()
                await deliver_capability.wait()
            return result

        with open_room_store(tmp_path / "client.sock") as store:
            client = RoomCommands(store, request, lambda binding: True)
            old_submission = asyncio.create_task(send(client))
            await asyncio.wait_for(capability_read.wait(), 2)
            server.generation = "generation-2"
            scope = CommandScope("profile", "account", MEMBER, other_room)
            queued = store.stage_command(
                scope,
                server.generation,
                server.membership,
                "key",
                "intent",
                {
                    "operation_id": str(uuid4()),
                    "expected_member_id": MEMBER,
                    "server_generation": server.generation,
                    "room_id": other_room,
                    "kind": "send_message",
                    "parameters": {"text": "New private draft"},
                },
            )
            assert queued is not None
            assert store.mark_command_dispatch(queued)
            deliver_capability.set()
            with pytest.raises(ClientError) as stale:
                await old_submission
            assert stale.value.code == "server_generation_changed"
            still_pending = store.pending_command(scope)
            assert still_pending is not None
            assert still_pending.request == queued.request
            assert still_pending.attempts == 1

    asyncio.run(scenario())


def test_keyed_leave_issued_while_absent_cannot_leave_a_later_membership(tmp_path):
    async def scenario():
        server = Server()
        server.commands.append("leave_room")
        server.membership = None
        socket_path = tmp_path / "client.sock"
        room = {"room_id": ROOM}
        leave = {**room, "client_request_id": "absent-leave"}
        with open_room_store(socket_path) as store:
            client = server.client(store)
            assert await client.execute(Binding(), "leave_room", room) == room
            with pytest.raises(ClientError) as absent:
                await client.execute(Binding(), "leave_room", leave)
            assert absent.value.code == "command_blocked"
            assert not server.attempts
        server.membership = "2" * 32
        server.membership_revision += 1
        with open_room_store(socket_path) as store:
            client = server.client(store)
            with pytest.raises(ClientError) as replay:
                await client.execute(Binding(), "leave_room", leave)
            assert replay.value.code == "command_blocked"
            assert not server.attempts
            assert server.membership == "2" * 32
            fresh = {**room, "client_request_id": "new-membership"}
            assert await client.execute(Binding(), "leave_room", fresh) == room
        assert len(server.attempts) == 1
        assert server.membership is None

    asyncio.run(scenario())
