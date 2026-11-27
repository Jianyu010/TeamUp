"""Loopback HTTP fixture with the Server's supported composition clock."""

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import uvicorn
from teamup_server.main import create_app

from tests.scenarios._stack import Member, new_secret


@dataclass
class RoomClock:
    now: datetime = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def focused_server(ws, keys, clock):
    app = create_app(ws.path("focused.db"), keys, clock=lambda: clock.now)
    config = uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                        raise AssertionError("The focused-room Server did not start")
                    await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
                timeout=30,
            ) as client:
                yield client
        finally:
            server.should_exit = True
            await serving


class FocusedApi:
    def __init__(self, client):
        self.client = client

    async def request(self, method, path, token, *, expected=None, **kwargs):
        allowed = {200: {None}} if expected is None else expected
        response = await self.client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        body = response.json()
        code = body.get("error", {}).get("code")
        status = response.status_code
        assert status in allowed and code in allowed[status], (
            f"{method} {path} returned {status} {code or ''}: {body}"
        )
        return status, body

    async def call(self, method, path, token, **kwargs):
        _, body = await self.request(method, path, token, **kwargs)
        return body

    async def enroll(self, key, name):
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
        await self.call("POST", "/v1/presence/heartbeat", token)
        return Member(profile["member_id"], token, profile["role_description"])

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
