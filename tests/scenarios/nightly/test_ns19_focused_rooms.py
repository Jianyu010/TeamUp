"""Focused room leases, notices and task continuation on a controlled clock.

The Server runs in-process with its production lifespan (``_focused_rooms``).
Sweeps come from restarts (the startup sweep) and once from the 15-second cadence.
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from _focused_rooms import (
    CADENCE,
    LEASE,
    NOTICE_WINDOW,
    FocusedApi,
    FocusedServer,
    RoomClock,
    room_path,
    task_path,
    until,
)
from _stack import new_secret

KEY = new_secret()
GENERIC_TITLE = "Coordinate continuation of restricted work"
CONFLICT = {409: {"conflict"}}
ARCHIVED = {409: {"room_archived"}}
INVALID = {422: {"invalid_request"}}
NOT_FOUND = {404: {"not_found"}}


async def create_task(api, child, token, key, title, **extra):
    return await api.call(
        "POST",
        f"{room_path(child)}/tasks",
        token,
        json={"title": title, "client_task_id": key, **extra},
    )


async def seed(api, clock, *, sentinels=(), lead=None):
    """A parent with one focused child: public, restricted and finished work.

    ``sentinels`` are child rooms created after it, each at a chosen clock time.
    With ``lead``, a control child comes first and its lease ends that much earlier.
    """
    owner = await api.enroll(KEY, "Release Coordinator")
    worker = await api.enroll(KEY, "Release Engineer")
    observer = await api.enroll(KEY, "Release Observer")
    stranger = await api.enroll(KEY, "Outsider")
    parent = await api.call("POST", "/v1/rooms", owner.token, json={"name": "Release"})
    await api.acknowledge(room_path(parent), owner.token)
    await api.call("POST", f"{room_path(parent)}/join", observer.token)
    await api.acknowledge(room_path(parent), observer.token)
    control = None
    if lead is not None:
        control = await api.call(
            "POST",
            f"{room_path(parent)}/children",
            owner.token,
            json={"name": "Control", "client_request_id": "control"},
        )
        clock.now += lead
    child = await api.call(
        "POST",
        f"{room_path(parent)}/children",
        owner.token,
        json={"name": "Focused verification", "client_request_id": "verification"},
    )
    assert child["parent_room_id"] == parent["room_id"]
    assert datetime.fromisoformat(child["created_at"]) == clock.now
    assert datetime.fromisoformat(child["expires_at"]) == clock.now + LEASE
    await api.acknowledge(room_path(child), owner.token)
    await api.call("POST", f"{room_path(child)}/join", worker.token)
    await api.acknowledge(room_path(child), worker.token)
    assert await api.reject(
        "POST",
        room_path(parent) + "/archive",
        owner.token,
        {409: {"room_has_live_children"}},
    )
    public = await create_task(
        api,
        child,
        owner.token,
        "public",
        "Publish the release notes",
        description="Draft from the changelog",
    )
    private = await create_task(
        api,
        child,
        owner.token,
        "private",
        "Secret rollback plan",
        description="Secret credentials location",
        addressed_member_ids=[worker.member_id],
        assignment={"closing_criteria": "Secret approval code", "eta": "Secret Friday"},
    )
    done = await create_task(api, child, owner.token, "done", "Complete the checklist")
    done = await api.call("POST", task_path(child, done) + "/finish", owner.token)
    path = task_path(child, private)
    await api.call("POST", path + "/join", worker.token)
    await api.call(
        "POST",
        path + "/progress",
        worker.token,
        json={
            "client_progress_id": "private-progress",
            "note": "Secret progress: halfway",
        },
    )
    restricted = await api.call("GET", path, worker.token)
    history = (await api.call("GET", path + "/events", worker.token))["events"]
    transcript = (await api.call("GET", room_path(child) + "/messages", worker.token))[
        "messages"
    ]
    later = []
    for name, at in sentinels:
        clock.now = at
        later.append(
            await api.call(
                "POST",
                room_path(parent) + "/children",
                owner.token,
                json={
                    "name": f"Sentinel {name}",
                    "client_request_id": f"sentinel-{name}",
                },
            )
        )
    return SimpleNamespace(
        owner=owner,
        worker=worker,
        observer=observer,
        stranger=stranger,
        parent=parent,
        child=child,
        public=public,
        private=private,
        done=done,
        restricted=restricted,
        history=history,
        transcript=transcript,
        sentinels=later,
        control=control,
        expires=datetime.fromisoformat(child["expires_at"]),
    )


async def lease_notices(api, member, room):
    room = room["room_id"] if isinstance(room, dict) else room
    return [
        notice
        for notice in await api.notices(member.token)
        if notice["kind"] == "child_lease_due" and notice["room_id"] == room
    ]


async def archived_row(api, member, room):
    room = room["room_id"] if isinstance(room, dict) else room
    return next(
        (
            row
            for row in await api.pages(
                "/v1/rooms/archive", member.token, "rooms", limit=100
            )
            if row["room_id"] == room
        ),
        None,
    )


def renewal(api, member, child, seconds, deadline):
    return api.request(
        "POST",
        room_path(child) + "/lease/extend",
        member.token,
        json={"seconds": seconds, "expected_expires_at": deadline},
    )


async def parent_tasks(api, member, parent):
    """Every task the member may list in the parent, read one per page."""
    return await api.pages(room_path(parent) + "/tasks", member.token, "tasks", limit=1)


def run(ws, steps, *, timeout=90):
    async def scenario():
        clock = RoomClock()
        async with FocusedServer(ws, {"owner": KEY}, clock) as server:
            await steps(FocusedApi(server.client), server, clock)

    asyncio.run(asyncio.wait_for(scenario(), timeout))


@pytest.mark.scenario(
    "NS-19A",
    "Lease notice at 15 minutes, scoped idempotent acknowledgment and extension",
)
def test_lease_notice_ack_and_extension(ws):
    async def steps(api, server, clock):
        team = await seed(api, clock)
        child, parent = team.child, team.parent
        room = child["room_id"]
        due = team.expires - NOTICE_WINDOW

        # The leader stays quiet and cannot renew outside the offer window.
        clock.now = due - timedelta(seconds=1)
        await server.restart()
        assert await api.notices(team.owner.token) == []
        assert await api.reject(
            "POST",
            room_path(child) + "/lease/extend",
            team.owner.token,
            CONFLICT,
            json={"seconds": 3600, "expected_expires_at": child["expires_at"]},
        )

        # The periodic sweep records the offer and signals the inbox.
        headers = {
            "Authorization": f"Bearer {team.owner.token}",
            "Accept": "text/event-stream",
        }
        limit = CADENCE + 5
        async with server.client.stream("GET", "/v1/events", headers=headers) as stream:
            lines = stream.aiter_lines()
            assert (await anext(lines)).startswith(":")

            async def hinted():
                async with asyncio.timeout(limit):
                    async for line in lines:
                        if line.startswith("event:"):
                            return line

            async def offered():
                return await until(
                    lambda: lease_notices(api, team.owner, room),
                    what="the periodic lease offer",
                    timeout=limit,
                )

            moved = asyncio.get_running_loop().time()
            clock.now = due
            event, [notice] = await asyncio.gather(hinted(), offered())
            assert event == "event: inbox_changed"
            assert asyncio.get_running_loop().time() - moved <= limit
        assert notice["expires_at"] == child["expires_at"]
        assert room in notice["text"] and "teamup_extend_room_lease" in notice["text"]
        assert child["expires_at"] in notice["text"]
        assert await lease_notices(api, team.worker, room) == []
        assert await api.notices(team.stranger.token) == []
        assert await api.reject(
            "POST",
            room_path(child) + "/lease/extend",
            team.worker.token,
            {403: {"forbidden"}},
            json={"seconds": 600, "expected_expires_at": child["expires_at"]},
        )

        # Acknowledgment belongs to the recipient and repeats safely.
        ack = f"/v1/room-notices/{notice['notice_id']}/ack"
        for member in (team.worker, team.observer, team.stranger):
            assert await api.reject("POST", ack, member.token, NOT_FOUND)
        assert await api.reject(
            "POST", "/v1/room-notices/999999/ack", team.owner.token, NOT_FOUND
        )
        receipt = {"notice_id": notice["notice_id"]}
        assert await api.call("POST", ack, team.owner.token) == receipt
        assert await api.call("POST", ack, team.owner.token) == receipt
        assert await lease_notices(api, team.owner, room) == []

        # Only the leader renews, against the current deadline, and only once.
        deadline = child["expires_at"]
        path = room_path(child) + "/lease/extend"
        for seconds in (0, 3601):
            assert await api.reject(
                "POST",
                path,
                team.owner.token,
                INVALID,
                json={"seconds": seconds, "expected_expires_at": deadline},
            )
        assert await api.reject(
            "POST",
            path,
            team.owner.token,
            CONFLICT,
            json={
                "seconds": 600,
                "expected_expires_at": (team.expires + timedelta(seconds=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            },
        )
        renewed = team.expires + timedelta(hours=1)
        payload = {"seconds": 3600, "expected_expires_at": deadline}
        extended = await api.call("POST", path, team.owner.token, json=payload)
        assert extended["expires_at"] == renewed.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert extended["parent_room_id"] == parent["room_id"]
        assert extended["created_at"] == child["created_at"]
        assert await api.call("POST", path, team.owner.token, json=payload) == extended
        assert await api.reject(
            "POST",
            path,
            team.owner.token,
            CONFLICT,
            json={"seconds": 60, "expected_expires_at": deadline},
        )
        assert (await api.call("GET", room_path(child), team.owner.token))[
            "expires_at"
        ] == extended["expires_at"]
        [listed] = await api.pages(
            room_path(parent) + "/children", team.owner.token, "rooms"
        )
        assert listed["expires_at"] == extended["expires_at"]

        # The first deadline passes; the extended room stays open and opens a new window.
        clock.now = team.expires
        await api.call(
            "POST",
            room_path(child) + "/messages",
            team.worker.token,
            json={"client_message_id": "still-open", "text": "Past the first deadline"},
        )
        clock.now = renewed - NOTICE_WINDOW
        await server.restart()
        [offer] = await until(
            lambda: lease_notices(api, team.owner, room),
            what="the offer for the new window",
            timeout=10,
        )
        assert (offer["expires_at"], offer["notice_id"] > notice["notice_id"]) == (
            extended["expires_at"],
            True,
        )

    run(ws, steps)


@pytest.mark.scenario(
    "NS-19B",
    "Lease expiry at equality, late operations and private task continuation",
)
def test_expiry_and_private_continuation(ws):
    async def steps(api, server, clock):
        team = await seed(
            api, clock, sentinels=[("late", clock.now + timedelta(seconds=1))]
        )
        child, parent = team.child, team.parent
        room, path = child["room_id"], room_path(child)
        expiry = child["expires_at"]

        # No sweep can have run yet: expiry still wins at equality.
        await server.restart()
        clock.now = team.expires
        assert await api.reject(
            "POST",
            path + "/lease/extend",
            team.owner.token,
            CONFLICT,
            json={"seconds": 600, "expected_expires_at": expiry},
        )
        for method, suffix, body in (
            ("POST", f"/tasks/{team.public['task_id']}/finish", None),
            ("POST", "/tasks", {"title": "Late", "client_task_id": "late"}),
            ("POST", "/messages", {"client_message_id": "late", "text": "late"}),
        ):
            assert await api.reject(
                method, path + suffix, team.owner.token, ARCHIVED, json=body
            )
        assert await api.reject("POST", path + "/join", team.stranger.token, NOT_FOUND)
        assert "archive_reason" not in await api.call("GET", path, team.owner.token)

        await server.restart()
        row = await until(
            lambda: archived_row(api, team.owner, room),
            what="the lease expiry",
            timeout=10,
        )
        assert (row["archive_reason"], row["archived_at"], row["expires_at"]) == (
            "lease_expired",
            expiry,
            expiry,
        )
        assert row["parent_room_id"] == parent["room_id"]
        assert await api.reject(
            "POST",
            path + "/lease/extend",
            team.owner.token,
            CONFLICT,
            json={"seconds": 600, "expected_expires_at": expiry},
        )
        assert await api.reject(
            "POST",
            f"{path}/tasks/{team.public['task_id']}/finish",
            team.owner.token,
            ARCHIVED,
        )

        # Each unfinished task continues once; restricted content stays in the child.
        tasks = await parent_tasks(api, team.owner, parent)
        assert [
            (t["title"], t["status"], t["addressed_member_ids"]) for t in tasks
        ] == [
            (team.public["title"], "open", []),
            (GENERIC_TITLE, "open", []),
        ]
        public, generic = tasks
        assert public["description"] == team.public["description"]
        assert "assignment" not in public and "Secret" not in json.dumps(tasks)
        link = {"room_id": room}
        assert public["origin"] == link | {"task_id": team.public["task_id"]}
        assert generic["origin"] == link | {"task_id": team.private["task_id"]}
        assert all(task["room_id"] == parent["room_id"] for task in tasks)

        # A parent-only member sees the continuation without the source link.
        seen = await parent_tasks(api, team.observer, parent)
        assert [(t["task_id"], t["title"]) for t in seen] == [
            (t["task_id"], t["title"]) for t in tasks
        ]
        assert all("origin" not in task for task in seen)
        assert "Secret" not in json.dumps(seen)
        for member in (team.worker, team.stranger):
            assert await api.reject(
                "GET", room_path(parent) + "/tasks", member.token, NOT_FOUND
            )
        assert await api.reject(
            "GET",
            f"{room_path(parent)}/tasks/{generic['task_id']}",
            team.stranger.token,
            NOT_FOUND,
        )

        # The frozen sources keep their number, events and visibility.
        assert (
            await api.call("GET", task_path(child, team.public), team.owner.token)
            == team.public
        )
        assert (
            await api.call("GET", task_path(child, team.private), team.worker.token)
            == team.restricted
        )
        events = await api.call(
            "GET", task_path(child, team.private) + "/events", team.worker.token
        )
        assert events["events"] == team.history
        assert await api.reject(
            "GET", task_path(child, team.private), team.observer.token, NOT_FOUND
        )
        history = await api.call("GET", path + "/messages", team.worker.token)
        assert history["messages"] == team.transcript
        for suffix in ("/board/view", "/messages"):
            assert await api.reject(
                "GET", path + suffix, team.observer.token, NOT_FOUND
            )
            assert await api.reject(
                "GET", path + suffix, team.stranger.token, NOT_FOUND
            )

        for member in (team.owner, team.worker):
            row = await archived_row(api, member, room)
            assert (row["archive_reason"], row["expires_at"]) == (
                "lease_expired",
                expiry,
            )
        assert await archived_row(api, team.observer, room) is None
        listed = await api.pages(
            room_path(parent) + "/children", team.owner.token, "rooms"
        )
        assert [item["room_id"] for item in listed if "archive_reason" in item] == [
            room
        ]
        # Closing writes one roll-up notice per current parent member.
        rolled = ("child_tasks_rolled_up", parent["room_id"])
        for member, expected in (
            (team.owner, [rolled]),
            (team.observer, [rolled]),
            (team.worker, []),
            (team.stranger, []),
        ):
            assert [
                (n["kind"], n["room_id"])
                for n in await api.notices(member.token)
                if n["kind"] == "child_tasks_rolled_up"
            ] == expected

        # A later sweep of the same database adds no duplicate continuation.
        await api.acknowledge_notices(team.owner.token)
        clock.now = team.expires + timedelta(seconds=1)
        await server.restart()
        control = team.sentinels[0]["room_id"]
        await until(
            lambda: archived_row(api, team.owner, control),
            what="the control expiry",
            timeout=10,
        )
        assert await parent_tasks(api, team.owner, parent) == tasks
        closed = await api.call(
            "POST", room_path(parent) + "/archive", team.owner.token
        )
        assert (closed["room_id"], closed["archive_reason"]) == (
            parent["room_id"],
            "goal_reached",
        )
        assert (
            await api.call("POST", room_path(parent) + "/archive", team.owner.token)
            == closed
        )

    run(ws, steps)


@pytest.mark.scenario(
    "NS-19C", "Leadership change in a notice window moves the offer to the new holder"
)
def test_new_leader_receives_the_offer(ws):
    async def steps(api, server, clock):
        team = await seed(api, clock)
        child = team.child
        room, path = child["room_id"], room_path(child)
        clock.now = team.expires - NOTICE_WINDOW
        await server.restart()
        offers = await until(
            lambda: lease_notices(api, team.owner, room),
            what="the leader's offer",
            timeout=10,
        )
        assert len(offers) == 1
        await api.acknowledge_notices(team.owner.token)

        await api.heartbeat(team.worker)
        ranking = await api.call(
            "GET", path + "/leadership/candidates", team.owner.token
        )
        assert [row["member_id"] for row in ranking["candidates"]] == [
            team.worker.member_id
        ]
        handoff = await api.call(
            "POST",
            path + "/leadership/handoff",
            team.owner.token,
            json={"leadership_epoch": ranking["leadership_epoch"], "mode": "auto"},
        )
        assert (handoff["leader_member_id"], handoff["leadership_epoch"]) == (
            team.worker.member_id,
            ranking["leadership_epoch"] + 1,
        )
        assert not handoff["pending"] and not handoff["left"]
        [appointed] = [
            n
            for n in await api.notices(team.worker.token)
            if n["kind"] == "leadership_assigned"
        ]
        assert appointed["leadership_epoch"] == handoff["leadership_epoch"]
        assert (
            "board" in appointed["text"].lower() and "task" in appointed["text"].lower()
        )

        await server.restart()
        [offer] = await until(
            lambda: lease_notices(api, team.worker, room),
            what="the new holder's offer",
            timeout=10,
        )
        assert offer["expires_at"] == child["expires_at"]
        assert offer["notice_id"] > appointed["notice_id"]
        # The previous holder, who acknowledged the first offer, gets no second one.
        assert await lease_notices(api, team.owner, room) == []
        assert await api.reject(
            "POST",
            path + "/lease/extend",
            team.owner.token,
            {403: {"forbidden"}},
            json={"seconds": 600, "expected_expires_at": child["expires_at"]},
        )
        renewed = await api.call(
            "POST",
            path + "/lease/extend",
            team.worker.token,
            json={"seconds": 600, "expected_expires_at": child["expires_at"]},
        )
        assert datetime.fromisoformat(
            renewed["expires_at"]
        ) == team.expires + timedelta(seconds=600)
        assert (await api.led_rooms(team.worker.token))[room][
            "leader_member_id"
        ] == team.worker.member_id
        assert not (await api.led_rooms(team.owner.token))[room]["can_remove_members"]

    run(ws, steps)


@pytest.mark.scenario("NS-19D", "One lease notice per window for the same leader")
def test_one_offer_per_window(ws):
    async def steps(api, server, clock):
        # The control child's lease ends one minute into the main child's notice window.
        team = await seed(api, clock, lead=NOTICE_WINDOW - timedelta(minutes=1))
        room = team.child["room_id"]
        control = team.control["room_id"]
        first_due = team.expires - NOTICE_WINDOW

        clock.now = first_due
        await server.restart()
        [first] = await until(
            lambda: lease_notices(api, team.owner, room),
            what="the lease offer",
            timeout=10,
        )
        # Same leader, same window: the sweep that expires the control adds no offer.
        clock.now = datetime.fromisoformat(team.control["expires_at"])
        await server.restart()
        await until(
            lambda: archived_row(api, team.owner, control),
            what="the control expiry",
            timeout=10,
        )
        assert [n["notice_id"] for n in await lease_notices(api, team.owner, room)] == [
            first["notice_id"]
        ]

    run(ws, steps)
