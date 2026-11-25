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


def test_only_creating_member_can_publish_a_new_version(team):
    path = room(team)
    board_path = f"{path}/board"
    join(team, "target", path)
    # Another session owned by the creator's human account still has its own identity.
    enroll(team, "sibling")
    join(team, "sibling", path)
    confirm(team, "target", path)
    update = {"version": 1, "rules": "Review the diff.", "protocol": "Summarize it."}

    error(post(team, "target", board_path, json=update), 403, "forbidden")
    error(post(team, "sibling", board_path, json=update), 403, "forbidden")
    error(post(team, "reader", board_path, json=update), 404, "not_found")
    error(
        post(team, "sender", board_path, json={**update, "rules": "   "}),
        422,
        "invalid_request",
    )
    assert get(team, "sender", f"{board_path}/view").json()["can_edit"] is True
    response = post(team, "sender", board_path, json=update)
    assert response.status_code == 200
    assert response.json() == {
        **update,
        "version": 2,
        "room_id": path.rsplit("/", 1)[1],
        "can_edit": True,
    }
    error(post(team, "sender", board_path, json=update), 409, "conflict")
    error(get(team, "target", f"{path}/messages"), 409, "board_ack_required")
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
        board_path = room(client) + "/board"
        together = Barrier(2)

        def publish(rules):
            together.wait(timeout=5)
            return post(
                client,
                "sender",
                board_path,
                json={"version": 1, "rules": rules, "protocol": "Post a summary."},
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(publish, ("First edit", "Second edit")))
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
