"""Concurrent child-room work and production sweeps at controlled lease times."""

import asyncio
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from tests.scenarios import _results, _stack
from tests.scenarios._focused_rooms import FocusedApi, RoomClock, focused_server
from tests.scenarios.perf.benchmarks import BenchmarkFailure, metric, ms

BENCHMARK = "NP-16"
SCALES = {"nightly": (4, 32, 26), "weekly": (8, 80, 50)}
SUCCESS = {200: {None}}
ARCHIVED = SUCCESS | {409: {"room_archived"}}
LEASE_CONFLICT = {409: {"conflict"}}
HAS_CHILD = {409: {"room_has_live_children"}}


def room_path(room):
    return f"/v1/rooms/{room['room_id']}"


def task_path(room, task):
    return f"{room_path(room)}/tasks/{task['task_id']}"


class Workload:
    def __init__(self, api, clock, owner, samples):
        self.api, self.clock, self.owner, self.samples = api, clock, owner, samples

    async def request(self, name, method, path, **kwargs):
        start = time.perf_counter()
        try:
            return await self.api.request(method, path, self.owner, **kwargs)
        finally:
            self.samples[name].append(ms(time.perf_counter() - start))

    async def root(self, name):
        room = await self.api.call("POST", "/v1/rooms", self.owner, json={"name": name})
        await self.api.acknowledge(room_path(room), self.owner)
        return room

    async def child_with_task(self, parent, index):
        payload = {"name": f"Focus {index}", "client_request_id": f"focus-{index}"}
        path = f"{room_path(parent)}/children"
        _, room = await self.request("child_create_latency", "POST", path, json=payload)
        _, retry = await self.request(
            "child_create_retry_latency", "POST", path, json=payload
        )
        assert retry == room
        assert room["parent_room_id"] == parent["room_id"]
        assert datetime.fromisoformat(room["created_at"]) == self.clock.now
        assert datetime.fromisoformat(room["expires_at"]) == self.clock.now + timedelta(
            hours=5
        )
        await self.api.acknowledge(room_path(room), self.owner)
        task = await self.api.call(
            "POST",
            f"{room_path(room)}/tasks",
            self.owner,
            json={
                "title": f"Complete objective {index}",
                "client_task_id": f"work-{index}",
            },
        )
        return room, task

    async def race_child_creation(self, parent):
        path = room_path(parent)
        created, closed = await asyncio.gather(
            self.request(
                "parent_close_contention",
                "POST",
                f"{path}/children",
                json={"name": "Racing child", "client_request_id": "race"},
                expected=ARCHIVED,
            ),
            self.request(
                "parent_close_contention",
                "POST",
                f"{path}/archive",
                expected=SUCCESS | HAS_CHILD,
            ),
        )
        assert (created[0], closed[0]) in {(200, 409), (409, 200)}
        if created[0] == 200:
            assert created[1]["parent_room_id"] == parent["room_id"]
            await self.api.call("POST", f"{room_path(created[1])}/archive", self.owner)
        await self.api.call("POST", f"{path}/archive", self.owner)
        await self.api.call(
            "POST",
            f"{path}/children",
            self.owner,
            json={"name": "Losing child", "client_request_id": "late"},
            expected={409: {"room_archived"}},
        )

    async def race_task_finish(self, room, task):
        # A child archive that wins freezes the unfinished source; if finish
        # wins first there is no successor. Two archive retries must agree.
        archived, finished, retry = await asyncio.gather(
            self.request(
                "child_archive_contention", "POST", f"{room_path(room)}/archive"
            ),
            self.request(
                "child_finish_contention",
                "POST",
                f"{task_path(room, task)}/finish",
                expected=ARCHIVED,
            ),
            self.request(
                "child_archive_contention", "POST", f"{room_path(room)}/archive"
            ),
        )
        assert archived == retry
        assert archived[1]["archive_reason"] == "goal_reached"
        source = await self.api.call("GET", task_path(room, task), self.owner)
        if finished[0] == 200:
            assert source == finished[1] and source["status"] == "finished"
        else:
            assert source == task
        return source

    async def set_time(self, now):
        self.clock.now = now
        await self.api.call("POST", "/v1/presence/heartbeat", self.owner)

    async def renew(self, room, deadline):
        path = f"{room_path(room)}/lease/extend"
        payload = {"seconds": 3600, "expected_expires_at": deadline}
        renewals = await asyncio.gather(
            *(
                self.request("child_renewal_contention", "POST", path, json=payload)
                for _ in range(2)
            )
        )
        assert renewals[0] == renewals[1]
        expiry = renewals[0][1]["expires_at"]
        assert datetime.fromisoformat(expiry) == (
            datetime.fromisoformat(deadline) + timedelta(hours=1)
        )
        await self.api.call(
            "POST",
            path,
            self.owner,
            json=payload | {"seconds": 1200},
            expected=LEASE_CONFLICT,
        )
        return expiry

    async def archive_observations(self, due, started):
        seen = set()
        async with asyncio.timeout(75):
            while seen != due.keys():
                archived = await self.api.pages(
                    "/v1/rooms/archive", self.owner, "rooms", limit=100
                )
                for row in archived:
                    room = row["room_id"]
                    if room in due and room not in seen:
                        assert row["archive_reason"] == "lease_expired"
                        assert datetime.fromisoformat(
                            row["archived_at"]
                        ) >= datetime.fromisoformat(due[room])
                        self.samples["child_expiry_observed_lag"].append(
                            ms(time.perf_counter() - started)
                        )
                        seen.add(room)
                if seen != due.keys():
                    await asyncio.sleep(0.15)

    async def race_expiry(self, room, task):
        await asyncio.gather(
            self.request(
                "expired_child_finish_contention",
                "POST",
                f"{task_path(room, task)}/finish",
                expected={409: {"room_archived"}},
            ),
            self.request(
                "expired_child_renewal_contention",
                "POST",
                f"{room_path(room)}/lease/extend",
                json={"seconds": 3600, "expected_expires_at": room["expires_at"]},
                expected=LEASE_CONFLICT | {409: {"conflict", "room_archived"}},
            ),
        )
        # Final state comes from the archive and the parent task lists.
        return task

    async def assert_parent_work(self, parent, sources):
        expected = Counter(
            (room["room_id"], task["task_id"], task["title"])
            for room, task in sources
            if task["status"] == "open"
        )
        actual = await self.api.pages(f"{room_path(parent)}/tasks", self.owner, "tasks")
        assert all(task["status"] == "open" for task in actual)
        assert (
            Counter(
                (task["origin"]["room_id"], task["origin"]["task_id"], task["title"])
                for task in actual
            )
            == expected
        )


async def exercise(work, parents, children, cycles):
    race_parents = [await work.root(f"Race parent {index}") for index in range(parents)]
    await asyncio.gather(*(work.race_child_creation(room) for room in race_parents))
    roots = [await work.root(f"Focus parent {index}") for index in range(parents)]
    by_root = {
        parent["room_id"]: await asyncio.gather(
            *(work.child_with_task(parent, index) for index in range(children))
        )
        for parent in roots
    }
    for root in roots:
        sources = by_root[root["room_id"]]
        listed = await work.api.pages(
            f"{room_path(root)}/children", work.owner, "rooms", limit=10
        )
        assert Counter(row["room_id"] for row in listed) == Counter(
            room["room_id"] for room, _ in sources
        )
        await work.api.call(
            "POST", f"{room_path(root)}/archive", work.owner, expected=HAS_CHILD
        )

    sources = [rows for rows in by_root.values()]
    manual = [entry for rows in sources for entry in rows[1::2]]
    expiring = [entry for rows in sources for entry in rows[2::2]]
    long_lived = [rows[0] for rows in sources]
    finished = await asyncio.gather(
        *(work.race_task_finish(*entry) for entry in manual)
    )
    terminal_sources = {
        room["room_id"]: task for (room, _), task in zip(manual, finished, strict=True)
    }

    deadline = long_lived[0][0]["expires_at"]
    await work.set_time(datetime.fromisoformat(deadline) - timedelta(minutes=15))
    [renewed, *_] = await asyncio.gather(
        *(work.renew(room, deadline) for room, _ in long_lived)
    )
    start = time.perf_counter()
    await work.set_time(datetime.fromisoformat(deadline))
    await asyncio.gather(
        work.archive_observations(
            {room["room_id"]: room["expires_at"] for room, _ in expiring}, start
        ),
        *(work.race_expiry(*entry) for entry in expiring),
    )
    deadline = renewed

    for _ in range(cycles - 1):
        await work.set_time(datetime.fromisoformat(deadline) - timedelta(minutes=15))
        deadlines = await asyncio.gather(
            *(work.renew(room, deadline) for room, _ in long_lived)
        )
        assert len(set(deadlines)) == 1
        deadline = deadlines[0]
    for root in roots:
        await work.api.call(
            "POST", f"{room_path(root)}/archive", work.owner, expected=HAS_CHILD
        )
    assert datetime.fromisoformat(deadline) - datetime.fromisoformat(
        long_lived[0][0]["created_at"]
    ) == timedelta(hours=5 + cycles)

    start = time.perf_counter()
    await work.set_time(datetime.fromisoformat(deadline))
    await work.archive_observations(
        {room["room_id"]: deadline for room, _ in long_lived}, start
    )
    for root in roots:
        rows = by_root[root["room_id"]]
        await work.assert_parent_work(
            root,
            [
                (room, terminal_sources.get(room["room_id"], task))
                for room, task in rows
            ],
        )
        await work.api.call("POST", f"{room_path(root)}/archive", work.owner)


async def focused_pressure(ws, quick, profile):
    parents, children, cycles = (2, 3, 3) if quick else SCALES[profile]
    labels = {
        "profile": profile,
        "parents": parents,
        "children_per_parent": children,
        "renewals": cycles,
    }
    samples = defaultdict(list)

    def metrics():
        return [
            metric(BENCHMARK, name, "ms", labels, _results.stats(values))
            for name, values in sorted(samples.items())
        ]

    key, clock = _stack.new_secret(), RoomClock()
    try:
        async with focused_server(ws, {"owner": key}, clock) as client:
            api = FocusedApi(client)
            owner = await api.enroll(key, "Focus owner")
            work = Workload(api, clock, owner.token, samples)
            await asyncio.wait_for(exercise(work, parents, children, cycles), 600)
    except (AssertionError, TimeoutError) as failure:
        raise BenchmarkFailure(
            str(failure) or type(failure).__name__, metrics()
        ) from failure
    return metrics()


FOCUSED_BENCHMARKS = {
    BENCHMARK: ("Child room lease and roll-up contention", focused_pressure)
}
