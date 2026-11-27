"""Public task visibility, retry and tracer behavior on a disposable Server."""

import asyncio

import pytest
from _stack import Server, deploy, new_secret, tool, wait_for


@pytest.mark.scenario(
    "NS-18-addressed", "Addressed tasks stay private through board changes and retries"
)
def test_addressed_task_visibility(ws):
    key = new_secret()
    server = Server(ws, {"owner": key}).start()
    api = server.api()
    try:
        creator, assignee, observer = [
            api.enroll(key, name, session=name)
            for name in ("Creator", "Assignee", "Observer")
        ]
        room = api.create_room(creator, "Task visibility", rules="Scripted room")
        for member in (assignee, observer):
            api.join(member, room)
        for member in (creator, assignee, observer):
            api.read_and_ack_board(member, room)

        room_path = f"/v1/rooms/{room}"
        tasks = f"{room_path}/tasks"
        public = api.call(
            "POST",
            tasks,
            creator.token,
            json={"title": "Visible to the room", "client_task_id": "public"},
        )
        addressed = {
            "title": "Visible to the assignee",
            "client_task_id": "directed",
            "addressed_member_ids": [assignee.member_id],
        }
        directed = api.call("POST", tasks, creator.token, json=addressed)
        path = f"{tasks}/{directed['task_id']}"
        assert api.call("GET", tasks, observer.token)["tasks"] == [public]
        assert api.call("GET", tasks, assignee.token)["tasks"] == [public, directed]
        assert api.call("POST", f"{path}/join", creator.token, error="not_found") == 404

        api.call("POST", f"{path}/join", assignee.token)
        progress = {"client_progress_id": "progress", "note": "Started work"}
        recorded = api.call("POST", f"{path}/progress", assignee.token, json=progress)

        api.call(
            "POST",
            f"{room_path}/board",
            creator.token,
            json={
                "version": 1,
                "rules": "Revised rules",
                "protocol": "Ack the revision",
            },
        )
        for method, suffix, kwargs in (
            ("GET", "", {}),
            ("GET", "/events", {}),
            ("POST", "/progress", {"json": progress}),
            ("POST", "/finish", {}),
        ):
            assert (
                api.call(
                    method, path + suffix, observer.token, error="not_found", **kwargs
                )
                == 404
            )
            assert (
                api.call(
                    method,
                    path + suffix,
                    assignee.token,
                    error="board_ack_required",
                    **kwargs,
                )
                == 409
            )

        api.read_and_ack_board(assignee, room)
        assert (
            api.call("POST", f"{path}/progress", assignee.token, json=progress)
            == recorded
        )
        assert (
            api.call(
                "POST",
                f"{path}/progress",
                assignee.token,
                json=progress | {"note": "Changed note"},
                error="conflict",
            )
            == 409
        )
        api.read_and_ack_board(creator, room)
        assert api.call("POST", tasks, creator.token, json=addressed) == recorded
        finished = api.call("POST", f"{path}/finish", creator.token)
        assert finished["version"] == 4
        assert (
            api.call("POST", f"{path}/progress", assignee.token, json=progress)
            == finished
        )
        assert [
            event["kind"]
            for event in api.call("GET", f"{path}/events", assignee.token)["events"]
        ] == ["created", "joined", "progress", "finished"]
        api.read_and_ack_board(observer, room)
        assert (
            api.call("GET", tasks, observer.token, params={"status": "finished"})[
                "tasks"
            ]
            == []
        )
    finally:
        api.close()


@pytest.mark.scenario(
    "NS-18-mcp",
    "Task creation, progress, finish and durable history through Client and MCP",
)
def test_task_tools_and_persistence(ws):
    server, (creator, worker) = deploy(ws, notify=False)

    async def run():
        async with creator.agent() as author, worker.agent() as participant:
            for agent, name in ((author, "Creator"), (participant, "Worker")):
                await tool(
                    agent,
                    "register",
                    name=name,
                    description="Runs task workflow checks.",
                )
            room = (
                await tool(
                    author, "create_room", name="MCP tasks", rules="Use explicit tasks"
                )
            )["room_id"]
            await tool(participant, "join_room", room_id=room)
            for agent in (author, participant):
                board = await tool(agent, "get_board", room_id=room)
                await tool(agent, "ack_board", room_id=room, version=board["version"])

            create = {
                "room_id": room,
                "client_task_id": "workflow",
                "title": "Check task history",
            }
            opened = await tool(author, "create_task", **create)
            target = {"room_id": room, "task_id": opened["task_id"]}
            assert (await tool(participant, "list_tasks", room_id=room))["tasks"] == [
                opened
            ]
            await tool(participant, "join_task", **target)
            progress = target | {
                "client_progress_id": "draft",
                "note": "Drafted the interface",
            }
            await tool(participant, "report_progress", **progress)
            finished = await tool(author, "finish_task", **target)
            assert finished["status"] == "finished" and finished["version"] == 4

            await asyncio.to_thread(server.restart)
            assert await tool(author, "create_task", **create) == finished
            assert await tool(participant, "report_progress", **progress) == finished
            assert await tool(participant, "get_task", **target) == finished
            events = (await tool(participant, "task_history", **target))["events"]
            assert [(event["kind"], event["note"]) for event in events] == [
                ("created", None),
                ("joined", None),
                ("progress", progress["note"]),
                ("finished", None),
            ]

    asyncio.run(asyncio.wait_for(run(), 75))


@pytest.mark.scenario(
    "NS-18-tracer", "Progress and finish invalidate previously issued reminders"
)
def test_tracer_invalidation(ws):
    key = new_secret()
    server = Server(
        ws,
        {"owner": key},
        server_args=("--task-stall-after", "2", "--task-reminder-interval", "300"),
    ).start()
    api = server.api()
    try:
        creator = api.enroll(key, "Creator", session="creator")
        assignee = api.enroll(key, "Assignee", session="assignee")
        room = api.create_room(creator, "Tracer invalidation", rules="Scripted room")
        api.join(assignee, room)
        for member in (creator, assignee):
            api.read_and_ack_board(member, room)
        tasks = f"/v1/rooms/{room}/tasks"
        task_paths = {}
        for title in ("Progress", "Finish"):
            task = api.call(
                "POST",
                tasks,
                creator.token,
                json={"client_task_id": title, "title": title},
            )
            task_paths[task["task_id"]] = f"{tasks}/{task['task_id']}"
            api.call("POST", f"{task_paths[task['task_id']]}/join", assignee.token)

        def pending():
            return api.call("GET", "/v1/task-reminders", assignee.token)["reminders"]

        def all_due():
            reminders = pending()
            return reminders if len(reminders) >= len(task_paths) else None

        reminders = wait_for(all_due, server.proc, timeout=12, what="two stalled tasks")
        assert {reminder["task_id"] for reminder in reminders} == task_paths.keys()
        assert {reminder["title"] for reminder in reminders} == {"Progress", "Finish"}
        assert api.call("GET", "/v1/task-reminders", creator.token)["reminders"] == []
        finished = None
        for reminder in reminders:
            path = task_paths[reminder["task_id"]]
            claim = f"/v1/task-reminders/{reminder['reminder_id']}/claim"
            assert api.call("POST", claim, creator.token, error="not_found") == 404
            if reminder["title"] == "Progress":
                progressed = api.call(
                    "POST",
                    f"{path}/progress",
                    assignee.token,
                    json={"client_progress_id": "fresh", "note": "Unblocked"},
                )
                assert progressed["stalled_since"] is None
            else:
                finished = api.call("POST", f"{path}/finish", creator.token)
                assert finished["stalled_since"] is None
            assert api.call("POST", claim, assignee.token, error="not_found") == 404

        [renewed] = wait_for(
            pending, server.proc, timeout=12, what="next activity period"
        )
        assert renewed["task_id"] == progressed["task_id"]
        previous = next(
            reminder
            for reminder in reminders
            if reminder["task_id"] == renewed["task_id"]
        )
        assert renewed["reminder_id"] > previous["reminder_id"]
        assert renewed["stalled_since"] > previous["stalled_since"]
        assert finished["status"] == "finished"
        claim = f"/v1/task-reminders/{renewed['reminder_id']}/claim"
        assert api.call("POST", claim, assignee.token) == renewed
        server.restart()
        assert pending() == []
        assert api.call("POST", claim, assignee.token, error="not_found") == 404
    finally:
        api.close()
