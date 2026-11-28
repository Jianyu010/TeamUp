"""Local wake defaults, owner policy overrides, and handoff authorization."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from teamup_client._config import Binding, owner_key
from teamup_client._coordinator import Coordinator, Lease
from teamup_client._notices import open_store

BINDING = Binding("binding", "member-token", "codex@mac", "session")
IDENTITY = (BINDING.harness_instance_id, BINDING.session_key)
ROOM = "00000000-0000-4000-8000-000000000001"
SENDER = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
PROFILE = {"member_id": OTHER, "role_description": "Agent"}
NOTICE = {
    "event_id": 1,
    "message_id": 1,
    "room_id": ROOM,
    "sender_member_id": SENDER,
}


def config(*, legacy=False):
    return SimpleNamespace(
        server_url="http://127.0.0.1:8000",
        enrollment_key="enrollment-key" if legacy else None,
        account_token=None if legacy else "account-token",
        bindings=(BINDING,),
        installs=(),
    )


async def set_wake(coordinator, rooms, senders):
    await coordinator.execute(
        owner_key(config()),
        "set_wake",
        {
            "harness_instance_id": IDENTITY[0],
            "session_key": IDENTITY[1],
            "rooms": rooms,
            "senders": senders,
        },
    )


async def member_profile(method, path, token, *, query):
    assert (method, path, token) == ("GET", "/members/me", BINDING.member_token)
    assert query == {"include_owner": "true"}
    return PROFILE


async def confirm(console):
    assert await console.confirmed_binding(console._console_id(IDENTITY)) is not None


def test_signed_in_console_defaults_to_wake_and_persists_owner_override(tmp_path):
    async def scenario():
        socket = tmp_path / "teamup.sock"
        with open_store(socket) as notices:
            console = Coordinator(
                config(), SimpleNamespace(request=member_profile),
                notices, None, None, {}, signed_in_console=True,
            )
            await confirm(console)
            session = console._sessions[IDENTITY]
            assert await console.console_wake_settings() == {
                "state": "ready",
                "enabled": True,
            }
            assert console._wake_allowed(session, NOTICE)

            assert await console.console_wake_settings(False) == {
                "state": "ready",
                "enabled": False,
            }
            assert session.harness_changed.is_set()
            assert not console._wake_allowed(session, NOTICE)
            await set_wake(console, [ROOM], [SENDER])
            assert console._wake_allowed(session, NOTICE)
        with open_store(socket) as notices:
            console = Coordinator(
                config(), SimpleNamespace(request=member_profile),
                notices, None, None, {}, signed_in_console=True,
            )
            await confirm(console)
            assert await console.console_wake_settings() == {
                "state": "ready",
                "enabled": False,
            }
            await console.console_wake_settings(True)
            session = console._sessions[IDENTITY]
            assert console._wake_allowed(session, NOTICE)
            assert not console._wake_allowed(session, {**NOTICE, "room_id": OTHER})
            assert not console._wake_allowed(
                session, {**NOTICE, "sender_member_id": OTHER}
            )
            await set_wake(console, [], [])
        with open_store(socket) as notices:
            console = Coordinator(
                config(), SimpleNamespace(request=member_profile),
                notices, None, None, {}, signed_in_console=True,
            )
            await confirm(console)
            assert await console.console_wake_settings() == {
                "state": "ready",
                "enabled": True,
            }
            assert not console._wake_allowed(console._sessions[IDENTITY], NOTICE)

    asyncio.run(scenario())


def test_headless_and_legacy_still_need_an_explicit_room_permission(tmp_path):
    async def scenario():
        with open_store(tmp_path / "teamup.sock") as notices:
            headless = Coordinator(config(), None, notices, None, None, {})
            legacy = Coordinator(
                config(legacy=True),
                None,
                notices,
                None,
                None,
                {},
                signed_in_console=True,
            )
            for coordinator in (headless, legacy):
                assert await coordinator.console_wake_settings(True) == {
                    "state": "unavailable"
                }
                assert not coordinator._wake_allowed(
                    coordinator._sessions[IDENTITY], NOTICE
                )
            await set_wake(headless, [ROOM], [])
            for coordinator in (headless, legacy):
                assert coordinator._wake_allowed(
                    coordinator._sessions[IDENTITY], NOTICE
                )

    asyncio.run(scenario())


def test_handoff_rechecks_changed_wake_permission_after_server_read(tmp_path):
    async def scenario(socket: Path):
        started = asyncio.Event()
        release = asyncio.Event()
        submitted = []

        class Server:
            async def request(self, method, path, token, *, query):
                if path == "/members/me":
                    return await member_profile(method, path, token, query=query)
                assert (method, path, token) == (
                    "GET",
                    f"/rooms/{ROOM}/messages",
                    BINDING.member_token,
                )
                assert query == {"after": 0, "limit": 1, "delivery": "true"}
                started.set()
                await release.wait()
                return {
                    "messages": [{"message_id": 1, "text": "Room mention"}],
                    "delivery_board": {"version": 1, "rules": "", "protocol": ""},
                }

        async def driver(address, token, prompt):
            submitted.append((address, token, prompt))
            return True

        with open_store(socket) as notices:
            console = Coordinator(
                config(),
                Server(),
                notices,
                None,
                None,
                {"codex": driver},
                signed_in_console=True,
            )
            await confirm(console)
            session = console._sessions[IDENTITY]
            notices.remember_members(
                session.scope, [{"member_id": SENDER, "role_description": "Teammate"}]
            )
            notices.record(session.scope, NOTICE)
            console._set_lease(IDENTITY, Lease("codex", "thread"))
            handoff = asyncio.create_task(console._hand_off(BINDING, session, NOTICE))
            await started.wait()
            await console.console_wake_settings(False)
            release.set()
            assert await handoff == "not_allowed"
            assert not submitted
            assert (
                notices.state(session.scope, NOTICE["event_id"])["harness_state"]
                is None
            )

    asyncio.run(scenario(tmp_path / "teamup.sock"))


def test_wake_drains_all_pending_mention_batches_after_attachment(tmp_path):
    async def scenario():
        complete = asyncio.Event()
        seen = []

        class Server:
            async def request(self, method, path, token, *, query):
                if path == "/members/me":
                    return await member_profile(method, path, token, query=query)
                message_id = query["after"] + 1
                return {
                    "messages": [
                        {"message_id": message_id, "text": f"Mention {message_id}"}
                    ],
                    "delivery_board": {"version": 1, "rules": "", "protocol": ""},
                }

        async def driver(address, token, prompt):
            seen.append(prompt)
            if len(seen) == 101:
                complete.set()
            return True

        with open_store(tmp_path / "teamup.sock") as notices:
            console = Coordinator(
                config(),
                Server(),
                notices,
                None,
                None,
                {"codex": driver},
                signed_in_console=True,
            )
            await confirm(console)
            session = console._sessions[IDENTITY]
            notices.remember_members(
                session.scope, [{"member_id": SENDER, "role_description": "Teammate"}]
            )
            for event_id in range(1, 102):
                notices.record(
                    session.scope,
                    {**NOTICE, "event_id": event_id, "message_id": event_id},
                )
            console._set_lease(IDENTITY, Lease("codex", "thread"))
            worker = asyncio.create_task(console._deliver(BINDING, session))
            try:
                await asyncio.wait_for(complete.wait(), timeout=5)
                assert notices.undecided(session.scope) == []
                assert all(
                    f"wake-{index}" in prompt for index, prompt in enumerate(seen, 1)
                )
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
