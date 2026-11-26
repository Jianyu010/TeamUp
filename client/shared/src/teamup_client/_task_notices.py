"""Desktop delivery for the separate task reminder stream."""

from ._desktop import PREVIEW_LIMIT, TITLE_LIMIT, clean
from ._protocol import is_integer, is_string, is_uuid
from ._upstream import unavailable
from .ipc import ClientError

_LIMIT = 100


async def drain_task_reminders(request, token, show) -> bool:
    """Attempt a desktop notice only after a successful provider claim."""
    after = 0
    while True:
        try:
            result = await request(
                "GET", "/task-reminders", token, query={"after": after, "limit": _LIMIT}
            )
        except ClientError as exc:
            if exc.code == "not_found":
                return False
            raise
        reminders = result.get("reminders")
        if not isinstance(reminders, list) or len(reminders) > _LIMIT:
            raise unavailable()
        for entry in reminders:
            if not isinstance(entry, dict) or not is_integer(
                entry.get("reminder_id"), after + 1, (1 << 63) - 1
            ):
                raise unavailable()
            after = entry["reminder_id"]
            try:
                claimed = await request("POST", f"/task-reminders/{after}/claim", token)
            except ClientError as exc:
                if exc.code == "not_found":
                    continue
                raise
            if (
                claimed.get("reminder_id") != after
                or not is_uuid(claimed.get("room_id"))
                or not is_uuid(claimed.get("task_id"))
                or not is_string(claimed.get("title"), 200)
            ):
                raise unavailable()
            await show(
                "TeamUp task stalled",
                clean(claimed["title"], TITLE_LIMIT),
                clean("Open TeamUp to review the task's progress.", PREVIEW_LIMIT),
            )
        if len(reminders) < _LIMIT:
            return True
