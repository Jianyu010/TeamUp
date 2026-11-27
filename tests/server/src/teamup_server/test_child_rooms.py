from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from teamup_server.collaboration import CollaborationError
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
