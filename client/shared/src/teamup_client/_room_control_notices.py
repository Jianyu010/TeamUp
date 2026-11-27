"""Durable room control delivery through the binding's notice store."""

from ._protocol import is_integer, is_string, is_uuid
from ._upstream import unavailable
from .ipc import ClientError

_LIMIT = 100
_MAX_ID = (1 << 63) - 1
_KINDS = {
    "child_lease_due",
    "child_tasks_rolled_up",
    "leadership_assigned",
    "leadership_challenge",
    "leadership_deferred",
}


async def drain_room_notices(request, token, record) -> bool:
    after = 0
    while True:
        try:
            page = await request(
                "GET", "/room-notices", token, query={"after": after, "limit": _LIMIT}
            )
        except ClientError as exc:
            if exc.code == "not_found":
                return False
            raise
        notices = page.get("notices")
        if not isinstance(notices, list) or len(notices) > _LIMIT:
            raise unavailable()
        for notice in notices:
            if (
                not isinstance(notice, dict)
                or not is_integer(notice.get("notice_id"), after + 1, _MAX_ID)
                or not isinstance(notice.get("kind"), str)
                or notice["kind"] not in _KINDS
                or not is_uuid(notice.get("room_id"))
                or not is_string(notice.get("text"), 2000)
            ):
                raise unavailable()
            after = notice["notice_id"]
            entry = {
                "event_id": after,
                "kind": notice["kind"],
                "room_id": notice["room_id"],
                "text": notice["text"],
            }
            if "leadership_epoch" in notice:
                if not is_integer(notice["leadership_epoch"], 1, _MAX_ID):
                    raise unavailable()
                entry["leadership_epoch"] = notice["leadership_epoch"]
            if "expires_at" in notice:
                if not is_string(notice["expires_at"], 64):
                    raise unavailable()
                entry["expires_at"] = notice["expires_at"]
            record(entry)
            try:
                await request("POST", f"/room-notices/{after}/ack", token)
            except ClientError as exc:
                if exc.code != "not_found":
                    raise
        if len(notices) < _LIMIT:
            return True
