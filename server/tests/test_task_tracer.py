"""Deterministic tests for the opt-in stalled-task reminder owner and routes."""

import asyncio
import sqlite3
import sys
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier

import httpx
import pytest
from teamup_server.collaboration import Collaboration, CollaborationError
from teamup_server.gateway import Gateway
from teamup_server.main import create_app, main


def credential(label):
    return urlsafe_b64encode(sha256(label.encode()).digest()).decode().rstrip("=")


KEYS = {"alice": credential("enroll-alice"), "bob": credential("enroll-bob")}
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        return self.now


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def token(label):
    return credential(label)


def collaboration(tmp_path, clock, *, stall_after=60, interval=300, policy=True):
    return Collaboration(
        tmp_path / "team.sqlite3",
        KEYS,
        task_stall_after=stall_after if policy else None,
        task_reminder_interval=interval if policy else None,
        clock=clock,
    )


def enroll(instance, label, owner):
    return instance.enroll(
        KEYS[owner],
        harness_instance_id="shared-harness",
        session_key=label,
        role_description=label,
        member_token=token(label),
    )


def team(instance, labels=("sender", "target")):
    members = {
        label: enroll(instance, label, "alice" if label == "sender" else "bob")
        for label in labels
    }
    room = instance.create_room(token("sender"), "Tasks", None, None)
    for label in labels:
        instance.join_room(token(label), room["room_id"])
        instance.read_board(token(label), room["room_id"])
        instance.confirm_board(token(label), room["room_id"], 1)
    return members, room["room_id"]


def create(instance, room_id, labels=("target",), key="task-1"):
    task = instance.create_task(token("sender"), room_id, key, "Check API", None, None)
    for label in labels:
        instance.join_task(token(label), room_id, task["task_id"])
    return task


def not_found(call):
    with pytest.raises(CollaborationError) as raised:
        call()
    assert raised.value.code == "not_found"


def test_disabled_policy_never_exposes_or_issues_reminders(tmp_path):
    clock = Clock()
    instance = collaboration(tmp_path, clock, policy=False)
    _members, room_id = team(instance)
    task = create(instance, room_id)

    clock.advance(10_000)
    assert (
        instance.get_task(token("target"), room_id, task["task_id"])["stalled_since"]
        is None
    )
    assert instance.scan_task_reminders() == []
    assert instance.task_reminders(token("target")) == []
    not_found(lambda: instance.claim_task_reminder(token("target"), 1))
    for request in (
        lambda: instance.task_reminders(token("unenrolled")),
        lambda: instance.claim_task_reminder(token("unenrolled"), 1),
    ):
        with pytest.raises(CollaborationError) as raised:
            request()
        assert raised.value.code == "unauthorized"


def test_stall_and_repeat_use_the_configured_deterministic_interval(tmp_path):
    clock = Clock()
    instance = collaboration(tmp_path, clock, stall_after=60, interval=120)
    members, room_id = team(instance, ("sender", "target", "reader"))
    task = create(instance, room_id, ("target",))

    clock.advance(59)
    assert instance.scan_task_reminders() == []
    assert instance.task_reminders(token("target")) == []
    assert (
        instance.get_task(token("target"), room_id, task["task_id"])["stalled_since"]
        is None
    )

    clock.advance(1)
    stalled_since = stamp(START + timedelta(seconds=60))
    assert (
        instance.get_task(token("target"), room_id, task["task_id"])["stalled_since"]
        == stalled_since
    )
    assert instance.scan_task_reminders() == [members["target"]["member_id"]]

    reminders = instance.task_reminders(token("target"))
    assert len(reminders) == 1
    reminder = reminders[0]
    assert set(reminder) == {
        "reminder_id",
        "room_id",
        "task_id",
        "title",
        "stalled_since",
    }
    assert reminder["room_id"] == room_id
    assert reminder["task_id"] == task["task_id"]
    assert reminder["title"] == "Check API"
    assert reminder["stalled_since"] == stalled_since
    assert instance.task_reminders(token("reader")) == []
    assert instance.task_reminders(token("sender")) == []

    not_found(
        lambda: instance.claim_task_reminder(token("reader"), reminder["reminder_id"])
    )
    claimed = instance.claim_task_reminder(token("target"), reminder["reminder_id"])
    assert claimed == reminder
    not_found(
        lambda: instance.claim_task_reminder(token("target"), reminder["reminder_id"])
    )

    clock.advance(119)
    assert instance.scan_task_reminders() == []
    clock.advance(1)
    assert instance.scan_task_reminders() == [members["target"]["member_id"]]
    repeated = instance.task_reminders(token("target"))
    assert len(repeated) == 1
    assert repeated[0]["reminder_id"] > reminder["reminder_id"]
    assert repeated[0]["stalled_since"] == stalled_since
    assert (
        instance.claim_task_reminder(token("target"), repeated[0]["reminder_id"])
        == repeated[0]
    )
    with sqlite3.connect(tmp_path / "team.sqlite3") as connection:
        retained = connection.execute(
            "SELECT reminder_id FROM task_reminders WHERE task_id = ? AND member_id = ?",
            (task["task_id"], members["target"]["member_id"]),
        ).fetchall()
    assert retained == [(repeated[0]["reminder_id"],)]
    assert instance.scan_task_reminders() == []
    not_found(
        lambda: instance.claim_task_reminder(token("target"), reminder["reminder_id"])
    )


def test_concurrent_claimers_consume_a_reminder_once(tmp_path):
    clock = Clock()
    instance = collaboration(tmp_path, clock)
    _members, room_id = team(instance)
    create(instance, room_id)
    clock.advance(60)
    instance.scan_task_reminders()
    reminder_id = instance.task_reminders(token("target"))[0]["reminder_id"]
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        try:
            return "claimed", instance.claim_task_reminder(token("target"), reminder_id)
        except CollaborationError as exc:
            return "error", exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result() for future in (pool.submit(claim), pool.submit(claim))
        ]

    assert sorted(outcome[0] for outcome in outcomes) == ["claimed", "error"]
    assert [outcome[1] for outcome in outcomes if outcome[0] == "error"] == [
        "not_found"
    ]
    assert instance.task_reminders(token("target")) == []


def test_progress_and_finish_invalidate_the_previous_anchor(tmp_path):
    clock = Clock()
    instance = collaboration(tmp_path, clock, stall_after=60, interval=300)
    _members, room_id = team(instance)
    task = create(instance, room_id)
    path = task["task_id"]

    clock.advance(60)
    instance.scan_task_reminders()
    old = instance.task_reminders(token("target"))[0]
    progress_at = clock.now
    progressed = instance.report_progress(
        token("target"), room_id, path, "progress-1", "Still working"
    )
    assert progressed["stalled_since"] is None
    not_found(lambda: instance.claim_task_reminder(token("target"), old["reminder_id"]))
    assert instance.task_reminders(token("target")) == []
    assert instance.scan_task_reminders() == []

    clock.advance(60)
    instance.scan_task_reminders()
    current = instance.task_reminders(token("target"))
    assert len(current) == 1
    assert current[0]["reminder_id"] > old["reminder_id"]
    assert current[0]["stalled_since"] == stamp(progress_at + timedelta(seconds=60))

    finished = instance.finish_task(token("sender"), room_id, path)
    assert finished["stalled_since"] is None
    not_found(
        lambda: instance.claim_task_reminder(token("target"), current[0]["reminder_id"])
    )
    assert instance.task_reminders(token("target")) == []
    assert instance.scan_task_reminders() == []


def test_anchor_and_claim_state_survive_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "team.sqlite3"
    instance = Collaboration(
        path,
        KEYS,
        task_stall_after=60,
        task_reminder_interval=300,
        clock=clock,
    )
    members, room_id = team(instance, ("sender", "target", "reader"))
    create(instance, room_id, ("target", "reader"))
    clock.advance(60)
    instance.scan_task_reminders()
    pending = {
        label: instance.task_reminders(token(label))[0]
        for label in ("target", "reader")
    }
    claimed = instance.claim_task_reminder(
        token("target"), pending["target"]["reminder_id"]
    )

    restarted = Collaboration(
        path,
        KEYS,
        task_stall_after=60,
        task_reminder_interval=300,
        clock=clock,
    )
    assert restarted.task_reminders(token("target")) == []
    assert restarted.task_reminders(token("reader")) == [pending["reader"]]
    not_found(
        lambda: restarted.claim_task_reminder(token("target"), claimed["reminder_id"])
    )

    clock.advance(300)
    assert restarted.scan_task_reminders() == [members["target"]["member_id"]]
    renewed = restarted.task_reminders(token("target"))
    assert len(renewed) == 1
    assert renewed[0]["reminder_id"] > claimed["reminder_id"]
    assert renewed[0]["stalled_since"] == claimed["stalled_since"]


def test_reminder_routes_are_owner_scoped_paged_and_atomic(tmp_path):
    clock = Clock()
    instance = collaboration(tmp_path, clock)
    _members, room_id = team(instance, ("sender", "target", "reader"))
    create(instance, room_id, ("target",), key="task-1")
    create(instance, room_id, ("target",), key="task-2")
    clock.advance(60)
    instance.scan_task_reminders()
    reminders = instance.task_reminders(token("target"))
    assert len(reminders) == 2

    async def exercise():
        transport = httpx.ASGITransport(app=Gateway(instance).app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            owner = {"Authorization": f"Bearer {token('target')}"}
            other = {"Authorization": f"Bearer {token('reader')}"}

            page = await client.get("/v1/task-reminders", headers=owner)
            assert page.status_code == 200
            assert page.json() == {"reminders": reminders}

            limited = await client.get(
                "/v1/task-reminders", headers=owner, params={"limit": 1}
            )
            assert limited.status_code == 200
            assert limited.json() == {"reminders": reminders[:1]}
            after = await client.get(
                "/v1/task-reminders",
                headers=owner,
                params={"after": reminders[0]["reminder_id"], "limit": 10},
            )
            assert after.json() == {"reminders": reminders[1:]}
            assert (await client.get("/v1/task-reminders", headers=other)).json() == {
                "reminders": []
            }

            reminder_id = reminders[0]["reminder_id"]
            path = f"/v1/task-reminders/{reminder_id}/claim"
            denied = await client.post(path, headers=other)
            assert denied.status_code == 404
            assert denied.json()["error"]["code"] == "not_found"
            rejected = await client.post(path, headers=owner, json={})
            assert rejected.status_code == 422
            claimed = await client.post(path, headers=owner)
            assert claimed.status_code == 200
            assert claimed.json() == reminders[0]
            repeated = await client.post(path, headers=owner)
            assert repeated.status_code == 404
            assert repeated.json()["error"]["code"] == "not_found"

    asyncio.run(exercise())


def test_create_app_requires_both_positive_tracer_flags(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path / "stall.sqlite3", task_stall_after=60)
    with pytest.raises(ValueError):
        create_app(tmp_path / "interval.sqlite3", task_reminder_interval=60)
    with pytest.raises(ValueError):
        create_app(
            tmp_path / "zero.sqlite3",
            task_stall_after=0,
            task_reminder_interval=60,
        )


def test_server_flags_must_be_supplied_together(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "teamup-server",
            "--db",
            str(tmp_path / "tools.sqlite3"),
            "--task-stall-after",
            "30",
        ],
    )
    with pytest.raises(SystemExit):
        main()


def test_a_rolled_back_reminder_scan_retries_the_same_page(tmp_path, monkeypatch):
    from teamup_server import collaboration as module

    clock = Clock()
    instance = collaboration(tmp_path, clock)
    members, room_id = team(instance)
    create(instance, room_id)
    clock.advance(60)
    monkeypatch.setattr(module, "_TRACER_TASK_BATCH", 1)
    original = module._task_reminder_recipients

    def fail(*args):
        raise sqlite3.OperationalError("test scan failure")

    monkeypatch.setattr(module, "_task_reminder_recipients", fail)
    with pytest.raises(sqlite3.OperationalError):
        instance.scan_task_reminders()
    monkeypatch.setattr(module, "_task_reminder_recipients", original)
    assert instance.scan_task_reminders() == [members["target"]["member_id"]]
