import asyncio
from types import SimpleNamespace

from teamup_client._coordinator import Coordinator
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


def test_slow_reminder_does_not_block_the_room_inbox_stream():
    async def scenario():
        displayed = asyncio.Event()
        another_drain = asyncio.Event()
        hold = asyncio.Event()
        count = 0
        reminder = {
            "reminder_id": 1,
            "room_id": ROOM,
            "task_id": TASK,
            "title": "Check fixtures",
        }

        async def request(method, path, token, **kwargs):
            return {"reminders": [reminder]} if method == "GET" else reminder

        async def show(*args):
            displayed.set()
            await hold.wait()

        async def drain(*args):
            nonlocal count
            count += 1
            if count == 2:
                another_drain.set()

        async def lines():
            await displayed.wait()
            yield "event: inbox_changed"
            yield "data: {}"
            yield ""
            await hold.wait()

        session = SimpleNamespace(
            scope="test",
            inbox_changed=asyncio.Event(),
            notices_changed=asyncio.Event(),
            harness_changed=asyncio.Event(),
        )
        binding = SimpleNamespace(member_token="member")
        coordinator = Coordinator.__new__(Coordinator)
        coordinator._upstream = SimpleNamespace(request=request)
        coordinator._notifier = SimpleNamespace(show=show)
        coordinator._drain = drain
        reader = asyncio.create_task(
            coordinator._drain_with_hints(binding, session, lines())
        )
        try:
            await asyncio.wait_for(another_drain.wait(), 1)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(scenario())


def test_transient_oserror_does_not_end_the_reminder_watcher(monkeypatch):
    async def scenario():
        wake = asyncio.Event()
        calls = 0

        async def drain(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                wake.set()
                raise ConnectionResetError("transient connection failure")
            return False

        monkeypatch.setattr("teamup_client._coordinator.drain_task_reminders", drain)
        coordinator = Coordinator.__new__(Coordinator)
        coordinator._upstream = SimpleNamespace(request=None)
        coordinator._notifier = SimpleNamespace(show=None)
        await asyncio.wait_for(
            coordinator._watch_task_reminders(
                SimpleNamespace(member_token="member"), wake
            ),
            timeout=1,
        )
        assert calls == 2

    asyncio.run(scenario())
