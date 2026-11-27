import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from teamup_server.collaboration import Collaboration, CollaborationError
from test_contract import KEYS, credential


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


def test_challenge_requires_bound_agent_and_survives_server_restart(team):
    peer = team.join("peer")
    team.db.record_leadership_heartbeat(team.leader)
    team.now += timedelta(minutes=30)
    team.db.record_leadership_heartbeat(team.leader)
    team.db.sweep_room_leadership()
    epoch = team.candidates()["leadership_epoch"]
    assert epoch > 1
    with pytest.raises(CollaborationError, match="forbidden"):
        team.db.confirm_room_leadership(peer, team.room, epoch)
    answer = team.db.confirm_room_leadership(team.leader, team.room, epoch)
    assert not answer["pending"]
    team.now += timedelta(minutes=30)
    team.db.record_leadership_heartbeat(team.leader)
    team.db.sweep_room_leadership()
    with pytest.raises(CollaborationError, match="conflict"):
        team.db.confirm_room_leadership(team.leader, team.room, epoch)
    probes = team.db.room_notices(team.leader)["notices"]
    current_epoch = team.candidates()["leadership_epoch"]
    [current_probe] = [probe for probe in probes if probe["leadership_epoch"] == current_epoch]
    team.db.ack_room_notice(team.leader, current_probe["notice_id"])
    team.now += timedelta(minutes=2)
    team.restart()
    for token in (peer, team.leader):
        team.db.record_leadership_heartbeat(token)
    team.db.sweep_room_leadership()
    assert team.db.view_board(peer, team.room)["can_edit"]


def test_sweep_recovers_from_lost_client_heartbeat(team):
    peer = team.join("peer")
    team.db.record_leadership_heartbeat(team.leader)
    team.now += timedelta(minutes=2, seconds=1)
    team.db.record_leadership_heartbeat(peer)
    team.db.sweep_room_leadership()
    assert team.db.view_board(peer, team.room)["can_edit"]


def test_migrate_v12_seeds_only_joined_enabled_creators(team):
    other = team.db.create_room(team.leader, "second", None, None)["room_id"]
    team.db.leave_room(team.leader, other)
    with closing(sqlite3.connect(team.path)) as conn, conn:
        conn.execute("DROP TABLE room_leadership")
    team.restart()
    assert team.db.view_board(team.leader, team.room)["can_edit"]
    team.db.join_room(team.leader, other)
    assert not team.db.view_board(team.leader, other)["can_edit"]
