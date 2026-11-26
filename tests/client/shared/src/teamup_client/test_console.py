import asyncio
from importlib.metadata import version

import httpx
import pytest
from teamup_client import _console, _console_data
from teamup_client._console import serve_console
from teamup_client._console_data import ConsoleReader, ConsoleSession
from teamup_client._upstream import Upstream as ServerTransport
from teamup_client.ipc import ClientError

ROOM = "247629c4-8e54-4d0e-8b25-7a8c43bf85b2"
DISCOVERABLE_ROOM = "847629c4-8e54-4d0e-8b25-7a8c43bf85b2"
BACKEND = "93e57f02-b2d7-4145-a968-9f0b60319c71"
REVIEWER = "a4a4610b-42b2-45d3-9002-c4ee89fe3427"
REMOTE = "1fb1fae4-88ac-4bb8-aec1-be36cb403825"
OUTSIDER = "bde4ac72-f5d5-4590-9ca0-51dcc075aa40"


class Upstream:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, path, token, *, query=None):
        self.calls.append((method, path, token, query))
        if path == "/members/me":
            if token == "backend-secret":
                return {"member_id": BACKEND, "role_description": "Backend"}
            return {"member_id": REVIEWER, "role_description": "Reviewer"}
        if path == "/members":
            return {"members": [{"member_id": BACKEND, "role_description": "Backend"}]}
        if path == "/rooms":
            return {
                "rooms": [
                    {
                        "room_id": ROOM,
                        "name": "Compatibility",
                        "acked_board_version": 2 if token == "backend-secret" else None,
                        "current_board_version": 2,
                        "latest_message_id": 15,
                    }
                ],
            }
        if path == "/room-directory":
            raise ClientError("not_found", "Directory is unavailable on this Server")
        if path == f"/rooms/{ROOM}/tasks":
            if token == "reviewer-secret":
                raise ClientError("board_ack_required", "Current board needed")
            return {
                "tasks": [{"participant_member_ids": [BACKEND]}],
                "next_after": None,
            }
        if path == f"/rooms/{ROOM}/messages":
            if token == "reviewer-secret":
                raise ClientError(
                    "board_ack_required", "Read and acknowledge the board"
                )
            return {
                "messages": [
                    {
                        "message_id": 15,
                        "room_id": ROOM,
                        "sender_member_id": BACKEND,
                        "client_message_id": "ready-for-review",
                        "text": "Ready for review",
                        "mention_member_id": None,
                        "created_at": "2026-11-25T00:00:00Z",
                    }
                ]
            }
        raise AssertionError(path)


def sessions():
    return (
        ConsoleSession("primary", "codex", "api", "backend-secret"),
        ConsoleSession("review", "claude-code", "review", "reviewer-secret"),
    )


def test_workspace_and_room_reads_keep_session_scope_and_board_ack():
    async def scenario():
        request = Upstream()
        reader = ConsoleReader(sessions, request)
        workspace = await reader.workspace()
        assert [room["id"] for room in workspace["rooms"]] == [ROOM]
        assert workspace["directory_available"] is False
        assert {item["id"] for item in workspace["sessions"]} == {"primary", "review"}
        assert {item["session"] for item in workspace["rooms"][0]["memberships"]} == {
            "primary",
            "review",
        }
        status = {
            item["session"]: item["active_task"]
            for item in workspace["rooms"][0]["memberships"]
        }
        assert status == {"primary": True, "review": None}
        assert "-secret" not in str(workspace)
        room = await reader.room("primary", ROOM)
        assert [message["text"] for message in room["messages"]] == ["Ready for review"]
        assert room["board"] == {"current": 2, "acknowledged": 2}
        assert "expired_through_message_id" not in room
        assert "history_truncated" not in room
        assert (
            "GET",
            f"/rooms/{ROOM}/messages",
            "backend-secret",
            {"before": 16, "limit": 30},
        ) in request.calls
        reviewer = await reader.room("review", ROOM)
        assert reviewer["state"] == "board_ack_required"
        assert reviewer["messages"] == []
        assert all(not path.endswith("/board") for _, path, _, _ in request.calls)
        assert all(path != "/presence" for _, path, _, _ in request.calls)

    asyncio.run(scenario())


def test_workspace_combines_all_directory_pages_with_joined_rooms():
    async def scenario():
        upstream = Upstream()
        cursors = []

        async def request(method, path, token, *, query=None):
            if path == "/room-directory":
                assert token == "backend-secret"
                cursor = query.get("after")
                cursors.append(cursor)
                if cursor is None:
                    return {
                        "rooms": [
                            {
                                "room_id": ROOM,
                                "name": "Compatibility",
                                "history_policy": "since_joining",
                            }
                        ],
                        "next_after": ROOM,
                    }
                assert cursor == ROOM
                return {
                    "rooms": [
                        {
                            "room_id": DISCOVERABLE_ROOM,
                            "name": "Teammate room",
                            "history_policy": "retained",
                        }
                    ],
                    "next_after": None,
                }
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(sessions, request).workspace()
        assert cursors == [None, ROOM]
        assert result["directory_available"] is True
        joined, discoverable = result["rooms"]
        assert joined["id"] == ROOM
        assert [item["session"] for item in joined["memberships"]] == [
            "primary",
            "review",
        ]
        assert discoverable == {
            "id": DISCOVERABLE_ROOM,
            "name": "Teammate room",
            "history_policy": "retained",
            "latest_message_id": None,
            "memberships": [],
        }

    asyncio.run(scenario())


def test_directory_discards_pages_when_its_local_session_is_revoked():
    async def scenario():
        upstream = Upstream()
        approved = list(sessions())[:1]

        async def request(method, path, token, *, query=None):
            if path == "/room-directory":
                approved.clear()
                return {
                    "rooms": [
                        {"room_id": ROOM, "name": "Build", "history_policy": "retained"}
                    ],
                    "next_after": None,
                }
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(lambda: tuple(approved), request).workspace()
        assert result == {"sessions": [], "rooms": [], "directory_available": False}

    asyncio.run(scenario())


def test_slow_directory_keeps_joined_rooms_within_the_workspace_deadline(monkeypatch):
    monkeypatch.setattr(_console, "CONSOLE_REQUEST_SECONDS", 1)
    monkeypatch.setattr(_console, "WORKSPACE_RESPONSE_RESERVE_SECONDS", 0.25)

    async def scenario():
        upstream = Upstream()
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(method, path, token, *, query=None):
            if path == "/rooms":
                await asyncio.sleep(0.3)
            if path == "/room-directory":
                started.set()
                await release.wait()
            return await upstream(method, path, token, query=query)

        async with serve_console(ConsoleReader(sessions, request)) as url:
            origin, token = url.split("#")
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                try:
                    response = await client.get(
                        origin + "api/workspace",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                finally:
                    release.set()
        assert started.is_set()
        assert response.status_code == 200
        workspace = response.json()
        assert workspace["directory_available"] is False
        assert [room["id"] for room in workspace["rooms"]] == [ROOM]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("target", "members", "sender", "mentioned"),
    [
        (None, [], "93e57f02", None),
        (REVIEWER, [(BACKEND, "Backend")], "Backend", "a4a4610b"),
        (
            REVIEWER,
            [(BACKEND, "Engineer"), (REVIEWER, "Engineer")],
            "Engineer (93e57f02)",
            "Engineer (a4a4610b)",
        ),
        (
            REVIEWER,
            [(BACKEND, "Backend"), (REVIEWER, "Reviewer")],
            "Backend",
            "Reviewer",
        ),
    ],
)
def test_room_projects_structured_mentions_with_member_labels(
    target, members, sender, mentioned
):
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path == "/members":
                return {
                    "members": [
                        {"member_id": identifier, "role_description": role}
                        for identifier, role in members
                    ]
                }
            result = await upstream(method, path, token, query=query)
            if path.endswith("/messages"):
                result["messages"][0].update(
                    text="Literal @Reviewer", mention_member_id=target
                )
            return result

        result = await ConsoleReader(sessions, request).room("primary", ROOM)
        assert result["messages"][0] == {
            "id": "15",
            "sender": sender,
            "text": "Literal @Reviewer",
            "created_at": "2026-11-25T00:00:00Z",
            "mentioned": mentioned,
            "sender_member_id": BACKEND,
            "recipient_member_ids": [target] if target else [],
        }

    asyncio.run(scenario())


def test_room_projects_all_structured_recipients_in_server_order():
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path == "/members":
                return {
                    "members": [
                        {"member_id": BACKEND, "role_description": "Backend"},
                        {"member_id": REVIEWER, "role_description": "Reviewer"},
                    ]
                }
            response = await upstream(method, path, token, query=query)
            if path.endswith("/messages"):
                response["messages"][0].update(
                    mention_member_id=REVIEWER,
                    mention_member_ids=[REVIEWER, REMOTE, OUTSIDER],
                )
            return response

        result = await ConsoleReader(sessions, request).room("primary", ROOM)
        message = result["messages"][0]
        assert message["sender_member_id"] == BACKEND
        assert message["recipient_member_ids"] == [REVIEWER, REMOTE, OUTSIDER]
        assert message["mentioned"] == "Reviewer"
        assert message["mentioned_recipients"] == ["Reviewer", "1fb1fae4", "bde4ac72"]

    asyncio.run(scenario())


def test_room_projects_compact_routing_and_retains_full_legacy_labels():
    async def scenario():
        upstream = Upstream()
        sender = "Coordinator: PR review support"
        first_recipient = "Release Lead：AS decisions and co-signs"
        structured = "API: Owner"
        extra_recipient = "Coordinator: Release rollbacks"

        async def request(method, path, token, *, query=None):
            if path == "/members":
                return {
                    "members": [
                        {"member_id": BACKEND, "role_description": sender},
                        {"member_id": REVIEWER, "role_description": first_recipient},
                        {"member_id": REMOTE, "role_description": structured, "name": structured},
                        {"member_id": OUTSIDER, "role_description": extra_recipient},
                    ]
                }
            response = await upstream(method, path, token, query=query)
            if path.endswith("/messages"):
                response["messages"][0].update(
                    text="Check #858",
                    mention_member_id=REVIEWER,
                    mention_member_ids=[REVIEWER, REMOTE, OUTSIDER],
                )
            return response

        result = await ConsoleReader(sessions, request).room("primary", ROOM)
        message = result["messages"][0]
        assert message["sender"] == sender
        assert message["sender_member_id"] == BACKEND
        assert message["mentioned"] == first_recipient
        assert message["mentioned_recipients"] == [first_recipient, structured, extra_recipient]
        assert message["recipient_member_ids"] == [REVIEWER, REMOTE, OUTSIDER]
        assert message["text"] == "Check #858"
        assert message["routing"] == {
            "sender": "Coordinator (93e57f02)",
            "recipients": ["Release Lead", structured],
            "legacy_titles": ["PR review support", "AS decisions and co-signs"],
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("label", ["https://example.test", "Shift 09:30", ": Empty name", "Lead: "])
def test_room_leaves_ambiguous_legacy_sender_labels_intact(label):
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path == "/members":
                return {"members": [{"member_id": BACKEND, "role_description": label}]}
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(sessions, request).room("primary", ROOM)
        message = result["messages"][0]
        assert message["sender"] == label
        assert "routing" not in message

    asyncio.run(scenario())


def test_room_labels_messages_from_retired_senders():
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path == "/members":
                return {"members": []}
            response = await upstream(method, path, token, query=query)
            if path.endswith("/messages"):
                response["messages"][0].update(
                    sender_member_id=REMOTE, sender_retired=True
                )
            return response

        result = await ConsoleReader(sessions, request).room("primary", ROOM)
        message = result["messages"][0]
        assert message["sender"] == f"Retired member ({REMOTE[:8]})"
        assert message["sender_member_id"] == REMOTE

    asyncio.run(scenario())


@pytest.mark.parametrize("truncated", [True, False])
def test_empty_room_page_keeps_server_retention_boundary(truncated):
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path.endswith("/messages"):
                return {
                    "messages": [],
                    "expired_through_message_id": 2**53 + 1,
                    "history_truncated": truncated,
                }
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(sessions, request).room("primary", ROOM)
        assert result["messages"] == []
        assert result["expired_through_message_id"] == "9007199254740993"
        assert result["history_truncated"] is truncated

    asyncio.run(scenario())


class RosterUpstream(Upstream):
    as_of = "2026-11-25T00:00:10Z"
    observed_at = "2026-11-25T00:00:00Z"

    async def __call__(self, method, path, token, *, query=None):
        roles = {
            REMOTE: "Remote",
            BACKEND: "Backend",
            REVIEWER: "Reviewer",
            OUTSIDER: "Outside the room",
        }
        if path not in {
            f"/rooms/{ROOM}",
            f"/rooms/{ROOM}/members",
            f"/rooms/{ROOM}/tasks",
            "/members",
            "/presence",
        }:
            return await super().__call__(method, path, token, query=query)
        self.calls.append((method, path, token, query))
        if path == f"/rooms/{ROOM}":
            return {
                "room_id": ROOM,
                "name": "Compatibility",
                "history_policy": "retained",
                "current_board_version": 2,
                "member_count": 3,
            }
        if path == f"/rooms/{ROOM}/members":
            return {
                "members": [
                    {
                        "member_id": member,
                        "role_description": roles[member],
                        "acked_board_version": None if member == REVIEWER else 2,
                    }
                    for member in (REMOTE, BACKEND, REVIEWER)
                ]
            }
        if path == "/members":
            return {
                "members": [
                    {"member_id": member, "role_description": role}
                    for member, role in roles.items()
                ]
            }
        if path == "/presence":
            return {
                "as_of": self.as_of,
                "members": [
                    {
                        "member_id": REMOTE,
                        "state": "offline",
                        "observed_at": "2026-11-24T23:59:00Z",
                        "age_ms": 70_000,
                    },
                    {
                        "member_id": BACKEND,
                        "state": "online",
                        "observed_at": self.observed_at,
                        "age_ms": 700,
                    },
                ],
            }
        return await super().__call__(method, path, token, query=query)


def test_room_members_route_keeps_scope_and_projects_complete_active_task_pages():
    async def scenario():
        upstream = RosterUpstream()
        task_ids = (
            "ab881a6d-f39e-4070-bdc0-99f5426cddd1",
            "ee358086-24dd-4748-af54-bad669e85c26",
        )

        async def request(method, path, token, *, query=None):
            assert method == "GET" and token == "backend-secret"
            if path.endswith("/tasks"):
                assert query == {
                    "status": "open",
                    "after": query["after"],
                    "limit": 100,
                }
                assert query["after"] in (0, 1)
                task_number = query["after"] + 1
                return {
                    "tasks": [
                        {
                            "task_id": task_ids[task_number - 1],
                            "title": f"Task {task_number}",
                            "participant_member_ids": [BACKEND, REMOTE],
                        }
                    ],
                    "next_after": 1 if task_number == 1 else None,
                }
            return await upstream(method, path, token, query=query)

        async with serve_console(ConsoleReader(sessions, request)) as url:
            origin, token = url.split("#")
            async with httpx.AsyncClient(trust_env=False) as client:
                endpoint = origin + "api/room-members"
                params = {"session": "primary", "room": ROOM}
                assert (await client.get(endpoint, params=params)).status_code == 401
                headers = {"Authorization": "Bearer " + token}
                assert (
                    await client.get(
                        endpoint, headers=headers, params={**params, "after": 0}
                    )
                ).status_code == 400
                response = await client.get(endpoint, headers=headers, params=params)

        assert response.status_code == 200
        assert "-secret" not in response.text
        data = response.json()
        assert data["state"] == "ready"
        assert data["history_policy"] == "retained"
        assert data["current"] == 2
        assert data["presence_as_of"] == upstream.as_of
        assert [member["id"] for member in data["members"]] == [
            REMOTE,
            BACKEND,
            REVIEWER,
        ]
        remote, backend, reviewer = data["members"]
        assert remote["presence"]["state"] == "offline"
        assert remote["presence"]["age_ms"] == 70_000
        assert backend == {
            "id": BACKEND,
            "role": "Backend",
            "acknowledged": 2,
            "presence": {
                "state": "online",
                "observed_at": upstream.observed_at,
                "age_ms": 700,
            },
            "tasks": [
                {
                    "id": identifier,
                    "title": f"Task {number}",
                    "teammates": [{"id": REMOTE, "role": "Remote"}],
                }
                for number, identifier in enumerate(task_ids, 1)
            ],
        }
        assert reviewer["acknowledged"] is None and reviewer["tasks"] == []
        assert reviewer["presence"] == {
            "state": "unknown",
            "observed_at": None,
            "age_ms": None,
        }
        assert upstream.calls[-1][1] == f"/rooms/{ROOM}/members"
        assert all(not path.endswith("/board") for _, path, _, _ in upstream.calls)

    asyncio.run(scenario())


def test_member_profiles_project_owner_and_contextual_description():
    async def scenario():
        upstream = RosterUpstream()
        profile = {
            "name": "Backend",
            "description": "Maintains the release pipeline.",
            "owner_username": "alex",
        }
        override = "Coordinates this release's dependency upgrades."

        async def request(method, path, token, *, query=None):
            if path.endswith("/tasks"):
                return {"tasks": [], "next_after": None}
            result = await upstream(method, path, token, query=query)
            if path == "/members/me" and result["member_id"] == BACKEND:
                return result | profile
            if path == "/members" or path.endswith("/members"):
                for member in result["members"]:
                    if member["member_id"] == BACKEND:
                        member.update(profile)
                        if path != "/members":
                            member["description"] = override
            return result

        reader = ConsoleReader(sessions, request)
        workspace = await reader.workspace()
        local = next(item for item in workspace["sessions"] if item["id"] == "primary")
        assert local["label"] == profile["name"]
        assert {field: local[field] for field in profile} == profile

        team = await reader.team("primary")
        teammate = next(member for member in team["members"] if member["id"] == BACKEND)
        assert {field: teammate[field] for field in profile} == profile
        legacy = next(member for member in team["members"] if member["id"] == OUTSIDER)
        assert legacy["role"] == "Outside the room"
        assert not legacy.keys() & profile.keys()

        roster = await reader.room_members("primary", ROOM)
        room_member = next(member for member in roster["members"] if member["id"] == BACKEND)
        assert {field: room_member[field] for field in profile} == profile | {
            "description": override,
        }

    asyncio.run(scenario())


def test_team_uses_same_scoped_presence_for_local_remote_and_other_members():
    async def scenario():
        upstream = RosterUpstream()
        result = await ConsoleReader(sessions, upstream).team("primary")
        assert result["presence_as_of"] == upstream.as_of
        assert {
            item["id"]: item["presence"]["state"] for item in result["members"]
        } == {
            BACKEND: "online",
            REMOTE: "offline",
            REVIEWER: "unknown",
            OUTSIDER: "unknown",
        }
        assert {token for _, _, token, _ in upstream.calls} == {"backend-secret"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"age_ms": None},
        {"age_ms": -1},
        {"age_ms": "7"},
        {"age_ms": 1.5},
        {"age_ms": True},
    ],
)
def test_team_projects_unknown_for_an_online_observation_without_valid_age(metadata):
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path == "/presence":
                return {
                    "as_of": RosterUpstream.as_of,
                    "members": [
                        {
                            "member_id": BACKEND,
                            "state": "online",
                            "observed_at": RosterUpstream.observed_at,
                            **metadata,
                        }
                    ],
                }
            return await upstream(method, path, token, query=query)

        team = await ConsoleReader(sessions, request).team("primary")
        assert team["state"] == "ready"
        assert team["members"][0]["presence"] == {
            "state": "unknown",
            "observed_at": None,
            "age_ms": None,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["not_found", "unavailable"])
def test_older_server_without_metadata_preserves_team_and_transcript_reads(failure):
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path in {"/presence", f"/rooms/{ROOM}", f"/rooms/{ROOM}/members"}:
                raise ClientError(failure, "Unsupported route")
            return await upstream(method, path, token, query=query)

        reader = ConsoleReader(sessions, request)
        assert await reader.room_members("primary", ROOM) == {
            "state": failure,
            "members": [],
        }
        team = await reader.team("primary")
        assert team["state"] == "ready"
        assert team["members"][0]["presence"] == {
            "state": "unknown",
            "observed_at": None,
            "age_ms": None,
        }
        transcript = await reader.room("primary", ROOM)
        assert transcript["messages"][0]["sender"] == "Backend"

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", [True, False])
def test_roster_keeps_pending_or_incomplete_task_details_unavailable(blocked):
    async def scenario():
        upstream = RosterUpstream()
        afters = []

        async def request(method, path, token, *, query=None):
            if path.endswith("/tasks"):
                afters.append(query["after"])
                if blocked:
                    raise ClientError("board_ack_required", "Current board needed")
                return {"tasks": [], "next_after": query["after"] + 1}
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(sessions, request).room_members("review", ROOM)
        assert result["state"] == "ready"
        assert all(member["tasks"] is None for member in result["members"])
        assert afters == ([0] if blocked else [0, 1])
        assert {token for _, _, token, _ in upstream.calls} == {"reviewer-secret"}

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["team", "roster"])
@pytest.mark.parametrize("local", [True, False])
def test_member_reads_discard_enrichment_if_access_revoked_mid_read(route, local):
    async def scenario():
        active = list(sessions())
        upstream = RosterUpstream()
        revoked = False

        async def request(method, path, token, *, query=None):
            nonlocal revoked
            if path == "/presence":
                revoked = True
                if local:
                    active.clear()
            if revoked and path in {"/members", f"/rooms/{ROOM}/members"}:
                raise ClientError(
                    "not_found" if route == "roster" else "not_registered", "Revoked"
                )
            return await upstream(method, path, token, query=query)

        reader = ConsoleReader(lambda: tuple(active), request)
        result = await (
            reader.team("primary")
            if route == "team"
            else reader.room_members("primary", ROOM)
        )
        assert revoked
        assert result == {
            "state": "not_found" if local or route == "roster" else "not_registered",
            "members": [],
        }

    asyncio.run(scenario())


def test_in_flight_history_is_discarded_when_local_approval_is_removed():
    async def scenario():
        active = list(sessions())
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path.endswith("/messages"):
                active.clear()
            return await upstream(method, path, token, query=query)

        result = await ConsoleReader(lambda: tuple(active), request).room(
            "primary", ROOM
        )
        assert result == {"state": "not_found"}

    asyncio.run(scenario())


def test_console_versions_follow_the_configured_server_without_member_credentials():
    async def scenario():
        replies = iter(
            [
                httpx.Response(200, json={"version": "0.0.1"}),
                httpx.Response(404),
                httpx.ConnectError("Server restarting"),
                httpx.Response(200, json={"version": "0.0.2.dev3"}),
                httpx.Response(200, json={"version": None}),
            ]
        )

        def upstream(request):
            assert request.method == "GET"
            assert str(request.url) == "https://teamup.example.test/v1/version"
            assert "authorization" not in request.headers
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        installed = version("teamup-client")
        async with (
            httpx.AsyncClient(
                base_url="https://teamup.example.test",
                transport=httpx.MockTransport(upstream),
            ) as server,
            serve_console(
                ConsoleReader(lambda: (), ServerTransport(server).request)
            ) as url,
            httpx.AsyncClient(trust_env=False) as client,
        ):
            origin, token = url.split("#")
            endpoint = origin + "api/versions"
            auth = {"Authorization": "Bearer " + token}
            assert (await client.get(endpoint)).status_code == 401
            assert (
                await client.get(endpoint + "?server=other", headers=auth)
            ).status_code == 400
            assert (
                await client.get(
                    endpoint, headers={**auth, "Origin": "http://other.test"}
                )
            ).status_code == 403
            for server_version, state in (
                ("0.0.1", "ready"),
                (None, "unknown"),
                (None, "unavailable"),
                ("0.0.2.dev3", "ready"),
                (None, "unknown"),
            ):
                snapshot = await client.get(endpoint, headers=auth)
                assert snapshot.status_code == 200
                assert snapshot.headers["cache-control"] == "no-store"
                assert snapshot.json() == {
                    "client_version": None
                    if installed == "0.0.0+unknown"
                    else installed,
                    "server_version": server_version,
                    "server_state": state,
                }

    asyncio.run(scenario())


def test_loopback_listener_requires_its_credential_and_serves_bundled_assets():
    async def scenario():
        async with serve_console(ConsoleReader(sessions, Upstream())) as url:
            origin, token = url.split("#")
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(), trust_env=False
            ) as client:
                home = await client.get(origin)
                assert home.status_code == 200
                assert "TeamUp" in home.text
                assert token not in home.text
                assert (await client.get(origin + "console.js")).status_code == 200
                for asset in (
                    "console-members.js",
                    "console-history.mjs",
                    "console-sessions.mjs",
                ):
                    script = await client.get(origin + asset)
                    assert script.status_code == 200
                    assert (
                        script.headers["content-type"]
                        == "text/javascript; charset=utf-8"
                    )
                logo = await client.get(origin + "jet-pair.png")
                assert logo.headers["content-type"] == "image/png"
                assert logo.content.startswith(b"\x89PNG\r\n\x1a\n")
                assert (await client.get(origin + "api/workspace")).status_code == 401
                auth = {"Authorization": "Bearer " + token}
                snapshot = await client.get(origin + "api/workspace", headers=auth)
                assert snapshot.status_code == 200
                assert snapshot.json()["rooms"][0]["id"] == ROOM
                assert snapshot.headers["cache-control"] == "no-store"
                bad_host = await client.get(
                    origin + "api/workspace", headers={**auth, "Host": "other.test"}
                )
                bad_origin = await client.get(
                    origin + "api/workspace",
                    headers={**auth, "Origin": "http://other.test"},
                )
                assert bad_host.status_code == bad_origin.status_code == 403
                assert (
                    await client.get(origin + "api/room", headers=auth)
                ).status_code == 400

    asyncio.run(scenario())


def test_task_actions_require_same_origin_and_keep_session_scope():
    task = "2f47cb81-36dc-42eb-8736-8f51a303455a"

    async def scenario():
        requests = []

        async def upstream(method, path, token, *, query=None, body=None):
            requests.append((method, path, token, query, body))
            if method == "POST" and token == "reviewer-secret":
                raise ClientError("board_ack_required", "Current board needed")
            if method == "GET":
                return {
                    "tasks": [{"task_id": task, "task_number": 1, "version": 1}],
                    "next_after": None,
                }
            return {"task_id": task, "task_number": 1, "status": "open"}

        async with serve_console(ConsoleReader(sessions, upstream)) as url:
            origin, token = url.split("#")
            async with httpx.AsyncClient(trust_env=False) as client:
                auth = {"Authorization": "Bearer " + token}
                endpoint = origin + f"api/tasks/{task}/join"
                params = {"session": "review", "room": ROOM}
                rejected = await client.post(endpoint, headers=auth, params=params)
                assert rejected.status_code == 403
                assert requests == []
                auth["Origin"] = origin.rstrip("/")
                rejected = await client.post(endpoint, headers=auth, params=params)
                assert rejected.status_code == 409
                assert rejected.json()["state"] == "board_ack_required"
                assert requests[-1][2] == "reviewer-secret"
                params["session"] = "primary"
                taken = await client.post(endpoint, headers=auth, params=params)
                assert (
                    taken.status_code == 200 and taken.json()["task"]["task_id"] == task
                )
                assert requests[-1] == (
                    "POST",
                    f"/rooms/{ROOM}/tasks/{task}/join",
                    "backend-secret",
                    None,
                    None,
                )
                page = await client.get(
                    origin + "api/tasks",
                    headers=auth,
                    params={"room": ROOM, "status": "open"},
                )
                assert page.json()["tasks"] == [
                    {
                        "task_id": task,
                        "task_number": "1",
                        "version": 1,
                        "visible_to": ["primary", "review"],
                    }
                ]
                assert "-secret" not in page.text

    asyncio.run(scenario())


def test_slow_task_observation_keeps_the_workspace_available(monkeypatch):
    monkeypatch.setattr(_console_data, "TASK_OBSERVATION_SECONDS", 0.02)

    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path.endswith("/tasks"):
                await asyncio.Event().wait()
            return await upstream(method, path, token, query=query)

        result = await asyncio.wait_for(ConsoleReader(sessions, request).workspace(), 1)
        assert [room["id"] for room in result["rooms"]] == [ROOM]
        assert all(
            membership["active_task"] is None
            for membership in result["rooms"][0]["memberships"]
        )

    asyncio.run(scenario())


def test_console_catches_up_using_the_last_displayed_message():
    async def scenario():
        upstream = Upstream()
        async with serve_console(ConsoleReader(sessions, upstream)) as url:
            origin, token = url.split("#")
            headers = {"Authorization": "Bearer " + token}
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    origin + "api/room",
                    headers=headers,
                    params={"room": ROOM, "session": "primary", "after": "10"},
                )
                assert response.status_code == 200
                assert response.json()["messages"][0]["id"] == "15"
                assert (
                    "GET",
                    f"/rooms/{ROOM}/messages",
                    "backend-secret",
                    {"after": 10, "limit": 30},
                ) in upstream.calls
                assert response.json()["more_recent"] is False

    asyncio.run(scenario())


def test_upstream_oserror_only_marks_the_affected_session_unavailable():
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if token == "reviewer-secret":
                raise ConnectionResetError("connection closed")
            return await upstream(method, path, token, query=query)

        data = await ConsoleReader(sessions, request).workspace()
        assert {item["id"]: item["state"] for item in data["sessions"]} == {
            "primary": "ready",
            "review": "unavailable",
        }
        assert [room["id"] for room in data["rooms"]] == [ROOM]

    asyncio.run(scenario())


def test_empty_room_still_uses_the_server_board_gate():
    async def scenario():
        upstream = Upstream()

        async def request(method, path, token, *, query=None):
            if path == "/rooms":
                response = await upstream(method, path, token, query=query)
                response["rooms"][0]["latest_message_id"] = None
                return response
            if path.endswith("/messages"):
                assert query == {"after": 0, "limit": 30}
                if token == "reviewer-secret":
                    raise ClientError("board_ack_required", "Current board needed")
                return {"messages": []}
            return await upstream(method, path, token, query=query)

        console = ConsoleReader(sessions, request)
        assert (await console.room("review", ROOM))["state"] == "board_ack_required"
        empty = await console.room("primary", ROOM)
        assert empty["state"] == "ready" and empty["messages"] == []

    asyncio.run(scenario())


def test_incomplete_task_observation_bounds_upstream_pages():
    async def scenario():
        upstream = Upstream()
        cursors = []

        async def request(method, path, token, *, query=None):
            if path.endswith("/tasks"):
                cursors.append(query["after"])
                return {
                    "tasks": [{"participant_member_ids": []}],
                    "next_after": query["after"] + 1 if query["after"] < 5 else None,
                }
            return await upstream(method, path, token, query=query)

        data = await ConsoleReader(sessions, request).workspace()
        assert cursors == [0, 1]
        assert all(
            membership["active_task"] is None
            for membership in data["rooms"][0]["memberships"]
        )

    asyncio.run(scenario())
