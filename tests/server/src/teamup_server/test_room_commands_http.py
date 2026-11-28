from uuid import uuid4

from test_contract import (
    api,
    auth,
    confirm,
    credential,
    enroll,
    error,
    get,
    join,
    member,
    post,
    room,
)


def test_durable_send_checks_actor_and_replays_without_a_second_message(tmp_path):
    with api(tmp_path / "rooms.sqlite3") as client:
        assert enroll(client, "sender").is_success
        assert enroll(client, "reader", owner="bob").is_success
        path = room(client)
        join(client, "reader", path)
        for label in ("sender", "reader"):
            confirm(client, label, path)
        member_id = member(client, "sender")
        summary = get(client, "sender", "/rooms?include_membership=true").json()[
            "rooms"
        ][0]
        generation = get(client, "sender", "/sync/capabilities").json()[
            "server_generation"
        ]
        command = {
            "operation_id": str(uuid4()),
            "expected_member_id": member_id,
            "server_generation": generation,
            "kind": "send_message",
            "room_id": path.rsplit("/", 1)[-1],
            "parameters": {
                "membership_id": summary["membership_id"],
                "expected_board_version": 1,
                "client_message_id": "durable-send",
                "text": "once",
                "mention_member_id": member(client, "reader"),
            },
        }
        error(
            post(client, "reader", "/room-commands", json=command),
            409,
            "member_identity_changed",
        )
        receipt = post(client, "sender", "/room-commands", json=command)
        assert receipt.is_success, receipt.text
        assert receipt.json()["receipt"]["kind"] == "send_message"
        assert "text" not in receipt.json()["receipt"]
        assert (
            post(client, "sender", "/room-commands", json=command).json()
            == receipt.json()
        )
        target = command["parameters"].pop("mention_member_id")
        command["parameters"]["mention_member_ids"] = [target]
        assert (
            post(client, "sender", "/room-commands", json=command).json()
            == receipt.json()
        )
        assert [
            item["text"]
            for item in get(client, "reader", path + "/messages").json()["messages"]
        ] == ["once"]


def test_room_command_preconditions_and_typed_operations(tmp_path):
    with api(tmp_path / "rooms.sqlite3") as client:
        assert enroll(client, "sender").is_success
        assert enroll(client, "reader", owner="bob").is_success
        sender = member(client, "sender")
        reader = member(client, "reader")
        generation = get(client, "sender", "/sync/capabilities").json()[
            "server_generation"
        ]

        def command(label, actor, kind, parameters, room_id=None, operation_id=None):
            body = {
                "operation_id": operation_id or str(uuid4()),
                "expected_member_id": actor,
                "server_generation": generation,
                "kind": kind,
                "parameters": parameters,
            }
            if room_id is not None:
                body["room_id"] = room_id
            return post(client, label, "/room-commands", json=body)

        create_id = str(uuid4())
        created = command(
            "sender", sender, "create_room", {"name": "Durable"}, operation_id=create_id
        )
        assert created.is_success, created.text
        assert (
            command(
                "sender",
                sender,
                "create_room",
                {"name": "Durable"},
                operation_id=create_id,
            ).json()
            == created.json()
        )
        receipt = created.json()["receipt"]
        room_id = receipt["room_id"]
        path = f"/rooms/{room_id}"
        state = get(
            client, "reader", f"{path}/membership-state?expected_member_id={reader}"
        ).json()
        assert state["membership_id"] is None
        join_id = str(uuid4())
        joined = command(
            "reader",
            reader,
            "join_room",
            {"membership_revision": state["membership_revision"]},
            room_id,
            join_id,
        )
        assert joined.is_success, joined.text
        reader_membership = joined.json()["receipt"]["membership_id"]
        assert get(client, "reader", f"{path}/board").is_success
        ack = command(
            "reader",
            reader,
            "acknowledge_board",
            {"membership_id": reader_membership, "version": 1},
            room_id,
        )
        assert ack.is_success, ack.text
        left = command(
            "reader",
            reader,
            "leave_room",
            {"membership_id": reader_membership},
            room_id,
        )
        assert left.is_success, left.text
        error(
            command(
                "reader",
                reader,
                "join_room",
                {"membership_revision": state["membership_revision"]},
                room_id,
                join_id,
            ),
            404,
            "not_found",
        )
        error(
            command(
                "reader",
                reader,
                "join_room",
                {"membership_revision": state["membership_revision"]},
                room_id,
            ),
            409,
            "membership_changed",
        )

        confirm(client, "sender", path)
        scope = {"membership_id": receipt["membership_id"], "expected_board_version": 1}
        created_task = command(
            "sender",
            sender,
            "create_task",
            {**scope, "client_task_id": "task-key", "title": "Verify"},
            room_id,
        )
        assert created_task.is_success, created_task.text
        task_scope = {**scope, "task_id": created_task.json()["receipt"]["task_id"]}
        for kind, payload in (
            ("join_task", task_scope),
            (
                "record_task_progress",
                {**task_scope, "client_progress_id": "progress", "note": "Done"},
            ),
            ("finish_task", task_scope),
        ):
            response = command("sender", sender, kind, payload, room_id)
            assert response.is_success, response.text
        error(
            command(
                "sender",
                sender,
                "record_task_progress",
                {**task_scope, "client_progress_id": "late", "note": "Late"},
                room_id,
            ),
            409,
            "conflict",
        )


def test_retirement_checks_target_and_generation_before_revocation(tmp_path):
    with api(tmp_path / "rooms.sqlite3") as client:
        assert enroll(client, "sender").is_success
        assert enroll(client, "reader", owner="bob").is_success
        sender = member(client, "sender")
        reader = member(client, "reader")
        generation = get(client, "sender", "/sync/capabilities").json()[
            "server_generation"
        ]
        wrong_actor = {"expected_member_id": sender, "server_generation": generation}
        error(
            client.post(
                "/v1/members/me/retire",
                content="null",
                headers={
                    **auth(credential("reader")),
                    "Content-Type": "application/json",
                },
            ),
            422,
            "invalid_request",
        )
        error(
            post(client, "reader", "/members/me/retire", json=wrong_actor),
            409,
            "member_identity_changed",
        )
        assert member(client, "reader") == reader
        old_generation = {"expected_member_id": sender, "server_generation": "obsolete"}
        error(
            post(client, "sender", "/members/me/retire", json=old_generation),
            409,
            "server_generation_changed",
        )
        assert member(client, "sender") == sender
        receipt = post(client, "sender", "/members/me/retire", json=wrong_actor)
        assert receipt.is_success, receipt.text
        query = f"expected_member_id={sender}&server_generation={generation}"
        assert (
            get(client, "sender", f"/members/me/retirement?{query}").json()
            == receipt.json()
        )
