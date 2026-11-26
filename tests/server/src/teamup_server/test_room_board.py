import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest
from test_contract import api, confirm, enroll, error, get, join, member, post, room


@pytest.fixture
def team(tmp_path):
    with api(tmp_path / "team.sqlite3") as client:
        for label, owner in (("sender", "alice"), ("target", "bob"), ("reader", "bob")):
            assert enroll(client, label, owner=owner).status_code == 200
        yield client


def test_board_view_preserves_receipt_gate_and_member_scope(team):
    path = room(team, rules="Share updates here.", protocol="Report blockers.\n")
    board_path = f"{path}/board"
    join(team, "target", path)

    response = get(team, "target", f"{board_path}/view")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "room_id": path.rsplit("/", 1)[1],
        "version": 1,
        "rules": "Share updates here.",
        "protocol": "Report blockers.\n",
        "can_edit": False,
    }
    error(get(team, "reader", f"{board_path}/view"), 404, "not_found")
    error(team.get(f"/v1{board_path}/view"), 401, "unauthorized")
    error(
        post(team, "target", f"{board_path}/ack", json={"version": 1}),
        409,
        "board_read_required",
    )
    confirm(team, "target", path)


def test_joined_sessions_of_the_creating_account_can_publish(team):
    path = room(team)
    board_path = f"{path}/board"
    join(team, "target", path)
    assert enroll(team, "sibling").is_success
    join(team, "sibling", path)
    confirm(team, "target", path)
    update = {"version": 1, "rules": "Review the diff.", "protocol": "Summarize it."}

    error(post(team, "target", board_path, json=update), 403, "forbidden")
    error(post(team, "reader", board_path, json=update), 404, "not_found")
    error(
        post(team, "sender", board_path, json={**update, "rules": "   "}),
        422,
        "invalid_request",
    )
    assert get(team, "sender", f"{board_path}/view").json()["can_edit"] is True
    assert get(team, "sibling", f"{board_path}/view").json()["can_edit"] is True
    response = post(team, "sibling", board_path, json=update)
    assert response.status_code == 200
    assert response.json() == {
        **update,
        "version": 2,
        "room_id": path.rsplit("/", 1)[1],
        "can_edit": True,
    }
    error(post(team, "sender", board_path, json=update), 409, "conflict")
    for label in ("target", "sibling"):
        error(get(team, label, f"{path}/messages"), 409, "board_ack_required")
    for label in ("sender", "target"):
        error(
            post(team, label, f"{board_path}/ack", json={"version": 2}),
            409,
            "board_read_required",
        )
        assert get(team, label, board_path).json()["version"] == 2
        assert (
            post(team, label, f"{board_path}/ack", json={"version": 2}).status_code
            == 200
        )
    assert get(team, "target", f"{path}/messages").status_code == 200


def test_competing_edits_have_one_winner_and_persist(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        path = room(client)
        assert enroll(client, "sibling").is_success
        join(client, "sibling", path)
        board_path = f"{path}/board"
        together = Barrier(2)

        def publish(label, rules):
            together.wait(timeout=5)
            return post(
                client,
                label,
                board_path,
                json={"version": 1, "rules": rules, "protocol": "Post a summary."},
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(publish, ("sender", "sibling"), ("First edit", "Second edit")))
        assert sorted(result.status_code for result in responses) == [200, 409]
        committed = next(result.json() for result in responses if result.is_success)

    with api(database) as client:
        assert get(client, "sender", f"{board_path}/view").json() == committed
        assert committed["version"] == 2


def test_upgrade_keeps_rooms_without_a_recorded_creator_readable(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        path = room(client, rules="Keep existing rules.", protocol="Keep the protocol.")
        creator = member(client, "sender")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("ALTER TABLE rooms DROP COLUMN creator_member_id")
        connection.execute("PRAGMA user_version = 9")
        connection.execute("UPDATE room_activity SET at = NULL WHERE room_id = ?", (path.rsplit("/", 1)[1],))

    with api(database) as client:
        view = f"{path}/board/view"
        board = get(client, "sender", view).json()
        assert board["can_edit"] is False
        assert board["rules"] == "Keep existing rules."
        assert board["protocol"] == "Keep the protocol."
        update = {"version": 1, "rules": "Replace rules.", "protocol": "New protocol."}
        error(post(client, "sender", f"{path}/board", json=update), 403, "forbidden")
        assert member(client, "sender") == creator
        new_room = room(client)
        assert (
            get(client, "sender", f"{new_room}/board/view").json()["can_edit"] is True
        )


def test_creator_moderation_terminates_only_the_selected_join(team):
    path = room(team)
    join(team, "target", path)
    confirm(team, "sender", path)
    confirm(team, "target", path)
    target = member(team, "target")
    removal = f"{path}/members/{target}/remove"

    def current_join():
        roster = get(team, "sender", f"{path}/members?include_membership=true").json()
        return next(row["membership_id"] for row in roster["members"] if row["member_id"] == target)

    assert get(team, "sender", "/rooms?include_membership=true").json()["rooms"][0][
        "can_remove_members"
    ]
    assert not get(team, "target", "/rooms?include_membership=true").json()["rooms"][0][
        "can_remove_members"
    ]
    assert "membership_id" not in get(team, "sender", f"{path}/members").json()["members"][0]
    chosen = {"membership_id": current_join()}
    error(post(team, "target", removal, json=chosen), 403, "forbidden")
    error(post(team, "reader", removal, json=chosen), 404, "not_found")
    error(post(team, "sender", removal, json={}), 422, "invalid_request")
    pending = post(
        team, "sender", f"{path}/messages", json={"client_message_id": "queued", "text": "Queued"}
    )
    assert pending.is_success
    sent = post(
        team, "target", f"{path}/messages", json={"client_message_id": "sent", "text": "Retained"}
    )
    assert sent.is_success

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(team, "sender", removal, json=chosen), range(2)))
    assert all(response.is_success for response in responses)
    receipts = [response.json() for response in responses]
    assert all(receipt["member_id"] == target for receipt in receipts)
    (receipt,) = [receipt for receipt in receipts if receipt["departure_event_id"] is not None]
    error(get(team, "target", f"{path}/messages"), 404, "not_found")
    assert post(
        team, "sender", f"{path}/messages", json={"client_message_id": "later", "text": "Later"}
    ).is_success
    assert get(team, "target", "/inbox?kinds=message,room_left").json() == {
        "events": [{"kind": "room_left", "room_id": receipt["room_id"],
                    "event_id": receipt["departure_event_id"]}]
    }
    assert get(team, "sender", f"{path}/messages").json()["messages"][-2] == sent.json()
    join(team, "target", path)
    assert current_join() != chosen["membership_id"]
    error(post(team, "sender", removal, json=chosen), 409, "membership_changed")
    assert get(team, "target", f"{path}/members").status_code == 200


def test_creator_regains_board_permission_on_rejoin_without_transferring_it(team):
    path = room(team)
    join(team, "target", path)
    board = f"{path}/board"
    update = {"version": 1, "rules": "Second rules.", "protocol": "Second protocol."}
    assert post(team, "sender", f"{path}/leave").status_code == 200
    error(post(team, "sender", board, json=update), 404, "not_found")
    assert get(team, "target", f"{board}/view").json()["can_edit"] is False
    error(post(team, "target", board, json=update), 403, "forbidden")
    join(team, "sender", path)
    assert get(team, "sender", f"{board}/view").json()["can_edit"] is True
    assert post(team, "sender", board, json=update).json()["version"] == 2



def test_upgrade_recovers_only_the_original_timestamped_creator(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        enroll(client, "sender")
        enroll(client, "target", owner="bob")
        enroll(client, "sibling")
        path = room(client)
        join(client, "target", path)
        assert post(client, "sender", f"{path}/leave").status_code == 200
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE rooms SET creator_member_id = NULL")
        connection.execute("PRAGMA user_version = 9")

    for _ in range(2):
        with api(database) as client:
            assert get(client, "target", f"{path}/board/view").json()["can_edit"] is False
            join(client, "sibling", path)
            assert get(client, "sibling", f"{path}/board/view").json()["can_edit"] is True


def test_replacement_session_can_manage_existing_room_after_creator_retires(tmp_path):
    database = tmp_path / "team.sqlite3"
    with api(database) as client:
        assert enroll(client, "sender").is_success
        assert enroll(client, "target", owner="bob").is_success
        path = room(client)
        join(client, "target", path)
        creator = member(client, "sender")
        target = member(client, "target")
        assert post(client, "sender", "/members/me/retire").is_success

    with api(database) as client:
        assert enroll(client, "replacement").is_success
        assert member(client, "replacement") != creator
        roster = get(client, "target", f"{path}/members?include_membership=true").json()
        (membership,) = roster["members"]
        assert membership["member_id"] == target
        removal = f"{path}/members/{target}/remove"
        chosen = {"membership_id": membership["membership_id"]}
        update = {"version": 1, "rules": "Updated rules.", "protocol": "Updated protocol."}

        error(get(client, "sender", f"{path}/board/view"), 401, "unauthorized")
        error(post(client, "replacement", f"{path}/board", json=update), 404, "not_found")
        error(post(client, "replacement", removal, json=chosen), 404, "not_found")
        error(post(client, "target", removal, json=chosen), 403, "forbidden")

        join(client, "replacement", path)
        (summary,) = get(client, "replacement", "/rooms?include_membership=true").json()["rooms"]
        assert summary["can_remove_members"] is True
        assert summary["acked_board_version"] is None
        assert get(client, "replacement", f"{path}/board/view").json()["can_edit"] is True
        published = post(client, "replacement", f"{path}/board", json=update)
        assert published.is_success
        assert published.json()["version"] == 2
        removed = post(client, "replacement", removal, json=chosen)
        assert removed.is_success
        assert removed.json()["departure_event_id"] is not None
        retry = post(client, "replacement", removal, json=chosen)
        assert retry.is_success
        assert retry.json()["departure_event_id"] is None
