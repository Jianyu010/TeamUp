"""Scripted, model-free benchmarks NP-01..NP-03.

Every benchmark drives TeamUp through its public entry points only: the
Client IPC socket, the Server HTTP/SSE API and the Server database file size
(via ``os.stat``, never SQL).
"""

import asyncio
import os
import sys
import time

import httpx

from tests.scenarios import _results, _stack

sys.path.insert(0, str(_stack.ROOT / "client/shared/src"))
from teamup_client.ipc import call as ipc_call  # public IPC entry point

KEY = _stack.new_secret()
ENV = _results.environment()


def ms(seconds):
    return seconds * 1000.0


def metric(id_, name, unit, labels, stats, **extra):
    return {
        "id": id_,
        "name": name,
        "unit": unit,
        "labels": labels,
        "stats": stats,
        "extra": ENV | extra,
    }


# NP-01 -----------------------------------------------------------------------


async def np01(ws, quick):
    """A -> IPC -> Client A -> Server -> SSE -> Client B notice, per mention."""
    n = 10 if quick else 200
    _, (alice, bob) = _stack.deploy(ws, notify=False)
    tail = _stack.LogTail(bob.log).start()
    try:
        sock_a, key_a = str(alice.socket_path), alice.binding_key
        sock_b, key_b = str(bob.socket_path), bob.binding_key
        await ipc_call(
            sock_a,
            key_a,
            "register",
            {"name": "Backend", "description": "Sends messages during the benchmark."},
        )
        b = await ipc_call(
            sock_b,
            key_b,
            "register",
            {
                "name": "Frontend",
                "description": "Receives messages during the benchmark.",
            },
        )
        room = (
            await ipc_call(
                sock_a, key_a, "create_room", {"name": "dm", "rules": "pair"}
            )
        )["room_id"]
        for sock, key, joined in ((sock_a, key_a, True), (sock_b, key_b, False)):
            if not joined:
                await ipc_call(sock, key, "join_room", {"room_id": room})
            board = await ipc_call(sock, key, "get_board", {"room_id": room})
            await ipc_call(
                sock, key, "ack_board", {"room_id": room, "version": board["version"]}
            )

        samples = []
        for i in range(n + 5):
            params = {
                "room_id": room,
                "client_message_id": f"np01-{i}",
                "text": f"ping {i}",
                "mention_member_id": b["member_id"],
            }
            started = time.perf_counter()
            receipt = await ipc_call(sock_a, key_a, "send_message", params)
            seen = await tail.wait(receipt["message_id"])
            if i >= 5:  # the first sends warm connections and are discarded
                samples.append(ms(seen - started))
            await asyncio.sleep(0.02)
        return [
            metric(
                "NP-01",
                "dm_delivery_latency",
                "ms",
                {"agents": 2},
                _results.stats(samples),
                transport="two-member room with @mention (DM not merged)",
                path="IPC -> Client A -> Server -> SSE -> Client B notice",
            )
        ]
    finally:
        tail.stop()


# NP-02 -----------------------------------------------------------------------

LEVELS = {"low": 2, "medium": 10, "high": 40}


async def consume(client, member, ready, recv):
    """Hold a live SSE stream and drain the inbox whenever it signals."""
    after = 0

    async def drain():
        nonlocal after
        while True:
            response = await client.get(
                "/v1/inbox",
                params={"after": after, "limit": 100},
                headers={"Authorization": f"Bearer {member.token}"},
            )
            stamp = time.perf_counter()
            events = response.json()["events"]
            for event in events:
                started = float(event["message"]["text"].split("|", 1)[0])
                recv.append(ms(stamp - started))
                after = event["event_id"]
            if len(events) < 100:
                return

    headers = {"Authorization": f"Bearer {member.token}", "Accept": "text/event-stream"}
    async with client.stream(
        "GET", "/v1/events", headers=headers, timeout=None
    ) as stream:
        lines = stream.aiter_lines()
        await anext(lines)  # ": ready"
        await drain()
        ready.set()
        async for line in lines:
            if line.startswith("event:"):
                await drain()


async def np02_level(server, label, rate, seconds):
    members = []
    api = server.api()
    try:
        members = [
            api.enroll(KEY, f"agent-{i}", session=f"{label}-{i}") for i in range(5)
        ]
        room = api.create_room(members[0], f"room-{label}", rules="bench")
        for member in members[1:]:
            api.join(member, room)
        for member in members:
            api.read_and_ack_board(member, room)
    finally:
        api.close()

    limits = httpx.Limits(max_connections=60, max_keepalive_connections=60)
    clients = [
        httpx.AsyncClient(
            base_url=server.url, verify=server.verify, limits=limits, timeout=30
        )
        for _ in members
    ]
    ready = [asyncio.Event() for _ in members]
    received = [[] for _ in members]
    consumers = [
        asyncio.create_task(consume(c, m, r, rec))
        for c, m, r, rec in zip(clients, members, ready, received, strict=True)
    ]
    post_ms, errors, sent_ok = [], 0, 0
    try:
        await asyncio.wait_for(asyncio.gather(*(r.wait() for r in ready)), 20)
        per_sender = rate / 5
        count = int(seconds * per_sender)

        async def post(idx, seq):
            nonlocal errors, sent_ok
            started = time.perf_counter()
            try:
                response = await clients[idx].post(
                    f"/v1/rooms/{room}/messages",
                    headers={"Authorization": f"Bearer {members[idx].token}"},
                    json={
                        "client_message_id": f"{label}-{idx}-{seq}",
                        "text": f"{started!r}|{idx}",
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError(response.status_code)
                post_ms.append(ms(time.perf_counter() - started))
                sent_ok += 1
            except Exception:  # noqa: BLE001 - any failed request counts as an error
                errors += 1

        async def sender(idx):
            origin = time.perf_counter() + idx * (1 / rate)  # stagger the senders
            tasks = []
            for seq in range(count):
                delay = origin + seq / per_sender - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                tasks.append(asyncio.create_task(post(idx, seq)))
            await asyncio.gather(*tasks)

        window = time.perf_counter()
        await asyncio.gather(*(sender(i) for i in range(5)))
        window = time.perf_counter() - window
        expected = sent_ok * 4
        # Wait for fan-out to settle, then stop the consumers.
        deadline = time.monotonic() + 15
        while sum(map(len, received)) < expected and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        fanout = [x for rec in received for x in rec]
        return post_ms, fanout, sent_ok, errors, window, max(0, expected - len(fanout))
    finally:
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        for c in clients:
            await c.aclose()


async def np02(ws, quick):
    seconds = 3 if quick else 30
    server = _stack.Server(ws, {"owner": KEY}).start()
    out = []
    for label, rate in LEVELS.items():
        post_ms, fanout, ok, errors, window, missing = await np02_level(
            server, label, rate, seconds
        )
        labels = {"agents": 5, "load": label}
        extra = {"target_msg_per_s": rate, "window_s": _results.rounded(window)}
        out += [
            metric(
                "NP-02",
                "room_post_latency",
                "ms",
                labels,
                _results.stats(post_ms),
                **extra,
            ),
            metric(
                "NP-02",
                "room_fanout_latency",
                "ms",
                labels,
                _results.stats(fanout),
                **extra,
            ),
            metric(
                "NP-02",
                "room_throughput",
                "msg/s",
                labels,
                _results.single(ok / window),
                **extra,
            ),
            metric(
                "NP-02",
                "room_errors",
                "count",
                labels,
                _results.single(errors),
                **extra,
            ),
            metric(
                "NP-02",
                "room_undelivered",
                "count",
                labels,
                _results.single(missing),
                **extra,
            ),
        ]
    return out


# NP-03 -----------------------------------------------------------------------


def db_bytes(path):
    size = os.stat(path).st_size
    wal = path.with_name(path.name + "-wal")
    return size, (os.stat(wal).st_size if wal.exists() else 0)


async def np03(ws, quick):
    checkpoints = [100, 500] if quick else [1_000, 10_000, 50_000]
    samples = 10 if quick else 30
    server = _stack.Server(ws, {"owner": KEY}).start()
    api = server.api()
    writer = api.enroll(KEY, "writer", session="w")
    reader = api.enroll(KEY, "reader", session="r")
    room = api.create_room(writer, "history", rules="bench")
    api.join(reader, room)
    api.read_and_ack_board(writer, room)
    api.read_and_ack_board(reader, room)
    api.close()

    client = httpx.AsyncClient(
        base_url=server.url,
        verify=server.verify,
        timeout=60,
        limits=httpx.Limits(max_connections=32),
    )
    auth = {
        w: {"Authorization": f"Bearer {m.token}"}
        for w, m in (("w", writer), ("r", reader))
    }
    url = f"/v1/rooms/{room}/messages"
    total = 0
    last_id = 0
    out = []

    async def post(i):
        started = time.perf_counter()
        response = await client.post(
            url,
            headers=auth["w"],
            json={
                "client_message_id": f"h-{i}",
                "text": f"message number {i} " + "x" * 80,
            },
        )
        response.raise_for_status()
        return response.json()["message_id"], ms(time.perf_counter() - started)

    try:
        for checkpoint in checkpoints:
            target = checkpoint - samples
            seeding = time.perf_counter()
            start_total = total
            queue = iter(range(total, target))

            async def worker(queue=queue):
                nonlocal last_id
                for i in queue:
                    message_id, _ = await post(i)
                    last_id = max(last_id, message_id)

            await asyncio.gather(*(worker() for _ in range(16)))
            seeded = max(1, target - start_total)
            seed_rate = seeded / (time.perf_counter() - seeding)
            total = target
            await asyncio.sleep(1.0)  # quiet window before measuring

            post_ms = []
            for _ in range(samples):
                message_id, took = await post(total)
                total += 1
                last_id = max(last_id, message_id)
                post_ms.append(took)
            page_ms = []
            for _ in range(samples):
                started = time.perf_counter()
                response = await client.get(
                    url,
                    headers=auth["r"],
                    params={"after": max(0, last_id - 100), "limit": 100},
                )
                response.raise_for_status()
                page_ms.append(ms(time.perf_counter() - started))
                assert response.json()["messages"], "latest page was empty"
            started = time.perf_counter()
            after, pages, seen = 0, 0, 0
            while True:
                response = await client.get(
                    url, headers=auth["r"], params={"after": after, "limit": 100}
                )
                response.raise_for_status()
                batch = response.json()["messages"]
                pages += 1
                seen += len(batch)
                if not batch:
                    break
                after = batch[-1]["message_id"]
            scan_ms = ms(time.perf_counter() - started)
            assert seen == total, f"history returned {seen} of {total} messages"
            size, wal = db_bytes(server.db)
            labels = {"history": checkpoint}
            extra = {"seed_msg_per_s": _results.rounded(seed_rate)}
            out += [
                metric(
                    "NP-03",
                    "history_post_latency",
                    "ms",
                    labels,
                    _results.stats(post_ms),
                    **extra,
                ),
                metric(
                    "NP-03",
                    "history_latest_page_latency",
                    "ms",
                    labels,
                    _results.stats(page_ms),
                    **extra,
                ),
                metric(
                    "NP-03",
                    "history_full_scan",
                    "ms",
                    labels,
                    _results.single(scan_ms),
                    pages=pages,
                    messages=seen,
                    **extra,
                ),
                metric(
                    "NP-03",
                    "history_db_size",
                    "bytes",
                    labels,
                    _results.single(size),
                    wal_bytes=wal,
                    **extra,
                ),
            ]
    finally:
        await client.aclose()
    return out


BENCHMARKS = {
    "NP-01": ("Two-agent mention delivery latency", np01),
    "NP-02": ("Five-agent room chat at three load levels", np02),
    "NP-03": ("History growth to 1k, 10k and 50k messages", np03),
}
