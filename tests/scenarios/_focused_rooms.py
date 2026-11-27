"""Loopback HTTP fixture with the Server's supported composition clock.

The Server runs in-process with its production lifespan on a loopback port, so
the startup sweep and the 15-second maintenance cadence behave as deployed. Only
time is replaced: leases, deadlines and heartbeats read ``RoomClock``.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import uvicorn
from teamup_server.main import create_app

from tests.scenarios._stack import Member, free_port, new_secret

LEASE = timedelta(hours=5)
NOTICE_WINDOW = timedelta(minutes=15)
CADENCE = 15  # seconds a due notice or expiry can wait for the next maintenance pass


@dataclass
class RoomClock:
    now: datetime = datetime(2026, 1, 1, tzinfo=UTC)


class FocusedServer:
    """One database and clock served on one loopback port.

    ``restart`` serves the same database again, which runs the startup sweep.
    ``client`` never routes loopback traffic through a configured proxy.
    """

    def __init__(self, ws, keys, clock, *, connections=32):
        self.keys, self.clock = keys, clock
        self.db = ws.path("focused.db")
        self.port = free_port()
        self._server = None
        self._serving = None
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            limits=httpx.Limits(
                max_connections=connections, max_keepalive_connections=connections
            ),
            timeout=30,
            # A system or environment proxy must not intercept loopback traffic.
            trust_env=False,
        )

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()
        await self.client.aclose()

    async def start(self):
        app = create_app(self.db, self.keys, clock=lambda: self.clock.now)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            timeout_graceful_shutdown=5,
        )
        self._server = uvicorn.Server(config)
        self._serving = asyncio.create_task(self._server.serve())
        async with asyncio.timeout(20):
            while not self._server.started:
                if self._serving.done():
                    await self._serving
                    raise AssertionError("The focused-room Server did not start")
                await asyncio.sleep(0.01)

    async def stop(self):
        server, self._server, serving = self._server, None, self._serving
        if server is not None:
            server.should_exit = True
            await serving

    async def restart(self):
        """Serve the same database again; the lifespan's startup sweep runs at once."""
        await self.stop()
        await self.start()


class FocusedApi:
    def __init__(self, client):
        self.client = client

    async def request(self, method, path, token, *, expected=None, **kwargs):
        allowed = {200: {None}} if expected is None else expected
        response = await self.client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        try:
            body = response.json()
        except ValueError:
            raise AssertionError(
                f"{method} {path} returned {response.status_code} without JSON: "
                f"{response.text[:300]!r}"
            ) from None
        code = body.get("error", {}).get("code")
        status = response.status_code
        assert status in allowed and code in allowed[status], (
            f"{method} {path} returned {status} {code or ''}: {body}"
        )
        return status, body

    async def call(self, method, path, token, **kwargs):
        _, body = await self.request(method, path, token, **kwargs)
        return body

    async def reject(self, method, path, token, expected, **kwargs):
        """Assert a documented rejection and return its status."""
        status, _ = await self.request(method, path, token, expected=expected, **kwargs)
        return status

    async def enroll(self, key, name, *, heartbeat=False):
        """Enroll a member.

        Without ``heartbeat`` the member sends no presence. A clock jump then
        never makes a leader look lost; it only owes answers to challenges.
        """
        token = new_secret()
        profile = await self.call(
            "POST",
            "/v1/members",
            key,
            json={
                "harness_instance_id": "focused-scenarios",
                "session_key": name,
                "name": name,
                "description": f"{name} runs the focused-room scenario.",
                "member_token": token,
            },
        )
        if heartbeat:
            await self.call("POST", "/v1/presence/heartbeat", token)
        return Member(profile["member_id"], token, profile["role_description"])

    async def heartbeat(self, *members):
        await asyncio.gather(
            *(
                self.call("POST", "/v1/presence/heartbeat", member.token)
                for member in members
            )
        )

    async def acknowledge(self, path, token):
        board = await self.call("GET", f"{path}/board", token)
        await self.call(
            "POST", f"{path}/board/ack", token, json={"version": board["version"]}
        )

    async def pages(self, path, token, key, *, cursor=None, limit=50):
        rows = []
        while True:
            params = {"limit": limit}
            if cursor is not None:
                params["after"] = cursor
            page = await self.call("GET", path, token, params=params)
            rows.extend(page[key])
            next_after = page["next_after"]
            if next_after is None:
                return rows
            assert page[key] and (cursor is None or next_after > cursor), (
                path,
                cursor,
                next_after,
            )
            cursor = next_after

    async def notices(self, token):
        """Unacknowledged room notices; a full page means more may follow."""
        rows = []
        after = 0
        while True:
            page = await self.call(
                "GET", "/v1/room-notices", token, params={"after": after, "limit": 100}
            )
            rows.extend(page["notices"])
            if len(page["notices"]) < 100:
                return rows
            after = page["notices"][-1]["notice_id"]

    async def respond(self, token):
        """Handle every pending notice as a responsive leader does.

        Confirms each leadership challenge with its epoch, then acknowledges all
        notices. No clock move or sweep may pass an unanswered challenge.
        """
        handled = []
        for notice in await self.notices(token):
            if notice["kind"] == "leadership_challenge":
                await self.call(
                    "POST",
                    f"/v1/rooms/{notice['room_id']}/leadership/confirm",
                    token,
                    json={"leadership_epoch": notice["leadership_epoch"]},
                )
            await self.call(
                "POST", f"/v1/room-notices/{notice['notice_id']}/ack", token
            )
            handled.append(notice)
        return handled

    async def led_rooms(self, token):
        """The member's live rooms with their current leaders."""
        rooms = await self.call(
            "GET", "/v1/rooms", token, params={"include_membership": "true"}
        )
        return {room["room_id"]: room for room in rooms["rooms"]}


def room_path(room):
    return f"/v1/rooms/{room['room_id'] if isinstance(room, dict) else room}"


def task_path(room, task):
    return (
        f"{room_path(room)}/tasks/{task['task_id'] if isinstance(task, dict) else task}"
    )


async def until(predicate, *, what, timeout, interval=0.05):
    """Await the first truthy value of ``predicate``; maintenance work is asynchronous."""
    try:
        async with asyncio.timeout(timeout):
            while not (value := await predicate()):
                await asyncio.sleep(interval)
            return value
    except TimeoutError:
        raise AssertionError(f"Timed out after {timeout}s waiting for {what}") from None


async def answer_challenges(api, member, rooms, *, timeout=10):
    """Wait for a sweep to challenge ``rooms`` and answer as that leader.

    A sweep after the challenge interval is the visible proof that it has run.
    """
    rooms = {room["room_id"] if isinstance(room, dict) else room for room in rooms}

    async def pending():
        return rooms <= {
            notice["room_id"]
            for notice in await api.notices(member.token)
            if notice["kind"] == "leadership_challenge"
        }

    await until(pending, what="leadership challenges", timeout=timeout)
    return await api.respond(member.token)
