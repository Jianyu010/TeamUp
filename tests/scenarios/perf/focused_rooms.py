"""Concurrent child-room work and production sweeps at controlled lease times.

Wall-clock stays bounded because leases follow a controlled clock: renewal rounds
and expiries are clock moves, and the sweeps under test are the Server's startup
sweep.
"""

import asyncio
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from functools import partial

from tests.scenarios import _results, _stack
from tests.scenarios._focused_rooms import (
    LEASE,
    NOTICE_WINDOW,
    FocusedApi,
    FocusedServer,
    RoomClock,
    room_path,
    task_path,
    until,
)
from tests.scenarios.perf.benchmarks import BenchmarkFailure, metric, ms

BENCHMARK = "NP-16"
# parents, children per parent, renewal rounds, request chains in flight, wall-clock budget
LEVELS = {
    "quick": (2, 4, 2, 4, 60),
    "nightly": (4, 16, 3, 8, 120),
    "weekly": (10, 100, 4, 16, 300),
}
LONG_LEASE_RENEWALS = {"quick": 2, "nightly": 28, "weekly": 72}
ROLES = ("expiring", "renewed", "manual", "notice")
GENERIC = "Coordinate continuation of restricted work"
SUCCESS = {200: {None}}
CONFLICT = {409: {"conflict"}}
ARCHIVED = {409: {"room_archived"}}
HAS_CHILDREN = {409: {"room_has_live_children"}}


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Workload:
    def __init__(
        self, api, server, clock, samples, parents, children, rounds, concurrency
    ):
        self.api, self.server, self.clock, self.samples = api, server, clock, samples
        self.parents, self.children, self.rounds = parents, children, rounds
        self.gate = asyncio.Semaphore(concurrency)
        self.roots, self.records, self.control = [], {}, None

    @classmethod
    async def build(cls, api, server, clock, samples, level):
        parents, children, rounds, concurrency = level[:4]
        work = cls(api, server, clock, samples, parents, children, rounds, concurrency)
        key = server.keys["owner"]
        work.collaborator = await api.enroll(key, "Collaborator")
        work.leaders = [
            await api.enroll(key, f"Focus Lead {i}") for i in range(parents)
        ]
        work.racers = [await api.enroll(key, f"Race Lead {i}") for i in range(parents)]
        return work

    @property
    def pairs(self):
        return list(zip(self.leaders, self.roots, strict=True))

    def of_role(self, role, parent=None):
        return [
            (self.leaders[p], record)
            for (p, _), record in self.records.items()
            if record["role"] == role and parent in (None, p)
        ]

    async def timed(self, name, method, path, token, *, expected=None, **kwargs):
        started = time.perf_counter()
        try:
            return await self.api.request(
                method, path, token, expected=expected, **kwargs
            )
        finally:
            self.samples[name].append(ms(time.perf_counter() - started))

    async def limited(self, operation):
        """One of at most ``concurrency`` request chains in flight."""
        async with self.gate:
            return await operation()

    def parallel(self, operations):
        return asyncio.gather(*(self.limited(operation) for operation in operations))

    async def acknowledge_notices(self):
        """Acknowledge the leaders’ current notices before advancing the clock."""
        await asyncio.gather(*(self.api.acknowledge_notices(lead.token) for lead in self.leaders))

    async def move(self, now):
        await self.acknowledge_notices()
        self.clock.now = now

    # --- building blocks ------------------------------------------------------

    async def root(self, leader, name):
        room = await self.api.call(
            "POST", "/v1/rooms", leader.token, json={"name": name}
        )
        await self.api.acknowledge(room_path(room), leader.token)
        return room

    async def child(self, leader, parent, key, name):
        path, payload = (
            room_path(parent) + "/children",
            {"name": name, "client_request_id": key},
        )
        _, room = await self.timed(
            "child_create_latency", "POST", path, leader.token, json=payload
        )
        _, retry = await self.timed(
            "child_create_retry_latency", "POST", path, leader.token, json=payload
        )
        assert retry == room and room["parent_room_id"] == parent["room_id"], (
            retry,
            room,
        )
        created = datetime.fromisoformat(room["created_at"])
        assert created == self.clock.now, (created, self.clock.now)
        assert datetime.fromisoformat(room["expires_at"]) == created + LEASE
        await self.api.acknowledge(room_path(room), leader.token)
        return room

    async def task(self, leader, room, key, title, **extra):
        return await self.api.call(
            "POST",
            room_path(room) + "/tasks",
            leader.token,
            json={"title": title, "client_task_id": key, **extra},
        )

    async def race_parent_archive(self, leader, parent):
        """Parent closure and child creation serialize; the loser gets its code."""
        path = room_path(parent)
        payload = {"name": "Racing child", "client_request_id": "race"}
        (created, child), (closed, _) = await asyncio.gather(
            self.timed(
                "parent_close_contention",
                "POST",
                path + "/children",
                leader.token,
                expected=SUCCESS | ARCHIVED,
                json=payload,
            ),
            self.timed(
                "parent_close_contention",
                "POST",
                path + "/archive",
                leader.token,
                expected=SUCCESS | HAS_CHILDREN,
            ),
        )
        assert (created, closed) in {(200, 409), (409, 200)}, (created, closed)
        if created == 200:
            assert child["parent_room_id"] == parent["room_id"]
            await self.api.reject("POST", path + "/archive", leader.token, HAS_CHILDREN)
            await self.api.call("POST", room_path(child) + "/archive", leader.token)
        closed = await self.api.call("POST", path + "/archive", leader.token)
        assert closed["archive_reason"] == "goal_reached"
        await self.api.reject(
            "POST",
            path + "/children",
            leader.token,
            ARCHIVED,
            json={"name": "Losing child", "client_request_id": "late"},
        )

    async def race_finish(self, leader, room, task):
        """A closure that wins freezes the open source; a finish that wins leaves none."""

        async def archive():
            return await self.timed(
                "child_archive_contention",
                "POST",
                room_path(room) + "/archive",
                leader.token,
            )

        (first, _), (second, finished), (retry, _) = await asyncio.gather(
            archive(),
            self.timed(
                "child_finish_contention",
                "POST",
                task_path(room, task) + "/finish",
                leader.token,
                expected=SUCCESS | ARCHIVED,
            ),
            archive(),
        )
        assert first == retry == 200, (first, retry)
        source = await self.api.call("GET", task_path(room, task), leader.token)
        assert source == (finished if second == 200 else task), (source, task, finished)
        return second == 200

    async def open_work(self, leader, room, index):
        """Untargeted and restricted open work plus one finished task."""
        path = room_path(room)
        await self.api.call("POST", path + "/join", self.collaborator.token)
        await self.api.acknowledge(path, self.collaborator.token)
        public = await self.task(
            leader,
            room,
            f"public-{index}",
            f"Objective {index}",
            description=f"Continue objective {index}",
        )
        private = await self.task(
            leader,
            room,
            f"private-{index}",
            f"Restricted objective {index}",
            description="Restricted details",
            addressed_member_ids=[self.collaborator.member_id],
        )
        done = await self.task(leader, room, f"done-{index}", f"Finished {index}")
        done = await self.api.call(
            "POST", task_path(room, done) + "/finish", leader.token
        )
        assert done["status"] == "finished"
        return [(public, False), (private, True)]

    async def focus(self, parent, index):
        role = ROLES[index % 4]
        lead, room = self.pairs[parent]
        child = await self.child(lead, room, f"child-{index}", f"Focus {role} {index}")
        record = self.records[(parent, index)] = {
            "role": role,
            "room": child,
            "index": index,
            "open": [],
        }
        if role == "manual":
            task = await self.task(lead, child, f"manual-{index}", f"Objective {index}")
            if not await self.race_finish(lead, child, task):
                record["open"] = [(task, False)]
        else:
            record["open"] = await self.open_work(lead, child, index)

    async def notice(self, parent, index):
        lead, room = self.pairs[parent]
        child = await self.child(lead, room, f"late-{index}", f"Focus notice {index}")
        task = await self.task(lead, child, f"late-{index}", f"Objective {index}")
        self.records[(parent, index)] = {
            "role": "notice",
            "room": child,
            "index": index,
            "open": [(task, False)],
        }

    # --- clock steps -------------------------------------------------------------

    async def renew(self, leader, room, deadline):
        path = room_path(room) + "/lease/extend"
        payload = {"seconds": 3600, "expected_expires_at": deadline}
        first, second = await asyncio.gather(
            *(
                self.timed(
                    "child_renewal_contention", "POST", path, leader.token, json=payload
                )
                for _ in range(2)
            )
        )
        assert first == second, (first, second)
        assert datetime.fromisoformat(first[1]["expires_at"]) == (
            datetime.fromisoformat(deadline) + timedelta(hours=1)
        ), first
        await self.timed(
            "child_renewal_conflict",
            "POST",
            path,
            leader.token,
            expected=CONFLICT,
            json=payload | {"seconds": 1800},
        )
        return first[1]["expires_at"]

    async def renewal_round(self, deadline):
        """Two identical racing requests extend every renewed lease exactly once."""
        await self.parallel(
            partial(self.renew, lead, record["room"], deadline)
            for lead, record in self.of_role("renewed")
        )

    async def expired_between_sweeps(self):
        """At the first deadline an unrenewed lease rejects work; a renewed one accepts it."""
        for lead, record in self.of_role("expiring"):
            await self.timed(
                "expired_child_rejection",
                "POST",
                room_path(record["room"]) + "/messages",
                lead.token,
                expected=ARCHIVED,
                json={"client_message_id": f"late-{record['index']}", "text": "late"},
            )
        for lead, record in self.of_role("renewed"):
            await self.timed(
                "renewed_child_post",
                "POST",
                room_path(record["room"]) + "/messages",
                lead.token,
                json={
                    "client_message_id": f"open-{record['index']}",
                    "text": "still open",
                },
            )

    async def sweep(self, due, *, what):
        """Serve the same database again and observe the startup sweep's expiries.

        ``due`` holds (leader, record, earliest); each archive is stamped no earlier
        than ``earliest``. Returns the archive rows by room ID.
        """
        expected = defaultdict(dict)
        for lead, record, earliest in due:
            expected[lead.token][record["room"]["room_id"]] = earliest
        started = time.perf_counter()
        await self.server.restart()
        rows = {}

        async def observe(token, rooms):
            while rooms.keys() - rows.keys():
                for row in await self.api.pages(
                    "/v1/rooms/archive", token, "rooms", limit=100
                ):
                    if row["room_id"] in rooms and row["room_id"] not in rows:
                        assert row["archive_reason"] == "lease_expired", row
                        assert row["archived_at"] >= stamp(rooms[row["room_id"]]), row
                        rows[row["room_id"]] = row
                        self.samples["child_expiry_observed_lag"].append(
                            ms(time.perf_counter() - started)
                        )
                if rooms.keys() - rows.keys():
                    await asyncio.sleep(0.1)

        limit = 30 + 0.05 * len(due)
        try:
            async with asyncio.timeout(limit):
                await asyncio.gather(
                    *(observe(token, rooms) for token, rooms in expected.items())
                )
        except TimeoutError:
            raise AssertionError(
                f"The startup sweep archived {len(rows)} of {len(due)} {what} in {limit:.0f}s"
            ) from None
        self.samples["startup_sweep_settle"].append(ms(time.perf_counter() - started))
        return rows

    async def pass_notices(self):
        """Wait for each leader's lease offers after the final startup sweep."""

        async def read(lead, parent):
            expected = {
                record["room"]["room_id"] for _, record in self.of_role("notice", parent)
            }

            async def offered():
                notices = await self.api.notices(lead.token)
                ready = {
                    notice["room_id"] for notice in notices
                    if notice["kind"] == "child_lease_due"
                }
                if expected <= ready:
                    return notices

            return await until(offered, what="the child lease offers", timeout=30)

        return await asyncio.gather(
            *(read(lead, parent) for parent, lead in enumerate(self.leaders))
        )

    # --- verification -------------------------------------------------------------

    def expected_work(self, parent):
        """One continuation per source task that was open when its child closed."""
        expected = Counter()
        for (owner, _), record in self.records.items():
            for source, restricted in record["open"] if owner == parent else ():
                title = GENERIC if restricted else source["title"]
                expected[(record["room"]["room_id"], source["task_id"], title)] += 1
        return expected

    async def parent_work(self, lead, parent):
        tasks = await self.api.pages(
            room_path(parent) + "/tasks", lead.token, "tasks", limit=100
        )
        assert all(task["status"] == "open" for task in tasks), tasks
        return tasks

    def assert_work(self, tasks, expected):
        actual = Counter(
            (task["origin"]["room_id"], task["origin"]["task_id"], task["title"])
            for task in tasks
        )
        assert actual == expected, (actual - expected, expected - actual)
        generic = [task for task in tasks if task["title"] == GENERIC]
        assert len(generic) == sum(1 for key in expected if key[2] == GENERIC)
        assert all(task["addressed_member_ids"] == [] for task in generic)
        assert "Restricted" not in json.dumps(tasks)

    # --- the benchmark -----------------------------------------------------------

    async def setup(self):
        # Parent closure races child creation.
        race = [
            await self.root(lead, f"Race parent {i}")
            for i, lead in enumerate(self.racers)
        ]
        await self.parallel(
            partial(self.race_parent_archive, lead, room)
            for lead, room in zip(self.racers, race, strict=True)
        )
        # Every parent gets its children concurrently; notice children come later.
        self.roots = [
            await self.root(lead, f"Focus parent {i}")
            for i, lead in enumerate(self.leaders)
        ]
        await self.parallel(
            partial(self.focus, parent, index)
            for parent in range(self.parents)
            for index in range(self.children)
            if ROLES[index % 4] != "notice"
        )
        for parent, (lead, room) in enumerate(self.pairs):
            listed = await self.api.pages(
                room_path(room) + "/children", lead.token, "rooms", limit=10
            )
            assert Counter(row["room_id"] for row in listed) == Counter(
                record["room"]["room_id"]
                for (owner, _), record in self.records.items()
                if owner == parent
            )
            await self.api.reject(
                "POST", room_path(room) + "/archive", lead.token, HAS_CHILDREN
            )

    async def exercise(self):
        expiring, renewed = self.of_role("expiring"), self.of_role("renewed")
        expiry = datetime.fromisoformat(renewed[0][1]["room"]["expires_at"])
        final = expiry + timedelta(hours=self.rounds)
        closing = final + timedelta(minutes=1)
        late = [index for index in range(self.children) if ROLES[index % 4] == "notice"]

        async def control():
            # Its lease ends just after the final pass, proving that a later sweep ran.
            self.control = {
                "room": await self.child(
                    self.leaders[0], self.roots[0], "control", "Control"
                ),
                "index": -1,
            }

        async def noticed():
            # These leases end six minutes after the final pass, inside their notice window.
            await self.parallel(
                partial(self.notice, parent, index)
                for parent in range(self.parents)
                for index in late
            )

        steps = [
            (closing - LEASE, control),
            (closing - LEASE + timedelta(minutes=5), noticed),
            (expiry, self.expired_between_sweeps),
            *(
                (
                    expiry + timedelta(hours=round_) - NOTICE_WINDOW,
                    partial(
                        self.renewal_round, stamp(expiry + timedelta(hours=round_))
                    ),
                )
                for round_ in range(self.rounds)
            ),
        ]
        for at, step in sorted(steps, key=lambda step: step[0]):
            await self.move(at)
            await step()

        # The startup sweep expires unrenewed and repeatedly renewed leases together.
        await self.move(final)
        rows = await self.sweep(
            [(lead, record, expiry) for lead, record in expiring]
            + [(lead, record, final) for lead, record in renewed],
            what="expiries",
        )
        assert all(
            rows[record["room"]["room_id"]]["archived_at"] == stamp(final)
            for _, record in renewed
        )
        for parent, feed in enumerate(await self.pass_notices()):
            assert all(notice["kind"] != "leadership_challenge" for notice in feed)
            offered = {
                notice["room_id"]: notice["expires_at"]
                for notice in feed
                if notice["kind"] == "child_lease_due"
            }
            assert all(
                offered.get(record["room"]["room_id"]) == record["room"]["expires_at"]
                for _, record in self.of_role("notice", parent)
            ), offered
            assert any(notice["kind"] == "child_tasks_rolled_up" for notice in feed)
        await self.acknowledge_notices()
        # Leaders close the noticed children; unfinished work continues in the parent.
        closed = await self.parallel(
            partial(
                self.timed,
                "child_close_latency",
                "POST",
                room_path(record["room"]) + "/archive",
                lead.token,
            )
            for lead, record in self.of_role("notice")
        )
        assert all(row["archive_reason"] == "goal_reached" for _, row in closed)

        # No orphaned work: every open source continues exactly once in its parent.
        work = {}
        for parent, (lead, room) in enumerate(self.pairs):
            work[parent] = await self.parent_work(lead, room)
            self.assert_work(work[parent], self.expected_work(parent))

        # A later sweep of the same database adds nothing; the parents then close.
        await self.move(closing)
        await self.sweep(
            [(self.leaders[0], self.control, closing)], what="control expiries"
        )
        for parent, (lead, room) in enumerate(self.pairs):
            assert await self.parent_work(lead, room) == work[parent]
        for lead, room in self.pairs:
            led = (await self.api.led_rooms(lead.token))[room["room_id"]]
            assert led["leader_member_id"] == lead.member_id, led
            _, receipt = await self.timed(
                "parent_close_latency", "POST", room_path(room) + "/archive", lead.token
            )
            assert receipt["archive_reason"] == "goal_reached"
            assert (
                await self.api.call("POST", room_path(room) + "/archive", lead.token)
                == receipt
            )
            await self.api.reject(
                "POST",
                room_path(room) + "/children",
                lead.token,
                ARCHIVED,
                json={"name": "Losing child", "client_request_id": "late"},
            )

    async def exercise_long_lease(self, renewals):
        lead = self.leaders[0]
        parent = await self.root(lead, "Long-lived parent")
        child = await self.child(lead, parent, "long-lease", "Long-lived child")
        task = await self.task(lead, child, "long-task", "Continue long-lived work")
        deadline = child["expires_at"]
        for _ in range(renewals):
            await self.api.acknowledge_notices(lead.token)
            self.clock.now = datetime.fromisoformat(deadline) - NOTICE_WINDOW
            deadline = await self.renew(lead, child, deadline)
        await self.api.acknowledge_notices(lead.token)
        self.clock.now = datetime.fromisoformat(deadline)
        await self.sweep([(lead, {"room": child}, self.clock.now)], what="long-lease expiry")
        self.assert_work(
            await self.parent_work(lead, parent),
            Counter({(child["room_id"], task["task_id"], task["title"]): 1}),
        )
        await self.api.call("POST", room_path(parent) + "/archive", lead.token)


async def focused_pressure(ws, quick, profile):
    level = "quick" if quick else profile
    parents, children, rounds, concurrency, budget = LEVELS[level]
    labels = {
        "profile": level,
        "parents": parents,
        "children_per_parent": children,
        "renewals": rounds,
        "long_lease_renewals": LONG_LEASE_RENEWALS[level],
        "concurrency": concurrency,
    }
    samples = defaultdict(list)

    def metrics():
        return [
            metric(BENCHMARK, name, "ms", labels, _results.stats(values))
            for name, values in sorted(samples.items())
        ]

    key, clock = _stack.new_secret(), RoomClock()
    try:
        async with FocusedServer(
            ws, {"owner": key}, clock, connections=3 * concurrency + 8
        ) as server:
            async with asyncio.timeout(budget):
                work = await Workload.build(
                    FocusedApi(server.client), server, clock, samples, LEVELS[level]
                )
                await work.setup()
                await work.exercise()
                await work.exercise_long_lease(LONG_LEASE_RENEWALS[level])
    except (AssertionError, TimeoutError) as failure:
        detail = str(failure) or type(failure).__name__
        if isinstance(failure, TimeoutError):
            detail = f"NP-16 {level} did not finish within {budget}s"
        raise BenchmarkFailure(detail, metrics()) from failure
    return metrics()


FOCUSED_BENCHMARKS = {
    BENCHMARK: (
        "Child room lease renewal, expiry and roll-up contention",
        focused_pressure,
    )
}
