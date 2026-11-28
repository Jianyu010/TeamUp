import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from teamup_client._config import Binding, Install, owner_key
from teamup_client._coordinator import Coordinator
from teamup_client._notices import binding_scope, open_store
from teamup_client._room_commands import RoomCommands
from teamup_client._room_store import CommandScope, RoomScope, open_room_store
from teamup_client._room_sync import RoomSync
from teamup_client.ipc import ClientError

MEMBER = "00000000-0000-4000-8000-000000000001"
REPLACEMENT = "00000000-0000-4000-8000-000000000002"
BINDING = Binding("binding", "member-token", "codex@mac", "session")
PARAMS = {"name": "Agent", "description": "Tests a stable binding"}


def config(*, server_socket=None):
    return SimpleNamespace(
        server_url="http://127.0.0.1:8000",
        server_socket=server_socket,
        account_token="account-token",
        enrollment_key=None,
        bindings=(BINDING,),
        installs=(),
    )


class Server:
    def __init__(self, member_id):
        self.member_id = member_id
        self.calls = []
        self.checked = asyncio.Event()
        self.owner_id = None
        self.signed_owner = None
        self.stream_opened = False
        self.generation = "generation-a"
        self.retired = False
        self.drop_retirement_reply = False

    async def request(self, method, path, token, *, query=None, body=None, **kwargs):
        self.calls.append((method, path, query, body))
        profile = {"member_id": self.member_id, "role_description": "Agent"}
        if path == "/members/me":
            self.checked.set()
            if not self.member_id or self.retired:
                raise ClientError("not_registered", "Missing member")
            if self.owner_id and query == {"include_owner": "true"}:
                profile["owner_id"] = self.owner_id
            return profile
        if path == "/auth/capabilities":
            assert token == "account-token" and kwargs["member_auth"] is False
            assert query == {"include_owner": "true"}
            return {"admin": False, **({"owner_id": self.signed_owner} if self.signed_owner else {})}
        if path == f"/members/{MEMBER}/archive":
            assert token == "account-token" and kwargs["member_auth"] is False
            if self.signed_owner != self.owner_id:
                raise ClientError("not_found", "Archive belongs to another account")
            return {"conversations": []}
        if path == "/members":
            self.member_id = REPLACEMENT
            return {**profile, "member_id": REPLACEMENT}
        if path == "/sync/capabilities":
            return {"version": 1, "server_generation": self.generation}
        if path in {"/members/me/retire", "/members/me/retirement"}:
            capture = body or query or {}
            if capture and capture["server_generation"] != self.generation:
                raise ClientError("server_generation_changed", "Generation changed")
            if capture and capture["expected_member_id"] != self.member_id:
                raise ClientError("member_identity_changed", "Member changed")
            if method == "POST":
                self.retired = True
                if self.drop_retirement_reply:
                    raise ClientError("unavailable", "The retirement reply was lost")
            elif not self.retired:
                raise ClientError("not_found", "Member is still active")
            receipt = {**profile, "retired": True}
            if capture:
                receipt["server_generation"] = self.generation
            return receipt
        assert path == "/rooms"
        return {"rooms": []}

    @asynccontextmanager
    async def events(self, token):
        self.stream_opened = True
        yield


@pytest.mark.parametrize("replacement", [None, REPLACEMENT])
def test_registration_pins_member_across_process_restarts(tmp_path, replacement):
    async def scenario():
        socket = tmp_path / "teamup.sock"
        with open_store(socket) as notices:
            coordinator = Coordinator(config(), Server(MEMBER), notices, None, None, {})
            result = await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            assert result["member_id"] == MEMBER
            session_id = coordinator.console_sessions()[0].id

        with open_store(socket) as notices:
            server = Server(replacement)
            coordinator = Coordinator(config(), server, notices, None, None, {})
            with pytest.raises(ClientError) as error:
                await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            assert error.value.code == "member_identity_changed"
            assert not any(path == "/members" for _, path, _, _ in server.calls)
            assert coordinator.console_sessions() == ()
            assert coordinator.console_retired_sessions() == []
            assert await coordinator.confirmed_binding(session_id) is None

            for method in ("notifications", "list_rooms", "retire_identity"):
                with pytest.raises(ClientError) as error:
                    await coordinator.execute(BINDING.binding_key, method, {})
                assert error.value.code == "member_identity_changed"
            assert not any(
                path in {"/rooms", "/members/me/retire"} for _, path, _, _ in server.calls
            )

            server.member_id = MEMBER
            assert (await coordinator.execute(BINDING.binding_key, "register", PARAMS))[
                "member_id"
            ] == MEMBER
            assert coordinator.console_sessions()[0].id == session_id

    asyncio.run(scenario())


def test_forwarded_servers_at_the_same_url_have_independent_retirement_state(tmp_path):
    async def scenario():
        socket = tmp_path / "teamup.sock"
        first = config(server_socket=tmp_path / "gateway-a.sock")
        second = config(server_socket=tmp_path / "gateway-b.sock")
        first_server = Server(MEMBER)
        first_server.drop_retirement_reply = True

        with open_store(socket) as notices:
            coordinator = Coordinator(first, first_server, notices, None, None, {})
            await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            with pytest.raises(ClientError) as error:
                await coordinator.execute(BINDING.binding_key, "retire_identity", {})
            assert error.value.code == "unavailable"

        with open_store(socket) as notices:
            coordinator = Coordinator(second, Server(REPLACEMENT), notices, None, None, {})
            profile = await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            assert profile["member_id"] == REPLACEMENT
            assert coordinator.console_retired_sessions() == []

        with open_store(socket) as notices:
            coordinator = Coordinator(first, first_server, notices, None, None, {})
            assert coordinator.console_sessions() == ()
            receipt = await coordinator.execute(BINDING.binding_key, "retire_identity", {})
            assert receipt["member_id"] == MEMBER

        with open_store(socket) as notices:
            coordinator = Coordinator(second, Server(REPLACEMENT), notices, None, None, {})
            profile = await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            assert profile["member_id"] == REPLACEMENT
            assert coordinator.console_retired_sessions() == []

    asyncio.run(scenario())


def test_reconnect_invalidates_previous_binding_without_opening_an_event_stream(tmp_path):
    async def scenario():
        socket = tmp_path / "teamup.sock"
        with open_store(socket) as notices:
            server = Server(MEMBER)
            coordinator = Coordinator(config(), server, notices, None, None, {})
            await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            session_id = coordinator.console_sessions()[0].id
            binding = await coordinator.confirmed_binding(session_id)
            assert binding is not None and binding.member_id == MEMBER
            server.checked.clear()
            server.member_id = REPLACEMENT
            async with coordinator.watching_inboxes():
                await asyncio.wait_for(server.checked.wait(), 1)
                assert not coordinator.binding_current(binding)
                assert coordinator.console_sessions() == ()
                assert not server.stream_opened

    asyncio.run(scenario())


def test_durable_retirement_carries_captured_actor_and_generation_after_restart(tmp_path):
    async def scenario():
        socket = tmp_path / "teamup.sock"
        server = Server(MEMBER)
        server.drop_retirement_reply = True
        with open_store(socket) as notices:
            coordinator = Coordinator(config(), server, notices, None, None, {})
            await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            with pytest.raises(ClientError) as unavailable:
                await coordinator.execute(BINDING.binding_key, "retire_identity", {})
            assert unavailable.value.code == "unavailable"
            assert server.calls[-1] == (
                "POST", "/members/me/retire", None,
                {"expected_member_id": MEMBER, "server_generation": "generation-a"},
            )
            assert coordinator.console_sessions() == ()

        server.generation = "generation-b"
        with open_store(socket) as notices:
            coordinator = Coordinator(config(), server, notices, None, None, {})
            assert coordinator.console_sessions() == ()
            with pytest.raises(ClientError) as error:
                await coordinator.execute(BINDING.binding_key, "retire_identity", {})
            assert error.value.code == "server_generation_changed"
            assert coordinator.console_retired_sessions() == []
            assert [call[1] for call in server.calls].count("/members/me/retire") == 1

            server.generation = "generation-a"
            assert await coordinator.execute(BINDING.binding_key, "retire_identity", {}) == {
                "member_id": MEMBER,
                "role_description": "Agent",
                "retired": True,
            }
            assert coordinator.console_retired_sessions()[0]["member_id"] == MEMBER

    asyncio.run(scenario())


def test_signed_in_console_requires_the_confirmed_members_owner(tmp_path):
    async def scenario():
        socket = tmp_path / "teamup.sock"
        server = Server(MEMBER)
        server.owner_id = "account-a"
        server.signed_owner = "account-a"
        with open_store(socket) as notices:
            coordinator = Coordinator(config(), server, notices, None, None, {}, signed_in_console=True)
            profile = await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            assert "owner_id" not in profile
            session_id = coordinator.console_sessions()[0].id
            for _ in range(2):
                binding = await coordinator.confirmed_binding(session_id)
                assert binding is not None and binding.account_id == "account-a"
            assert [c[1] for c in server.calls].count("/auth/capabilities") == 1
            server.owner_id = None
            assert await coordinator.confirmed_binding(session_id) is None
            assert coordinator.console_sessions() == ()

        server.owner_id = "account-a"
        server.signed_owner = "account-b"
        with open_store(socket) as notices:
            coordinator = Coordinator(config(), server, notices, None, None, {}, signed_in_console=True)
            assert await coordinator.confirmed_binding(session_id) is None
            assert coordinator.console_sessions() == ()
            assert coordinator.console_retired_sessions() == []
            with pytest.raises(ClientError) as error:
                await coordinator.execute(BINDING.binding_key, "list_rooms", {})
            assert error.value.code == "member_identity_changed"
            assert not any(path == "/rooms" for _, path, _, _ in server.calls)

        server.owner_id = server.signed_owner = None
        with open_store(socket) as notices:
            coordinator = Coordinator(config(), server, notices, None, None, {}, signed_in_console=True)
            assert await coordinator.confirmed_binding(session_id) is None
            assert coordinator.console_sessions() == ()

    asyncio.run(scenario())


def test_legacy_archive_requires_authorization_from_the_signed_in_account(tmp_path):
    async def scenario():
        config_ = config()
        identity = (BINDING.harness_instance_id, BINDING.session_key)
        server = Server(MEMBER)
        server.owner_id = "account-a"
        server.signed_owner = "account-b"
        with open_store(tmp_path / "teamup.sock") as notices:
            scope = binding_scope(config_.server_url, BINDING)
            notices.retire_session(
                config_.server_url, identity, scope, "codex",
                {"member_id": MEMBER, "role_description": "Former member"},
            )
            coordinator = Coordinator(config_, server, notices, None, None, {}, signed_in_console=True)
            await coordinator.verify_console_bindings()
            assert coordinator.console_retired_sessions() == []
            with pytest.raises(ClientError) as error:
                await coordinator.console_retired_history(coordinator._console_id(identity), after=0)
            assert error.value.code == "not_found"

            server.signed_owner = "account-a"
            coordinator = Coordinator(config_, server, notices, None, None, {}, signed_in_console=True)
            await coordinator.verify_console_bindings()
            session_id = coordinator.console_retired_sessions()[0]["id"]
            assert (await coordinator.console_retired_history(session_id, after=0))["notifications"] == []

    asyncio.run(scenario())


def test_late_read_is_discarded_even_if_original_member_is_reconfirmed(tmp_path):
    class DeferredRead(Server):
        def __init__(self):
            super().__init__(MEMBER)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, method, path, token, **kwargs):
            if path == "/rooms":
                self.started.set()
                await self.release.wait()
            return await super().request(method, path, token, **kwargs)

    async def scenario():
        server = DeferredRead()
        with open_store(tmp_path / "teamup.sock") as notices:
            coordinator = Coordinator(config(), server, notices, None, None, {})
            await coordinator.execute(BINDING.binding_key, "register", PARAMS)
            session_id = coordinator.console_sessions()[0].id
            read = asyncio.create_task(coordinator.execute(BINDING.binding_key, "list_rooms", {}))
            try:
                await asyncio.wait_for(server.started.wait(), 1)
                server.member_id = REPLACEMENT
                assert await coordinator.confirmed_binding(session_id) is None
                server.member_id = MEMBER
                assert await coordinator.confirmed_binding(session_id) is not None
            finally:
                server.release.set()
            with pytest.raises(ClientError) as error:
                await read
            assert error.value.code == "unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["identity_change", "owner_rejection"])
@pytest.mark.parametrize("dispatched", [False, True])
def test_binding_revocation_blocks_queued_work_even_after_reapproval(tmp_path, revocation, dispatched):
    async def scenario():
        path = tmp_path / "teamup.sock"
        server = Server(MEMBER)
        cfg = config()
        cfg.bindings = ()
        cfg.installs = (Install(BINDING.binding_key, BINDING.harness_instance_id),)
        identity = {
            "harness_instance_id": BINDING.harness_instance_id,
            "session_key": BINDING.session_key,
        }
        session_ref = {"id": BINDING.session_key, "kind": "codex"}
        with open_store(path) as notices, open_room_store(path) as store:
            notices.add_session(
                BINDING.harness_instance_id, BINDING.session_key, BINDING.member_token, "approved",
            )
            coordinator = Coordinator(cfg, server, notices, None, None, {})
            service = RoomCommands(store, server.request, coordinator.binding_current)
            coordinator.set_room_commands(service)
            sync = RoomSync(store, coordinator.confirmed_binding, coordinator.binding_current)
            coordinator.set_room_invalidator(sync.invalidate)
            await coordinator.execute(BINDING.binding_key, "register", PARAMS, session_ref)
            session_id = coordinator.console_sessions()[0].id
            bound = await coordinator.confirmed_binding(session_id)
            assert bound is not None
            actor = (bound.server_profile, bound.account_id, bound.member_id)
            room = "00000000-0000-4000-8000-000000000003"
            scope = CommandScope(*actor, room)
            view = RoomScope(actor[0], server.generation, *actor[1:], room, "membership")
            store.begin(view)
            request = {
                "kind": "send_message", "operation_id": "operation", "parameters": {"text": "private"},
            }
            command = store.stage_command(
                scope, server.generation, "membership", "message-key", "intent", request,
            )
            assert command is not None
            if dispatched:
                assert store.mark_command_dispatch(command)

            if revocation == "owner_rejection":
                for state in ("rejected", "approved"):
                    await coordinator.execute(owner_key(cfg), "decide_session", {**identity, "state": state})
            else:
                server.member_id = REPLACEMENT
                assert await coordinator.confirmed_binding(session_id) is None
                server.member_id = MEMBER

            assert await coordinator.confirmed_binding(session_id) is not None
            assert not coordinator.binding_current(bound)
            assert store.load(view) is None
            saved = store.find_command(scope, "send_message", "message-key", "intent")
            assert saved is not None and saved.blocked and saved.request is None
            assert saved.attempts == int(dispatched) and saved.receipt is None
            assert store.reconnect_queue(scope, 10) == []

    asyncio.run(scenario())
