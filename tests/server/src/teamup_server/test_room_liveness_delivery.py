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
