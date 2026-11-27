"""Model-free pressure on the public Client and Admin Console HTTP routes."""

import asyncio
import time
from collections import defaultdict
from contextlib import closing

import httpx

from tests.scenarios import _results, _stack
from tests.scenarios._scenario_browser import account_cli, client_console, console_login
from tests.scenarios.perf.benchmarks import metric, ms

CLIENT_CHAT_PAGE_SIZE = 30
ROOM_SETUP_WORKERS = 8
ADMIN_ROOMS = "/v1/console/rooms"
ADMIN_HEALTH = "/v1/console/maintenance/health"


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _request(client, method, path, **kwargs):
    started = time.perf_counter()
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    body = response.json()
    return body, ms(time.perf_counter() - started)


def _metrics(benchmark, labels, samples):
    return [
        metric(benchmark, name, "ms", labels, _results.stats(values), transport="HTTP")
        for name, values in samples.items()
    ]


async def _seed_rooms(client, creator, expected, total):
    start = len(expected)
    names = iter(f"Background room {i:05}" for i in range(start, total))

    async def worker():
        for name in names:
            body, _ = await _request(
                client,
                "POST",
                "/v1/rooms",
                headers=_bearer(creator.token),
                json={"name": name},
            )
            room = body["room_id"]
            assert room not in expected and body["name"] == name, "invalid room receipt"
            expected[room] = name

    async with asyncio.timeout(180), asyncio.TaskGroup() as group:
        for _ in range(ROOM_SETUP_WORKERS):
            group.create_task(worker())
    assert len(expected) == total


async def _send_chat(client, sender, room, key):
    path = f"/v1/rooms/{room}/messages"
    body = {
        "client_message_id": key,
        "text": f"# Console load\n\nBenchmark message {key}",
    }
    receipt, took = await _request(
        client, "POST", path, headers=_bearer(sender.token), json=body
    )
    assert (
        receipt["room_id"] == room and receipt["sender_member_id"] == sender.member_id
    )
    assert all(receipt[field] == value for field, value in body.items())
    return receipt, took


def _check_transcript(page, expected):
    assert page["state"] == "ready", "Client Console transcript became unavailable"
    assert [
        (row["id"], row["sender_member_id"], row["text"]) for row in page["messages"]
    ] == [
        (str(row["message_id"]), row["sender_member_id"], row["text"])
        for row in expected
    ], "Client Console transcript disagreed with send receipts"


async def client_console_refresh(ws, quick, profile):
    sizes, memberships, refreshes = (
        ((12, 110), (1, 4), 2)
        if quick
        else {
            "nightly": ((100, 350), (4, 12), 8),
            "weekly": ((1_000, 3_000), (12, 32), 14),
        }[profile]
    )
    username, password, key = "console-reader", _stack.new_secret(), _stack.new_secret()
    server = _stack.Server(ws, {"room-owner": key})
    account_cli(server, "create", username, password=password)
    server.start()
    room_name = "Client Console active room"
    async with httpx.AsyncClient(
        base_url=server.url, timeout=10, trust_env=False
    ) as writer:
        with client_console(ws, server, username) as (developer, launch, _):
            with closing(_stack.Api(server.url, trust_env=False)) as api:
                enrollment = api.call(
                    "POST",
                    "/v1/auth/login",
                    json={"username": username, "password": password},
                )["access_token"]
                binding = developer.bindings[0]
                local = api.enroll(
                    enrollment,
                    "Console reader",
                    harness=binding["harness_instance_id"],
                    session=binding["session_key"],
                    token=binding["member_token"],
                )
                peer = api.enroll(key, "Room creator", session="console-peer")
                room = api.create_room(peer, room_name)
                api.join(local, room)
                board = api.read_and_ack_board(local, room)
                api.read_and_ack_board(peer, room)
            console_login(launch, username, password)
            async with httpx.AsyncClient(
                base_url=launch.base_url,
                headers=launch.headers,
                timeout=10,
                trust_env=False,
            ) as console:
                return await _client_refreshes(
                    console,
                    writer,
                    local,
                    peer,
                    room,
                    room_name,
                    board["version"],
                    sizes,
                    memberships,
                    refreshes,
                    profile,
                )


async def _client_refreshes(
    console,
    writer,
    local,
    peer,
    room,
    room_name,
    version,
    sizes,
    memberships,
    refreshes,
    profile,
):
    rooms, joined = {room: room_name}, {room}
    messages = []
    for index in range(CLIENT_CHAT_PAGE_SIZE + 5):
        receipt, _ = await _send_chat(writer, peer, room, f"seed-{index}")
        messages.append(receipt)

    metrics = []
    for size, joined_size in zip(sizes, memberships, strict=True):
        await _seed_rooms(writer, peer, rooms, size)
        for room_id in [item for item in rooms if item not in joined][
            : joined_size - len(joined)
        ]:
            await _request(
                writer,
                "POST",
                f"/v1/rooms/{room_id}/join",
                headers=_bearer(local.token),
            )
            joined.add(room_id)
        samples = defaultdict(list)

        async def get(path, label, *, samples=samples, **params):
            body, elapsed = await _request(console, "GET", path, params=params)
            samples[label].append(elapsed)
            return body

        async with asyncio.timeout(120):
            for repetition in range(refreshes):
                started = time.perf_counter()
                workspace = await get(
                    "/api/workspace", "client_workspace_refresh_latency"
                )
                (session,) = workspace["sessions"]
                context = {"session": session["id"], "room": room}
                async with asyncio.TaskGroup() as group:
                    latest = group.create_task(
                        get("/api/room", "client_chat_latest_latency", **context)
                    )
                    roster = group.create_task(
                        get(
                            "/api/room-members", "client_room_roster_latency", **context
                        )
                    )
                    board = group.create_task(
                        get("/api/room-board", "client_room_board_latency", **context)
                    )
                samples["client_workspace_and_room_wall_latency"].append(
                    ms(time.perf_counter() - started)
                )

                assert (
                    session["member_id"] == local.member_id
                    and session["state"] == "ready"
                )
                assert workspace["directory_available"], (
                    f"directory unavailable at {size} rooms"
                )
                rows = {row["id"]: row for row in workspace["rooms"]}
                assert (
                    len(workspace["rooms"]) == size
                    and {identifier: row["name"] for identifier, row in rows.items()}
                    == rooms
                ), "Client Console omitted or duplicated rooms"
                assert {
                    identifier: [
                        membership["session"] for membership in row["memberships"]
                    ]
                    for identifier, row in rows.items()
                    if row["memberships"]
                } == {identifier: [session["id"]] for identifier in joined}, (
                    "Client Console changed room membership scope"
                )
                assert rows[room]["latest_message_id"] == str(
                    messages[-1]["message_id"]
                )
                assert roster.result()["state"] == "ready" and {
                    row["id"]: row["acknowledged"] for row in roster.result()["members"]
                } == {local.member_id: version, peer.member_id: version}
                assert (
                    board.result()["state"] == "ready"
                    and board.result()["board"]["version"] == version
                )

                _check_transcript(latest.result(), messages[-CLIENT_CHAT_PAGE_SIZE:])
                before = latest.result()["older"]
                assert before == str(messages[-CLIENT_CHAT_PAGE_SIZE]["message_id"])
                older = await get(
                    "/api/room",
                    "client_chat_older_page_latency",
                    before=before,
                    **context,
                )
                _check_transcript(
                    older, messages[-2 * CLIENT_CHAT_PAGE_SIZE : -CLIENT_CHAT_PAGE_SIZE]
                )

                receipt, _ = await _send_chat(
                    writer, peer, room, f"live-{size}-{repetition}"
                )
                after = str(messages[-1]["message_id"])
                catchup = await get(
                    "/api/room", "client_chat_catchup_latency", after=after, **context
                )
                _check_transcript(catchup, [receipt])
                messages.append(receipt)

        metrics.extend(
            _metrics(
                "NP-11",
                {"profile": profile, "rooms": size, "joined_rooms": joined_size},
                samples,
            )
        )
    return metrics


async def _poll_admin(client, token, room, expected_rooms, earlier, later, samples):
    async def get(path, name):
        body, elapsed = await _request(client, "GET", path, headers=_bearer(token))
        samples[name].append(elapsed)
        return body

    async with asyncio.TaskGroup() as group:
        rooms = group.create_task(get(ADMIN_ROOMS, "admin_room_list_latency"))
        health = group.create_task(get(ADMIN_HEALTH, "admin_health_poll_latency"))
        load = group.create_task(
            get(f"{ADMIN_ROOMS}/{room}/load", "admin_room_load_poll_latency")
        )
    assert [
        (row["room_id"], row["name"]) for row in rooms.result()["rooms"]
    ] == expected_rooms
    sources = health.result()["sources"]
    assert set(sources) == {"host", "gateway", "collaboration"}
    assert all(
        sources[name]["state"] == "fresh" for name in ("gateway", "collaboration")
    )
    snapshot = load.result()["room"]
    assert snapshot["state"] == "fresh" and snapshot["room_id"] == room
    assert (
        snapshot["member_count"] == 2
        and earlier <= snapshot["messages_stored"] <= later
    )


async def _chat_wave(client, writer, reader, room, wave, concurrency, cursor, samples):
    async def send(index):
        started = time.perf_counter()
        key = f"{room}-wave-{wave}-{index}"
        message, latency = await _send_chat(client, writer, room, key)
        return message, latency, started

    async with asyncio.TaskGroup() as group:
        posts = [group.create_task(send(i)) for i in range(concurrency)]
    expected = {}
    for post in posts:
        message, took, started = post.result()
        expected[message["message_id"]] = (message, started)
        samples["chat_commit_latency"].append(took)
    assert len(expected) == concurrency, "chat writes returned duplicate IDs"
    inbox, elapsed = await _request(
        client,
        "GET",
        "/v1/inbox",
        headers=_bearer(reader.token),
        params={"after": cursor, "limit": 100},
    )
    observed = time.perf_counter()
    samples["chat_inbox_read_latency"].append(elapsed)
    for event in inbox["events"]:
        assert event["event_id"] > cursor and event["kind"] == "message"
        cursor = event["event_id"]
        message = event["message"]
        sent, started = expected.pop(message["message_id"], (None, None))
        assert message == sent, "unexpected inbox delivery"
        samples["chat_delivery_observation_latency"].append(ms(observed - started))
    assert not expected, "missing chat deliveries during Admin polling workload"
    return cursor


async def admin_console_polling(ws, quick, profile):
    room_count, operators, concurrency, waves = (
        (12, 2, 2, 3)
        if quick
        else {"nightly": (300, 4, 6, 12), "weekly": (3_000, 8, 12, 20)}[profile]
    )
    key = _stack.new_secret()
    server = _stack.Server(ws, {"room-owner": key})
    accounts = [(f"admin-{i}", _stack.new_secret()) for i in range(operators)]
    for username, password in accounts:
        account_cli(server, "create", username, password=password)
        account_cli(server, "grant-console", username)
        account_cli(server, "grant-maintenance", username)
    server.start()
    rooms, chat_rooms = {}, []
    with closing(_stack.Api(server.url, trust_env=False)) as api:
        writer = api.enroll(key, "Admin benchmark sender", session="send")
        reader = api.enroll(key, "Admin benchmark receiver", session="receive")
        for name in ("Admin benchmark chat one", "Admin benchmark chat two"):
            room = api.create_room(writer, name)
            chat_rooms.append(room)
            rooms[room] = name
            api.join(reader, room)
            for member in (writer, reader):
                api.read_and_ack_board(member, room)
        tokens = [
            api.call(
                "POST",
                "/v1/auth/login",
                json={
                    "username": username,
                    "password": password,
                    "purpose": "console",
                },
            )["access_token"]
            for username, password in accounts
        ]
    limits = httpx.Limits(
        max_connections=max(ROOM_SETUP_WORKERS, operators * 3 + concurrency)
    )
    metrics, cursor = [], 0
    async with httpx.AsyncClient(
        base_url=server.url, timeout=10, limits=limits, trust_env=False
    ) as client:
        await _seed_rooms(client, writer, rooms, room_count)
        expected_rooms = sorted(rooms.items())
        for room, active in zip(chat_rooms, ([], tokens), strict=True):
            samples = defaultdict(list)
            async with asyncio.timeout(120):
                for wave in range(waves):
                    started = time.perf_counter()
                    async with asyncio.TaskGroup() as group:
                        chat = group.create_task(
                            _chat_wave(
                                client,
                                writer,
                                reader,
                                room,
                                wave,
                                concurrency,
                                cursor,
                                samples,
                            )
                        )
                        for token in active:
                            group.create_task(
                                _poll_admin(
                                    client,
                                    token,
                                    room,
                                    expected_rooms,
                                    wave * concurrency,
                                    (wave + 1) * concurrency,
                                    samples,
                                )
                            )
                    cursor = chat.result()
                    samples["chat_and_admin_wave_wall_latency"].append(
                        ms(time.perf_counter() - started)
                    )
            metrics.extend(
                _metrics(
                    "NP-12",
                    {
                        "profile": profile,
                        "rooms": room_count,
                        "polling_operators": len(active),
                        "concurrent_sends": concurrency,
                    },
                    samples,
                )
            )
    return metrics


CONSOLE_BENCHMARKS = {
    "NP-11": (
        "Client Console workspace and room refresh at scale",
        client_console_refresh,
    ),
    "NP-12": ("Admin Console polling during live chat", admin_console_polling),
}
