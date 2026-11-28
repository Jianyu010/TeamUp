"""Installed Client and Server durable room command guards."""

import asyncio
import sys
from hashlib import file_digest

import pytest

from tests.scenarios._room_sync import (
    COMMANDS,
    command_for,
    console_room,
    console_session_id,
    membership_id,
    sync_page,
    transcript,
)
from tests.scenarios._stack import ROOT, clean_env, command, run_process, tool, wait_for


def send_racing_with_removal(stack, room, *, send_first):
    api, reader, writer = stack.api, stack.reader, stack.writer
    membership = membership_id(api, reader, room)
    arguments = {
        "room_id": room,
        "client_message_id": "racing-send",
        "text": "One possible commit",
    }
    expected = "command_outcome_unknown" if send_first else "not_found"

    async def race():
        async with stack.first_reader.agent() as agent:
            with stack.relay.pause_exchange(
                COMMANDS, "POST", before=not send_first, drop=send_first
            ) as pause:
                pending = asyncio.create_task(
                    tool(agent, "send_message", error=expected, **arguments)
                )
                try:
                    assert await asyncio.to_thread(pause.reached.wait, 5), (
                        "Client did not dispatch the durable send"
                    )
                    api.call(
                        "POST",
                        f"/v1/rooms/{room}/members/{reader.member_id}/remove",
                        writer.token,
                        json={"membership_id": membership},
                    )
                finally:
                    pause.release.set()
                await asyncio.wait_for(pending, 8)

    async def replay():
        async with stack.first_reader.agent() as agent:
            await tool(
                agent, "send_message", error="command_outcome_unknown", **arguments
            )

    with stack.console(stack.first_reader) as console:
        console_session_id(console, reader)
        asyncio.run(race())
    [attempt] = stack.relay.calls(COMMANDS, "POST")
    assert attempt.operation_id and attempt.status == (200 if send_first else 404)
    history = api.history(writer, room)
    assert [message["text"] for message in history] == (
        [arguments["text"]] if send_first else []
    )

    if send_first:
        with stack.console(stack.first_reader) as console:
            session = console_session_id(console, reader)
            asyncio.run(asyncio.wait_for(replay(), 10))
            assert console_room(console, session, room) == {"state": "not_found"}
        assert stack.relay.calls(COMMANDS, "POST") == [attempt]
        assert api.history(writer, room) == history


@pytest.mark.scenario("NS-20G", "Removal before a durable send commits no message")
def test_removal_before_durable_send(stack):
    room = stack.create_room(name="Removal wins")
    send_racing_with_removal(stack, room, send_first=False)


@pytest.mark.scenario(
    "NS-20H", "A lost send reply stays unknown after removal and restart"
)
def test_send_before_removal_with_lost_reply(stack):
    room = stack.create_room(name="Send wins")
    send_racing_with_removal(stack, room, send_first=True)


def local_process(argv, **kwargs):
    completed = run_process(argv, capture_output=True, text=True, timeout=20, **kwargs)
    assert completed.returncode == 0, completed.stderr[-1000:]


@pytest.mark.scenario(
    "NS-20I", "Restore at the same URL rejects old cursors and ambiguous commands"
)
def test_restore_rotates_the_room_sync_scope(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = stack.create_room(name="Restore the prior snapshot")
    initial = api.send(writer, room, "In the backup")
    membership = membership_id(api, reader, room)
    baseline = sync_page(api, reader, room, membership)
    pending = command_for(
        api,
        reader,
        room,
        "send_message",
        membership_id=membership,
        expected_board_version=1,
        client_message_id="old-generation",
        text="Never replay",
    )
    server = stack.relay.upstream
    snapshot, restored = (
        server.ws.path("snapshot.sqlite3"),
        server.ws.path("restored.sqlite3"),
    )
    scripts = ROOT / "scripts" / "ops"
    local_process(
        [
            sys.executable,
            str(scripts / "snapshot_sqlite.py"),
            str(server.db),
            str(snapshot),
        ]
    )
    with snapshot.open("rb") as saved:
        checksum = file_digest(saved, "sha256").hexdigest()

    async def send():
        async with stack.first_reader.agent() as agent:
            await tool(
                agent,
                "send_message",
                error="command_outcome_unknown",
                room_id=room,
                client_message_id="after-backup",
                text="Committed after the backup",
            )

    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        initial_view = console_room(console, session, room)
        assert transcript(initial_view) == [
            (str(initial["message_id"]), initial["text"])
        ]
        with stack.relay.pause_exchange(COMMANDS, "POST", drop=True) as pause:
            pause.release.set()
            asyncio.run(asyncio.wait_for(send(), timeout=10))
            assert pause.reached.is_set()
        assert [row["text"] for row in api.history(writer, room)] == [
            "In the backup",
            "Committed after the backup",
        ]
        assert (
            transcript(console_room(console, session, room))[-1][1]
            == "Committed after the backup"
        )

    original_url = server.url
    server.stop()
    local_process(
        [
            sys.executable,
            str(scripts / "restore_backup.py"),
            "--snapshot",
            str(snapshot),
            "--sha256",
            checksum,
            "--destination",
            str(restored),
        ]
    )
    argv, env = command("server", server.bin_dir)
    local_process(
        [*argv, "rotate-sync-generation", "--db", str(restored)], env=clean_env() | env
    )
    server.db = restored
    server.start()
    assert server.url == original_url
    capabilities = api.call("GET", "/v1/sync/capabilities", reader.token)
    assert capabilities["server_generation"] != baseline["server_generation"]
    assert (
        sync_page(
            api,
            reader,
            room,
            membership,
            baseline["next_cursor"],
            error="sync_reset_required",
        )
        == 409
    )
    assert (
        api.call(
            "POST",
            COMMANDS,
            reader.token,
            error="server_generation_changed",
            json=pending,
        )
        == 409
    )
    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        assert console_room(console, session, room) == initial_view
        asyncio.run(asyncio.wait_for(send(), timeout=10))
    assert len(stack.relay.calls(COMMANDS, "POST")) == 1
    assert [row["text"] for row in api.history(writer, room)] == ["In the backup"]


@pytest.mark.scenario(
    "NS-20J", "Recovering a send receipt preserves intervening peer changes"
)
def test_receipt_and_peer_write_share_the_catchup_path(stack):
    api, writer, reader = stack.api, stack.writer, stack.reader
    room = stack.create_room(name="Interleaved receipt", history_policy="retained")
    seed = api.send(writer, room, "Before the durable send")
    arguments = {
        "room_id": room,
        "client_message_id": "own-send",
        "text": "Own command",
    }

    async def send(error=None):
        async with stack.first_reader.agent() as agent:
            return await tool(agent, "send_message", error=error, **arguments)

    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        assert transcript(console_room(console, session, room)) == [
            (str(seed["message_id"]), seed["text"])
        ]
        wait_for(
            lambda: stack.relay.calls("/v1/inbox"),
            what="first connection's inbox drain",
        )
        with stack.relay.pause_exchange(COMMANDS, "POST", drop=True) as pause:
            pause.release.set()
            asyncio.run(asyncio.wait_for(send("command_outcome_unknown"), timeout=10))
            assert pause.reached.is_set()
        peer = api.send(writer, room, "Peer committed before receipt recovery")

    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        wait_for(
            lambda: len(stack.relay.calls(COMMANDS, "POST")) >= 2,
            timeout=8,
            what="automatic retry after Client reconnect",
        )
        committed = asyncio.run(asyncio.wait_for(send(), timeout=10))
        assert [row["text"] for row in api.history(writer, room)] == [
            seed["text"],
            committed["text"],
            peer["text"],
        ]
        latest = console_room(console, session, room, after=seed["message_id"])
        assert transcript(latest) == [
            (str(message["message_id"]), message["text"])
            for message in (committed, peer)
        ]
    first, replay = stack.relay.calls(COMMANDS, "POST")
    assert first.operation_id and first.operation_id == replay.operation_id
    assert first.status == replay.status == 200
    assert any(
        "cursor" in call.query for call in stack.relay.calls(f"/v1/rooms/{room}/sync")
    )


@pytest.mark.scenario(
    "NS-20K", "A lost enrollment reply recovers the same member after restart"
)
def test_lost_enrollment_reply_recovers_original_member(stack):
    profile = stack.profile("new-binding")
    arguments = {"name": "New member", "description": "Checks enrollment recovery."}

    async def register(error=None):
        async with profile.agent() as agent:
            return await tool(agent, "register", error=error, **arguments)

    with (
        stack.console(profile),
        stack.relay.pause_exchange("/v1/members", "POST", drop=True) as pause,
    ):
        pause.release.set()
        asyncio.run(asyncio.wait_for(register("unavailable"), timeout=10))
        assert pause.reached.is_set()
    binding_token = profile.bindings[0]["member_token"]
    member = stack.api.call("GET", "/v1/members/me", binding_token)
    with stack.console(profile):
        recovered = asyncio.run(asyncio.wait_for(register(), timeout=10))
    assert recovered["member_id"] == member["member_id"]
    [enrollment] = stack.relay.calls("/v1/members", "POST")
    assert enrollment.status == 200


@pytest.mark.scenario(
    "NS-20L", "A changed member on reconnect suspends the original verified binding"
)
def test_changed_member_on_reconnect_blocks_durable_work(stack):
    api, writer, reader = stack.api, stack.writer, stack.reader
    room = stack.create_room(name="Original binding's room")
    api.send(writer, room, "Visible only to the original binding")
    original = api.call("GET", "/v1/members/me", reader.token)
    history = api.history(writer, room)
    profile = stack.first_reader
    with stack.console(profile) as console:
        session = console_session_id(console, reader)
        confirmed_view = console_room(console, session, room)
        assert transcript(confirmed_view)[0][1] == history[0]["text"]

    async def blocked_actions():
        async with profile.agent() as agent:
            expected = "member_identity_changed"
            await tool(
                agent,
                "send_message",
                error=expected,
                room_id=room,
                client_message_id="wrong-actor",
                text="Must never commit",
            )
            await tool(
                agent,
                "register",
                error=expected,
                name="Wrong actor",
                description="Must never enroll",
            )
            await tool(agent, "retire_identity", error=expected)

    sync_path = f"/v1/rooms/{room}/sync"
    verified_reads = len(stack.relay.calls(sync_path))
    with (
        stack.relay.report_member_as(reader.member_id, writer.member_id),
        stack.console(profile) as console,
    ):
        asyncio.run(asyncio.wait_for(blocked_actions(), timeout=10))
        hidden = console_room(console, session, room)
        assert hidden["state"] != "ready" and not hidden.get("messages")
    assert any(
        call.overridden_member_id == writer.member_id
        for call in stack.relay.calls("/v1/members/me")
    )
    assert len(stack.relay.calls(sync_path)) == verified_reads
    for path in (
        COMMANDS,
        "/v1/members",
        "/v1/members/me/retire",
        f"/v1/rooms/{room}/messages",
    ):
        assert stack.relay.calls(path, "POST") == [], path
    assert api.call("GET", "/v1/members/me", reader.token) == original
    assert api.history(writer, room) == history

    with stack.console(profile) as console:
        assert console_session_id(console, reader) == session
        assert console_room(console, session, room) == confirmed_view
