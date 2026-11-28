"""Installed Server and Client room-sync guards using public interfaces."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from tests.scenarios._room_sync import (
    COMMANDS,
    change_records,
    command_for,
    console_room,
    console_session_id,
    membership_id,
    sync_page,
    transcript,
)
from tests.scenarios._stack import Api, wait_for


@pytest.mark.scenario("NS-20A", "Room sync snapshots preserve a racing legacy commit")
def test_baseline_and_racing_legacy_write(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = stack.create_room(name="Atomic baseline")
    membership = membership_id(api, reader, room)
    api.send(writer, room, "Before the baseline")
    original_history = api.history(reader, room)
    capabilities = api.call("GET", "/v1/sync/capabilities", reader.token)
    assert capabilities["version"] == 1
    generation = capabilities["server_generation"]
    assert isinstance(generation, str) and generation

    with (
        closing(Api(stack.relay.url, trust_env=False)) as relayed,
        ThreadPoolExecutor(max_workers=1) as worker,
        stack.relay.pause_exchange(f"/v1/rooms/{room}/sync") as pause,
    ):
        pending = worker.submit(sync_page, relayed, reader, room, membership)
        assert pause.reached.wait(5), "Server did not produce the baseline"
        racing = api.send(writer, room, "After the read snapshot")
        pause.release.set()
        baseline = pending.result(timeout=5)

    scope = {
        "member_id": reader.member_id,
        "room_id": room,
        "server_generation": generation,
        "membership_id": membership,
        "board_version": 1,
    }
    assert {field: baseline[field] for field in scope} == scope
    assert baseline["has_more"] is False
    assert baseline["coverage"]["messages"]["has_older"] is False
    assert {change["collection"] for change in baseline["changes"]} == {
        "room",
        "board",
        "roster",
        "messages",
    }
    assert all(isinstance(change["entity_id"], str) for change in baseline["changes"])
    assert {
        change["entity_id"]
        for change in baseline["changes"]
        if change["collection"] == "roster"
    } == {writer.member_id, reader.member_id}
    assert change_records(baseline, "messages") == original_history

    stack.relay.upstream.restart()
    assert api.call("GET", "/v1/sync/capabilities", reader.token) == capabilities
    delta = sync_page(api, reader, room, membership, baseline["next_cursor"])
    assert {field: delta[field] for field in scope} == scope
    assert [row["message_id"] for row in change_records(delta, "messages")] == [
        racing["message_id"]
    ]
    assert delta["next_cursor"] != baseline["next_cursor"]
    tail = sync_page(api, reader, room, membership, delta["next_cursor"])
    assert tail["changes"] == [] and tail["has_more"] is False


@pytest.mark.scenario(
    "NS-20B", "Restarted Client catches up with legacy writes without SSE"
)
def test_persisted_client_catchup_without_sse(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = stack.create_room(name="Bounded catch-up", history_policy="retained")
    path = f"/v1/rooms/{room}/sync"
    history_path = f"/v1/rooms/{room}/messages"
    history = [api.send(writer, room, f"Backlog {sequence}") for sequence in range(105)]
    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        assert transcript(console_room(console, session, room))[-1] == (
            str(history[-1]["message_id"]),
            history[-1]["text"],
        )
        [first_read] = stack.relay.calls(path)
        assert "cursor" not in first_read.query and first_read.status == 200
        wait_for(lambda: stack.relay.calls("/v1/events"), what="dropped SSE request")

    arrived = [
        api.send(writer, room, f"While offline {sequence}") for sequence in range(4)
    ]
    history += arrived
    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        caught_up = console_room(console, session, room)
        assert "cursor" in stack.relay.calls(path)[1].query
        assert stack.relay.calls(history_path) == [], "Latest reads bypassed sync"
        assert transcript(
            console_room(console, session, room, after=history[-5]["message_id"])
        ) == [(str(message["message_id"]), message["text"]) for message in arrived]
        older = console_room(console, session, room, before=history[4]["message_id"])
        assert transcript(older) == [
            (str(message["message_id"]), message["text"]) for message in history[:4]
        ]
        assert stack.relay.calls(history_path), (
            "Older page did not use authorized fallback"
        )
        stack.relay.online.clear()
        try:
            offline = console_room(console, session, room)
            assert offline["state"] != "ready" and offline.get("messages", []) == []
        finally:
            stack.relay.online.set()

    fresh_profile = stack.profile("fresh", same_binding=True)
    with stack.console(fresh_profile) as console:
        session = console_session_id(console, reader)
        before = len(stack.relay.calls(path))
        assert console_room(console, session, room) == caught_up
        assert "cursor" not in stack.relay.calls(path)[before].query

    membership = membership_id(api, reader, room)
    baseline = sync_page(api, reader, room, membership)
    assert baseline["coverage"]["messages"]["has_older"] is True
    assert sorted(
        row["message_id"] for row in change_records(baseline, "messages")
    ) == [row["message_id"] for row in history[-100:]]


@pytest.mark.scenario(
    "NS-20C", "Room sync denies the wrong actor and expires a prior join"
)
def test_read_scope_cannot_cross_an_actor_or_membership(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = stack.create_room(
        name="Private to each join", history_policy="since_joining"
    )
    path = f"/v1/rooms/{room}"
    old = membership_id(api, reader, room)
    prior = api.send(writer, room, "Only visible during the first membership")
    response = api.raw(
        "GET",
        f"{path}/sync",
        reader.token,
        params={"expected_member_id": writer.member_id, "membership_id": old},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "member_identity_changed"
    assert all(
        value not in response.text for value in (reader.member_id, writer.member_id)
    )
    stale_send = command_for(
        api,
        reader,
        room,
        "send_message",
        membership_id=old,
        expected_board_version=1,
        client_message_id="stale-join",
        text="Stale membership",
    )
    assert (
        api.call(
            "POST",
            COMMANDS,
            writer.token,
            error="member_identity_changed",
            json=stale_send,
        )
        == 409
    )
    assert api.history(writer, room) == api.history(reader, room)

    baseline = sync_page(api, reader, room, old)
    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        assert transcript(console_room(console, session, room)) == [
            (str(prior["message_id"]), prior["text"])
        ]
        api.call("POST", f"{path}/leave", reader.token)
        assert sync_page(api, reader, room, old, error="not_found") == 404
        assert console_room(console, session, room) == {"state": "not_found"}
        absent = api.call(
            "GET",
            f"{path}/membership-state",
            reader.token,
            params={"expected_member_id": reader.member_id},
        )
        delayed_join = command_for(
            api,
            reader,
            room,
            "join_room",
            membership_revision=absent["membership_revision"],
        )
        api.send(writer, room, "Committed during the absence")
        api.join(reader, room)
        current = membership_id(api, reader, room)
        assert current != old
        assert sync_page(api, reader, room, current, error="board_ack_required") == 409
        waiting = console_room(console, session, room)
        assert waiting["state"] == "board_ack_required" and not waiting.get("messages")
        api.read_and_ack_board(reader, room)
        assert sync_page(api, reader, room, old, error="membership_changed") == 409
        assert (
            sync_page(
                api,
                reader,
                room,
                current,
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
                error="membership_changed",
                json=stale_send,
            )
            == 409
        )
        latest = api.send(writer, room, "Visible after rejoining")
        assert transcript(console_room(console, session, room)) == [
            (str(latest["message_id"]), latest["text"])
        ]
        api.call("POST", f"{path}/leave", reader.token)
        assert (
            api.call(
                "POST",
                COMMANDS,
                reader.token,
                error="membership_changed",
                json=delayed_join,
            )
            == 409
        )
        still_absent = api.call(
            "GET",
            f"{path}/membership-state",
            reader.token,
            params={"expected_member_id": reader.member_id},
        )
        assert still_absent["membership_id"] is None
        assert all(
            row["text"] != "Stale membership" for row in api.history(writer, room)
        )


@pytest.mark.scenario(
    "NS-20D", "An empty authorized delta advances without exposing private tasks"
)
def test_hidden_task_page_advances(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = stack.create_room(name="Task visibility")
    membership = membership_id(api, reader, room)
    baseline = sync_page(api, reader, room, membership)
    path = f"/v1/rooms/{room}/tasks"
    hidden_ids = set()
    for number in range(100):
        hidden = api.call(
            "POST",
            path,
            writer.token,
            json={
                "client_task_id": f"hidden-{number}",
                "title": f"Private verification {number}",
                "addressed_member_ids": [writer.member_id],
            },
        )
        hidden_ids.add(hidden["task_id"])
    visible = api.call(
        "POST",
        path,
        writer.token,
        json={"client_task_id": "public", "title": "Visible verification"},
    )

    empty = sync_page(api, reader, room, membership, baseline["next_cursor"])
    assert empty["changes"] == [] and empty["has_more"] is True
    assert empty["next_cursor"] != baseline["next_cursor"]
    following = sync_page(api, reader, room, membership, empty["next_cursor"])
    assert following["has_more"] is False
    assert change_records(following, "tasks") == [visible]
    bodies = json.dumps([empty, following, sync_page(api, reader, room, membership)])
    assert "Private verification" not in bodies
    assert not any(task_id in bodies for task_id in hidden_ids)


@pytest.mark.scenario(
    "NS-20E", "Archiving closes the live sync scope and preserves authorized history"
)
def test_archive_closes_live_projection(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = api.create_room(reader, "Close live sync")
    api.join(writer, room)
    for member in (reader, writer):
        api.read_and_ack_board(member, room)
    untouched = api.enroll(
        stack.relay.upstream.keys["writer"], "Outside", session="outside"
    )
    membership = membership_id(api, reader, room)
    message = api.send(writer, room, "Committed before archival")
    baseline = sync_page(api, reader, room, membership)
    path = f"/v1/rooms/{room}"

    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        expected = [(str(message["message_id"]), message["text"])]
        assert transcript(console_room(console, session, room)) == expected
        closing_message = api.send(
            writer, room, "Committed while the live view was open"
        )
        expected.append((str(closing_message["message_id"]), closing_message["text"]))
        with (
            ThreadPoolExecutor(max_workers=1) as worker,
            stack.relay.pause_exchange(f"{path}/sync") as pause,
        ):
            pending = worker.submit(console_room, console, session, room)
            assert pause.reached.wait(5), (
                "Server did not produce the delayed archive-race delta"
            )
            close_room = console.post(
                "/api/room-archive",
                params={"session": session, "room": room},
                headers={"Origin": str(console.base_url).rstrip("/")},
            )
            assert (
                close_room.status_code == 200 and close_room.json()["state"] == "ready"
            )
            pause.release.set()
            delayed = pending.result(timeout=5)
            assert delayed["state"] != "ready" and not delayed.get("messages")
        assert (
            sync_page(
                api,
                reader,
                room,
                membership,
                baseline["next_cursor"],
                error="room_archived",
            )
            == 409
        )
        with pytest.raises(AssertionError, match="HTTP 409 room_archived"):
            api.send(writer, room, "Attempted after archival")
        live = console_room(console, session, room)
        assert live["state"] in {"not_found", "room_archived"} and not live.get(
            "messages"
        )
        archive = console_room(console, session, room, archive=True)
        assert transcript(archive) == expected
        assert (
            api.call("GET", f"{path}/messages", untouched.token, error="not_found")
            == 404
        )
        assert console_room(console, session, room)["state"] in {
            "not_found",
            "room_archived",
        }


@pytest.mark.scenario(
    "NS-20F", "A local leave invalidates a delayed successful room page"
)
def test_delayed_page_cannot_restore_a_left_room(stack):
    api, reader, writer = stack.api, stack.reader, stack.writer
    room = stack.create_room(
        name="Close an in-flight read", history_policy="since_joining"
    )
    path = f"/v1/rooms/{room}/sync"
    old = membership_id(api, reader, room)
    before = api.send(writer, room, "Visible only before leaving")
    with stack.console(stack.first_reader) as console:
        session = console_session_id(console, reader)
        assert transcript(console_room(console, session, room)) == [
            (str(before["message_id"]), before["text"])
        ]
        api.send(writer, room, "Caught in the delayed page")
        with (
            ThreadPoolExecutor(max_workers=1) as worker,
            stack.relay.pause_exchange(path) as pause,
        ):
            pending = worker.submit(console_room, console, session, room)
            assert pause.reached.wait(5), "Server did not produce the delayed delta"
            assert stack.relay.calls(path)[-1].status == 200
            leave = console.post(
                "/api/sessions/leave",
                params={"session": session, "room": room, "membership_id": old},
                headers={"Origin": str(console.base_url).rstrip("/")},
            )
            assert leave.status_code == 200 and leave.json()["state"] == "ready"
            pause.release.set()
            delayed = pending.result(timeout=5)
        assert delayed["state"] != "ready" and not delayed.get("messages")
        assert console_room(console, session, room) == {"state": "not_found"}
