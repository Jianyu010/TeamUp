from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from teamup_server.collaboration import Collaboration, CollaborationError
from test_contract import KEYS, credential


@pytest.fixture
def team(tmp_path):
    database = tmp_path / "team.sqlite3"
    collaboration = Collaboration(database, KEYS)
    participants = {}
    for name in ("leader", "peer", "observer"):
        token = credential(name)
        member = collaboration.enroll(
            KEYS["alice" if name == "leader" else "bob"],
            harness_instance_id=name,
            session_key=name,
            member_token=token,
            name=name,
            description=f"{name} test member",
        )
        participants[name] = (token, member["member_id"])
    return database, collaboration, participants


def test_invitation_uses_the_private_dm_and_requests_a_deliberate_join(team):
    _, collaboration, participants = team
    leader_token, leader_id = participants["leader"]
    peer_token, peer_id = participants["peer"]
    observer_token, _ = participants["observer"]
    room_id = collaboration.create_room(
        leader_token, "Planning", "private rules", "private protocol"
    )["room_id"]
    collaboration.join_room(observer_token, room_id)
    dm_id = collaboration.open_dm(leader_token, peer_id)["dm_id"]
    legacy = collaboration.send_dm(leader_token, dm_id, "shared-key", "An ordinary DM")
    seen = []

    def observe():
        seen.append(collaboration.inbox(peer_token, 0, 50, ("dm",)))

    with collaboration.inbox_changes(peer_token)(observe):
        receipt = collaboration.invite_to_room(
            leader_token, room_id, peer_id, "shared-key"
        )
        assert (
            collaboration.invite_to_room(leader_token, room_id, peer_id, "shared-key")
            == receipt
        )

    assert set(receipt) == set(legacy)
    assert receipt["dm_id"] == dm_id
    assert receipt["sender_member_id"] == leader_id
    assert room_id in receipt["text"]
    assert "Planning" in receipt["text"]
    for instruction in ("join", "read", "acknowledge", "board"):
        assert instruction in receipt["text"].lower()
    assert "private rules" not in receipt["text"]
    assert "private protocol" not in receipt["text"]
    assert len(seen) == 1
    assert [event["kind"] for event in seen[0]] == ["dm", "dm"]
    assert [event["message"] for event in seen[0]] == [legacy, receipt]
    for token in (leader_token, observer_token):
        assert collaboration.inbox(token, 0, 50, ("dm",)) == []
    with pytest.raises(CollaborationError, match="not_found"):
        collaboration.dm_history(observer_token, dm_id, 0, 50)
    assert collaboration.dm_history(peer_token, dm_id, 0, 50)["messages"] == [
        legacy,
        receipt,
    ]
    assert collaboration.list_rooms(peer_token) == []
    with pytest.raises(CollaborationError, match="not_found"):
        collaboration.read_board(peer_token, room_id)
    collaboration.join_room(peer_token, room_id)
    assert collaboration.read_board(peer_token, room_id)["version"] == 1


def test_invitation_retries_serialize_and_bind_to_the_original_room_and_peer(team):
    database, collaboration, participants = team
    leader_token, _ = participants["leader"]
    peer_token, peer_id = participants["peer"]
    _, observer_id = participants["observer"]
    room_id = collaboration.create_room(leader_token, "Planning", None, None)["room_id"]
    second_room = collaboration.create_room(leader_token, "Follow-up", None, None)[
        "room_id"
    ]

    with ThreadPoolExecutor(max_workers=8) as workers:
        receipts = list(
            workers.map(
                lambda _: collaboration.invite_to_room(
                    leader_token, room_id, peer_id, "retry"
                ),
                range(8),
            )
        )
    receipt = receipts[0]
    assert receipts == [receipt] * 8
    restarted = Collaboration(database, KEYS)
    assert restarted.invite_to_room(leader_token, room_id, peer_id, "retry") == receipt
    for changed_room, changed_peer in ((second_room, peer_id), (room_id, observer_id)):
        with pytest.raises(CollaborationError, match="conflict"):
            restarted.invite_to_room(leader_token, changed_room, changed_peer, "retry")
    assert [
        event["message"] for event in restarted.inbox(peer_token, 0, 50, ("dm",))
    ] == [receipt]


def test_invitation_requires_current_room_authority_and_an_active_peer(team):
    _, collaboration, participants = team
    leader_token, leader_id = participants["leader"]
    peer_token, peer_id = participants["peer"]
    observer_token, observer_id = participants["observer"]
    room_id = collaboration.create_room(leader_token, "Planning", None, None)["room_id"]
    collaboration.join_room(observer_token, room_id)

    with pytest.raises(CollaborationError, match="unauthorized"):
        collaboration.invite_to_room(credential("unknown"), room_id, peer_id, "key")
    with pytest.raises(CollaborationError, match="not_found"):
        collaboration.invite_to_room(peer_token, room_id, leader_id, "key")
    with pytest.raises(CollaborationError, match="forbidden"):
        collaboration.invite_to_room(observer_token, room_id, peer_id, "key")
    with pytest.raises(CollaborationError, match="invalid_request"):
        collaboration.invite_to_room(leader_token, room_id, leader_id, "key")
    with pytest.raises(CollaborationError, match="not_found"):
        collaboration.invite_to_room(leader_token, room_id, str(uuid4()), "key")
    with pytest.raises(CollaborationError, match="invalid_request"):
        collaboration.invite_to_room(leader_token, room_id, observer_id, "  ")
    collaboration.retire_self(peer_token)
    with pytest.raises(CollaborationError, match="not_found"):
        collaboration.invite_to_room(leader_token, room_id, peer_id, "key")
    collaboration.archive_room(leader_token, room_id)
    with pytest.raises(CollaborationError, match="room_archived"):
        collaboration.invite_to_room(leader_token, room_id, observer_id, "key")
    assert collaboration.inbox(observer_token, 0, 50, ("dm",)) == []


def test_archiving_serializes_against_an_invitation(team):
    _, collaboration, participants = team
    leader_token, _ = participants["leader"]
    peer_token, peer_id = participants["peer"]
    room_id = collaboration.create_room(leader_token, "Planning", None, None)["room_id"]
    together = Barrier(2)

    def archive():
        together.wait(timeout=5)
        return collaboration.archive_room(leader_token, room_id)

    def invite():
        together.wait(timeout=5)
        try:
            return collaboration.invite_to_room(leader_token, room_id, peer_id, "race")
        except CollaborationError as error:
            assert error.code == "room_archived"
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        closing_room = workers.submit(archive)
        sending_invitation = workers.submit(invite)
        assert closing_room.result()["room_id"] == room_id
        receipt = sending_invitation.result()

    events = collaboration.inbox(peer_token, 0, 50, ("dm",))
    assert [event["message"] for event in events] == (
        [] if receipt is None else [receipt]
    )
