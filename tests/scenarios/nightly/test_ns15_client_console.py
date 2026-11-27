"""The browser Console works against a real signed-in Client and Server."""

from contextlib import closing

import pytest
from _stack import Api, Server, new_secret

from tests.scenarios._scenario_browser import (
    account_cli,
    browser_page,
    client_console,
    console_login,
)

ROOM_BOARD = "/api/room-board"


@pytest.fixture
def client_workspace(ws):
    password = new_secret()
    peer_key = new_secret()
    server = Server(ws, {"remote-owner": peer_key})
    account_cli(server, "create", "alice", password=password)
    server.start()
    with (
        closing(Api(server.url, trust_env=False)) as api,
        client_console(ws, server, "alice") as (developer, console, url),
    ):
        enrollment = api.call(
            "POST",
            "/v1/auth/login",
            json={"username": "alice", "password": password},
        )["access_token"]
        binding = developer.bindings[0]
        local = api.enroll(
            enrollment,
            "Local reviewer",
            harness=binding["harness_instance_id"],
            session=binding["session_key"],
            token=binding["member_token"],
        )
        peer = api.enroll(peer_key, "Remote reviewer", session="remote")
        yield api, developer, console, url, password, local, peer


def room_session(console, local):
    response = console.get("/api/workspace")
    response.raise_for_status()
    (session,) = response.json()["sessions"]
    assert session["state"] == "ready"
    assert session["member_id"] == local.member_id
    return session["id"]


def console_read(console, path, **params):
    response = console.get(path, params=params)
    response.raise_for_status()
    return response.json()


def board_draft(console, params, **text):
    board = console_read(console, ROOM_BOARD, **params)
    assert board["state"] == "ready"
    return {"version": board["board"]["version"], **text}


def publish(console, params, draft):
    return console.post(
        ROOM_BOARD,
        params=params,
        headers={"Origin": str(console.base_url).rstrip("/")},
        json=draft,
    )


@pytest.mark.scenario("NS-15.01", "Client console sign-in and loopback guard")
def test_client_sign_in_and_loopback_guard(client_workspace):
    _, developer, console, _, password, local, _ = client_workspace
    assert console.get("/").status_code == 200
    assert console.get("/api/login").json() == {"state": "sign_in_required"}
    assert console.get("/api/workspace").status_code == 409
    assert (
        console.get("/api/workspace", headers={"Authorization": ""}).status_code == 401
    )
    profile = developer.config_path.read_bytes()
    assert not developer.socket_path.exists()

    response = console.post(
        "/api/login",
        headers={"Origin": "https://invalid.example"},
        json={"username": "alice", "password": password},
    )
    assert response.status_code == 403
    response = console.post(
        "/api/login",
        headers={"Origin": str(console.base_url).rstrip("/")},
        json={"username": "alice", "password": new_secret()},
    )
    assert response.status_code == 401
    assert response.json() == {"state": "wrong_credentials"}
    profile_unchanged = developer.config_path.read_bytes() == profile
    assert profile_unchanged
    assert not developer.socket_path.exists()

    console_login(console, "alice", password)
    assert developer.socket_path.is_socket()
    assert console.get("/api/login").json() == {"state": "ready"}
    room_session(console, local)


@pytest.mark.scenario(
    "NS-15.02", "Client workspace discovery and authorized transcript"
)
def test_client_discovery_and_transcript_scope(client_workspace):
    api, _, console, _, password, local, peer = client_workspace
    console_login(console, "alice", password)
    session = room_session(console, local)
    room = api.create_room(peer, "Retained workshop", history_policy="retained")
    api.read_and_ack_board(peer, room)
    message = api.send(peer, room, "Discussed before the reader joined")
    params = {"session": session, "room": room}

    workspace = console_read(console, "/api/workspace")
    assert workspace["directory_available"] is True
    (discovered,) = workspace["rooms"]
    assert discovered["id"] == room
    assert discovered["memberships"] == []
    assert discovered["latest_message_id"] is None
    assert console_read(console, "/api/room", **params) == {"state": "not_found"}

    api.join(local, room)
    (joined,) = console_read(console, "/api/workspace")["rooms"]
    assert [membership["session"] for membership in joined["memberships"]] == [session]
    gated = console_read(console, "/api/room", **params)
    assert gated["state"] == "board_ack_required"
    assert gated["messages"] == []
    api.read_and_ack_board(local, room)
    authorized = console_read(console, "/api/room", **params)
    assert authorized["state"] == "ready"
    assert [(row["id"], row["text"]) for row in authorized["messages"]] == [
        (str(message["message_id"]), message["text"])
    ]

    catchup = api.send(peer, room, "New message after the reader joined")
    result = console_read(
        console, "/api/room", after=str(message["message_id"]), **params
    )
    assert result["state"] == "ready"
    assert [row["id"] for row in result["messages"]] == [str(catchup["message_id"])]
    denied = console_read(console, "/api/room", session="0" * 64, room=room)
    assert denied == {"state": "not_found"}


@pytest.mark.scenario(
    "NS-15.03", "Client Board publish authorization and revision conflict"
)
def test_client_board_publish_permissions_and_conflict(client_workspace):
    api, _, console, _, password, local, peer = client_workspace
    console_login(console, "alice", password)
    session = room_session(console, local)
    remote_room = api.create_room(peer, "Remote Board")
    api.join(local, remote_room)
    remote = {"session": session, "room": remote_room}
    inspected = console_read(console, ROOM_BOARD, **remote)["board"]
    assert inspected["can_edit"] is False
    text = {"rules": "Share reviewed drafts.", "protocol": "Post concise updates."}
    rejected = publish(console, remote, {"version": inspected["version"], **text})
    assert rejected.status_code == 403
    assert rejected.json() == {"state": "forbidden"}

    owned = api.create_room(local, "Local Board")
    params = {"session": session, "room": owned}
    board = console_read(console, ROOM_BOARD, **params)["board"]
    assert board["can_edit"] is True
    draft = {"version": board["version"], **text}
    assert console.post(ROOM_BOARD, params=params, json=draft).status_code == 403
    response = publish(console, params, draft)
    assert response.status_code == 200
    assert response.json() == {
        "state": "ready",
        "board": {
            **text,
            "version": board["version"] + 1,
            "room_id": owned,
            "can_edit": True,
        },
    }
    conflict = publish(console, params, {**draft, "rules": "Conflicting edit"})
    assert conflict.status_code == 409
    assert conflict.json() == {"state": "conflict"}
    assert console_read(console, ROOM_BOARD, **params) == response.json()


@pytest.mark.scenario("NS-15.04", "Active conversation stale-Board gate and recovery")
def test_client_board_revision_pauses_and_restores_live_conversation(client_workspace):
    api, _, console, _, password, local, peer = client_workspace
    console_login(console, "alice", password)
    room = api.create_room(local, "Active conversation", rules="Share reviewed drafts.")
    api.join(peer, room)
    for member in (local, peer):
        assert api.read_and_ack_board(member, room)["version"] == 1
    first = api.send(local, room, "First instruction")
    second = api.send(peer, room, "Received before the revision")
    messages_path = f"/v1/rooms/{room}/messages"
    board_path = f"/v1/rooms/{room}/board"
    params = {"session": room_session(console, local), "room": room}

    assert api.history(peer, room) == [first, second]
    assert [
        row["id"] for row in console_read(console, "/api/room", **params)["messages"]
    ] == [str(first["message_id"]), str(second["message_id"])]
    (pending,) = api.call("GET", "/v1/inbox", peer.token)["events"]
    assert pending["message"]["message_id"] == first["message_id"]

    draft = board_draft(
        console, params, rules="Share final drafts.", protocol="Post test outcomes."
    )
    published = publish(console, params, draft)
    assert published.status_code == 200
    assert published.json()["board"]["version"] == 2

    for member in (local, peer):
        assert (
            api.call("GET", messages_path, member.token, error="board_ack_required")
            == 409
        )
        assert (
            api.call(
                "POST",
                f"{board_path}/ack",
                member.token,
                json={"version": 2},
                error="board_read_required",
            )
            == 409
        )
    attempt = {
        "client_message_id": "after-board",
        "text": "Continue after re-acknowledging",
    }
    assert (
        api.call(
            "POST", messages_path, peer.token, json=attempt, error="board_ack_required"
        )
        == 409
    )
    assert api.call("GET", "/v1/inbox", peer.token)["events"] == []
    gated = console_read(console, "/api/room", **params)
    assert gated["state"] == "board_ack_required"
    assert gated["messages"] == []
    assert (gated["board"]["current"], gated["board"]["acknowledged"]) == (2, 1)

    current = api.call("GET", board_path, peer.token)
    assert (current["version"], current["rules"], current["protocol"]) == (
        2,
        draft["rules"],
        draft["protocol"],
    )
    assert api.call("GET", messages_path, peer.token, error="board_ack_required") == 409
    assert api.call("POST", f"{board_path}/ack", peer.token, json={"version": 2}) == {
        "version": 2
    }
    assert api.call("GET", "/v1/inbox", peer.token)["events"] == [pending]
    resumed = api.call("POST", messages_path, peer.token, json=attempt)
    assert api.history(peer, room) == [first, second, resumed]
    assert console_read(console, "/api/room", **params)["state"] == "board_ack_required"

    api.read_and_ack_board(local, room)
    transcript = console_read(console, "/api/room", **params)
    assert transcript["state"] == "ready"
    assert [row["id"] for row in transcript["messages"]] == [
        str(message["message_id"]) for message in (first, second, resumed)
    ]


@pytest.mark.scenario("NS-15.05", "Browser Client sign-in")
def test_browser_client_signs_in(client_workspace):
    _, _, console, url, password, local, _ = client_workspace
    with browser_page(
        str(console.base_url), redact=(password, url.split("#", 1)[1])
    ) as (page, expect):
        page.goto(url, wait_until="domcontentloaded")
        heading = page.get_by_role("heading", name="Sign in to TeamUp")
        expect(heading).to_be_visible()
        page.get_by_label("Username").fill("alice")
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(heading).to_be_hidden()
        room_session(console, local)


@pytest.mark.scenario("NS-15.06", "Browser room Board editing and live transcript")
def test_browser_client_publishes_board(client_workspace):
    api, _, console, url, password, local, peer = client_workspace
    console_login(console, "alice", password)
    room = api.create_room(
        local, "Browser Board", rules="Share drafts.", protocol="Read the draft."
    )
    api.join(peer, room)
    for member in (local, peer):
        api.read_and_ack_board(member, room)
    api.send(peer, room, "Review is in progress")

    with browser_page(
        str(console.base_url), redact=(password, url.split("#", 1)[1])
    ) as (page, expect):
        page.goto(url, wait_until="domcontentloaded")
        page.locator(f'#rooms button[data-room="{room}"]').click()
        expect(page.locator("#messages")).to_contain_text("Review is in progress")

        board = page.get_by_role("button", name="Board", exact=True)
        expect(board).to_have_attribute("aria-expanded", "false")
        expect(page.locator("#board-status")).to_contain_text("Version 1")
        board.click()
        expect(page.locator("#board-rules")).to_have_text("Share drafts.")
        page.get_by_role("button", name="Edit board").click()
        page.get_by_label("Rules", exact=True).fill("Share the final review.")
        page.get_by_label("Protocol", exact=True).fill("Confirm test results.")
        page.get_by_role("button", name="Publish board").click()
        expect(page.locator("#board-status")).to_contain_text("Version 2")
        expect(page.locator("#board-rules")).to_have_text("Share the final review.")
        expect(page.locator("#chat-status")).to_contain_text(
            "must read and acknowledge", timeout=12000
        )
        expect(page.locator("#messages")).not_to_contain_text("Review is in progress")

        api.read_and_ack_board(local, room)
        expect(page.locator("#messages")).to_contain_text(
            "Review is in progress", timeout=12000
        )


@pytest.mark.scenario("NS-15.07", "Browser task lanes track first join and finish")
def test_browser_client_moves_tasks_between_lanes(client_workspace):
    api, _, console, url, password, local, peer = client_workspace
    console_login(console, "alice", password)
    room = api.create_room(peer, "Task lane verification", rules="Join before work.")
    api.join(local, room)
    for member in (local, peer):
        api.read_and_ack_board(member, room)

    tasks = f"/v1/rooms/{room}/tasks"
    open_title = "Verify client release"
    assigned_title = "Review the release notes"
    api.call(
        "POST",
        tasks,
        peer.token,
        json={"client_task_id": "unclaimed", "title": open_title},
    )
    api.call(
        "POST",
        tasks,
        local.token,
        json={
            "client_task_id": "addressed",
            "title": assigned_title,
            "addressed_member_ids": [peer.member_id],
        },
    )

    with browser_page(
        str(console.base_url), redact=(password, url.split("#", 1)[1])
    ) as (page, expect):
        page.goto(url, wait_until="domcontentloaded")
        page.locator(f'#rooms button[data-room="{room}"]').click()
        page.get_by_role("button", name="Tasks", exact=True).click()
        available = page.get_by_role("region", name="Available tasks")
        taken = page.get_by_role("region", name="Taken tasks")
        ongoing = page.get_by_role("region", name="Ongoing tasks")

        expect(ongoing.get_by_role("heading")).to_have_text("Ongoing (0)")
        expect(available.get_by_role("heading")).to_have_text("Available (2)")
        expect(taken.get_by_role("heading")).to_have_text("Taken (0)")
        expect(available.get_by_text("Waiting for addressed members")).to_be_visible()
        assigned = available.locator("details").filter(has_text=assigned_title)
        assigned.get_by_text(assigned_title, exact=True).click()
        expect(assigned.get_by_text("None yet")).to_be_visible()
        expect(assigned.get_by_role("button", name="Join task")).to_have_count(0)

        unclaimed = available.locator("details").filter(has_text=open_title)
        unclaimed.get_by_text(open_title, exact=True).click()
        expect(unclaimed.get_by_text("None yet")).to_be_visible()
        unclaimed.get_by_role("button", name="Join task").click()

        expect(available.get_by_role("heading")).to_have_text("Available (1)")
        expect(taken.get_by_text(open_title, exact=True)).to_be_visible()
        expect(ongoing.get_by_role("heading")).to_have_text("Ongoing (1)")
        ongoing.get_by_text(open_title, exact=True).click()
        expect(ongoing.get_by_text("Local reviewer", exact=True)).to_be_visible()
        ongoing.get_by_role("button", name="Finish task").click()

        expect(taken.get_by_role("heading")).to_have_text("Taken (0)")
        expect(ongoing.get_by_role("heading")).to_have_text("Ongoing (0)")
        page.get_by_role("button", name="Finished", exact=True).click()
        finished = page.get_by_role("region", name="Finished tasks")
        expect(finished.get_by_role("heading")).to_have_text("Finished (1)")
        expect(finished.get_by_text(open_title, exact=True)).to_be_visible()
