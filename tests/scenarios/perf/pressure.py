"""Model-free room, connection, presence and direct-message workloads."""

import asyncio
import time
from contextlib import AsyncExitStack

import httpx

from tests.scenarios import _results, _stack


def _samples(id_, name, labels, values, **extra):
    from tests.scenarios.perf.benchmarks import metric

    return metric(id_, name, "ms", labels, _results.stats(values), **extra)


def _client(server, connections):
    return httpx.AsyncClient(
        base_url=server.url,
        verify=server.verify,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=connections, max_keepalive_connections=connections
        ),
        timeout=30,
    )


def _headers(member):
    return {"Authorization": f"Bearer {member.token}"}


async def _request(client, method, path, member, **kwargs):
    started = time.perf_counter()
    response = await client.request(method, path, headers=_headers(member), **kwargs)
    response.raise_for_status()
    return response.json(), (time.perf_counter() - started) * 1_000


async def _parallel(operations):
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _enroll(api, key, count, prefix):
    return [
        api.enroll(key, f"{prefix} {i}", session=f"{prefix}-{i}") for i in range(count)
    ]


async def room_scale(ws, quick, profile):
    sizes = (
        (3, 8) if quick else {"nightly": (100, 300), "weekly": (1_000, 10_000)}[profile]
    )
    workers = 8
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    api = server.api()
    try:
        creator, observer = _enroll(api, key, 2, "room-scale")
    finally:
        api.close()

    metrics, expected = [], {}
    async with _client(server, workers) as client:
        for size in sizes:
            pending = iter(range(len(expected), size))

            async def create_rooms(pending):
                samples = []
                for index in pending:
                    name = f"room-{index}"
                    room, elapsed = await _request(
                        client, "POST", "/v1/rooms", creator, json={"name": name}
                    )
                    room_id = room["room_id"]
                    assert room_id not in expected, (
                        "room creation returned a duplicate ID"
                    )
                    assert room["name"] == name, "room creation returned the wrong name"
                    expected[room_id] = name
                    samples.append(elapsed)
                return samples

            # Bound the database seeding phase and keep at most eight writes in flight.
            async with asyncio.timeout(300):
                create_samples = [
                    elapsed
                    for worker_samples in await _parallel(
                        create_rooms(pending) for _ in range(workers)
                    )
                    for elapsed in worker_samples
                ]

            ordered = sorted(expected.items())
            # Use multiple pages even in the quick workload to exercise its cursor.
            page_limit = 3 if quick else 50
            directory_samples, seen, cursor = [], [], None
            for _ in range((size + page_limit - 1) // page_limit):
                params = {"limit": page_limit}
                if cursor is not None:
                    params["after"] = cursor
                page, elapsed = await _request(
                    client, "GET", "/v1/room-directory", observer, params=params
                )
                directory_samples.append(elapsed)
                batch = [(row["room_id"], row["name"]) for row in page["rooms"]]
                assert batch == ordered[len(seen) : len(seen) + page_limit], (
                    "room directory returned missing, duplicate or incorrectly ordered rooms"
                )
                seen.extend(batch)
                cursor = page["next_after"]
                expected_cursor = batch[-1][0] if len(seen) < size else None
                assert cursor == expected_cursor, (
                    "room directory returned an invalid cursor"
                )
            assert len(seen) == size, (
                "room directory did not return the full population"
            )

            list_samples = []
            for _ in range(2 if quick else 5):
                page, elapsed = await _request(client, "GET", "/v1/rooms", creator)
                list_samples.append(elapsed)
                assert [
                    (row["room_id"], row["name"]) for row in page["rooms"]
                ] == ordered, "creator's room list did not match the seeded population"
            observer_rooms, _ = await _request(client, "GET", "/v1/rooms", observer)
            assert observer_rooms["rooms"] == [], "directory read changed membership"

            labels = {"profile": profile, "rooms": size}
            metrics.extend(
                [
                    _samples(
                        "NP-04",
                        "room_create_latency",
                        labels,
                        create_samples,
                        workers=workers,
                    ),
                    _samples(
                        "NP-04",
                        "room_directory_page_latency",
                        labels,
                        directory_samples,
                    ),
                    _samples("NP-04", "joined_room_list_latency", labels, list_samples),
                ]
            )
    return metrics


async def _check_inbox(client, member, messages, outsider=False):
    expected = {
        msg["message_id"]: msg
        for msg in messages
        if msg["sender_member_id"] != member.member_id and not outsider
    }
    after = 0
    while True:
        page, _ = await _request(
            client, "GET", "/v1/inbox", member, params={"after": after, "limit": 100}
        )
        batch = page["events"]
        for event in batch:
            assert event["event_id"] > after, "inbox repeated or reordered a delivery"
            after = event["event_id"]
            message = event["message"]
            assert (
                event["kind"] == "message"
                and expected.pop(message["message_id"], None) == message
            ), "inbox returned an unexpected, duplicate or altered message"
        if len(batch) < 100:
            break
    assert not expected, f"inbox missed {len(expected)} deliveries"


async def _send_waves(client, writers, room, concurrency, waves, phase):
    async def send(index):
        writer = writers[index % len(writers)]
        key = f"{phase}-{index}"
        body = {"client_message_id": key, "text": f"message {index}"}
        message, elapsed = await _request(
            client, "POST", f"/v1/rooms/{room}/messages", writer, json=body
        )
        assert (
            message["room_id"] == room
            and message["sender_member_id"] == writer.member_id
        ), "send returned the wrong room or sender"
        assert all(message[field] == value for field, value in body.items()), (
            "send returned a different message body or key"
        )
        return message, elapsed

    sent, post_samples, wave_samples = [], [], []
    for index in range(0, concurrency * waves, concurrency):
        started = time.perf_counter()
        receipts = await _parallel(send(i) for i in range(index, index + concurrency))
        wave_samples.append((time.perf_counter() - started) * 1_000)
        for message, elapsed in receipts:
            sent.append(message)
            post_samples.append(elapsed)
    return sent, post_samples, wave_samples


async def concurrent_writes(ws, quick, profile):
    levels = (2, 4) if quick else {"nightly": (4, 16), "weekly": (16, 48)}[profile]
    waves = 2 if quick else {"nightly": 8, "weekly": 12}[profile]
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    api = server.api()
    try:
        *writers, outsider = _enroll(api, key, 5, "writes")
        rooms = []
        for concurrency in levels:
            room = api.create_room(writers[0], f"write-load-{concurrency}")
            for writer in writers[1:]:
                api.join(writer, room)
            for writer in writers:
                api.read_and_ack_board(writer, room)
            rooms.append(room)
    finally:
        api.close()

    metrics, messages = [], []
    async with _client(server, max(levels)) as client:
        for room, concurrency in zip(rooms, levels, strict=True):
            sent, post_samples, wave_samples = await _send_waves(
                client, writers, room, concurrency, waves, f"writes-{concurrency}"
            )
            messages.extend(sent)
            assert len({m["message_id"] for m in messages}) == len(messages), (
                "two concurrent writes returned the same message ID"
            )

            # Check the persistent inbox after each level, including earlier sends.
            checks = [_check_inbox(client, writer, messages) for writer in writers]
            checks.append(_check_inbox(client, outsider, messages, outsider=True))
            await _parallel(checks)
            labels = {
                "profile": profile,
                "room_members": len(writers),
                "concurrent_sends": concurrency,
                "sends": len(sent),
            }
            metrics.extend(
                [
                    _samples(
                        "NP-05", "concurrent_room_post_latency", labels, post_samples
                    ),
                    _samples(
                        "NP-05", "concurrent_room_wave_latency", labels, wave_samples
                    ),
                ]
            )
    return metrics


async def _connect_stream(client, streams, member):
    started = time.perf_counter()
    headers = _headers(member) | {"Accept": "text/event-stream"}
    response = await streams.enter_async_context(
        client.stream("GET", "/v1/events", headers=headers, timeout=None)
    )
    response.raise_for_status()
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.aiter_lines()
    assert await anext(lines) == ": ready", "SSE stream did not become ready"
    return lines, (time.perf_counter() - started) * 1_000


def _stream_counts(quick, profile):
    return (2,) if quick else {"nightly": (32, 64), "weekly": (128, 256)}[profile]


async def idle_connections(ws, quick, profile):
    counts = _stream_counts(quick, profile)
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    api = server.api()
    try:
        members = _enroll(api, key, max(counts), "stream")
    finally:
        api.close()

    metrics = []
    async with _client(server, max(counts)) as client:
        for count in counts:
            async with AsyncExitStack() as streams, asyncio.timeout(50):
                opened = await _parallel(
                    _connect_stream(client, streams, member)
                    for member in members[:count]
                )
                idle_started = time.perf_counter()

                async def wait_for_keepalive(lines, idle_started):
                    async for line in lines:
                        if line == ": keepalive":
                            return (time.perf_counter() - idle_started) * 1_000
                        assert not line, "unexpected SSE frame on an idle stream"
                    raise AssertionError("idle SSE stream closed before its keepalive")

                labels = {"profile": profile, "idle_streams": count}
                metrics.append(
                    _samples(
                        "NP-06",
                        "idle_stream_ready_latency",
                        labels,
                        [ms for _, ms in opened],
                    )
                )
                if not quick:
                    held = await _parallel(
                        wait_for_keepalive(lines, idle_started) for lines, _ in opened
                    )
                    metrics.append(
                        _samples("NP-06", "idle_stream_keepalive_latency", labels, held)
                    )
    return metrics


async def heartbeat_presence(ws, quick, profile):
    count, concurrency, rounds = (
        (3, 3, 2)
        if quick
        else {"nightly": (32, 16, 5), "weekly": (128, 32, 10)}[profile]
    )
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    api = server.api()
    try:
        observer, outsider, *members = _enroll(api, key, count + 2, "heartbeat")
        room = api.create_room(observer, "heartbeat")
        for member in members:
            api.join(member, room)
    finally:
        api.close()

    labels = {
        "profile": profile,
        "reporting_members": count,
        "concurrent_heartbeats": concurrency,
    }
    heartbeat_samples, read_samples = [], []
    async with _client(server, concurrency) as client:

        async def heartbeat(member):
            return await _request(client, "POST", "/v1/presence/heartbeat", member)

        await heartbeat(outsider)
        first = None
        for _ in range(rounds):
            observed = {}
            for offset in range(0, count, concurrency):
                batch = members[offset : offset + concurrency]
                receipts = await _parallel(heartbeat(member) for member in batch)
                for member, (receipt, elapsed) in zip(batch, receipts, strict=True):
                    observed[member.member_id] = receipt["observed_at"]
                    heartbeat_samples.append(elapsed)
            if first is None:
                first = observed
            snapshot, elapsed = await _request(client, "GET", "/v1/presence", observer)
            read_samples.append(elapsed)
            rows = snapshot["members"]
            assert [row["member_id"] for row in rows] == sorted(
                [observer.member_id, *observed]
            ), "presence snapshot returned the wrong membership scope"
            for row in rows:
                member_id = row["member_id"]
                if member_id == observer.member_id:
                    assert row["state"] == "unknown" and row["observed_at"] is None, (
                        "reading presence created a heartbeat"
                    )
                else:
                    assert (
                        row["state"] == "online"
                        and row["observed_at"] == observed[member_id]
                    ), (
                        "presence did not contain the reporting member's latest heartbeat"
                    )
                    assert row["online_since"] == first[member_id], (
                        "renewing a heartbeat reset the member's online period"
                    )
    return [
        _samples("NP-07", "presence_heartbeat_latency", labels, heartbeat_samples),
        _samples("NP-07", "presence_snapshot_latency", labels, read_samples),
    ]


async def writes_with_idle_streams(ws, quick, profile):
    counts = (0, *_stream_counts(quick, profile))
    concurrency, waves = (
        (4, 2) if quick else {"nightly": (16, 8), "weekly": (48, 12)}[profile]
    )
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    api = server.api()
    try:
        *writers, outsider = _enroll(api, key, 5, "mixed-writer")
        idle = _enroll(api, key, max(counts), "mixed-idle")
        rooms = [api.create_room(writers[0], f"mixed-{count}") for count in counts]
        for room in rooms:
            for writer in writers[1:]:
                api.join(writer, room)
            for writer in writers:
                api.read_and_ack_board(writer, room)
    finally:
        api.close()

    async def watch(lines):
        async for line in lines:
            assert line in ("", ": keepalive"), "idle member received a room event"
        raise AssertionError("idle stream disconnected during concurrent writes")

    metrics, messages = [], []
    async with (
        _client(server, concurrency) as client,
        _client(server, max(counts)) as sse,
    ):
        for count, room in zip(counts, rooms, strict=True):
            async with AsyncExitStack() as streams, asyncio.timeout(90):
                opened = await _parallel(
                    _connect_stream(sse, streams, member) for member in idle[:count]
                )
                monitors = [asyncio.create_task(watch(lines)) for lines, _ in opened]
                try:
                    warmup, _, _ = await _send_waves(
                        client, writers, room, concurrency, 1, f"mixed-warmup-{count}"
                    )
                    sent, post_samples, _ = await _send_waves(
                        client, writers, room, concurrency, waves, f"mixed-{count}"
                    )
                    messages.extend(warmup)
                    messages.extend(sent)
                    assert len({m["message_id"] for m in messages}) == len(messages), (
                        "two concurrent writes returned the same message ID"
                    )
                    await _parallel(
                        _check_inbox(
                            client, member, messages, outsider=member not in writers
                        )
                        for member in (*writers, outsider, *idle[:1])
                    )
                    for monitor in monitors:
                        if monitor.done():
                            monitor.result()
                finally:
                    for monitor in monitors:
                        monitor.cancel()
                    await asyncio.gather(*monitors, return_exceptions=True)
            metrics.append(
                _samples(
                    "NP-13",
                    "concurrent_room_post_with_idle_streams_latency",
                    {
                        "profile": profile,
                        "idle_streams": count,
                        "concurrent_sends": concurrency,
                        "sends": len(sent),
                    },
                    post_samples,
                    warmup_sends=len(warmup),
                )
            )
    return metrics


async def _ipc(dev, method, **params):
    from teamup_client.ipc import call as ipc_call

    return await ipc_call(dev.socket_path, dev.binding_key, method, params)


def _dm_notice(message):
    return {
        "kind": "dm",
        "dm_id": message["dm_id"],
        "message_id": message["message_id"],
        "sender_member_id": message["sender_member_id"],
    }


async def direct_delivery(ws, quick, profile):
    sends = 4 if quick else {"nightly": 80, "weekly": 160}[profile]
    interval = 0.01
    _, (alice, bob, bystander) = _stack.deploy(
        ws, names=("alice", "bob", "bystander"), notify=False
    )

    a, b, c = [
        await _ipc(dev, "register", name=name, description="DM latency participant")
        for dev, name in zip(
            (alice, bob, bystander), ("Alice", "Bob", "Bystander"), strict=True
        )
    ]
    dm_id = (await _ipc(alice, "open_dm", member_id=b["member_id"]))["dm_id"]
    assert (await _ipc(bob, "open_dm", member_id=a["member_id"]))["dm_id"] == dm_id

    cursors = {"alice": 0, "bob": 0, "bystander": 0}

    async def receive(dev, message):
        expected = _dm_notice(message)
        async with asyncio.timeout(20):
            while True:
                page = await _ipc(dev, "notifications", after=cursors[dev.name])
                observed_at = time.perf_counter()
                notices = page["notifications"]
                if notices:
                    assert len(notices) == 1, (
                        "recipient received duplicate or unrelated DM notices"
                    )
                    notice = notices[0]
                    event_id = notice.pop("event_id")
                    assert event_id > cursors[dev.name], (
                        "DM notice cursor did not advance"
                    )
                    assert notice == expected, (
                        "recipient received an incorrect DM notice"
                    )
                    cursors[dev.name] = event_id
                    return observed_at
                await asyncio.sleep(interval)

    samples, messages = [], []
    warmups = 2 if quick else 4
    for index in range(sends + warmups):
        sender, recipient, identity = (
            (alice, bob, a) if index % 2 == 0 else (bob, alice, b)
        )
        payload = {
            "client_message_id": f"direct-{index}",
            "text": f"direct message {index}",
        }
        started = time.perf_counter()
        message = await _ipc(sender, "send_dm", dm_id=dm_id, **payload)
        assert (
            message["dm_id"] == dm_id
            and message["sender_member_id"] == identity["member_id"]
        ), "DM send returned the wrong conversation or sender"
        assert all(message[field] == value for field, value in payload.items()), (
            "DM send returned a different body or key"
        )
        observed_at = await receive(recipient, message)
        messages.append(message)
        if index >= warmups:
            samples.append((observed_at - started) * 1_000)

    # A later, separate DM proves that the bystander's feed caught up without receiving the pair's messages.
    control_dm = (await _ipc(alice, "open_dm", member_id=c["member_id"]))["dm_id"]
    control = await _ipc(
        alice, "send_dm", dm_id=control_dm, client_message_id="control", text="control"
    )
    await receive(bystander, control)
    for dev in (alice, bob):
        remaining = (await _ipc(dev, "notifications", after=cursors[dev.name]))[
            "notifications"
        ]
        assert not remaining, "participant received an extra DM notice"
        after = 0
        for offset in range(0, len(messages), 100):
            page = await _ipc(dev, "read_dm", dm_id=dm_id, after=after, limit=100)
            batch = page["messages"]
            assert batch == messages[offset : offset + 100], (
                "DM history disagrees with send receipts"
            )
            after = batch[-1]["message_id"]

    return [
        _samples(
            "NP-14",
            "direct_message_delivery_latency",
            {"profile": profile, "agents": 2, "sends": sends},
            samples,
            observation_interval_ms=interval * 1_000,
            path="Client IPC -> Server -> SSE -> recipient Client IPC notifications",
        )
    ]


async def direct_reconnect(ws, quick, profile):
    count = 5 if quick else {"nightly": 45, "weekly": 120}[profile]
    interval = 0.01
    _, (alice, bob) = _stack.deploy(ws, notify=False)
    sender = await _ipc(alice, "register", name="Alice", description="Sends queued DMs")
    recipient = await _ipc(
        bob, "register", name="Bob", description="Recovers queued DMs"
    )
    dm_id = (await _ipc(alice, "open_dm", member_id=recipient["member_id"]))["dm_id"]
    bob.stop()
    pending = []
    for index in range(count):
        text = f"Offline DM {index}"
        message = await _ipc(
            alice,
            "send_dm",
            dm_id=dm_id,
            client_message_id=f"queued-{index}",
            text=text,
        )
        assert (
            message["dm_id"] == dm_id
            and message["sender_member_id"] == sender["member_id"]
            and message["text"] == text
        ), "queued DM receipt did not match the send"
        pending.append(_dm_notice(message))

    started = time.perf_counter()
    bob.start()
    after, samples = 0, []
    async with asyncio.timeout(30):
        while len(samples) < count:
            page = await _ipc(bob, "notifications", after=after, limit=100)
            elapsed = (time.perf_counter() - started) * 1_000
            notices = page["notifications"]
            for notice in notices:
                event_id = notice.pop("event_id")
                assert event_id > after, (
                    "reconnected Client repeated or reordered a DM event"
                )
                assert len(samples) < count and notice == pending[len(samples)], (
                    "reconnected Client returned an unexpected or duplicate DM notice"
                )
                after = event_id
                samples.append(elapsed)
            if not notices:
                await asyncio.sleep(interval)
    extra = (await _ipc(bob, "notifications", after=after))["notifications"]
    assert not extra, "reconnected Client received extra DM notices"
    return [
        _samples(
            "NP-15",
            "direct_message_reconnect_latency",
            {"profile": profile, "queued_dms": count},
            samples,
            observation_interval_ms=interval * 1_000,
        )
    ]


PRESSURE_BENCHMARKS = {
    "NP-04": ("Room creation and discovery at scale", room_scale),
    "NP-05": ("Concurrent room writes and inbox delivery", concurrent_writes),
    "NP-06": ("Idle SSE connections", idle_connections),
    "NP-07": ("Heartbeat and shared-room presence under load", heartbeat_presence),
    "NP-13": (
        "Concurrent room writes with idle SSE connections",
        writes_with_idle_streams,
    ),
    "NP-14": ("Direct message delivery to durable Client notices", direct_delivery),
    "NP-15": (
        "Direct message backlog delivery after Client reconnect",
        direct_reconnect,
    ),
}
