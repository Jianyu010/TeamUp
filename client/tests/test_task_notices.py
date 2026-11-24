import asyncio

from teamup_client._task_notices import drain_task_reminders
from teamup_client.ipc import ClientError

ROOM = "247629c4-8e54-4d0e-8b25-7a8c43bf85b2"
TASK = "2f47cb81-36dc-42eb-8736-8f51a303455a"


def test_notifier_uses_only_a_successful_claim_and_ignores_legacy_server():
    async def scenario():
        pending = {
            1: {
                "reminder_id": 1,
                "room_id": ROOM,
                "task_id": TASK,
                "title": "Check fixtures",
                "stalled_since": "2026-11-25T00:00:00Z",
            },
            2: {
                "reminder_id": 2,
                "room_id": ROOM,
                "task_id": TASK,
                "title": "Expired",
                "stalled_since": "2026-11-25T00:00:00Z",
            },
        }
        shown = []

        async def request(method, path, token, *, query=None):
            assert token == "member"
            if method == "GET":
                return {"reminders": list(pending.values())}
            identifier = int(path.split("/")[-2])
            reminder = pending.pop(identifier)
            if identifier == 2:
                raise ClientError("not_found", "Invalidated by progress")
            return reminder

        async def show(title, subtitle, body):
            shown.append((title, subtitle, body))

        assert await drain_task_reminders(request, "member", show) is True
        assert await drain_task_reminders(request, "member", show) is True
        assert len(shown) == 1
        assert "Check fixtures" in str(shown[0])

        async def missing(*args, **kwargs):
            raise ClientError("not_found", "Legacy Server")

        assert await drain_task_reminders(missing, "member", show) is False
        assert len(shown) == 1

    asyncio.run(scenario())
