"""Read projections for the local console using existing member authorization."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from .ipc import ClientError

PAGE = 30
MAX_BEFORE = 1 << 63


@dataclass(frozen=True)
class ConsoleSession:
    id: str
    harness: str
    label: str
    token: str = field(repr=False)


class Request(Protocol):
    def __call__(
        self, method: str, path: str, token: str, *, query: dict | None = None
    ) -> Awaitable[dict]: ...


class ConsoleReader:
    def __init__(
        self, sessions: Callable[[], tuple[ConsoleSession, ...]], request: Request
    ):
        self._sessions = sessions
        self._request = request
        self._reads = asyncio.Semaphore(8)

    def _current(self, identity: str) -> ConsoleSession | None:
        return next((item for item in self._sessions() if item.id == identity), None)

    async def _read(
        self, session: ConsoleSession, path: str, query: dict | None = None
    ):
        async with self._reads:
            if self._current(session.id) != session:
                raise ClientError("not_found", "Local session is unavailable")
            response = await self._request("GET", path, session.token, query=query)
            if self._current(session.id) != session:
                raise ClientError("not_found", "Local session is unavailable")
            return response

    async def _describe(
        self, session: ConsoleSession
    ) -> tuple[ConsoleSession, dict, list]:
        description = {
            "id": session.id,
            "label": session.label,
            "harness": session.harness,
            "state": "ready",
            "member_id": None,
        }
        try:
            member = await self._read(session, "/members/me")
            description["label"] = member["role_description"]
            description["member_id"] = member["member_id"]
            listing = await self._read(session, "/rooms")
        except ClientError as exc:
            description["state"] = exc.code
            return session, description, []
        return session, description, listing["rooms"]

    async def workspace(self) -> dict:
        reports = await asyncio.gather(
            *(self._describe(item) for item in self._sessions())
        )
        sessions = []
        rooms: dict[str, dict] = {}
        for reference, session, joined in reports:
            if self._current(reference.id) != reference:
                continue
            sessions.append(session)
            for room in joined:
                identifier = room["room_id"]
                latest = room["latest_message_id"]
                shared = rooms.setdefault(
                    identifier,
                    {
                        "id": identifier,
                        "name": room["name"],
                        "latest_message_id": latest,
                        "memberships": [],
                    },
                )
                if latest is not None and (shared["latest_message_id"] or 0) < latest:
                    shared["latest_message_id"] = latest
                shared["memberships"].append(
                    {
                        "session": session["id"],
                        "acknowledged": room["acked_board_version"],
                        "current": room["current_board_version"],
                    }
                )
        ordered = sorted(
            rooms.values(),
            key=lambda entry: (
                -int(entry["latest_message_id"] or 0),
                entry["name"].casefold(),
                entry["id"],
            ),
        )
        for room in ordered:
            message = room["latest_message_id"]
            room["latest_message_id"] = str(message) if message is not None else None
        return {"sessions": sessions, "rooms": ordered}

    async def team(self, identity: str) -> dict:
        session = self._current(identity)
        if session is None:
            return {"state": "not_found", "members": []}
        try:
            result = await self._read(session, "/members")
        except ClientError as exc:
            return {"state": exc.code, "members": []}
        return {
            "state": "ready",
            "members": [
                {"id": member["member_id"], "role": member["role_description"]}
                for member in result["members"]
            ],
        }

    async def room(
        self, identity: str, room_id: str, before: int | None = None
    ) -> dict:
        session = self._current(identity)
        if session is None:
            return {"state": "not_found"}
        try:
            rooms = (await self._read(session, "/rooms"))["rooms"]
        except ClientError as exc:
            return {"state": exc.code}
        selected = next((room for room in rooms if room["room_id"] == room_id), None)
        if selected is None:
            return {"state": "not_found"}

        board = {
            "current": selected["current_board_version"],
            "acknowledged": selected["acked_board_version"],
        }
        if before is None:
            latest = selected["latest_message_id"]
            before = latest + 1 if latest is not None else MAX_BEFORE
        try:
            history = await self._read(
                session, f"/rooms/{room_id}/messages", {"before": before, "limit": PAGE}
            )
        except ClientError as exc:
            if exc.code in {"not_found", "not_registered", "unauthorized"}:
                return {"state": exc.code}
            return {"state": exc.code, "board": board, "messages": [], "older": None}

        messages = history["messages"]
        team = await self.team(identity) if messages else {"members": []}
        if self._current(identity) != session:
            return {"state": "not_found"}
        if team.get("state") in {"not_found", "not_registered", "unauthorized"}:
            return {"state": team["state"]}
        roles = {member["id"]: member["role"] for member in team["members"]}
        return {
            "state": "ready",
            "board": board,
            "older": str(messages[0]["message_id"]) if len(messages) == PAGE else None,
            "messages": [
                {
                    "id": str(item["message_id"]),
                    "sender": roles.get(
                        item["sender_member_id"], item["sender_member_id"][:8]
                    ),
                    "text": item["text"],
                    "created_at": item.get("created_at"),
                    "mentioned": roles.get(item["mention_member_id"])
                    if item["mention_member_id"]
                    else None,
                }
                for item in messages
            ],
        }
