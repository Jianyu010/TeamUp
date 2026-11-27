"""Scripted room-task and opt-in reminder workloads over the Server HTTP API."""

import asyncio
import time
from collections import Counter
from datetime import UTC, datetime
from functools import partial

import httpx

from tests.scenarios import _results, _stack
from tests.scenarios.perf.benchmarks import BenchmarkFailure, metric, ms

GROUPS = {"nightly": (3, 4, 5), "weekly": (10, 30, 50, 100)}
STALL_AFTER = 2
TRACER_ARGS = (
    "--task-stall-after",
    str(STALL_AFTER),
    "--task-reminder-interval",
    "300",
)
REMINDERS = "/v1/task-reminders"


class TaskTraffic:
    def __init__(self, client):
        self.client = client

    async def request(self, method, path, member, *, expected=None, **kwargs):
        started = time.perf_counter()
        response = await self.client.request(
            method, path, headers={"Authorization": f"Bearer {member.token}"}, **kwargs
        )
        elapsed = ms(time.perf_counter() - started)
        status = response.status_code
        allowed = {200: None} if expected is None else expected
        try:
            body = response.json()
        except ValueError:
            raise AssertionError(
                f"{method} {path} returned HTTP {status} without JSON"
            ) from None
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        assert status in allowed and code == allowed[status], (
            f"{method} {path} returned HTTP {status}; API error code {code!r}"
        )
        return body, elapsed, status

    async def result(self, method, path, member, **kwargs):
        body, _, _ = await self.request(method, path, member, **kwargs)
        return body

    async def create(self, room_tasks, creator, key):
        return await self.result(
            "POST", room_tasks, creator, json={"client_task_id": key, "title": key}
        )

    async def action(self, task, action, member, **kwargs):
        return await self.request("POST", f"{task}/{action}", member, **kwargs)

    async def history(self, task, member, *, after=0):
        events = []
        samples = []
        while True:
            page, duration, _ = await self.request(
                "GET", f"{task}/events", member, params={"after": after, "limit": 4}
            )
            incoming = page["events"]
            ids = [event["event_id"] for event in incoming]
            assert ids == sorted(set(ids)), ids
            assert all(event_id > after for event_id in ids), ids
            events.extend(incoming)
            samples.append(duration)
            cursor = page["next_after"]
            if cursor is None:
                return events, samples
            assert ids and cursor == ids[-1], (cursor, ids)
            after = cursor


def joined_room(api, members, label):
    room = api.create_room(members[0], label, rules="Scripted workload")
    for member in members[1:]:
        api.join(member, room)
    for member in members:
        api.read_and_ack_board(member, room)
    return room


def http_client(server):
    return httpx.AsyncClient(
        base_url=server.url,
        verify=server.verify,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
        timeout=30,
    )


async def join_group(traffic, task, members):
    return await asyncio.gather(
        *(traffic.action(task, "join", member) for member in members)
    )


def durations(results):
    return [sample for _, sample, _ in results]


async def join_pattern(traffic, task, participants, _late_member):
    joined = await join_group(traffic, task, participants)
    final = await traffic.result("GET", task, participants[0])
    assert final["version"] == len(participants) + 1, final["version"]
    assert final["participant_member_ids"] == sorted(m.member_id for m in participants)
    assert final["started_at"], final
    assert all(body["started_at"] == final["started_at"] for body, _, _ in joined)
    assert sorted(body["version"] for body, _, _ in joined) == list(
        range(2, final["version"] + 1)
    )

    repeated = await join_group(traffic, task, participants)
    assert all(body == final for body, _, _ in repeated)
    events, _ = await traffic.history(task, participants[0])
    assert [event["kind"] for event in events] == ["created"] + ["joined"] * len(
        participants
    )
    assert Counter(event["actor_member_id"] for event in events[1:]) == Counter(
        member.member_id for member in participants
    )
    assert final["started_at"] == events[1]["created_at"]
    return {"join": durations(joined), "join_retry": durations(repeated)}


async def progress_pattern(traffic, task, participants, _late_member):
    await join_group(traffic, task, participants)
    baseline, _ = await traffic.history(task, participants[0])
    after = baseline[-1]["event_id"]
    # The same members work on different tasks at each size. Retry keys are member-scoped.
    task_id = task.rsplit("/", 1)[-1]
    payload = {"client_progress_id": f"{task_id}-initial", "note": "Initial progress"}

    # Each actor reuses a key concurrently. Actors may independently use the same key.
    posts = await asyncio.gather(
        *(
            traffic.action(task, "progress", member, json=payload)
            for member in participants
            for _ in range(2)
        )
    )
    latest = await traffic.result("GET", task, participants[0])
    assert latest["version"] == 1 + 2 * len(participants), latest["version"]
    assert latest["last_progress_at"], latest
    for member, (first, second) in zip(
        participants, zip(posts[::2], posts[1::2], strict=True), strict=True
    ):
        assert member.member_id in first[0]["participant_member_ids"]
        assert member.member_id in second[0]["participant_member_ids"]

    initial, initial_pages = await traffic.history(task, participants[0], after=after)
    assert Counter(
        (event["kind"], event["actor_member_id"], event["note"]) for event in initial
    ) == Counter(
        ("progress", member.member_id, payload["note"]) for member in participants
    )
    assert latest["last_progress_at"] == initial[-1]["created_at"]

    next_payload = {"client_progress_id": f"{task_id}-next", "note": "More progress"}
    updated = await asyncio.gather(
        *(
            traffic.action(task, "progress", member, json=next_payload)
            for member in participants
        )
    )
    latest = await traffic.result("GET", task, participants[0])
    assert latest["version"] == 1 + 3 * len(participants), latest["version"]
    retries = await asyncio.gather(
        *(
            traffic.action(task, "progress", member, json=payload)
            for member in participants
        )
    )
    assert all(body == latest for body, _, _ in retries)
    await traffic.action(
        task,
        "progress",
        participants[0],
        json=payload | {"note": "Conflict"},
        expected={409: "conflict"},
    )
    tail, tail_pages = await traffic.history(
        task, participants[-1], after=initial[-1]["event_id"]
    )
    assert Counter(
        (event["kind"], event["actor_member_id"], event["note"]) for event in tail
    ) == Counter(
        ("progress", member.member_id, next_payload["note"]) for member in participants
    )
    assert latest["last_progress_at"] == tail[-1]["created_at"]
    full, full_pages = await traffic.history(task, participants[-1])
    assert full == baseline + initial + tail
    return {
        "progress_with_concurrent_retry": durations(posts),
        "progress": durations(updated),
        "progress_retry": durations(retries),
        "history_page": initial_pages + tail_pages + full_pages,
    }


async def finish_pattern(traffic, task, participants, late_member):
    await join_group(traffic, task, participants)
    payload = {"client_progress_id": "racing-progress", "note": "Races finish"}
    allowed = {200: None, 409: "conflict"}
    progress_result, join_result, *finishes = await asyncio.gather(
        traffic.action(
            task, "progress", participants[-1], json=payload, expected=allowed
        ),
        traffic.action(task, "join", late_member, expected=allowed),
        *(traffic.action(task, "finish", member) for member in participants),
    )
    progress, progress_ms, progress_status = progress_result
    join, join_ms, join_status = join_result
    final = await traffic.result("GET", task, participants[0])
    assert final["status"] == "finished" and final["finished_at"], final
    assert all(body == final for body, _, _ in finishes)
    events, _ = await traffic.history(task, participants[0])
    assert events[-1]["kind"] == "finished"
    assert events[-1]["created_at"] == final["finished_at"]
    assert events[-1]["actor_member_id"] in {
        member.member_id for member in participants
    }
    assert Counter(event["kind"] for event in events) == Counter(
        created=1,
        joined=len(participants) + (join_status == 200),
        progress=int(progress_status == 200),
        finished=1,
    )
    assert final["version"] == len(events)
    assert (late_member.member_id in final["participant_member_ids"]) == (
        join_status == 200
    )
    if join_status == 200:
        assert join["status"] == "open" and join["version"] < final["version"]
    if progress_status == 200:
        assert progress["status"] == "open" and progress["version"] < final["version"]
    await traffic.action(
        task,
        "progress",
        participants[-1],
        json=payload | {"client_progress_id": "new"},
        expected={409: "conflict"},
    )
    retried, _, _ = await traffic.action(task, "finish", participants[0])
    assert retried == final
    return {
        "finish": durations(finishes),
        "racing_progress": [progress_ms],
        "racing_join": [join_ms],
    }


async def task_pattern(ws, quick, profile, *, id_, pattern):
    sizes = (2,) if quick else GROUPS[profile]
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    out = []
    api = server.api()
    try:
        members = [
            api.enroll(key, f"Participant {i}", session=f"participant-{i}")
            for i in range(max(sizes) + 1)
        ]
        async with http_client(server) as client:
            traffic = TaskTraffic(client)
            for size in sizes:
                try:
                    participants = members[:size]
                    late_member = members[-1]
                    room = joined_room(
                        api, [*participants, late_member], f"Tasks {size}"
                    )
                    room_tasks = f"/v1/rooms/{room}/tasks"
                    created = await traffic.create(
                        room_tasks, participants[0], f"Task {size}"
                    )
                    task = f"{room_tasks}/{created['task_id']}"
                    samples = await pattern(traffic, task, participants, late_member)
                except AssertionError as error:
                    raise BenchmarkFailure(
                        f"{id_} failed with {size} participants: {error}", out
                    ) from error
                except httpx.HTTPError as error:
                    raise RuntimeError(
                        f"{id_} failed with {size} participants: {error}"
                    ) from error
                out.extend(
                    metric(
                        id_,
                        f"task_{name}_latency",
                        "ms",
                        {"participants": size},
                        _results.stats(values),
                    )
                    for name, values in samples.items()
                )
    finally:
        api.close()
    return out


async def foreground(traffic, room, creator, count, window):
    """Keep a fixed offered load while the tracer scans and clients claim."""
    room_path = f"/v1/rooms/{room}"
    samples = {"task_create": [], "task_get": [], "room_send": []}
    origin = time.monotonic()

    async def work(index):
        await asyncio.sleep(max(0, origin + index * window / count - time.monotonic()))
        key = f"foreground-{index}"
        body, elapsed, _ = await traffic.request(
            "POST",
            f"{room_path}/tasks",
            creator,
            json={"client_task_id": key, "title": key},
        )
        samples["task_create"].append(elapsed)
        current, elapsed, _ = await traffic.request(
            "GET", f"{room_path}/tasks/{body['task_id']}", creator
        )
        assert current == body
        samples["task_get"].append(elapsed)
        _, elapsed, _ = await traffic.request(
            "POST",
            f"{room_path}/messages",
            creator,
            json={"client_message_id": key, "text": key},
        )
        samples["room_send"].append(elapsed)

    await asyncio.gather(*(work(index) for index in range(count)))
    return samples


async def reminder_page(traffic, member, *, after=0, limit=10):
    body = await traffic.result(
        "GET", REMINDERS, member, params={"after": after, "limit": limit}
    )
    return body["reminders"]


async def drain_reminders(traffic, member, expected, room, *, race_claim):
    seen = set()
    latencies = []
    after = 0
    while len(seen) < len(expected):
        page = await reminder_page(traffic, member, after=after)
        for reminder in page:
            reminder_id = reminder["reminder_id"]
            task_id = reminder["task_id"]
            assert reminder_id > after, (reminder_id, after)
            assert task_id in expected and task_id not in seen, reminder
            assert reminder["room_id"] == room
            assert reminder["title"] == expected[task_id]
            path = f"{REMINDERS}/{reminder_id}/claim"
            if race_claim:
                outcomes = await asyncio.gather(
                    *(
                        traffic.request(
                            "POST", path, member, expected={200: None, 404: "not_found"}
                        )
                        for _ in range(2)
                    )
                )
                assert sorted(status for _, _, status in outcomes) == [200, 404]
                [claimed] = [body for body, _, status in outcomes if status == 200]
                race_claim = False
            else:
                claimed = await traffic.result("POST", path, member)
            claimed_at = datetime.now(UTC)
            assert claimed == reminder
            due = datetime.fromisoformat(claimed["stalled_since"])
            assert due <= claimed_at, (due, claimed_at)
            latencies.append(ms((claimed_at - due).total_seconds()))
            seen.add(task_id)
            after = reminder_id
        if not page:
            await asyncio.sleep(0.08)

    assert seen == set(expected)
    assert await reminder_page(traffic, member) == [], (
        "unexpected pending reminder after claims"
    )
    return latencies


async def tracer_pattern(ws, quick, profile, *, enabled):
    # Identical pressure and seed data in both configurations.
    task_count, participant_count, foreground_count, window = (
        (4, 2, 10, 4)
        if quick
        else {"nightly": (60, 4, 80, 6), "weekly": (240, 6, 180, 12)}[profile]
    )
    key = _stack.new_secret()
    server = _stack.Server(
        ws, {"owner": key}, server_args=TRACER_ARGS if enabled else ()
    ).start()
    api = server.api()
    try:
        creator, *participants = [
            api.enroll(key, f"Tracer member {i}", session=f"tracer-{i}")
            for i in range(participant_count + 1)
        ]
        room = joined_room(api, [creator, *participants], "Stalling tasks")
        foreground_room = joined_room(api, [creator], "Foreground work")
    finally:
        api.close()

    async with http_client(server) as client:
        traffic = TaskTraffic(client)
        room_tasks = f"/v1/rooms/{room}/tasks"
        tasks = await asyncio.gather(
            *(
                traffic.create(room_tasks, creator, f"Stalling task {i}")
                for i in range(task_count)
            )
        )
        expected = {task["task_id"]: task["title"] for task in tasks}

        async def seed():
            for start in range(0, task_count, 16):
                await asyncio.gather(
                    *(
                        join_group(
                            traffic, f"{room_tasks}/{task['task_id']}", participants
                        )
                        for task in tasks[start : start + 16]
                    )
                )

        async def collect():
            await seed()
            if enabled:
                try:
                    async with asyncio.timeout(45):
                        collected = await asyncio.gather(
                            *(
                                drain_reminders(
                                    traffic,
                                    member,
                                    expected,
                                    room,
                                    race_claim=index == 0,
                                )
                                for index, member in enumerate(participants)
                            )
                        )
                except TimeoutError as error:
                    raise AssertionError(
                        f"Timed out claiming reminders for {task_count} tasks and {participant_count} participants"
                    ) from error
                assert await reminder_page(traffic, creator) == []
                latencies = [latency for samples in collected for latency in samples]
                assert len(latencies) == task_count * participant_count
                return latencies

            await asyncio.sleep(STALL_AFTER + 0.2)
            for member in [creator, *participants]:
                assert await reminder_page(traffic, member) == []
            probe_task = f"{room_tasks}/{tasks[0]['task_id']}"
            projection = await traffic.result("GET", probe_task, participants[0])
            assert projection["started_at"] and projection["stalled_since"] is None
            return []

        async with asyncio.TaskGroup() as group:
            foreground_task = group.create_task(
                foreground(traffic, foreground_room, creator, foreground_count, window)
            )
            claims_task = group.create_task(collect())
        operations = foreground_task.result()
        claims = claims_task.result()

    tracer = "on" if enabled else "off"
    id_ = f"NP-09-tracer-{tracer}"
    labels = {
        "tracer": tracer,
        "tasks": task_count,
        "participants": participant_count,
        "foreground_operations": foreground_count * 3,
    }
    out = [
        metric(
            id_,
            f"tracer_foreground_{name}_latency",
            "ms",
            labels,
            _results.stats(values),
        )
        for name, values in operations.items()
    ]
    out.append(
        metric(id_, "tracer_claims", "count", labels, _results.single(len(claims)))
    )
    if enabled:
        out.append(
            metric(
                id_, "tracer_due_to_claim_latency", "ms", labels, _results.stats(claims)
            )
        )
    return out


TASK_BENCHMARKS = {
    "NP-09-join": (
        "Concurrent task joins and idempotent joins",
        partial(task_pattern, id_="NP-09-join", pattern=join_pattern),
    ),
    "NP-09-progress": (
        "Task progress bursts, retries and paged history synchronization",
        partial(task_pattern, id_="NP-09-progress", pattern=progress_pattern),
    ),
    "NP-09-finish": (
        "Task finish contention with concurrent progress and join",
        partial(task_pattern, id_="NP-09-finish", pattern=finish_pattern),
    ),
    "NP-09-tracer-off": (
        "Foreground task and chat load with the stalled-task tracer disabled",
        partial(tracer_pattern, enabled=False),
    ),
    "NP-09-tracer-on": (
        "Foreground task and chat load with the stalled-task tracer enabled",
        partial(tracer_pattern, enabled=True),
    ),
}
