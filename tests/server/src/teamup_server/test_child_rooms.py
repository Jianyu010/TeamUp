import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from threading import Barrier

import pytest
from teamup_server.collaboration import Collaboration, CollaborationError
from test_task_tracer import Clock, collaboration, enroll, stamp, team, token


def child(service, parent, actor="sender", name="Focus", key="create"):
    return service.create_linked_room(token(actor), parent, name, None, None, "retained", key)


def enter(service, label, room):
    service.join_room(token(label), room)
    service.read_board(token(label), room)
    service.confirm_board(token(label), room, 1)


def test_link_requires_parent_membership_and_independent_child_access(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    _, parent = team(service)
    enroll(service, "outside", "bob")
    with pytest.raises(CollaborationError, match="not_found"):
        child(service, parent, "outside")
    created = child(service, parent, "target")
    assert created == child(service, parent, "target")
    assert created["parent_room_id"] == parent
    assert created["created_at"] == stamp(clock.now)
    assert created["expires_at"] == stamp(clock.now + timedelta(hours=5))
    assert service.list_child_rooms(token("sender"), parent, None, 1)["rooms"] == [created]
    room = created["room_id"]
    with pytest.raises(CollaborationError, match="conflict"):
        child(service, parent, "target", name="Changed")
    with pytest.raises(CollaborationError, match="conflict"):
        child(service, room, "target")
    with pytest.raises(CollaborationError, match="not_found"):
        service.room_detail(token("sender"), room)
    assert service.room_detail(token("target"), room)["expires_at"] == created["expires_at"]
    joined = service.list_rooms(token("target"), include_membership=True)
    assert next(r for r in joined if r["room_id"] == room)["parent_room_id"] == parent
    assert all("parent_room_id" not in r for r in service.list_rooms(token("target")))
    service.archive_room(token("target"), room)
    assert service.list_child_rooms(token("sender"), parent, None, 1)["rooms"] == []
    archived = service.list_archived_rooms(token("target"), None, 1)["rooms"][0]
    assert archived["expires_at"] == created["expires_at"]
    assert service.list_child_rooms(token("target"), parent, None, 1)["rooms"][0]["room_id"] == room


def test_lease_window_retry_expiry_and_restart(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    _, parent = team(service)
    linked = child(service, parent)
    room, expires = linked["room_id"], linked["expires_at"]
    enter(service, "sender", room)
    enter(service, "target", room)
    clock.advance(5 * 3600 - 901)
    with pytest.raises(CollaborationError, match="conflict"):
        service.extend_room_lease(token("sender"), room, 901, expires)
    service.sweep_child_rooms()
    assert service.room_notices(token("sender"), 0, 50)["notices"] == []
    clock.advance(1)
    service.sweep_child_rooms()
    service = collaboration(tmp_path, clock)
    service.sweep_child_rooms()
    notices = service.room_notices(token("sender"), 0, 50)["notices"]
    assert [(n["kind"], n["expires_at"]) for n in notices] == [("child_lease_due", expires)]
    with pytest.raises(CollaborationError, match="forbidden"):
        service.extend_room_lease(token("target"), room, 901, expires)
    renewed = service.extend_room_lease(token("sender"), room, 901, expires)
    assert renewed["expires_at"] == stamp(clock.now + timedelta(seconds=1801))
    assert service.extend_room_lease(token("sender"), room, 901, expires) == renewed
    with pytest.raises(CollaborationError, match="conflict"):
        service.extend_room_lease(token("sender"), room, 900, expires)
    clock.advance(1801)
    with pytest.raises(CollaborationError, match="conflict"):
        service.extend_room_lease(token("sender"), room, 5, renewed["expires_at"])
    with pytest.raises(CollaborationError, match="room_archived"):
        service.create_task(token("sender"), room, "late", "Late", None, None)
    service.sweep_child_rooms()
    assert service.room_detail(token("sender"), room)["archive_reason"] == "lease_expired"


def test_archive_rolls_up_open_work_and_preserves_restricted_sources(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    members, parent = team(service, ("sender", "target", "reader", "parent-only"))
    room = child(service, parent)["room_id"]
    for label in ("sender", "target", "reader"):
        enter(service, label, room)
    visible = service.create_task(token("sender"), room, "vis", "Objective", "Keep me", None)
    private = service.create_task(
        token("sender"), room, "priv", "Secret title", "Secret description",
        [members["target"]["member_id"]], {"closing_criteria": "Secret criteria"},
    )
    service.join_task(token("target"), room, private["task_id"])
    service.report_progress(token("target"), room, private["task_id"], "report", "Secret progress")
    finished = service.create_task(token("sender"), room, "done", "Complete", None, None)
    service.finish_task(token("sender"), room, finished["task_id"])
    history = service.task_history(token("target"), room, private["task_id"])
    with pytest.raises(CollaborationError, match="room_has_live_children"):
        service.archive_room(token("sender"), parent)
    clock.advance(5 * 3600)
    service.sweep_child_rooms()
    service = collaboration(tmp_path, clock)
    service.sweep_child_rooms()
    assert service.archive_room(token("sender"), room)["archive_reason"] == "lease_expired"
    assert service.task_history(token("target"), room, private["task_id"]) == history
    for label in members:
        todos = service.list_tasks(token(label), parent)["tasks"]
        assert len(todos) == 2
        public, generic = todos
        assert public["title"] == visible["title"] and public["description"] == "Keep me"
        assert generic["addressed_member_ids"] == [] and generic["status"] == "open"
        assert "Secret" not in str(todos)
        link = {"room_id": room, "task_id": private["task_id"]}
        assert generic.get("origin") == (link if label in ("sender", "target") else None)
        assert ("origin" in public) == (label != "parent-only")
        notices = service.room_notices(token(label), 0, 50)["notices"]
        assert [(n["kind"], n["room_id"]) for n in notices] == [("child_tasks_rolled_up", parent)]
    assert service.archive_room(token("sender"), parent)["archive_reason"] == "goal_reached"


def test_child_create_and_parent_archive_serialize(tmp_path):
    service = collaboration(tmp_path, Clock())
    _, parent = team(service)
    together = Barrier(2)

    def race(action):
        together.wait(5)
        try:
            return action()
        except CollaborationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        creating = workers.submit(race, lambda: child(service, parent))
        closing = workers.submit(race, lambda: service.archive_room(token("sender"), parent))
        created, closed = creating.result(), closing.result()
    if isinstance(created, dict):
        assert closed == "room_has_live_children"
    else:
        assert created == "room_archived" and closed["archive_reason"] == "goal_reached"


def live(service, label, room):
    return "archived_at" not in service.room_detail(token(label), room)


def titles(service, label, room):
    return [task["title"] for task in service.list_tasks(token(label), room)["tasks"]]


def rolled_up(service, label, parent):
    return [
        (notice["kind"], notice["room_id"])
        for notice in service.room_notices(token(label))["notices"]
        if notice["kind"] == "child_tasks_rolled_up"
    ]


def test_parent_close_waits_for_a_just_expired_child_to_roll_up(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    members, parent = team(service)
    room = child(service, parent)["room_id"]
    enter(service, "sender", room)
    service.create_task(token("sender"), room, "open", "Pending", None, None)
    clock.advance(5 * 3600)
    with pytest.raises(CollaborationError, match="room_has_live_children"):
        service.archive_room(token("sender"), parent)
    assert live(service, "sender", parent) and live(service, "sender", room)
    assert titles(service, "sender", parent) == []
    assert all(rolled_up(service, label, parent) == [] for label in members)

    service.sweep_child_rooms()
    assert live(service, "sender", parent)
    assert service.room_detail(token("sender"), room)["archive_reason"] == "lease_expired"
    for label in members:
        assert titles(service, label, parent) == ["Pending"]
        assert rolled_up(service, label, parent) == [("child_tasks_rolled_up", parent)]
    assert service.archive_room(token("sender"), parent)["archive_reason"] == "goal_reached"
    assert all(titles(service, label, parent) == ["Pending"] for label in members)


def test_parent_close_racing_the_child_expiry_sweep_keeps_exactly_one_successor(
    tmp_path,
):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    _, parent = team(service)
    room = child(service, parent)["room_id"]
    enter(service, "sender", room)
    work = service.create_task(token("sender"), room, "open", "Pending", None, None)
    clock.advance(5 * 3600)
    together = Barrier(2)

    def race(action):
        together.wait(5)
        try:
            return action()
        except CollaborationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        closing = workers.submit(race, lambda: service.archive_room(token("sender"), parent))
        sweeping = workers.submit(race, service.sweep_child_rooms)
        closed = closing.result()
        sweeping.result()
    successors = service.list_tasks(token("sender"), parent)["tasks"]
    assert [task["origin"] for task in successors] == [
        {"room_id": room, "task_id": work["task_id"]}
    ]
    if closed == "room_has_live_children":
        assert live(service, "sender", parent)
        assert rolled_up(service, "sender", parent) == [("child_tasks_rolled_up", parent)]
    else:
        assert closed["archive_reason"] == "goal_reached"
    assert service.room_detail(token("sender"), room)["archive_reason"] == "lease_expired"


def test_roll_up_keeps_an_idle_parent_live_until_the_inherited_work_is_seen(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    members, parent = team(service)
    room = child(service, parent)["room_id"]
    enter(service, "sender", room)
    service.create_task(token("sender"), room, "open", "Pending", None, None)
    with closing(sqlite3.connect(tmp_path / "team.sqlite3")) as connection, connection:
        connection.execute(
            "UPDATE rooms SET last_chat_at = ?",
            (int((clock.now - timedelta(hours=25)).timestamp() * 1_000_000),),
        )
    assert service.archive_inactive_rooms() is False
    assert live(service, "sender", parent)

    clock.advance(5 * 3600)
    assert service.maintain_rooms() is False
    assert live(service, "sender", parent)
    for label in members:
        assert titles(service, label, parent) == ["Pending"]
        assert rolled_up(service, label, parent) == [("child_tasks_rolled_up", parent)]
    sent = service.send_message(token("sender"), parent, "resume", "Continue here")
    assert sent["room_id"] == parent
    clock.advance(24 * 3600)
    service.archive_inactive_rooms()
    assert service.room_detail(token("sender"), parent)["archive_reason"] == "inactive"
    assert service.history(token("sender"), parent, 0, 50)["messages"] == [sent]
    assert titles(service, "sender", parent) == ["Pending"]


def test_maintenance_pass_isolates_a_failing_sweep(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    team(service)
    idle = service.create_room(token("sender"), "Idle", None, None)["room_id"]
    with closing(sqlite3.connect(tmp_path / "team.sqlite3")) as connection, connection:
        connection.execute(
            "UPDATE rooms SET last_chat_at = 0 WHERE room_id = ?", (idle,)
        )

    class Broken(Collaboration):
        def sweep_child_rooms(self):
            raise sqlite3.OperationalError("sweep failed")

    broken = Broken(tmp_path / "team.sqlite3", {}, clock=clock)
    assert broken.maintain_rooms() is False
    assert broken.room_detail(token("sender"), idle)["archive_reason"] == "inactive"
    clock.advance(30 * 60)
    assert broken.maintain_rooms() is False
    notices = broken.room_notices(token("sender"))["notices"]
    assert [notice["kind"] for notice in notices] == ["leadership_challenge"]


def test_only_the_current_lease_offer_is_delivered(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    members, parent = team(service)
    linked = child(service, parent)
    room, expires = linked["room_id"], linked["expires_at"]
    for label in ("sender", "target"):
        enter(service, label, room)
    clock.advance(5 * 3600 - 900)
    service.sweep_child_rooms()
    offer = [("child_lease_due", expires)]
    assert [(n["kind"], n["expires_at"]) for n in service.room_notices(token("sender"))["notices"]] == offer
    renewed = service.extend_room_lease(token("sender"), room, 900, expires)
    assert service.room_notices(token("sender"))["notices"] == []
    clock.advance(899)
    service.sweep_child_rooms()
    assert service.room_notices(token("sender"))["notices"] == []
    clock.advance(1)
    service.sweep_child_rooms()
    assert [
        (n["kind"], n["expires_at"]) for n in service.room_notices(token("sender"))["notices"]
    ] == [("child_lease_due", renewed["expires_at"])]
    service.record_leadership_heartbeat(token("target"))
    ranking = service.room_leadership_candidates(token("sender"), room)
    (successor,) = ranking["candidates"]
    assert successor["member_id"] == members["target"]["member_id"]
    receipt = service.handoff_room_leadership(
        token("sender"), room, ranking["leadership_epoch"], "choose",
        member_id=successor["member_id"], membership_id=successor["membership_id"],
    )
    service.sweep_child_rooms()
    assert service.room_notices(token("sender"))["notices"] == []
    assigned, offer = service.room_notices(token("target"))["notices"]
    assert (assigned["kind"], assigned["leadership_epoch"]) == (
        "leadership_assigned", receipt["leadership_epoch"]
    )
    assert (offer["kind"], offer["expires_at"], offer["leadership_epoch"]) == (
        "child_lease_due", renewed["expires_at"], receipt["leadership_epoch"]
    )
    with pytest.raises(CollaborationError, match="conflict"):
        service.extend_room_lease(token("target"), room, 60, expires)
    clock.advance(900)
    service.sweep_child_rooms()
    assert service.room_notices(token("target"))["notices"] == []


def test_console_room_detail_reports_child_metadata(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    service.create_account("operator", "a sufficiently long password")
    console = service.login("operator", "a sufficiently long password", "console")[
        "access_token"
    ]
    _, parent = team(service)
    linked = child(service, parent)
    with pytest.raises(CollaborationError, match="forbidden"):
        service.console_room_detail(console, linked["room_id"])
    service.grant_console_read("operator")
    detail = service.console_room_detail(console, linked["room_id"])["room"]
    keys = ("parent_room_id", "created_at", "expires_at")
    assert {key: detail[key] for key in keys} == {key: linked[key] for key in keys}
    root = service.console_room_detail(console, parent)["room"]
    assert "parent_room_id" not in root and "expires_at" not in root


def test_rolled_up_restricted_work_leaks_nothing_into_the_parent(tmp_path):
    clock = Clock()
    service = collaboration(tmp_path, clock)
    members, parent = team(service, ("sender", "target", "parent-only"))
    room = child(service, parent)["room_id"]
    enter(service, "sender", room)
    enter(service, "target", room)
    private = service.create_task(
        token("sender"), room, "priv", "Secret title", "Secret description",
        [members["target"]["member_id"]], {"closing_criteria": "Secret criteria", "eta": "Secret eta"},
    )
    service.join_task(token("target"), room, private["task_id"])
    service.report_progress(token("target"), room, private["task_id"], "report", "Secret progress")
    service.send_message(token("target"), room, "secret-message", "Secret body")
    clock.advance(5 * 3600)
    service.sweep_child_rooms()
    for label in members:
        (generic,) = service.list_tasks(token(label), parent)["tasks"]
        assert generic["title"] == "Coordinate continuation of restricted work"
        assert generic["description"] == (
            "Contact the child room's team to coordinate access and continuation."
        )
        assert generic["addressed_member_ids"] == [] and generic["participant_member_ids"] == []
        assert "assignment" not in generic
        assert generic.get("origin") == (
            {"room_id": room, "task_id": private["task_id"]}
            if label in ("sender", "target") else None
        )
        history = service.task_history(token(label), parent, generic["task_id"])
        assert [(event["kind"], event["note"]) for event in history["events"]] == [("created", None)]
        (notice,) = service.room_notices(token(label))["notices"]
        assert notice["kind"] == "child_tasks_rolled_up" and notice["room_id"] == parent
        board = service.room_directory(token(label), 0, 50)["rooms"]
        assert all(entry["room_id"] != room for entry in board)
        exposed = (
            str(generic) + str(history) + str(notice) + str(board)
            + str(service.room_members(token(label), parent, include_membership=True))
            + str(service.room_leadership_candidates(token("sender"), parent))
        )
        assert "Secret" not in exposed
        if label == "parent-only":
            with pytest.raises(CollaborationError, match="not_found"):
                service.room_detail(token(label), room)
            assert private["task_id"] not in exposed
