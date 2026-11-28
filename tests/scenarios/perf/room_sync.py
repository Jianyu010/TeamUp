"""Reconnect independent Client profiles after an offline mixed-version workload."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

from tests.scenarios import _results
from tests.scenarios._room_sync import (
    console_room,
    console_session_id,
    membership_id,
    room_sync_stack,
    sync_page,
    transcript,
)
from tests.scenarios.perf.benchmarks import metric, ms

BENCHMARK = "NP-17"


def recover(ws, quick):
    writes, count = (120, 2) if quick else (10_001, 6)
    with room_sync_stack(ws) as stack:
        api, writer, reader = stack.api, stack.writer, stack.reader
        active = stack.create_room(name="Offline recovery", history_policy="retained")
        closed = stack.create_room(name="Archived during the outage")
        active_sync = f"/v1/rooms/{active}/sync"
        initial = api.send(writer, active, "Before going offline")
        archived = api.send(writer, closed, "Available through the archive")
        membership = membership_id(api, reader, active)
        cursor = sync_page(api, reader, active, membership)["next_cursor"]
        profiles = [stack.first_reader] + [
            stack.profile(f"offline-{index}", same_binding=True)
            for index in range(1, count)
        ]

        for profile in profiles:
            with stack.console(profile) as console:
                session = console_session_id(console, reader)
                assert transcript(console_room(console, session, active)) == [
                    (str(initial["message_id"]), initial["text"])
                ]
                assert transcript(console_room(console, session, closed)) == [
                    (str(archived["message_id"]), archived["text"])
                ]

        api.call("POST", f"/v1/rooms/{closed}/archive", writer.token)
        hidden = api.call(
            "POST",
            f"/v1/rooms/{active}/tasks",
            writer.token,
            json={
                "client_task_id": "restricted-review",
                "title": "Hidden verification",
                "addressed_member_ids": [writer.member_id],
            },
        )
        recent = []
        for number in range(writes):
            message = api.send(
                writer,
                active,
                f"Offline legacy message {number}",
                cid=f"offline-{number}",
            )
            if number >= writes - 30:
                recent.append((str(message["message_id"]), message["text"]))
        if not quick:
            assert (
                sync_page(
                    api, reader, active, membership, cursor, error="sync_reset_required"
                )
                == 409
            )
        stack.relay.upstream.restart()
        start_index = len(stack.relay.calls(active_sync))

        def observe(viewer):
            console, session = viewer
            started = time.monotonic()
            current = console_room(console, session, active)
            caught_up = ms(time.monotonic() - started)
            assert transcript(current) == recent
            live = console_room(console, session, closed)
            assert live["state"] in {"not_found", "room_archived"} and not live.get(
                "messages"
            )
            assert transcript(console_room(console, session, closed, archive=True)) == [
                (str(archived["message_id"]), archived["text"])
            ]
            return current, caught_up

        with ExitStack() as listeners, ThreadPoolExecutor(max_workers=count) as clients:
            # Authenticate the shared account before measuring concurrent room reads.
            viewers = []
            for profile in profiles:
                console = listeners.enter_context(stack.console(profile))
                viewers.append((console, console_session_id(console, reader)))
            recovered = list(clients.map(observe, viewers))
        catchup = stack.relay.calls(active_sync)[start_index:]
        assert sum("cursor" in request.query for request in catchup) >= count
        if not quick:
            assert sum(request.status == 409 for request in catchup) == count
            assert (
                sum(
                    "cursor" not in request.query and request.status == 200
                    for request in catchup
                )
                == count
            )

        fresh = stack.profile("fresh", same_binding=True)
        with stack.console(fresh) as console:
            fresh_view, _ = observe((console, console_session_id(console, reader)))
        assert all(view == fresh_view for view, _ in recovered)
        baseline = sync_page(api, reader, active, membership)
        assert all(
            change["entity_id"] != hidden["task_id"] for change in baseline["changes"]
        )
        assert all(
            request.status is None for request in stack.relay.calls("/v1/events")
        )

    return [
        metric(
            BENCHMARK,
            "room_sync_client_recovery",
            "ms",
            {"clients": count, "legacy_writes": writes, "pruned_cursor": not quick},
            _results.stats(elapsed for _, elapsed in recovered),
        )
    ]


async def room_sync_recovery(ws, quick, _profile):
    return await asyncio.to_thread(recover, ws, quick)


ROOM_SYNC_BENCHMARKS = {
    BENCHMARK: ("Room sync recovery from offline legacy writes", room_sync_recovery)
}
