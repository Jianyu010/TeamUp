"""Read projections for the local console using existing member authorization."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from .ipc import ClientError

PAGE = 30
VISIBLE_WIRE_LIMIT = 20
MAX_BEFORE = 1 << 63


@dataclass(frozen=True)
class ConsoleSession:
    id: str
    harness: str
    label: str
    token: str = field(repr=False)


class Request(Protocol):
    def __call__(
        self,
        method: str,
        path: str,
        token: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
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
                        "active_task": None,
                    }
                )
        if (
            sum(len(room["memberships"]) for room in rooms.values())
            <= VISIBLE_WIRE_LIMIT
        ):
            by_session = {
                reference.id: (reference, session["member_id"])
                for reference, session, _ in reports
            }
            work = [
                (membership, room["id"])
                for room in rooms.values()
                for membership in room["memberships"]
                if membership["acknowledged"] == membership["current"]
            ]
            observations = await asyncio.gather(
                *(
                    self._participating(*by_session[membership["session"]], room_id)
                    for membership, room_id in work
                )
            )
            for (membership, _), active in zip(work, observations, strict=True):
                membership["active_task"] = active
        approved = {
            reference.id
            for reference, _, _ in reports
            if self._current(reference.id) == reference
        }
        sessions = [session for session in sessions if session["id"] in approved]
        for room in rooms.values():
            room["memberships"] = [
                item for item in room["memberships"] if item["session"] in approved
            ]
        ordered = sorted(
            (room for room in rooms.values() if room["memberships"]),
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

    async def _participating(
        self, session: ConsoleSession, member_id: str, room_id: str
    ) -> bool | None:
        after = 0
        while True:
            try:
                page = await self._read(
                    session,
                    f"/rooms/{room_id}/tasks",
                    {"status": "open", "after": after, "limit": 100},
                )
            except ClientError:
                return None
            if any(
                member_id in task["participant_member_ids"] for task in page["tasks"]
            ):
                return True
            cursor = page["next_after"]
            if cursor is None:
                return False
            if cursor <= after:
                return None
            after = cursor

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

    async def tasks(self, room_id: str, status: str, after: int) -> dict:
        path = f"/rooms/{room_id}/tasks"

        async def read_page(session):
            try:
                result = await self._read(
                    session, path, {"status": status, "after": after, "limit": PAGE}
                )
            except ClientError as exc:
                return session, {"state": exc.code}
            return session, result

        reports = await asyncio.gather(*(read_page(item) for item in self._sessions()))
        tasks: dict[str, dict] = {}
        scopes: dict[str, list[str]] = {}
        errors = set()
        has_more = False
        ready = False
        for session, result in reports:
            if self._current(session.id) != session:
                continue
            if "state" in result:
                errors.add(result["state"])
                continue
            ready = True
            has_more |= result["next_after"] is not None
            for task in result["tasks"]:
                identifier = task["task_id"]
                previous = tasks.get(identifier)
                if previous is None or task["version"] > previous["version"]:
                    tasks[identifier] = task
                scopes.setdefault(identifier, []).append(session.id)
        if not ready:
            state = next(
                (
                    code
                    for code in ("board_ack_required", "unavailable", "not_registered")
                    if code in errors
                ),
                "not_found",
            )
            return {"state": state, "tasks": [], "next_after": None}
        ordered = sorted(tasks.values(), key=lambda task: task["task_number"])
        page = ordered[:PAGE]
        more = (
            str(page[-1]["task_number"])
            if page and (len(ordered) > PAGE or has_more)
            else None
        )
        return {
            "state": "ready",
            "tasks": [
                {
                    **task,
                    "task_number": str(task["task_number"]),
                    "visible_to": scopes[task["task_id"]],
                }
                for task in page
            ],
            "next_after": more,
        }

    async def task_events(self, identity: str, room_id: str, task_id: str, after: int):
        session = self._current(identity)
        if session is None:
            return {"state": "not_found"}
        try:
            result = await self._read(
                session,
                f"/rooms/{room_id}/tasks/{task_id}/events",
                {"after": after, "limit": PAGE},
            )
        except ClientError as exc:
            return {"state": exc.code}
        cursor = result["next_after"]
        return {
            "state": "ready",
            "events": result["events"],
            "next_after": str(cursor) if cursor is not None else None,
        }

    async def task_action(
        self, identity: str, room_id: str, task_id: str, action: str, body: dict | None
    ) -> dict:
        async with self._reads:
            session = self._current(identity)
            if session is None:
                return {"state": "not_found"}
            try:
                result = await self._request(
                    "POST",
                    f"/rooms/{room_id}/tasks/{task_id}/{action}",
                    session.token,
                    body=body,
                )
            except ClientError as exc:
                return {"state": exc.code}
            if self._current(identity) != session:
                return {"state": "not_found"}
            return {"state": "ready", "task": result}
