import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from teamup_server.collaboration import Collaboration, CollaborationError
from test_contract import (
    KEYS,
    api,
    candidates,
    confirm,
    credential,
    enroll,
    error,
    get,
    heartbeat,
    join,
    member,
    post,
    room,
)


class Team:
    def __init__(self, path):
        self.path = path
        self.now = datetime(2026, 11, 28, tzinfo=UTC)
        self.db = self.restart()
        self.db.create_account("alice", "a sufficiently long password")
        self.login = self.db.login("alice", "a sufficiently long password")["access_token"]
        self.leader = self.enroll("original")
        self.room = self.db.create_room(self.leader, "room", None, None)["room_id"]

    def restart(self):
        self.db = Collaboration(self.path, KEYS, clock=lambda: self.now)
        return self.db

    def enroll(self, name, owner="alice"):
        token = credential(name)
        self.db.enroll(
            (self.login if owner == "alice" else KEYS.get(owner, owner)), harness_instance_id=name, session_key=name,
            member_token=token, name=name, description="Working on implementation.",
        )
        return token

    def join(self, name, owner="bob", heartbeat=True):
        token = self.enroll(name, owner)
        self.db.join_room(token, self.room)
        if heartbeat:
            self.db.record_leadership_heartbeat(token)
        return token

    def candidates(self, token=None):
        return self.db.room_leadership_candidates(token or self.leader, self.room)

    def id(self, token):
        return self.db.get_member(token)["member_id"]

    def ack(self, token):
        board = self.db.read_board(token, self.room)
        self.db.confirm_board(token, self.room, board["version"])

    def handoff(self, epoch, mode, **kwargs):
        return self.db.handoff_room_leadership(self.leader, self.room, epoch, mode, **kwargs)


@pytest.fixture
def team(tmp_path):
    return Team(tmp_path / "leadership.sqlite3")


def test_rank_includes_only_eligible_members_and_public_work(team):
    db = team.db
    sibling = team.join("sibling", "alice")
    chief = team.join("Release Lead")
    other = team.join("specialist")
    team.join("offline", heartbeat=False)
    team.ack(other)
    team.ack(team.leader)
    db.create_task(other, team.room, "restricted", "PRIVATE", None, [team.id(sibling)])
    rows = team.candidates()["candidates"]
    assert [(r["member_id"], r["reason"]) for r in rows] == [
        (team.id(chief), "leader_profile"), (team.id(sibling), "same_owner"),
        (team.id(other), "recent_activity"),
    ]
    assert rows[2]["last_active_at"] is None
    db.send_message(other, team.room, "visible", "Visible room work")
    assert team.candidates()["candidates"][2]["last_active_at"] == "2026-11-28T00:00:00Z"
    team.now += timedelta(seconds=45)
    assert team.candidates()["candidates"] == []


def test_recent_work_references_the_latest_public_event_without_bodies(team):
    db = team.db
    worker = team.join("worker")
    for token in (worker, team.leader):
        team.ack(token)
    db.create_task(worker, team.room, "private", "PRIVATE", None, [team.id(team.leader)])

    def recent():
        rows = team.candidates()["candidates"]
        row, = [row for row in rows if row["member_id"] == team.id(worker)]
        assert row["reason"] == "recent_activity"
        return row

    assert recent()["recent_work"] is None
    task = db.create_task(worker, team.room, "public", "PUBLIC", None, None)
    summary = {"kind": "task", "task_number": task["task_number"], "title": "PUBLIC"}
    assert recent()["recent_work"] == {**summary, "event": "created", "created_at": "2026-11-28T00:00:00Z"}
    task_id = task["task_id"]

    def step(action, second):
        team.now += timedelta(seconds=1)
        db.record_leadership_heartbeat(worker)
        return action(), {**summary, "created_at": f"2026-11-28T00:00:{second:02d}Z"}

    _, work = step(lambda: db.join_task(worker, team.room, task_id), 1)
    assert recent()["recent_work"] == {**work, "event": "joined"}
    _, work = step(lambda: db.report_progress(worker, team.room, task_id, "p-1", "NOTE"), 2)
    assert recent()["recent_work"] == {**work, "event": "progress"}
    _, work = step(lambda: db.finish_task(worker, team.room, task_id), 3)
    assert recent()["recent_work"] == {**work, "event": "finished"}
    message, work = step(lambda: db.send_message(worker, team.room, "latest", "SECRET BODY"), 4)
    row = recent()
    assert row["recent_work"] == {
        "kind": "message", "message_id": message["message_id"], "created_at": work["created_at"]
    }
    assert row["last_active_at"] == work["created_at"]
    listed = json.dumps(team.candidates())
    assert all(word not in listed for word in ("SECRET", "NOTE", "PRIVATE"))


def test_recent_work_excludes_history_the_requesting_leader_cannot_read(team):
    db = team.db
    author = team.join("author")
    successor = team.join("successor", heartbeat=False)

    def acknowledge(token, room_id):
        db.confirm_board(token, room_id, db.read_board(token, room_id)["version"])

    def hand_over(history_policy):
        room_id = db.create_room(author, "focused", None, None, history_policy)["room_id"]
        acknowledge(author, room_id)
        first = db.send_message(author, room_id, f"first-{history_policy}", "FIRST BODY")["message_id"]
        db.join_room(successor, room_id)
        db.record_leadership_heartbeat(successor)
        row, = db.room_leadership_candidates(author, room_id)["candidates"]
        assert row["member_id"] == team.id(successor)
        db.handoff_room_leadership(
            author, room_id, 1, "choose", member_id=row["member_id"], membership_id=row["membership_id"]
        )
        return room_id, first

    def recent(room_id):
        db.record_leadership_heartbeat(successor)
        row, = db.room_leadership_candidates(successor, room_id)["candidates"]
        assert row["member_id"] == team.id(author)
        return row["recent_work"]

    hidden, _ = hand_over("since_joining")
    assert recent(hidden) is None
    acknowledge(successor, hidden)
    assert recent(hidden) is None
    second = db.send_message(author, hidden, "second", "SECOND BODY")["message_id"]
    assert recent(hidden) == {"kind": "message", "message_id": second, "created_at": "2026-11-28T00:00:00Z"}
    shown, first = hand_over("retained")
    acknowledge(successor, shown)
    assert recent(shown) == {"kind": "message", "message_id": first, "created_at": "2026-11-28T00:00:00Z"}


def test_explicit_choice_checks_membership_generation_and_transfers_only_once(team):
    peer = team.join("peer")
    original = team.candidates()
    row, = original["candidates"]
    epoch = original["leadership_epoch"]
    db = team.db
    db.leave_room(peer, team.room)
    db.join_room(peer, team.room)
    kwargs = {"member_id": row["member_id"], "membership_id": row["membership_id"]}
    with pytest.raises(CollaborationError, match="conflict"):
        team.handoff(epoch, "choose", **kwargs)
    kwargs["membership_id"] = team.candidates()["candidates"][0]["membership_id"]

    def transfer(_):
        try:
            return team.handoff(epoch, "choose", **kwargs)
        except CollaborationError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transfer, range(2)))
    assert outcomes.count("conflict") == 1
    receipt = next(outcome for outcome in outcomes if isinstance(outcome, dict))
    assert receipt == {"room_id": team.room, "leader_member_id": team.id(peer),
                           "leadership_epoch": epoch+1, "pending": False, "left": False}
    with pytest.raises(CollaborationError, match="forbidden"):
        db.publish_board(team.leader, team.room, 1, "changed", "changed")
    assert db.publish_board(peer, team.room, 1, "changed", "changed")["version"] == 2
    assert not db.list_rooms(team.leader, include_membership=True)[0]["can_remove_members"]
    assert db.list_rooms(peer, include_membership=True)[0]["can_remove_members"]


def test_deferred_departure_commits_after_restart_exactly_at_deadline(team):
    peer = team.join("peer")
    epoch = team.candidates()["leadership_epoch"]
    pending = team.handoff(epoch, "defer", leave=True)
    assert pending["pending"] and not pending["left"]
    team.now += timedelta(seconds=119)
    team.restart()
    team.db.record_leadership_heartbeat(peer)
    team.db.sweep_room_leadership()
    assert team.candidates()["leadership_epoch"] == pending["leadership_epoch"]
    team.now += timedelta(seconds=1)
    team.db.sweep_room_leadership()
    assert team.db.list_rooms(team.leader) == []
    assert team.db.view_board(peer, team.room)["can_edit"]
    assert team.candidates(peer)["leadership_epoch"] > pending["leadership_epoch"]


@pytest.mark.parametrize("forced", ["leave", "self_retire", "owner_retire", "remove_self"])
def test_departure_elects_successor_in_the_same_transaction(team, forced):
    peer = team.join("peer")
    if forced == "leave":
        team.db.leave_room(team.leader, team.room)
    elif forced == "self_retire":
        team.db.retire_self(team.leader)
    elif forced == "owner_retire":
        team.db.retire_member(team.login, team.id(team.leader))
    else:
        row = team.db.list_rooms(team.leader, include_membership=True)[0]
        team.db.remove_room_member(team.leader, team.room, team.id(team.leader), row["membership_id"])
    assert team.db.view_board(peer, team.room)["can_edit"]


def test_disabled_owner_members_excluded_from_successor_selection(team):
    team.db.create_account("account", "a sufficiently long password")
    login = team.db.login("account", "a sufficiently long password")["access_token"]
    owner = team.join("Account Leader", login)
    team.join("Other Lead", login)
    team.join("peer")
    row = next(r for r in team.candidates()["candidates"] if r["member_id"] == team.id(owner))
    team.handoff(1, "choose", member_id=row["member_id"], membership_id=row["membership_id"])
    team.db.disable_account("account")
    # The former leader has no observed heartbeat. The other disabled account member is ineligible.
    assert team.db.view_board(credential("peer"), team.room)["can_edit"]


def test_vacancy_recovers_from_subsequent_member_heartbeat(team):
    peer = team.join("peer", heartbeat=False)
    team.db.leave_room(team.leader, team.room)
    assert not team.db.view_board(peer, team.room)["can_edit"]
    team.restart()
    team.db.record_leadership_heartbeat(peer)
    team.db.sweep_room_leadership()
    assert team.db.view_board(peer, team.room)["can_edit"]


def test_idle_leader_keeps_its_epoch_without_routine_notices(team):
    peer = team.join("peer")
    epoch = team.candidates()["leadership_epoch"]
    for advance in (30, 2, 30):
        team.now += timedelta(minutes=advance)
        team.db.record_leadership_heartbeat(team.leader)
        team.db.record_leadership_heartbeat(peer)
        team.db.sweep_room_leadership()
        team.restart()
        assert team.db.room_notices(team.leader)["notices"] == []
        assert team.db.room_notices(peer)["notices"] == []
        assert team.candidates()["leadership_epoch"] == epoch


def test_disconnected_child_leader_keeps_authority_to_renew_on_reconnect(team):
    child = team.db.create_linked_room(
        team.leader, team.room, "Child", None, None, "since_joining", "lease"
    )
    room_id = child["room_id"]
    peer = team.join("peer")
    team.db.join_room(peer, room_id)
    team.db.record_leadership_heartbeat(team.leader)
    team.now += timedelta(hours=4, minutes=45)
    team.db.record_leadership_heartbeat(peer)
    team.db.maintain_rooms()
    team.restart()
    team.db.record_leadership_heartbeat(team.leader)
    renewed = team.db.extend_room_lease(team.leader, room_id, 3_600, child["expires_at"])
    assert renewed["expires_at"] != child["expires_at"]
    assert not team.db.view_board(peer, room_id)["can_edit"]
    assert team.db.room_notices(peer)["notices"] == []


def test_migrate_v12_seeds_only_joined_enabled_creators(team):
    other = team.db.create_room(team.leader, "second", None, None)["room_id"]
    team.db.leave_room(team.leader, other)
    with closing(sqlite3.connect(team.path)) as conn, conn:
        conn.execute("DROP TABLE room_leadership")
    team.restart()
    assert team.db.view_board(team.leader, team.room)["can_edit"]
    team.db.join_room(team.leader, other)
    assert not team.db.view_board(team.leader, other)["can_edit"]


def race(together, action):
    together.wait(timeout=5)
    try:
        return action()
    except CollaborationError as error:
        return error.code


def test_disabling_an_account_recovers_every_led_room(team):
    peer = team.join("peer")
    rooms = [team.room]
    for index in (2, 3):
        room_id = team.db.create_room(team.leader, f"Room {index}", None, None)["room_id"]
        team.db.join_room(peer, room_id)
        rooms.append(room_id)
    sibling = team.join("sibling", owner="alice")
    room_id = team.db.create_room(sibling, "Sibling room", None, None)["room_id"]
    team.db.join_room(peer, room_id)
    rooms.append(room_id)
    team.db.disable_account("alice")
    for room_id in rooms:
        assert team.db.view_board(peer, room_id)["can_edit"]
    assert sorted(n["room_id"] for n in team.db.room_notices(peer)["notices"]) == sorted(rooms)


def test_all_rooms_keep_their_leader_through_disconnect_and_restart(team):
    peer = team.join("peer")
    rooms = [team.room]
    for index in (2, 3):
        room_id = team.db.create_room(team.leader, f"Room {index}", None, None)["room_id"]
        team.db.join_room(peer, room_id)
        rooms.append(room_id)
    team.db.record_leadership_heartbeat(team.leader)
    team.now += timedelta(minutes=2, seconds=1)
    team.db.record_leadership_heartbeat(peer)
    team.db.sweep_room_leadership()
    team.restart()
    for room_id in rooms:
        assert team.db.view_board(team.leader, room_id)["can_edit"]
        assert not team.db.view_board(peer, room_id)["can_edit"]
    assert team.db.room_notices(peer)["notices"] == []
    team.db.record_leadership_heartbeat(team.leader)
    for room_id in rooms:
        assert team.db.view_board(team.leader, room_id)["can_edit"]


def test_vacancy_recovers_on_the_join_or_heartbeat_that_restores_eligibility(team):
    peer = team.join("peer", heartbeat=False)
    early = team.enroll("early")
    team.db.record_leadership_heartbeat(early)
    team.db.leave_room(team.leader, team.room)
    assert not team.db.view_board(peer, team.room)["can_edit"]
    team.db.record_leadership_heartbeat(peer)
    assert team.db.view_board(peer, team.room)["can_edit"]
    epoch = team.candidates(peer)["leadership_epoch"]
    assert [n["kind"] for n in team.db.room_notices(peer)["notices"]] == [
        "leadership_assigned"
    ]
    team.db.record_leadership_heartbeat(peer)
    team.db.sweep_room_leadership()
    assert team.candidates(peer)["leadership_epoch"] == epoch
    assert len(team.db.room_notices(peer)["notices"]) == 1

    team.db.leave_room(peer, team.room)
    team.db.join_room(early, team.room)
    assert team.db.view_board(early, team.room)["can_edit"]
    assert team.candidates(early)["leadership_epoch"] == epoch + 2


def test_returning_leader_can_fill_a_legacy_probe_vacancy(team):
    with closing(sqlite3.connect(team.path)) as conn, conn:
        # The former sweep recorded a block against this still-joined member.
        conn.execute(
            """
            UPDATE room_leadership SET leader_member_id = NULL,
                previous_member_id = ?, blocked_membership_id = (
                    SELECT membership_id FROM room_members WHERE room_id = ? AND member_id = ?
                ), next_check_at = NULL WHERE room_id = ?
            """,
            (team.id(team.leader), team.room, team.id(team.leader), team.room),
        )
    team.restart()
    assert not team.db.view_board(team.leader, team.room)["can_edit"]
    team.db.record_leadership_heartbeat(team.leader)
    assert team.db.view_board(team.leader, team.room)["can_edit"]


def test_upgrade_retires_pending_probe_and_preserves_deferred_handoff(team):
    peer = team.join("peer")
    handoff_room = team.db.create_room(team.leader, "Handoff", None, None)["room_id"]
    team.db.join_room(peer, handoff_room)
    pending = team.db.handoff_room_leadership(team.leader, handoff_room, 1, "defer", leave=True)

    # Store the same pending challenge and durable notice an older Server wrote.
    leader_id = team.id(team.leader)
    deadline = int((team.now + timedelta(minutes=2)).timestamp()) * 1_000_000
    with closing(sqlite3.connect(team.path)) as conn, conn:
        (epoch,) = conn.execute(
            """
            UPDATE room_leadership SET pending_kind = 'challenge', deadline = ?,
                next_check_at = ?, leadership_epoch = leadership_epoch + 1
            WHERE room_id = ? RETURNING leadership_epoch
            """,
            (deadline, deadline, team.room),
        ).fetchone()
        (notice_id,) = conn.execute(
            """
            UPDATE id_counters SET last_id = last_id + 1
            WHERE counter = 'event_id' RETURNING last_id
            """
        ).fetchone()
        conn.execute(
            """
            INSERT INTO room_control_notices (notice_id, member_id, room_id, kind, text, leadership_epoch)
            VALUES (?, ?, ?, 'leadership_challenge', 'Confirm availability.', ?)
            """,
            (notice_id, leader_id, team.room, epoch),
        )

    [deferred, challenge] = team.db.room_notices(team.leader)["notices"]
    assert challenge["notice_id"] == notice_id
    for _ in range(2):
        team.restart()
        assert team.db.room_notices(team.leader)["notices"] == [deferred]
        assert team.candidates()["leadership_epoch"] == epoch + 1
        assert team.db.view_board(team.leader, team.room)["can_edit"]
    with pytest.raises(CollaborationError, match="conflict"):
        team.db.confirm_room_leadership(team.leader, team.room, epoch)

    team.now += timedelta(seconds=119)
    team.db.record_leadership_heartbeat(peer)
    team.db.sweep_room_leadership()
    assert team.db.room_leadership_candidates(team.leader, handoff_room)["leadership_epoch"] == (
        pending["leadership_epoch"]
    )
    team.now += timedelta(seconds=1)
    team.db.sweep_room_leadership()
    [appointed] = team.db.room_notices(peer)["notices"]
    assert (appointed["room_id"], appointed["kind"]) == (handoff_room, "leadership_assigned")
    assert [room["room_id"] for room in team.db.list_rooms(team.leader)] == [team.room]


def test_timed_out_deferral_notice_is_no_longer_delivered(team):
    pending = team.handoff(1, "defer", leave=True)
    assert [
        (n["kind"], n["leadership_epoch"]) for n in team.db.room_notices(team.leader)["notices"]
    ] == [("leadership_deferred", pending["leadership_epoch"])]
    team.now += timedelta(seconds=119)
    team.db.sweep_room_leadership()
    assert [n["kind"] for n in team.db.room_notices(team.leader)["notices"]] == [
        "leadership_deferred"
    ]
    team.now += timedelta(seconds=1)
    team.db.sweep_room_leadership()
    assert team.db.view_board(team.leader, team.room)["can_edit"]
    assert team.db.room_notices(team.leader)["notices"] == []
    with pytest.raises(CollaborationError, match="conflict"):
        team.handoff(pending["leadership_epoch"], "auto")


def test_assignment_notice_expires_with_the_leadership_that_produced_it(team):
    peer = team.join("peer")
    third = team.join("third")
    first, second = [
        next(row for row in team.candidates()["candidates"] if row["member_id"] == member)
        for member in (team.id(peer), team.id(third))
    ]
    chosen = {"member_id": first["member_id"], "membership_id": first["membership_id"]}
    receipt = team.handoff(1, "choose", **chosen)
    assert [
        (n["kind"], n["leadership_epoch"]) for n in team.db.room_notices(peer)["notices"]
    ] == [("leadership_assigned", receipt["leadership_epoch"])]
    assert team.db.room_notices(third)["notices"] == []
    moved = team.db.handoff_room_leadership(
        peer, team.room, receipt["leadership_epoch"], "choose",
        member_id=second["member_id"], membership_id=second["membership_id"],
    )
    assert team.db.room_notices(peer)["notices"] == []
    assert moved["leader_member_id"] == team.id(third)
    (assigned,) = team.db.room_notices(third)["notices"]
    assert (assigned["kind"], assigned["leadership_epoch"]) == (
        "leadership_assigned", moved["leadership_epoch"]
    )
    with pytest.raises(CollaborationError, match="forbidden"):
        team.db.publish_board(peer, team.room, 1, "changed", "changed")
    with pytest.raises(CollaborationError, match="conflict"):
        team.handoff(receipt["leadership_epoch"], "auto")


def test_choice_is_revalidated_at_appointment(team):
    peer = team.join("peer")
    (row,), epoch = team.candidates()["candidates"], team.candidates()["leadership_epoch"]
    chosen = {"member_id": row["member_id"], "membership_id": row["membership_id"]}
    team.now += timedelta(seconds=45)
    assert team.candidates()["candidates"] == []
    with pytest.raises(CollaborationError, match="conflict"):
        team.handoff(epoch, "choose", **chosen)
    assert team.db.view_board(team.leader, team.room)["can_edit"]
    team.db.create_account("account", "a sufficiently long password")
    login = team.db.login("account", "a sufficiently long password")["access_token"]
    team.join("Lead Claimant", login)
    team.db.record_leadership_heartbeat(peer)
    claim = next(
        candidate for candidate in team.candidates()["candidates"]
        if candidate["reason"] == "leader_profile"
    )
    team.db.disable_account("account")
    with pytest.raises(CollaborationError, match="conflict"):
        team.handoff(epoch, "choose", member_id=claim["member_id"], membership_id=claim["membership_id"])
    assert team.handoff(epoch, "choose", **chosen)["leader_member_id"] == team.id(peer)


def test_confirm_route_cannot_answer_a_deferred_handoff(team):
    peer = team.join("peer")
    pending = team.handoff(team.candidates()["leadership_epoch"], "defer")
    epoch = pending["leadership_epoch"]
    with pytest.raises(CollaborationError, match="forbidden"):
        team.db.confirm_room_leadership(peer, team.room, epoch)
    with pytest.raises(CollaborationError, match="conflict"):
        team.db.confirm_room_leadership(team.leader, team.room, epoch)
    [notice] = team.db.room_notices(team.leader)["notices"]
    assert (notice["kind"], notice["leadership_epoch"]) == ("leadership_deferred", epoch)


def test_choosing_a_claimant_who_leaves_concurrently_never_appoints_a_non_member(
    team,
):
    peer = team.join("peer")
    third = team.join("third")
    claimant, other = team.candidates()["candidates"]
    chosen = {"member_id": claimant["member_id"], "membership_id": claimant["membership_id"]}
    tokens = {team.id(token): token for token in (peer, third)}
    together = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        choosing = pool.submit(race, together, lambda: team.handoff(1, "choose", **chosen))
        leaving = pool.submit(race, together, lambda: team.db.leave_room(tokens[claimant["member_id"]], team.room))
        picked, left = choosing.result(), leaving.result()
    (summary,) = team.db.list_rooms(tokens[other["member_id"]], include_membership=True)
    assert summary["leader_member_id"] in {team.id(team.leader), other["member_id"]}
    if isinstance(picked, dict):
        assert left == {"room_id": team.room}
        assert summary["leader_member_id"] == other["member_id"]
        assert summary["leadership_epoch"] == picked["leadership_epoch"] + 1
    else:
        assert picked == "conflict" and summary["leader_member_id"] == team.id(team.leader)


@pytest.fixture
def routes(tmp_path):
    with api(tmp_path / "routes.sqlite3") as client:
        for label, owner in (("sender", "alice"), ("target", "bob"), ("reader", "bob")):
            assert enroll(client, label, owner=owner).status_code == 200
        yield client


def notices(client, label, query=""):
    response = get(client, label, f"/room-notices{query}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["notices"]


def test_leadership_routes_bind_every_write_to_the_current_epoch_and_membership(routes):
    assert enroll(routes, "sibling").status_code == 200
    path = room(routes)
    room_id = path.rsplit("/", 1)[1]
    handoff_path, confirm_path = f"{path}/leadership/handoff", f"{path}/leadership/confirm"
    join(routes, "target", path)
    join(routes, "sibling", path)
    confirm(routes, "target", path)
    target, sibling = member(routes, "target"), member(routes, "sibling")
    view = f"{path}/board/view"

    error(get(routes, "target", f"{path}/leadership/candidates"), 403, "forbidden")
    error(get(routes, "reader", f"{path}/leadership/candidates"), 404, "not_found")
    error(
        get(routes, "sender", f"/rooms/{uuid4()}/leadership/candidates"),
        404, "not_found",
    )
    assert candidates(routes, "sender", path) == {"leadership_epoch": 1, "candidates": []}
    heartbeat(routes, "sibling")
    ranking = candidates(routes, "sender", path)
    (row,) = ranking["candidates"]
    assert (ranking["leadership_epoch"], row["member_id"], row["reason"]) == (
        1, sibling, "same_owner",
    )
    chosen = {"member_id": sibling, "membership_id": row["membership_id"]}
    error(post(routes, "sender", handoff_path, json={"leadership_epoch": 2, "mode": "auto"}), 409, "conflict")
    error(post(routes, "target", handoff_path, json={"leadership_epoch": 1, "mode": "auto"}), 403, "forbidden")
    error(post(routes, "reader", handoff_path, json={"leadership_epoch": 1, "mode": "auto"}), 404, "not_found")
    for body in (
        {"leadership_epoch": 1, "mode": "choose"},
        {"leadership_epoch": 1, "mode": "auto", "member_id": sibling},
        {"leadership_epoch": 0, "mode": "auto"},
        {"leadership_epoch": 1, "mode": "defer", "leave": "yes"},
        {"leadership_epoch": 1, "mode": "choose", **chosen, "leave": False, "extra": 1},
    ):
        error(post(routes, "sender", handoff_path, json=body), 422, "invalid_request")
    error(
        post(routes, "sender", handoff_path, json={
            "leadership_epoch": 1, "mode": "choose",
            "member_id": target, "membership_id": row["membership_id"],
        }),
        409, "conflict",
    )
    assert candidates(routes, "sender", path) == ranking
    receipt = post(routes, "sender", handoff_path, json={"leadership_epoch": 1, "mode": "choose", **chosen})
    assert receipt.json() == {
        "room_id": room_id, "leader_member_id": sibling, "leadership_epoch": 2,
        "pending": False, "left": False,
    }
    (assigned,) = notices(routes, "sibling")
    assert (assigned["kind"], assigned["leadership_epoch"]) == ("leadership_assigned", 2)
    assert notices(routes, "target") == []
    error(get(routes, "sender", f"{path}/leadership/candidates"), 403, "forbidden")
    assert get(routes, "sender", view).json()["can_edit"] is False

    deferred = post(routes, "sibling", handoff_path, json={"leadership_epoch": 2, "mode": "defer", "leave": True})
    assert deferred.json() == {
        "room_id": room_id, "leader_member_id": sibling, "leadership_epoch": 3,
        "pending": True, "left": False,
    }
    (pending,) = notices(routes, "sibling", f"?after={assigned['notice_id']}")
    assert (pending["kind"], pending["leadership_epoch"]) == ("leadership_deferred", 3)
    error(
        post(routes, "sibling", handoff_path, json={"leadership_epoch": 3, "mode": "defer"}),
        409, "conflict",
    )
    error(post(routes, "target", confirm_path, json={"leadership_epoch": 3}), 403, "forbidden")
    heartbeat(routes, "target")
    (row,) = candidates(routes, "sibling", path)["candidates"]
    committed = post(routes, "sibling", handoff_path, json={
        "leadership_epoch": 3, "mode": "choose",
        "member_id": target, "membership_id": row["membership_id"],
    })
    assert committed.json() == {
        "room_id": room_id, "leader_member_id": target, "leadership_epoch": 4,
        "pending": False, "left": True,
    }
    assert get(routes, "sibling", "/rooms").json() == {"rooms": []}
    error(post(routes, "sibling", confirm_path, json={"leadership_epoch": 4}), 404, "not_found")
    for epoch in (3, 4):
        error(post(routes, "target", confirm_path, json={"leadership_epoch": epoch}), 409, "conflict")
    for body in ({"leadership_epoch": "4"}, {"leadership_epoch": 0}, {"leadership_epoch": 4, "mode": "auto"}):
        error(post(routes, "target", confirm_path, json=body), 422, "invalid_request")

    (assigned,) = notices(routes, "target")
    assert (assigned["kind"], assigned["leadership_epoch"]) == ("leadership_assigned", 4)
    ack = f"/room-notices/{assigned['notice_id']}/ack"
    assert post(routes, "target", ack).json() == {"notice_id": assigned["notice_id"]}
    assert post(routes, "target", ack).json() == {"notice_id": assigned["notice_id"]}
    error(post(routes, "sender", ack), 404, "not_found")
    for query in ("/room-notices?limit=101", "/room-notices?limit=0", "/room-notices?after=-1"):
        error(get(routes, "target", query), 422, "invalid_request")
    error(post(routes, "target", "/room-notices/0/ack"), 422, "invalid_request")
    assert notices(routes, "target") == []

    assert post(routes, "target", f"{path}/leave").status_code == 200
    assert get(routes, "sender", view).json()["can_edit"] is False
    heartbeat(routes, "sender")
    assert get(routes, "sender", view).json()["can_edit"] is True
    (recovered,) = notices(routes, "sender")
    assert recovered["kind"] == "leadership_assigned" and recovered["leadership_epoch"] > 4


def test_child_room_routes_report_deferral_and_lease_conflicts(routes):
    path = room(routes)
    created = post(routes, "sender", f"{path}/children", json={"name": "Focus", "client_request_id": "focus"})
    assert created.status_code == 200, created.text
    linked = created.json()
    assert set(linked) == {"room_id", "name", "history_policy", "parent_room_id", "created_at", "expires_at"}
    child, extend = f"/rooms/{linked['room_id']}", f"/rooms/{linked['room_id']}/lease/extend"
    retry = {"name": "Focus", "client_request_id": "focus"}
    assert post(routes, "sender", f"{path}/children", json=retry).json() == linked
    error(post(routes, "sender", f"{path}/children", json={**retry, "rules": "Changed"}), 409, "conflict")
    error(post(routes, "target", f"{path}/children", json=retry), 404, "not_found")
    error(post(routes, "sender", f"{child}/children", json=retry), 409, "conflict")
    error(post(routes, "sender", f"{path}/children", json={"name": "Focus"}), 422, "invalid_request")
    assert get(routes, "sender", f"{path}/children").json() == {"rooms": [linked], "next_after": None}
    assert get(routes, "target", f"{child}").status_code == 404
    detail = get(routes, "sender", child).json()
    assert {key: detail[key] for key in ("parent_room_id", "created_at", "expires_at")} == {
        key: linked[key] for key in ("parent_room_id", "created_at", "expires_at")
    }
    error(post(routes, "sender", f"{path}/archive"), 409, "room_has_live_children")
    error(post(routes, "target", f"{path}/archive"), 404, "not_found")

    join(routes, "target", child)
    error(post(routes, "target", extend, json={"seconds": 60, "expected_expires_at": linked["expires_at"]}), 403, "forbidden")
    error(post(routes, "reader", extend, json={"seconds": 60, "expected_expires_at": linked["expires_at"]}), 404, "not_found")
    for body in (
        {"seconds": 0, "expected_expires_at": linked["expires_at"]},
        {"seconds": 3601, "expected_expires_at": linked["expires_at"]},
        {"seconds": "60", "expected_expires_at": linked["expires_at"]},
        {"seconds": 60, "expected_expires_at": "2026-01-01T00:00:00"},
        {"seconds": 60},
        {"seconds": 60, "expected_expires_at": linked["expires_at"], "extra": 1},
    ):
        error(post(routes, "sender", extend, json=body), 422, "invalid_request")
    error(post(routes, "sender", extend, json={"seconds": 60, "expected_expires_at": linked["expires_at"]}), 409, "conflict")
    error(post(routes, "target", f"{child}/archive"), 403, "forbidden")
    closed = post(routes, "sender", f"{child}/archive").json()
    assert set(closed) == {"room_id", "name", "archived_at", "archive_reason"}
    assert (closed["room_id"], closed["archive_reason"]) == (linked["room_id"], "goal_reached")
    assert get(routes, "sender", f"{path}/children").json() == {
        "rooms": [
            {
                **linked,
                "archived_at": closed["archived_at"],
                "archive_reason": "goal_reached",
            }
        ],
        "next_after": None,
    }
    assert post(routes, "sender", f"{path}/archive").json()["archive_reason"] == "goal_reached"
    error(post(routes, "sender", f"{path}/children", json={"name": "Late", "client_request_id": "late"}), 409, "room_archived")
