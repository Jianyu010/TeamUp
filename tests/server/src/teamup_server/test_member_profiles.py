import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from teamup_server.collaboration import Collaboration
from test_contract import KEYS, api, auth, credential, error
from test_server_accounts import PASSWORD, bind, login_token

PROFILE = {
    "name": "Release Lead",
    "description": "Coordinates releases and hands deployment off to the operator.",
}
BODY = {
    **PROFILE,
    "harness_instance_id": "installation",
    "session_key": "release-session",
    "member_token": credential("release-session"),
}


@pytest.fixture
def profiles(tmp_path):
    path = tmp_path / "team.db"
    collaboration = Collaboration(path, KEYS)
    for username in ("alice", "bob"):
        collaboration.create_account(username, PASSWORD)
    with api(path) as client:
        yield (
            client,
            login_token(client, "alice", PASSWORD),
            login_token(client, "bob", PASSWORD),
        )


def test_register_and_read_profiles(profiles):
    client, alice, _ = profiles
    headers = auth(alice)
    response = client.post("/v1/members", headers=headers, json=BODY)
    assert response.status_code == 200, response.text
    registered = response.json()
    assert registered == {
        **PROFILE,
        "member_id": registered["member_id"],
        "role_description": PROFILE["name"],
        "owner_username": "alice",
    }
    retry = client.post(
        "/v1/members", headers=headers, json={**BODY, "name": "Changed"}
    )
    assert retry.json() == registered
    old_payload = {key: value for key, value in BODY.items() if key not in PROFILE}
    old_payload["role_description"] = "Old Client label"
    assert (
        client.post("/v1/members", headers=headers, json=old_payload).json()
        == registered
    )
    member_auth = auth(BODY["member_token"])
    assert client.get("/v1/members/me", headers=member_auth).json() == registered
    assert client.get("/v1/members?q=deployment", headers=member_auth).json() == {
        "members": [registered],
    }
    assert client.get("/v1/members?q=release%20lead", headers=member_auth).json() == {
        "members": [registered],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Release Operations Quality Lead"},
        {"name": "X" * 201},
        {"name": "Release\nLead"},
        {"name": " Release Lead"},
        {"description": "X" * 161},
        {"description": "Coordinates\u2028releases"},
        {"description": " "},
        {"description": None},
        {"role_description": "Legacy label"},
    ],
)
def test_reject_invalid_profiles(profiles, changes):
    client, alice, _ = profiles
    response = client.post("/v1/members", headers=auth(alice), json={**BODY, **changes})
    error(response, 422, "invalid_request")


def test_rename_enforces_the_same_name_limits(profiles):
    client, alice, _ = profiles
    headers = auth(alice)
    registered = client.post("/v1/members", headers=headers, json=BODY).json()
    path = f"/v1/members/{registered['member_id']}"
    error(
        client.patch(path, headers=headers, json={"role_description": "A B C D"}),
        422,
        "invalid_request",
    )
    name = "Release Operations Coordinator"
    response = client.patch(path, headers=headers, json={"role_description": name})
    assert response.status_code == 200, response.text
    assert response.json() == {**registered, "name": name, "role_description": name}


def test_role_only_registration_cannot_create_a_new_member(profiles):
    client, alice, _ = profiles
    legacy_body = {
        key: BODY[key] for key in ("harness_instance_id", "session_key", "member_token")
    } | {"role_description": PROFILE["name"]}
    error(
        client.post("/v1/members", headers=auth(alice), json=legacy_body),
        422,
        "invalid_request",
    )


def test_v10_identity_survives_upgrade_and_role_only_retry(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_label = "An existing name longer than three words"
    collaboration = Collaboration(path, KEYS)
    collaboration.create_account("alice", PASSWORD)
    with api(path) as client:
        owner_token = login_token(client, "alice", PASSWORD)
        enrolled = client.post("/v1/members", headers=auth(owner_token), json=BODY)
        assert enrolled.status_code == 200, enrolled.text
        member_id = enrolled.json()["member_id"]
        member_headers = auth(BODY["member_token"])
        created = client.post(
            "/v1/rooms", headers=member_headers, json={"name": "Release"}
        )
        assert created.status_code == 200, created.text
        room_path = f"/v1/rooms/{created.json()['room_id']}"
        board = client.get(f"{room_path}/board", headers=member_headers).json()
        acked = client.post(
            f"{room_path}/board/ack",
            headers=member_headers,
            json={"version": board["version"]},
        )
        assert acked.status_code == 200, acked.text
        sent = client.post(
            f"{room_path}/messages",
            headers=member_headers,
            json={"text": "Before upgrade", "client_message_id": "legacy-message"},
        )
        assert sent.status_code == 200, sent.text

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE members DROP COLUMN description")
        connection.execute("ALTER TABLE room_members DROP COLUMN responsibility")
        connection.execute("PRAGMA user_version = 10")
        connection.execute("UPDATE members SET role_description = ?", (legacy_label,))

    with api(path) as client:
        expected = {
            "member_id": member_id,
            "role_description": legacy_label,
            "owner_username": "alice",
        }
        assert client.get("/v1/members/me", headers=member_headers).json() == expected
        assert client.get(f"{room_path}/members", headers=member_headers).json() == {
            "members": [{**expected, "acked_board_version": board["version"]}],
        }
        assert client.get(f"{room_path}/messages", headers=member_headers).json() == {
            "messages": [sent.json()],
        }
        old_body = {
            key: BODY[key]
            for key in ("harness_instance_id", "session_key", "member_token")
        } | {"role_description": legacy_label}
        assert (
            client.post("/v1/members", headers=auth(owner_token), json=old_body).json()
            == expected
        )
        assert (
            client.post("/v1/members", headers=auth(owner_token), json=BODY).json()
            == expected
        )

        name_path = f"/v1/members/{member_id}"
        owner_headers = auth(owner_token)
        body = {"role_description": legacy_label}
        assert (
            client.patch(name_path, headers=owner_headers, json=body).json() == expected
        )
        error(
            client.patch(
                name_path,
                headers=owner_headers,
                json={"role_description": "Another long legacy member label"},
            ),
            422,
            "invalid_request",
        )
        assert client.get("/v1/members/me", headers=member_headers).json() == expected


def test_room_responsibility_is_owner_managed_and_scoped_to_current_membership(
    profiles,
):
    client, alice, bob = profiles
    registered = client.post("/v1/members", headers=auth(alice), json=BODY)
    assert registered.status_code == 200, registered.text
    member_id = registered.json()["member_id"]
    token = BODY["member_token"]
    peer_token = credential("room-peer")
    peer = bind(client, bob, "room-peer")
    assert peer.status_code == 200, peer.text

    def create_room(member_token):
        response = client.post(
            "/v1/rooms", headers=auth(member_token), json={"name": "Release"}
        )
        assert response.status_code == 200, response.text
        return f"/v1/rooms/{response.json()['room_id']}"

    primary = create_room(peer_token)
    secondary = create_room(token)

    def join():
        assert client.post(f"{primary}/join", headers=auth(token)).status_code == 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: join(), range(2)))
    roster_path = f"{primary}/members"
    override_path = f"{roster_path}/{member_id}/responsibility"

    def profile(path=roster_path):
        result = client.get(
            path, headers=auth(token), params={"include_membership": "true"}
        )
        matches = [
            entry
            for entry in result.json()["members"]
            if entry["member_id"] == member_id
        ]
        assert len(matches) == 1
        return matches[0]

    initial = profile()
    assert initial["description"] == PROFILE["description"]
    assert initial["owner_username"] == "alice"
    override = {
        "membership_id": initial["membership_id"],
        "description": "Coordinates rollback.",
    }
    error(
        client.patch(override_path, headers=auth(bob), json=override), 404, "not_found"
    )
    error(
        client.patch(override_path, headers=auth(token), json=override),
        401,
        "unauthorized",
    )

    error(
        client.post(
            f"{primary}/join",
            headers=auth(token),
            json={"description": "A competing responsibility."},
        ),
        422,
        "invalid_request",
    )

    updated = client.patch(override_path, headers=auth(alice), json=override)
    assert updated.json() == {"description": "Coordinates rollback."}
    join()
    assert profile() == {**initial, "description": "Coordinates rollback."}
    assert profile(f"{secondary}/members")["description"] == PROFILE["description"]

    cleared = client.patch(
        override_path, headers=auth(alice), json={**override, "description": None}
    )
    assert cleared.json() == {"description": None}
    assert profile() == initial
    assert client.patch(override_path, headers=auth(alice), json=override).is_success

    assert client.post(f"{primary}/leave", headers=auth(token)).status_code == 200
    join()
    error(
        client.patch(override_path, headers=auth(alice), json=override),
        409,
        "membership_changed",
    )
    assert profile()["description"] == PROFILE["description"]
