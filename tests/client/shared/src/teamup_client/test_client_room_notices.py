import asyncio
import sqlite3
from contextlib import closing
from uuid import uuid4

from teamup_client._notices import NoticeStore
from teamup_client._room_control_notices import drain_room_notices


def test_room_notice_is_stored_before_ack_and_retried_without_duplicates(tmp_path):
    path = tmp_path / "notices.db"
    room_id = str(uuid4())
    notice = {
        "notice_id": 42,
        "room_id": room_id,
        "kind": "leadership_assigned",
        "text": "Read the Board and manage the tasks.",
        "leadership_epoch": 2,
    }
    local = {
        "event_id": 42,
        **{key: value for key, value in notice.items() if key != "notice_id"},
    }
    acknowledged = []
    emitted = []

    async def request(method, route, token, query=None):
        assert token == "member-token"
        if method == "GET":
            assert route == "/room-notices"
            assert query == {"after": 0, "limit": 100}
            return {"notices": [notice]}
        assert method == "POST" and route == "/room-notices/42/ack"
        with closing(sqlite3.connect(path)) as separate_process:
            saved = NoticeStore(separate_process)
            assert saved.page("current", 0, 25) == [local]
            assert saved.page("other", 0, 25) == []
        acknowledged.append(42)

    with closing(sqlite3.connect(path)) as connection:
        store = NoticeStore(connection)

        def record(entry):
            if store.record_room_notice("current", entry):
                emitted.append(entry)

        assert asyncio.run(drain_room_notices(request, "member-token", record))
        assert asyncio.run(drain_room_notices(request, "member-token", record))
        assert emitted == [local]
        assert acknowledged == [42, 42]
        assert store.unread_room_notices("current") == 1
        store.mark_delivered("current", [42])
        assert store.unread_room_notices("current") == 0
        assert store.page("current", 42, 25) == []
