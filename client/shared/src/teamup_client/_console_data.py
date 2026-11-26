"""Read projections for the local console using existing member authorization."""

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol

from ._protocol import is_integer, is_string, is_uuid
from ._upstream import unavailable
from .ipc import ClientError

PAGE = 30
VISIBLE_WIRE_LIMIT = 20
TASK_OBSERVATION_SECONDS = 2
TASK_OBSERVATION_PAGES = 2
DIRECTORY_OBSERVATION_SECONDS = 3
PRESENCE_OBSERVATION_SECONDS = 2
VERSION_OBSERVATION_SECONDS = 2
MAX_BEFORE = 1 << 63  # The Server treats this as an unbounded latest-page read.


@dataclass(frozen=True)
class _MessageMember:
    label: str
    route_name: str
    legacy_title: str | None = None


def _message_members(members: list[dict]) -> dict[str, _MessageMember]:
    profiles = {}
    for member in members:
        label = member["role_description"]
        name = label
        title = None
        structured_name = member.get("name")
        if isinstance(structured_name, str) and is_string(structured_name):
            name = structured_name
        else:
            parts = [part.strip() for part in re.split(r":(?=\s)|：", label, maxsplit=1)]
            if len(parts) == 2 and all(parts):
                name, title = parts
        profiles[member["member_id"]] = _MessageMember(label, name, title)
    labels = Counter(profile.label for profile in profiles.values())
    route_names = Counter(profile.route_name for profile in profiles.values())
    return {
        identifier: _MessageMember(
            f"{profile.label} ({identifier[:8]})"
            if labels[profile.label] > 1 else profile.label,
            f"{profile.route_name} ({identifier[:8]})"
            if route_names[profile.route_name] > 1 else profile.route_name,
            profile.legacy_title,
        )
        for identifier, profile in profiles.items()
    }


def _message(item: dict, members: dict[str, _MessageMember]) -> dict:
    def member_for(identifier: str) -> _MessageMember:
        short_id = identifier[:8]
        return members.get(identifier) or _MessageMember(short_id, short_id)

    sender = item["sender_member_id"]
    sender_profile = member_for(sender)
    if item.get("sender_retired") is True:
        label = f"Retired member ({sender[:8]})"
        sender_profile = _MessageMember(label, label)
    first_target = item["mention_member_id"]
    targets = item.get("mention_member_ids", [first_target] if first_target else [])
    target_profiles = [member_for(target) for target in targets]
    mentioned = [profile.label for profile in target_profiles]
    result = {
        "id": str(item["message_id"]),
        "sender": sender_profile.label,
        "sender_member_id": sender,
        "text": item["text"],
        "created_at": item.get("created_at"),
        "mentioned": mentioned[0] if mentioned else None,
        "recipient_member_ids": targets,
    }
    if len(mentioned) > 1:
        result["mentioned_recipients"] = mentioned
    visible_targets = target_profiles[:2]
    displayed_profiles = [sender_profile, *visible_targets]
    legacy_titles = [profile.legacy_title for profile in displayed_profiles if profile.legacy_title]
    if legacy_titles or any(profile.route_name != profile.label for profile in displayed_profiles):
        result["routing"] = {
            "sender": sender_profile.route_name,
            "recipients": [profile.route_name for profile in visible_targets],
            "legacy_titles": legacy_titles,
        }
    return result


def _profile_fields(member: dict) -> dict:
    return {
        field: value
        for field in ("name", "owner_username", "description")
        if is_string(value := member.get(field))
    }


def _member_with_presence(member: dict, presence: dict[str, dict]) -> dict:
    identifier = member["member_id"]
    return {
        "id": identifier,
        "role": member["role_description"],
        **_profile_fields(member),
        "presence": presence.get(
            identifier, {"state": "unknown", "observed_at": None, "age_ms": None}
        ),
    }


def _tasks_by_member(members: list[dict], tasks: list[dict]) -> dict[str, list[dict]]:
    roles = {member["member_id"]: member["role_description"] for member in members}
    by_member: dict[str, list[dict]] = {identifier: [] for identifier in roles}
    for task in tasks:
        participants = [
            {"id": identifier, "role": roles[identifier]}
            for identifier in task["participant_member_ids"]
            if identifier in roles
        ]
        for participant in participants:
            by_member[participant["id"]].append(
                {
                    "id": task["task_id"],
                    "title": task["title"],
                    "teammates": [peer for peer in participants if peer != participant],
                }
            )
    return by_member


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
        token: str | None,
        *,
        query: dict | None = None,
        body: dict | None = None,
    ) -> Awaitable[dict]: ...


class SessionLifecycle(Protocol):
    async def console_wake_settings(self, enabled: bool | None = None) -> dict: ...

    def console_retired_sessions(self) -> list[dict]: ...

    async def console_session_action(
        self, session_id: str, room_id: str | None, membership_id: str | None
    ) -> dict: ...

    async def console_retired_history(
        self,
        session_id: str,
        *,
        after: int | None = None,
        kind: str | None = None,
        conversation: str | None = None,
        before: int | None = None,
    ) -> dict: ...


class ConsoleReader:
    def __init__(
        self,
        sessions: Callable[[], tuple[ConsoleSession, ...]],
        request: Request,
        lifecycle: SessionLifecycle | None = None,
    ):
        self._sessions = sessions
        self._request = request
        self._lifecycle = lifecycle
        self._reads = asyncio.Semaphore(8)
        try:
            installed_version = version("teamup-client")
        except PackageNotFoundError:
            installed_version = None
        self._version = (
            None if installed_version == "0.0.0+unknown" else installed_version
        )

    async def wake_settings(self, enabled: bool | None = None) -> dict:
        if self._lifecycle is None:
            return {"state": "unavailable"}
        return await self._lifecycle.console_wake_settings(enabled)

    async def versions(self) -> dict:
        server_version = None
        try:
            async with asyncio.timeout(VERSION_OBSERVATION_SECONDS):
                result = await self._request("GET", "/version", None)
        except (ClientError, TimeoutError):
            state = "unavailable"
        else:
            reported = result.get("version")
            server_version = reported if is_string(reported, 100) else None
            state = "ready" if server_version is not None else "unknown"
        return {
            "client_version": self._version,
            "server_version": server_version,
            "server_state": state,
        }

    def _current(self, identity: str) -> ConsoleSession | None:
        return next((item for item in self._sessions() if item.id == identity), None)

    async def _read(
        self, session: ConsoleSession, path: str, query: dict | None = None
    ):
        async with self._reads:
            if self._current(session.id) != session:
                raise ClientError("not_found", "Local session is unavailable")
            try:
                response = await self._request("GET", path, session.token, query=query)
            except OSError:
                raise unavailable() from None
            if self._current(session.id) != session:
                raise ClientError("not_found", "Local session is unavailable")
            return response

    async def _write(self, identity: str, path: str, body: dict | None) -> dict:
        async with self._reads:
            session = self._current(identity)
            if session is None:
                raise ClientError("not_found", "Local session is unavailable")
            result = await self._request("POST", path, session.token, body=body)
            if self._current(identity) != session:
                raise ClientError("not_found", "Local session is unavailable")
            return result

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
            description.update(_profile_fields(member))
            description["label"] = description.get("name", member["role_description"])
            description["member_id"] = member["member_id"]
            listing = await self._read(session, "/rooms", {"include_membership": "true"})
        except ClientError as exc:
            description["state"] = exc.code
            return session, description, []
        return session, description, listing["rooms"]

    async def _directory_pages(self, session: ConsoleSession) -> list[dict]:
        rooms: list[dict] = []
        after: str | None = None
        while True:
            query: dict = {"limit": 100}
            if after is not None:
                query["after"] = after
            page = await self._read(session, "/room-directory", query)
            entries = page.get("rooms")
            if not isinstance(entries, list) or any(
                not isinstance(room, dict)
                or not is_uuid(room.get("room_id"))
                or not is_string(room.get("name"), 200)
                or room.get("history_policy") not in ("retained", "since_joining")
                for room in entries
            ):
                raise unavailable()
            rooms.extend(entries)
            cursor = page.get("next_after")
            if cursor is None:
                return rooms
            if (
                not entries
                or cursor != entries[-1]["room_id"]
                or (after is not None and cursor <= after)
            ):
                raise unavailable()
            after = cursor

    async def _directory(
        self, candidates: list[ConsoleSession], deadline: float | None
    ) -> tuple[ConsoleSession | None, list[dict]]:
        now = asyncio.get_running_loop().time()
        stop = now + DIRECTORY_OBSERVATION_SECONDS
        if deadline is not None:
            stop = min(stop, deadline)
        if stop <= now:
            return None, []
        try:
            async with asyncio.timeout_at(stop):
                for session in candidates:
                    try:
                        return session, await self._directory_pages(session)
                    except ClientError as exc:
                        # Another approved member can replace a lost credential.
                        if (
                            exc.code not in {"unauthorized", "not_registered"}
                            and self._current(session.id) == session
                        ):
                            break
        except TimeoutError:
            pass
        return None, []

    async def workspace(self, *, directory_deadline: float | None = None) -> dict:
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
                if room.get("can_remove_members") is True:
                    permissions = {"can_remove_members": True}
                else:
                    permissions = {}
                shared["memberships"].append(
                    {
                        "session": session["id"],
                        "acknowledged": room["acked_board_version"],
                        "current": room["current_board_version"],
                        "active_task": None,
                        **permissions,
                        **({"membership_id": room["membership_id"]} if room.get("membership_id") else {}),
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
        directory_session, directory = await self._directory(
            [
                reference
                for reference, session, _ in reports
                if session["state"] == "ready"
            ],
            directory_deadline,
        )
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
        rooms = {
            identifier: room
            for identifier, room in rooms.items()
            if room["memberships"]
        }
        directory_available = (
            directory_session is not None and directory_session.id in approved
        )
        if directory_available:
            for entry in directory:
                identifier = entry["room_id"]
                room = rooms.setdefault(
                    identifier,
                    {
                        "id": identifier,
                        "name": entry["name"],
                        "latest_message_id": None,
                        "memberships": [],
                    },
                )
                room["history_policy"] = entry["history_policy"]
        ordered = sorted(
            rooms.values(),
            key=lambda entry: (
                not entry["memberships"],
                -int(entry["latest_message_id"] or 0),
                entry["name"].casefold(),
                entry["id"],
            ),
        )
        for room in ordered:
            message = room["latest_message_id"]
            room["latest_message_id"] = str(message) if message is not None else None
        result = {
            "sessions": sessions,
            "rooms": ordered,
            "directory_available": directory_available,
        }
        if self._lifecycle is not None:
            result["retired_sessions"] = self._lifecycle.console_retired_sessions()
        return result

    async def session_action(
        self, session_id: str, room_id: str | None, membership_id: str | None
    ) -> dict:
        if self._lifecycle is None:
            return {"state": "not_found"}
        try:
            return await self._lifecycle.console_session_action(session_id, room_id, membership_id)
        except ClientError as exc:
            return {"state": exc.code}

    async def retired_history(
        self,
        session_id: str,
        *,
        after: int | None = None,
        kind: str | None = None,
        conversation: str | None = None,
        before: int | None = None,
    ) -> dict:
        if self._lifecycle is None:
            return {"state": "not_found"}
        async with self._reads:
            try:
                return await self._lifecycle.console_retired_history(
                    session_id, after=after, kind=kind, conversation=conversation, before=before
                )
            except ClientError as exc:
                return {"state": exc.code}

    async def _participating(
        self, session: ConsoleSession, member_id: str, room_id: str
    ) -> bool | None:
        after = 0
        try:
            async with asyncio.timeout(TASK_OBSERVATION_SECONDS):
                for _ in range(TASK_OBSERVATION_PAGES):
                    page = await self._read(
                        session,
                        f"/rooms/{room_id}/tasks",
                        {"status": "open", "after": after, "limit": 100},
                    )
                    if any(
                        member_id in task["participant_member_ids"]
                        for task in page["tasks"]
                    ):
                        return True
                    cursor = page["next_after"]
                    if cursor is None:
                        return False
                    if cursor <= after:
                        return None
                    after = cursor
                return None
        except (ClientError, TimeoutError):
            return None

    async def team(self, identity: str) -> dict:
        session = self._current(identity)
        if session is None:
            return {"state": "not_found", "members": []}
        as_of, presence = await self._presence(session, include_uptime=True)
        try:
            result = await self._read(session, "/members")
        except ClientError as exc:
            return {"state": exc.code, "members": []}
        return {
            "state": "ready",
            "presence_as_of": as_of,
            "members": [
                _member_with_presence(member, presence) for member in result["members"]
            ],
        }

    async def _presence(
        self, session: ConsoleSession, *, include_uptime: bool = False
    ) -> tuple[str | None, dict]:
        try:
            async with asyncio.timeout(PRESENCE_OBSERVATION_SECONDS):
                result = await self._read(session, "/presence")
        except (ClientError, TimeoutError):
            return None, {}
        presence = {}
        for member in result["members"]:
            state = member["state"]
            age = member.get("age_ms")
            observed_at = member.get("observed_at")
            if (
                state in ("online", "offline")
                and is_integer(age, 0)
                and is_string(observed_at)
            ):
                observation = {
                    "state": state,
                    "observed_at": observed_at,
                    "age_ms": age,
                }
                if include_uptime and state == "online":
                    since = member.get("online_since")
                    duration = member.get("online_for_ms")
                    if is_string(since) and is_integer(duration, 0):
                        observation.update(online_since=since, online_for_ms=duration)
                presence[member["member_id"]] = observation
        return result["as_of"], presence

    async def _roster_tasks(
        self, session: ConsoleSession, room_path: str
    ) -> list[dict] | None:
        after = 0
        tasks = []
        try:
            async with asyncio.timeout(TASK_OBSERVATION_SECONDS):
                for _ in range(TASK_OBSERVATION_PAGES):
                    page = await self._read(
                        session,
                        f"{room_path}/tasks",
                        {"status": "open", "after": after, "limit": 100},
                    )
                    tasks.extend(page["tasks"])
                    cursor = page["next_after"]
                    if cursor is None:
                        return tasks
                    if cursor <= after:
                        break
                    after = cursor
        except (ClientError, TimeoutError):
            return None
        return None

    async def room_board(
        self, identity: str, room_id: str, draft: dict | None = None
    ) -> dict:
        path = f"/rooms/{room_id}/board"
        try:
            if draft is None:
                session = self._current(identity)
                if session is None:
                    return {"state": "not_found"}
                board = await self._read(session, path + "/view")
            else:
                board = await self._write(identity, path, draft)
        except ClientError as exc:
            return {"state": exc.code}
        return {"state": "ready", "board": board}

    async def remove_room_member(
        self, identity: str, room_id: str, member_id: str, membership_id: str
    ) -> dict:
        try:
            result = await self._write(
                identity, f"/rooms/{room_id}/members/{member_id}/remove",
                {"membership_id": membership_id},
            )
        except ClientError as exc:
            return {"state": exc.code}
        return {"state": "ready", **result}

    async def room_members(self, identity: str, room_id: str) -> dict:
        session = self._current(identity)
        if session is None:
            return {"state": "not_found", "members": []}
        path = f"/rooms/{room_id}"
        try:
            detail = await self._read(session, path)
            (as_of, presence), tasks = await asyncio.gather(
                self._presence(session), self._roster_tasks(session, path)
            )
            # Read membership last so optional observations cannot outlive this read scope.
            members = (await self._read(
                session, f"{path}/members", {"include_membership": "true"}
            ))["members"]
        except ClientError as exc:
            return {"state": exc.code, "members": []}
        by_member = _tasks_by_member(members, tasks) if tasks is not None else {}
        roster = []
        for member in members:
            item = {
                **_member_with_presence(member, presence),
                "acknowledged": member["acked_board_version"],
                "tasks": by_member.get(member["member_id"]),
            }
            if membership_id := member.get("membership_id"):
                item["membership_id"] = membership_id
            roster.append(item)
        return {
            "state": "ready",
            "current": detail["current_board_version"],
            "history_policy": detail["history_policy"],
            "presence_as_of": as_of,
            "members": roster,
        }

    async def room(
        self,
        identity: str,
        room_id: str,
        before: int | None = None,
        *,
        after: int | None = None,
    ) -> dict:
        session = self._current(identity)
        if session is None:
            return {"state": "not_found"}
        try:
            listing = await self._read(session, "/rooms", {"include_membership": "true"})
            rooms = listing["rooms"]
        except ClientError as exc:
            return {"state": exc.code}
        selected = next((room for room in rooms if room["room_id"] == room_id), None)
        if selected is None:
            return {"state": "not_found"}

        board = {
            "current": selected["current_board_version"],
            "acknowledged": selected["acked_board_version"],
        }
        if after is None and before is None:
            latest = selected["latest_message_id"]
            if latest is None:
                after = 0
            else:
                before = min(latest + 1, MAX_BEFORE)
        query = (
            {"after": after, "limit": PAGE}
            if after is not None
            else {"before": before, "limit": PAGE}
        )
        if membership_id := selected.get("membership_id"):
            board["membership_id"] = membership_id
            query["membership_id"] = membership_id
        try:
            history = await self._read(session, f"/rooms/{room_id}/messages", query)
        except ClientError as exc:
            if exc.code in {"not_found", "not_registered", "unauthorized"}:
                return {"state": exc.code}
            return {"state": exc.code, "board": board, "messages": [], "older": None}

        messages = history["messages"]
        members: dict[str, _MessageMember] = {}
        if messages:
            try:
                directory = await self._read(session, "/members")
            except ClientError as exc:
                if self._current(identity) != session:
                    return {"state": "not_found"}
                if exc.code in {"not_found", "not_registered", "unauthorized"}:
                    return {"state": exc.code}
            else:
                members = _message_members(directory["members"])
        result = {
            "state": "ready",
            "board": board,
            "older": str(messages[0]["message_id"])
            if after is None and len(messages) == PAGE
            else None,
            "more_recent": after is not None and len(messages) == PAGE,
            "messages": [_message(item, members) for item in messages],
        }
        if "expired_through_message_id" in history:
            result["expired_through_message_id"] = str(
                history["expired_through_message_id"]
            )
            result["history_truncated"] = history["history_truncated"]
        return result

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
        try:
            result = await self._write(
                identity, f"/rooms/{room_id}/tasks/{task_id}/{action}", body
            )
        except ClientError as exc:
            return {"state": exc.code}
        return {"state": "ready", "task": result}
