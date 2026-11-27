from datetime import timedelta

import pytest
from test_room_leadership import Team


@pytest.mark.parametrize("with_successor", [True, False])
def test_connected_incumbent_survives_challenge_without_delivery_or_successor(
    tmp_path, with_successor
):
    team = Team(tmp_path / "live-room.sqlite3")
    successor = team.join("peer") if with_successor else None
    team.db.record_leadership_heartbeat(team.leader)
    team.now += timedelta(minutes=30)
    team.db.record_leadership_heartbeat(team.leader)
    if successor:
        team.db.record_leadership_heartbeat(successor)
    team.db.sweep_room_leadership()
    [probe] = team.db.room_notices(team.leader)["notices"]
    assert probe["kind"] == "leadership_challenge"

    if successor is None:
        team.db.ack_room_notice(team.leader, probe["notice_id"])
    team.now += timedelta(minutes=2)
    team.db.record_leadership_heartbeat(team.leader)
    if successor:
        team.db.record_leadership_heartbeat(successor)
    team.db.sweep_room_leadership()

    assert team.db.view_board(team.leader, team.room)["can_edit"]


def test_leadership_challenge_does_not_duplicate_a_lease_offer(tmp_path):
    team = Team(tmp_path / "live-lease.sqlite3")
    child = team.db.create_linked_room(
        team.leader, team.room, "Short work", None, None, "since_joining", "lease"
    )
    team.now += timedelta(hours=4, minutes=45)
    team.db.record_leadership_heartbeat(team.leader)
    team.db.sweep_child_rooms()
    team.db.sweep_room_leadership()
    team.db.sweep_child_rooms()
    offers = [
        row for row in team.db.room_notices(team.leader)["notices"]
        if row["kind"] == "child_lease_due" and row["room_id"] == child["room_id"]
    ]
    assert len(offers) == 1
