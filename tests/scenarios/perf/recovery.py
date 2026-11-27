"""Archive pressure and the weekly isolated online backup/restore drill."""

import asyncio
import json
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts.ops import pull_backup
from tests.scenarios import _results, _stack
from tests.scenarios.perf.benchmarks import metric, ms

OPS = _stack.ROOT / "scripts" / "ops"


def _auth(member):
    return {"Authorization": f"Bearer {member.token}"}


async def _create_room(client, number):
    response = await client.post("/v1/rooms", json={"name": f"Archive load {number}"})
    response.raise_for_status()
    return response.json()["room_id"]


async def _archive_room(client, room):
    response = await client.post(f"/v1/rooms/{room}/archive")
    response.raise_for_status()
    archived = response.json()
    assert archived["room_id"] == room
    assert archived["archive_reason"] == "goal_reached"
    return archived


async def _bounded_map(run, items, *, workers):
    # One shared iterator bounds the number of outstanding HTTP requests.
    entries = iter(items)

    async def consume():
        return [await run(item) for item in entries]

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(consume()) for _ in range(workers)]
    return [value for task in tasks for value in task.result()]


async def archive_writes(ws, quick, profile):
    background, races = (
        (12, 6) if quick else ((2500, 120) if profile == "weekly" else (300, 30))
    )
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    with closing(server.api()) as api:
        creator = api.enroll(key, "Archive load creator", session="creator")

    async with httpx.AsyncClient(
        base_url=server.url, headers=_auth(creator), timeout=10
    ) as client:
        await _bounded_map(
            lambda number: _create_room(client, number), range(background), workers=8
        )

        async def race(number):
            room = await _create_room(client, background + number)
            path = f"/v1/rooms/{room}"
            response = await client.get(f"{path}/board")
            response.raise_for_status()
            response = await client.post(
                f"{path}/board/ack", json={"version": response.json()["version"]}
            )
            response.raise_for_status()
            body = {"client_message_id": f"before-{number}", "text": "Before archive"}
            response = await client.post(f"{path}/messages", json=body)
            response.raise_for_status()
            before = response.json()

            async def close_room():
                started = time.perf_counter()
                receipt = await _archive_room(client, room)
                return receipt, ms(time.perf_counter() - started)

            async with asyncio.TaskGroup() as group:
                archive = group.create_task(close_room())
                send = group.create_task(
                    client.post(
                        f"{path}/messages",
                        json={
                            "client_message_id": f"race-{number}",
                            "text": "Racing chat",
                        },
                    )
                )

            receipt, took = archive.result()
            racing = send.result()
            expected = [before]
            if racing.status_code == 200:
                expected.append(racing.json())
            else:
                assert racing.status_code == 409, racing.status_code
                assert racing.json()["error"]["code"] == "room_archived"
            response = await client.get(f"{path}/messages")
            response.raise_for_status()
            assert response.json()["messages"] == expected, (
                "archive/send race changed history"
            )
            assert await _archive_room(client, room) == receipt
            return took, racing.is_success

        samples = await _bounded_map(race, range(races), workers=8)
        response = await client.get("/v1/rooms")
        response.raise_for_status()
        assert len(response.json()["rooms"]) == background

    committed = sum(wrote for _, wrote in samples)
    return [
        metric(
            "NP-08-write",
            "archive_transition_latency",
            "ms",
            {"profile": profile, "background_rooms": background, "workers": 8},
            _results.stats([took for took, _ in samples]),
            racing_chat_commits=committed,
            racing_chat_rejections=races - committed,
        )
    ]


async def archive_reads(ws, quick, profile):
    total, readers = (
        (24, 2) if quick else ((3000, 8) if profile == "weekly" else (300, 4))
    )
    page_size = 10 if quick else 50
    key = _stack.new_secret()
    server = _stack.Server(ws, {"owner": key}).start()
    with closing(server.api()) as api:
        creator = api.enroll(key, "Archive reader", session="reader")
        outsider = api.enroll(key, "Archive outsider", session="outsider")
        live = api.create_room(creator, "Active room")

    async with httpx.AsyncClient(
        base_url=server.url, headers=_auth(creator), timeout=10
    ) as client:

        async def seed(number):
            return await _archive_room(client, await _create_room(client, number))

        records = await _bounded_map(seed, range(total), workers=8)
        expected = sorted(records, key=lambda record: record["room_id"])

        async def scan():
            started = time.perf_counter()
            after, page_times = None, []
            for offset in range(0, total, page_size):
                params = {"limit": page_size}
                if after:
                    params["after"] = after
                page_start = time.perf_counter()
                response = await client.get("/v1/rooms/archive", params=params)
                response.raise_for_status()
                page_times.append(ms(time.perf_counter() - page_start))
                page = response.json()
                expected_page = expected[offset : offset + page_size]
                assert page["rooms"] == expected_page, (
                    f"archive page differs at offset {offset}"
                )
                after = page["next_after"]
                assert after == (
                    expected_page[-1]["room_id"] if offset + page_size < total else None
                )
            return page_times, ms(time.perf_counter() - started)

        async with asyncio.TaskGroup() as group:
            scans = [group.create_task(scan()) for _ in range(readers)]
        response = await client.get("/v1/rooms", headers=_auth(creator))
        response.raise_for_status()
        assert [row["room_id"] for row in response.json()["rooms"]] == [live]
        response = await client.get("/v1/rooms/archive", headers=_auth(outsider))
        response.raise_for_status()
        assert response.json() == {"rooms": [], "next_after": None}

    labels = {
        "profile": profile,
        "archived_rooms": total,
        "readers": readers,
        "page_size": page_size,
    }
    return [
        metric(
            "NP-08-read",
            "archive_page_latency",
            "ms",
            labels,
            _results.stats([took for scan in scans for took in scan.result()[0]]),
        ),
        metric(
            "NP-08-read",
            "archive_scan_latency",
            "ms",
            labels,
            _results.stats([scan.result()[1] for scan in scans]),
        ),
    ]


def _run_cli(script, *args, timeout=35):
    command = [sys.executable, str(OPS / script), *map(str, args)]
    result = _stack.run_process(
        command, capture_output=True, text=True, timeout=timeout
    )
    assert result.returncode == 0, f"{script} failed: {result.stderr[-500:]}"
    return result.stdout


def _capture_and_receive(source, scratch, receiver):
    """Exercise receiver publication using the production capture process as transport."""
    host, label = "teamup-recovery-drill", "synthetic"
    options = ["--root", str(receiver), "--source", label]
    ssh_config = scratch / "unused-ssh-config"
    capture_window = []

    def local_transport(command, **kwargs):
        assert command == [
            "/usr/bin/ssh",
            "-F",
            str(ssh_config),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ClearAllForwardings=yes",
            "--",
            host,
            label,
        ]
        capture_window.append(time.perf_counter())
        result = _stack.run_process(
            [
                sys.executable,
                str(OPS / "capture_backup.py"),
                "--source",
                str(source),
                "--scratch",
                str(scratch),
                "--timeout-seconds",
                "20",
            ],
            stdin=kwargs["stdin"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            timeout=kwargs["timeout"],
        )
        capture_window.append(time.perf_counter())
        return result

    adapter = SimpleNamespace(
        run=local_transport,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    started = time.perf_counter()
    with patch.object(pull_backup, "subprocess", adapter):
        assert (
            pull_backup.main(
                [
                    "pull",
                    *options,
                    "--host",
                    host,
                    "--ssh-config",
                    str(ssh_config),
                    "--ssh-timeout-seconds",
                    "25",
                ]
            )
            == 0
        ), "receiver failed to publish a verified backup generation"
    published = time.perf_counter()
    _run_cli("pull_backup.py", "check", *options, "--max-age-hours", "1")
    [generation] = [path for path in receiver.iterdir() if path.is_dir()]
    manifest = json.loads((generation / "manifest.json").read_text())
    return (
        generation / manifest["sources"][label]["file"],
        manifest["sources"][label]["sha256"],
        manifest["created_at"],
        started,
        published,
        time.perf_counter(),
        tuple(capture_window),
    )


async def _send_backup_messages(client, room, start, stop):
    path = f"/v1/rooms/{room}/messages"
    samples = []
    for sequence in range(start, stop):
        response = await client.post(
            path,
            json={
                "client_message_id": f"backup-{sequence}",
                "text": f"Recovery log entry {sequence}: " + "r" * 3800,
            },
        )
        response.raise_for_status()
        samples.append((time.perf_counter(), response.json()))
    return samples


async def _live_capture(client, room, source, scratch, receiver, seed_end, write_count):
    # Start online capture beside the continuous writer on the live Server.
    # Both the HTTP calls and the capture script have bounded timeouts.
    capture = asyncio.create_task(
        asyncio.to_thread(_capture_and_receive, source, scratch, receiver)
    )
    try:
        writes = await _send_backup_messages(
            client, room, seed_end, seed_end + write_count
        )
    finally:
        # The workspace must not be deleted while the capture subprocess uses it.
        result = await asyncio.shield(capture)
    return result, writes


def _read_history(api, member, room, expected_count):
    history = []
    for _ in range(expected_count // 100 + 1):
        after = history[-1]["message_id"] if history else 0
        page = api.history(member, room, after=after, limit=100)
        history.extend(page)
        assert len(history) <= expected_count, "unexpected restored room messages"
        if len(page) < 100:
            return history
    raise AssertionError("room history failed to terminate")


async def backup_restore(ws, quick, profile):
    seed_count, write_count = (40, 40) if quick else (600, 160)
    scratch, receiver, restore = (
        ws.path(name) for name in ("capture", "receiver", "restore")
    )
    for directory in (scratch, receiver, restore):
        directory.mkdir(mode=0o700)

    keys = {"owner": _stack.new_secret(), "peer": _stack.new_secret()}
    source = _stack.Server(ws, keys).start()
    with closing(source.api()) as api:
        creator = api.enroll(keys["owner"], "Backup creator", session="creator")
        peer = api.enroll(keys["peer"], "Backup peer", session="peer")
        archived_room = api.create_room(
            creator, "Recovered archive", rules="Keep this Board"
        )
        api.join(peer, archived_room)
        for member in (creator, peer):
            api.read_and_ack_board(member, archived_room)
        live_room = api.create_room(creator, "Live backup traffic")
        api.read_and_ack_board(creator, live_room)
        archived_path = f"/v1/rooms/{archived_room}"
        message = api.send(creator, archived_room, "Archived recovery sentinel")
        task = api.call(
            "POST",
            f"{archived_path}/tasks",
            creator.token,
            json={"title": "Recovered task", "client_task_id": "recover-task"},
        )
        task_path = f"{archived_path}/tasks/{task['task_id']}"
        api.call("POST", f"{task_path}/join", peer.token)
        api.call(
            "POST",
            f"{task_path}/progress",
            peer.token,
            json={
                "client_progress_id": "recover-progress",
                "note": "Recovery evidence",
            },
        )
        task = api.call("POST", f"{task_path}/finish", creator.token)
        task_history = api.call("GET", f"{task_path}/events", peer.token)
        assert [event["kind"] for event in task_history["events"]] == [
            "created",
            "joined",
            "progress",
            "finished",
        ]
        receipt = api.call("POST", f"{archived_path}/archive", creator.token)
        board = api.call("GET", f"{archived_path}/board/view", creator.token)
        roster = api.call("GET", f"{archived_path}/members", creator.token)

    async with httpx.AsyncClient(
        base_url=source.url, headers=_auth(creator), timeout=5
    ) as client:
        seeded = await _send_backup_messages(client, live_room, 0, seed_count)
        capture, written = await _live_capture(
            client, live_room, source.db, scratch, receiver, seed_count, write_count
        )
    (
        snapshot,
        digest,
        published_at,
        receive_start,
        receive_end,
        verification_end,
        (capture_start, capture_end),
    ) = capture
    live_commits = sum(
        capture_start <= committed <= capture_end for committed, _ in written
    )
    assert live_commits > 0, "no source writes committed during the capture process"

    with closing(source.api()) as api:
        after_capture = api.send(creator, live_room, "Post-capture source sentinel")

    destination = restore / "recovered.sqlite3"
    started = time.perf_counter()
    _run_cli(
        "restore_backup.py",
        "--snapshot",
        snapshot,
        "--sha256",
        digest,
        "--destination",
        destination,
        "--timeout-seconds",
        "25",
    )
    restored_at = time.perf_counter()
    recovered = _stack.Server(ws, {}, db=destination).start()
    with closing(recovered.api()) as api:
        assert api.call("GET", "/v1/rooms/archive", peer.token)["rooms"] == [receipt]
        assert api.call("GET", f"{archived_path}/members", peer.token) == roster
        assert api.call("GET", f"{archived_path}/board/view", peer.token) == board
        assert api.history(peer, archived_room) == [message]
        assert api.call("GET", task_path, peer.token) == task
        assert api.call("GET", f"{task_path}/events", peer.token) == task_history
        assert (
            api.call(
                "POST",
                f"{archived_path}/messages",
                creator.token,
                json={"client_message_id": "frozen", "text": "Still frozen"},
                error="room_archived",
            )
            == 409
        )
        expected = [message for _, message in seeded + written]
        history = _read_history(api, creator, live_room, len(expected))
        assert len(history) >= seed_count, (
            "recovery lost writes committed before capture"
        )
        assert history == expected[: len(history)], (
            "restored history has missing or changed writes"
        )
        assert (
            api.call(
                "GET",
                f"/v1/rooms/{live_room}/messages",
                _stack.new_secret(),
                error="unauthorized",
            )
            == 401
        )
        restored_only = api.send(creator, live_room, "Recovery service is writable")
        assert api.history(creator, live_room, after=history[-1]["message_id"]) == [
            restored_only
        ]

    recovery_ms = ms(time.perf_counter() - started)
    recovered.stop()
    with closing(source.api()) as api:
        assert api.history(creator, live_room, after=written[-1][1]["message_id"]) == [
            after_capture
        ], "restored Server writes changed the source"

    # Recovery time ends when the replacement Server passes its API checks.
    return [
        metric(
            "NP-10",
            name,
            unit,
            {"profile": profile, "seed_messages": seed_count},
            _results.single(value),
            transport="local subprocess carrying the production capture stream",
            source_writes_during_capture=live_commits,
            rows_recovered=len(history),
            recovery_point_at=history[-1]["created_at"],
            generation_published_at=published_at,
        )
        for name, unit, value in (
            ("backup_capture_receive_latency", "ms", ms(receive_end - receive_start)),
            ("backup_verify_latency", "ms", ms(verification_end - receive_end)),
            ("backup_snapshot_size", "bytes", snapshot.stat().st_size),
            ("backup_restore_latency", "ms", ms(restored_at - started)),
            ("backup_recovery_time", "ms", recovery_ms),
            (
                "backup_recovery_point_lag",
                "ms",
                ms(
                    (
                        datetime.fromisoformat(published_at)
                        - datetime.fromisoformat(history[-1]["created_at"])
                    ).total_seconds()
                ),
            ),
        )
    ]


ARCHIVE_BENCHMARKS = {
    "NP-08-write": ("Archive transitions under room and chat pressure", archive_writes),
    "NP-08-read": ("Archived room reads and pagination under pressure", archive_reads),
    "NP-10": ("Online backup and isolated Server recovery", backup_restore),
}
