from uuid import uuid4

import pytest
from teamup_server.collaboration import Collaboration, CollaborationError
from test_contract import KEYS, api, credential, enroll, join, member, room


def test_room_notice_is_recipient_scoped_and_survives_ack_retry_and_restart(tmp_path):
    db = tmp_path / "team.sqlite3"
    with api(db) as client:
        for label in ("sender", "target", "reader"):
            assert enroll(client, label).status_code == 200
        room_path = room(client)
        join(client, "target", room_path)
        join(client, "reader", room_path)
        target = member(client, "target")
    room_id = room_path.rsplit("/", 1)[-1]

    collaboration = Collaboration(db, KEYS)
    with collaboration._transaction(write=True) as transaction:
        notice_id = collaboration._add_room_notice(
            transaction,
            target,
            room_id,
            "leadership_assigned",
            "Review the Board.",
            leadership_epoch=2,
        )
        assert (
            collaboration._add_room_notice(
                transaction,
                target,
                room_id,
                "leadership_assigned",
                "Review the Board.",
                leadership_epoch=2,
            )
            == notice_id
        )

    recovered = Collaboration(db, KEYS)
    assert recovered.room_notices(credential("reader")) == {"notices": []}
    assert recovered.room_notices(credential("target"), after=notice_id) == {
        "notices": []
    }
    assert recovered.room_notices(credential("target"), limit=1) == {
        "notices": [
            {
                "notice_id": notice_id,
                "kind": "leadership_assigned",
                "room_id": room_id,
                "text": "Review the Board.",
                "leadership_epoch": 2,
            }
        ]
    }
    with pytest.raises(CollaborationError, match="not_found"):
        recovered.ack_room_notice(credential("reader"), notice_id)
    with pytest.raises(CollaborationError, match="unauthorized"):
        recovered.room_notices(str(uuid4()))
    assert recovered.ack_room_notice(credential("target"), notice_id) == {
        "notice_id": notice_id
    }
    assert recovered.ack_room_notice(credential("target"), notice_id) == {
        "notice_id": notice_id
    }
    assert recovered.room_notices(credential("target")) == {"notices": []}
