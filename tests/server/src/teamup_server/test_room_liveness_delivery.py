from datetime import timedelta

import pytest
from teamup_server.collaboration import CollaborationError
from test_room_leadership import Team


@pytest.mark.parametrize("with_successor", [True, False])
def test_connected_incumbent_stays_quiet_with_or_without_successor(
    tmp_path, with_successor
):
    team = Team(tmp_path / "live-room.sqlite3")
    successor = team.join("peer") if with_successor else None
    epoch = team.candidates()["leadership_epoch"]
    for _ in range(2):
        team.now += timedelta(minutes=30)
        team.db.record_leadership_heartbeat(team.leader)
        if successor:
            team.db.record_leadership_heartbeat(successor)
        team.db.sweep_room_leadership()
        assert team.db.room_notices(team.leader)["notices"] == []
        assert team.candidates()["leadership_epoch"] == epoch

    assert team.db.view_board(team.leader, team.room)["can_edit"]


def test_deferred_handoff_does_not_duplicate_a_lease_offer(tmp_path):
    team = Team(tmp_path / "live-lease.sqlite3")
    child = team.db.create_linked_room(
        team.leader, team.room, "Short work", None, None, "since_joining", "lease"
    )
    team.now += timedelta(hours=4, minutes=45)
    team.db.record_leadership_heartbeat(team.leader)
    team.db.sweep_child_rooms()
    room_id = child["room_id"]
    epoch = team.db.room_leadership_candidates(team.leader, room_id)["leadership_epoch"]
    team.db.handoff_room_leadership(team.leader, room_id, epoch, "defer")
    team.db.sweep_room_leadership()
    team.db.sweep_child_rooms()
    offers = [
        row for row in team.db.room_notices(team.leader)["notices"]
        if row["kind"] == "child_lease_due" and row["room_id"] == child["room_id"]
    ]
    assert len(offers) == 1


def test_room_notice_positions_above_sqlite_range(tmp_path):
    team = Team(tmp_path / "large-cursor.sqlite3")
    assert team.db.room_notices(team.leader, after=1 << 63) == {"notices": []}
    with pytest.raises(CollaborationError, match="not_found"):
        team.db.ack_room_notice(team.leader, 1 << 63)
