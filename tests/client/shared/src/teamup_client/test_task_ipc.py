import asyncio
from types import SimpleNamespace

import pytest
from teamup_client._config import Binding
from teamup_client._coordinator import Coordinator
from teamup_client.ipc import ClientError

ROOM = "00000000-0000-4000-8000-000000000003"
TASK = "00000000-0000-4000-8000-000000000004"
MEMBERS = [
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
]


class Notices:
    def retired_sessions(self, _server_url):
        return {}

    def sessions(self):
        return []


class Upstream:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.failures = {}

    async def request(
        self, verb, path, token=None, *, query=None, body=None, member_auth=True
    ):
        self.calls.append((verb, path, token, query, body))
        if path in self.failures:
            raise self.failures[path]
        return self.results.get((verb, path), {"ok": True})


def coordinator():
    bindings = (
        Binding("key-a", "token-a", "harness", "session-a"),
        Binding("key-b", "token-b", "harness", "session-b"),
    )
    config = SimpleNamespace(
        server_url="http://127.0.0.1",
        account_token=None,
        enrollment_key=None,
        bindings=bindings,
        installs=(),
    )
    upstream = Upstream()
    return (
        Coordinator(config, upstream, Notices(), None, None, {}),
        upstream,
    )


def test_task_routes_payloads_bindings_and_results():
    async def scenario():
        client, upstream = coordinator()
        task = {"task_id": TASK, "status": "open"}
        page = {"tasks": [task], "next_after": 7}
        history = {"events": [], "next_after": None}

        upstream.results[("POST", f"/rooms/{ROOM}/tasks")] = task
        assert (
            await client.execute(
                "key-a",
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "task-1",
                    "title": "Check API mapping",
                    "description": "Map every field",
                    "addressed_member_ids": [MEMBERS[1]],
                },
            )
            == task
        )
        assert upstream.calls[-1] == (
            "POST",
            f"/rooms/{ROOM}/tasks",
            "token-a",
            None,
            {
                "client_task_id": "task-1",
                "title": "Check API mapping",
                "description": "Map every field",
                "addressed_member_ids": [MEMBERS[1]],
            },
        )

        await client.execute(
            "key-a",
            "create_task",
            {
                "room_id": ROOM,
                "client_task_id": "task-2",
                "title": "Unassigned",
                "description": None,
            },
        )
        assert upstream.calls[-1][4] == {
            "client_task_id": "task-2",
            "title": "Unassigned",
            "description": None,
        }

        upstream.results[("GET", f"/rooms/{ROOM}/tasks")] = page
        assert (
            await client.execute(
                "key-b",
                "list_tasks",
                {"room_id": ROOM, "status": "open", "after": 2, "limit": 10},
            )
            == page
        )
        assert upstream.calls[-1] == (
            "GET",
            f"/rooms/{ROOM}/tasks",
            "token-b",
            {"status": "open", "after": 2, "limit": 10},
            None,
        )
        await client.execute("key-a", "list_tasks", {"room_id": ROOM})
        assert upstream.calls[-1] == (
            "GET",
            f"/rooms/{ROOM}/tasks",
            "token-a",
            {},
            None,
        )

        upstream.results[("GET", f"/rooms/{ROOM}/tasks/{TASK}")] = task
        assert (
            await client.execute(
                "key-a", "get_task", {"room_id": ROOM, "task_id": TASK}
            )
            == task
        )
        assert upstream.calls[-1] == (
            "GET",
            f"/rooms/{ROOM}/tasks/{TASK}",
            "token-a",
            {},
            None,
        )

        upstream.results[("POST", f"/rooms/{ROOM}/tasks/{TASK}/join")] = task
        await client.execute(
            "key-a", "join_task", {"room_id": ROOM, "task_id": TASK}
        )
        assert upstream.calls[-1] == (
            "POST",
            f"/rooms/{ROOM}/tasks/{TASK}/join",
            "token-a",
            None,
            None,
        )

        upstream.results[("POST", f"/rooms/{ROOM}/tasks/{TASK}/progress")] = task
        await client.execute(
            "key-b",
            "report_progress",
            {
                "room_id": ROOM,
                "task_id": TASK,
                "client_progress_id": "progress-1",
                "note": "Fixture passes",
            },
        )
        assert upstream.calls[-1] == (
            "POST",
            f"/rooms/{ROOM}/tasks/{TASK}/progress",
            "token-b",
            None,
            {"client_progress_id": "progress-1", "note": "Fixture passes"},
        )

        upstream.results[("POST", f"/rooms/{ROOM}/tasks/{TASK}/finish")] = task
        await client.execute(
            "key-a", "finish_task", {"room_id": ROOM, "task_id": TASK}
        )
        assert upstream.calls[-1] == (
            "POST",
            f"/rooms/{ROOM}/tasks/{TASK}/finish",
            "token-a",
            None,
            None,
        )

        upstream.results[("GET", f"/rooms/{ROOM}/tasks/{TASK}/events")] = history
        assert (
            await client.execute(
                "key-b",
                "task_history",
                {"room_id": ROOM, "task_id": TASK, "after": 4, "limit": 25},
            )
            == history
        )
        assert upstream.calls[-1] == (
            "GET",
            f"/rooms/{ROOM}/tasks/{TASK}/events",
            "token-b",
            {"after": 4, "limit": 25},
            None,
        )

    asyncio.run(scenario())


def test_task_parameters_are_rejected_locally():
    async def scenario():
        client, upstream = coordinator()
        common = {"room_id": ROOM, "task_id": TASK}
        too_many = [
            f"00000000-0000-4000-8000-{number:012d}" for number in range(101)
        ]
        cases = [
            ("create_task", {"room_id": ROOM, "title": "Task"}),
            ("create_task", {"room_id": "room", "client_task_id": "t", "title": "T"}),
            (
                "create_task",
                {"room_id": ROOM, "client_task_id": "t", "title": "  "},
            ),
            (
                "create_task",
                {"room_id": ROOM, "client_task_id": "t", "title": "x" * 201},
            ),
            (
                "create_task",
                {"room_id": ROOM, "client_task_id": "", "title": "T"},
            ),
            (
                "create_task",
                {"room_id": ROOM, "client_task_id": "x" * 129, "title": "T"},
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "description": " ",
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "description": "x" * 8_001,
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "description": 1,
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "addressed_member_ids": ["member"],
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "addressed_member_ids": [MEMBERS[0], MEMBERS[0]],
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "addressed_member_ids": too_many,
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "addressed_member_ids": MEMBERS[0],
                },
            ),
            (
                "create_task",
                {
                    "room_id": ROOM,
                    "client_task_id": "t",
                    "title": "T",
                    "addressed_member_ids": None,
                },
            ),
            ("list_tasks", {"room_id": ROOM, "status": "taken"}),
            ("list_tasks", {"room_id": ROOM, "after": True}),
            ("list_tasks", {"room_id": ROOM, "after": -1}),
            ("list_tasks", {"room_id": ROOM, "limit": 0}),
            ("list_tasks", {"room_id": ROOM, "limit": 101}),
            ("list_tasks", {"room_id": ROOM, "limit": "5"}),
            ("get_task", {"room_id": ROOM}),
            ("get_task", {"room_id": ROOM, "task_id": "task"}),
            ("join_task", {**common, "unexpected": True}),
            ("finish_task", {"room_id": ROOM}),
            (
                "report_progress",
                {
                    **common,
                    "client_progress_id": "progress-1",
                    "note": " ",
                },
            ),
            (
                "report_progress",
                {
                    **common,
                    "client_progress_id": "progress-1",
                    "note": "x" * 8_001,
                },
            ),
            (
                "report_progress",
                {**common, "client_progress_id": "", "note": "Progress"},
            ),
            (
                "report_progress",
                {
                    **common,
                    "client_progress_id": "x" * 129,
                    "note": "Progress",
                },
            ),
            ("report_progress", {**common, "note": "Progress"}),
            ("task_history", {**common, "before": 1}),
            ("task_history", {**common, "limit": 101}),
            ("create_task_x", common),
        ]
        for method, params in cases:
            before = len(upstream.calls)
            with pytest.raises(ClientError) as error:
                await client.execute("key-a", method, params)
            assert error.value.code == "invalid_request"
            assert len(upstream.calls) == before

        for params in (
            {
                "room_id": ROOM,
                "client_task_id": "task-1",
                "title": "Task",
                "description": None,
            },
            {
                "room_id": ROOM,
                "client_task_id": "task-2",
                "title": "Task",
                "addressed_member_ids": [],
            },
            {"room_id": ROOM, "client_task_id": "task-3", "title": "Task"},
        ):
            await client.execute("key-a", "create_task", params)

    asyncio.run(scenario())


def test_upstream_codes_are_returned_unchanged():
    async def scenario():
        client, upstream = coordinator()
        for method, params, path, code in [
            (
                "list_tasks",
                {"room_id": ROOM},
                f"/rooms/{ROOM}/tasks",
                "board_ack_required",
            ),
            (
                "get_task",
                {"room_id": ROOM, "task_id": TASK},
                f"/rooms/{ROOM}/tasks/{TASK}",
                "not_found",
            ),
            (
                "finish_task",
                {"room_id": ROOM, "task_id": TASK},
                f"/rooms/{ROOM}/tasks/{TASK}/finish",
                "conflict",
            ),
        ]:
            upstream.failures[path] = ClientError(code, "Server result")
            with pytest.raises(ClientError) as error:
                await client.execute("key-a", method, params)
            assert error.value.code == code
            assert error.value.message == "Server result"

    asyncio.run(scenario())


def test_mcp_task_schemas_and_guidance():
    from teamup_mcp.main import TOOLS, create_mcp

    tools = {entry.name: entry.inputSchema for entry in TOOLS}
    assert {
        "teamup_create_task",
        "teamup_list_tasks",
        "teamup_get_task",
        "teamup_join_task",
        "teamup_report_progress",
        "teamup_finish_task",
        "teamup_task_history",
    } <= tools.keys()

    create = tools["teamup_create_task"]
    assert create["required"] == ["room_id", "client_task_id", "title"]
    assert create["additionalProperties"] is False
    assert set(create["properties"]) == {
        "room_id",
        "client_task_id",
        "title",
        "description",
        "addressed_member_ids",
        "assignment",
    }
    assignment = create["properties"]["assignment"]
    assert assignment["required"] == ["closing_criteria"]
    assert assignment["additionalProperties"] is False
    assert create["properties"]["description"]["type"] == ["string", "null"]
    assert create["properties"]["description"]["maxLength"] == 8_000
    addressed = create["properties"]["addressed_member_ids"]
    assert addressed["maxItems"] == 100
    assert addressed["uniqueItems"] is True
    assert addressed["items"]["format"] == "uuid"

    listed = tools["teamup_list_tasks"]
    assert listed["required"] == ["room_id"]
    assert listed["properties"]["status"]["enum"] == ["open", "finished"]
    assert listed["properties"]["after"]["default"] == 0
    assert listed["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 50,
    }

    for name in ("teamup_get_task", "teamup_join_task", "teamup_finish_task"):
        assert tools[name]["required"] == ["room_id", "task_id"]
        assert set(tools[name]["properties"]) == {"room_id", "task_id"}

    progress = tools["teamup_report_progress"]
    assert progress["required"] == [
        "room_id",
        "task_id",
        "client_progress_id",
        "note",
    ]
    assert progress["properties"]["client_progress_id"]["maxLength"] == 128
    assert progress["properties"]["note"]["maxLength"] == 8_000

    history = tools["teamup_task_history"]
    assert history["required"] == ["room_id", "task_id"]
    assert set(history["properties"]) == {"room_id", "task_id", "after", "limit"}

    instructions = create_mcp("/tmp/teamup-test.sock", "key").instructions
    assert "never invent task tools" not in instructions
    for text in ("teamup_create_task", "client_task_id", "teamup_join_task"):
        assert text in instructions


def test_assignment_metadata_passes_through_ipc():
    async def scenario():
        client, upstream = coordinator()
        assignment = {"closing_criteria": "Checks pass", "eta": "By Friday"}
        body = {
            "client_task_id": "assigned",
            "title": "Check API mapping",
            "addressed_member_ids": [MEMBERS[1]],
            "assignment": assignment,
        }
        task = {"task_id": TASK, "assignment": assignment}
        upstream.results[("POST", f"/rooms/{ROOM}/tasks")] = task
        assert await client.execute("key-a", "create_task", {"room_id": ROOM, **body}) == task
        assert upstream.calls[-1] == ("POST", f"/rooms/{ROOM}/tasks", "token-a", None, body)
        with pytest.raises(ClientError) as error:
            await client.execute(
                "key-a",
                "create_task",
                {"room_id": ROOM, **body, "assignment": {"eta": "By Friday"}},
            )
        assert error.value.code == "invalid_request"
        assert len(upstream.calls) == 1

    asyncio.run(scenario())
