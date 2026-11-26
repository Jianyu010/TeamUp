import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from threading import Event

from teamup_server import presence
from teamup_server.collaboration import Collaboration
from test_contract import (
    KEYS,
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
from test_server_accounts import PASSWORD, bind, login_token


def test_room_metadata_shows_current_members_with_pending_ack(tmp_path):
    db_path = tmp_path / "team.db"
    with api(db_path) as client:
        sender = enroll(client, "sender").json()
        target = enroll(client, "target", owner="bob").json()
        enroll(client, "outsider")
        path = room(client, history_policy="retained")
        room_id = path.split("/")[-1]
        confirm(client, "sender", path)
        join(client, "target", path)
        join(client, "target", path)

        assert get(client, "target", path).json() == {
            "room_id": room_id,
            "name": "  Build  ",
            "history_policy": "retained",
            "current_board_version": 1,
            "member_count": 2,
        }
        assert get(client, "target", f"{path}/members").json() == {
            "members": sorted(
                [
                    {**sender, "acked_board_version": 1},
                    {**target, "acked_board_version": None},
                ],
                key=lambda row: row["member_id"],
            )
        }
        error(get(client, "target", f"{path}/messages"), 409, "board_ack_required")
        error(
            post(client, "target", f"{path}/board/ack", json={"version": 1}),
            409,
            "board_read_required",
        )

        assert post(client, "target", f"{path}/leave").json() == {"room_id": room_id}
        for suffix in ("", "/members"):
            error(get(client, "target", path + suffix), 404, "not_found")
            error(get(client, "outsider", path + suffix), 404, "not_found")
            error(client.get(f"/v1{path}{suffix}"), 401, "unauthorized")
        assert get(client, "sender", path).json()["member_count"] == 1
        assert [
            m["member_id"]
            for m in get(client, "sender", f"{path}/members").json()["members"]
        ] == [sender["member_id"]]


def test_presence_uses_current_shared_rooms_and_self_only(tmp_path):
    db_path = tmp_path / "team.db"
    with api(db_path) as client:
        sender = enroll(client, "sender").json()["member_id"]
        peer = enroll(client, "peer", owner="bob").json()["member_id"]
        outsider = enroll(client, "outsider", owner="bob").json()["member_id"]
        for _ in range(2):
            join(client, "peer", room(client))
        unknown = {"state": "unknown", "observed_at": None, "age_ms": None}
        response = get(client, "sender", "/presence")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        assert set(snapshot) == {"as_of", "members"}
        assert datetime.fromisoformat(snapshot["as_of"]).utcoffset() == UTC.utcoffset(
            None
        )
        assert snapshot["members"] == [
            {"member_id": member_id, **unknown} for member_id in sorted([sender, peer])
        ]
        error(
            post(client, "peer", "/presence/heartbeat", json={"member_id": outsider}),
            422,
            "invalid_request",
        )
        error(
            client.post("/v1/presence/heartbeat", headers=auth(KEYS["bob"])),
            401,
            "unauthorized",
        )
        with ThreadPoolExecutor(max_workers=4) as workers:
            receipts = list(
                workers.map(
                    lambda _: post(client, "peer", "/presence/heartbeat"), range(8)
                )
            )
        assert all(receipt.status_code == 200 for receipt in receipts)
        assert all(set(receipt.json()) == {"observed_at"} for receipt in receipts)
        latest = max(
            (receipt.json()["observed_at"] for receipt in receipts),
            key=datetime.fromisoformat,
        )
        members = get(client, "sender", "/presence").json()["members"]
        peer_age = next(row["age_ms"] for row in members if row["member_id"] == peer)
        assert isinstance(peer_age, int) and 0 <= peer_age <= 45_000
        assert members == sorted(
            [
                {"member_id": sender, **unknown},
                {
                    "member_id": peer,
                    "state": "online",
                    "observed_at": latest,
                    "age_ms": peer_age,
                },
            ],
            key=lambda row: row["member_id"],
        )
        assert get(client, "outsider", "/presence").json()["members"] == [
            {"member_id": outsider, **unknown}
        ]
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DELETE FROM room_members WHERE member_id = ?", (peer,))
        assert get(client, "sender", "/presence").json()["members"] == [
            {"member_id": sender, **unknown}
        ]
    with api(db_path) as client:
        assert get(client, "peer", "/presence").json()["members"] == [
            {"member_id": peer, **unknown}
        ]


def test_disabled_account_immediately_loses_room_reads_and_presence(tmp_path):
    db_path = tmp_path / "team.db"
    owner = Collaboration(db_path, KEYS)
    owner.create_account("remote", PASSWORD)
    with api(db_path) as client:
        enroll(client, "sender")
        remote_token = login_token(client, "remote", PASSWORD)
        assert bind(client, remote_token, "peer").status_code == 200
        sender = member(client, "sender")
        path = room(client)
        join(client, "peer", path)
        assert post(client, "peer", "/presence/heartbeat").status_code == 200
        owner.disable_account("remote")
        for route in (path, f"{path}/members", "/presence"):
            error(get(client, "peer", route), 401, "unauthorized")
        error(post(client, "peer", "/presence/heartbeat"), 401, "unauthorized")
        assert get(client, "sender", "/presence").json()["members"] == [
            {
                "member_id": sender,
                "state": "unknown",
                "observed_at": None,
                "age_ms": None,
            }
        ]


def test_presence_expiry_and_renewal_use_monotonic_time(tmp_path, monkeypatch):
    collaboration = Collaboration(tmp_path / "team.db", KEYS)
    token = credential("peer")
    member_id = collaboration.enroll(
        KEYS["alice"],
        harness_instance_id="harness",
        session_key="session",
        name="Peer",
        description="Coordinates assigned work.",
        member_token=token,
    )["member_id"]
    observed = presence.Presence(collaboration)
    tick_ns = 10_000_000_000
    utc = "2026-04-01T12:00:00Z"
    monkeypatch.setattr(presence, "monotonic_ns", lambda: tick_ns)
    monkeypatch.setattr(presence, "_utc_now", lambda: utc)

    def check(state, observed_at, age_ms):
        assert observed.snapshot(token) == {
            "as_of": utc,
            "members": [
                {
                    "member_id": member_id,
                    "state": state,
                    "observed_at": observed_at,
                    "age_ms": age_ms,
                }
            ],
        }

    old_receipt = observed.heartbeat(token)["observed_at"]
    assert old_receipt == utc
    # A wall-clock correction must not shorten or extend the lease.
    check("online", old_receipt, 0)
    tick_ns += 44_998_125_000
    utc = "2026-04-01T11:58:00Z"
    check("online", old_receipt, 44_999)
    tick_ns = 55_000_000_000
    check("offline", old_receipt, 45_000)
    assert observed.heartbeat(token) == {"observed_at": utc}
    tick_ns += 44_000_000_000
    check("online", utc, 44_000)
    tick_ns += 1_000_000_000
    check("offline", utc, 45_000)


def test_slow_authorization_cannot_reorder_concurrent_heartbeat_receipts(
    tmp_path, monkeypatch
):
    collaboration = Collaboration(tmp_path / "team.db", KEYS)
    token = credential("peer")
    collaboration.enroll(
        KEYS["alice"],
        harness_instance_id="harness",
        session_key="session",
        name="Peer",
        description="Coordinates assigned work.",
        member_token=token,
    )
    observed = presence.Presence(collaboration)
    authorizing, resume = Event(), Event()
    get_member = collaboration.get_member
    tick_ns = 100_000_000_000
    utc = "2026-04-01T12:00:00Z"
    initial = utc

    def slow_first_lookup(token):
        if not authorizing.is_set():
            authorizing.set()
            assert resume.wait(5)
        return get_member(token)

    monkeypatch.setattr(collaboration, "get_member", slow_first_lookup)
    monkeypatch.setattr(presence, "monotonic_ns", lambda: tick_ns)
    monkeypatch.setattr(presence, "_utc_now", lambda: utc)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(observed.heartbeat, token)
        try:
            assert authorizing.wait(5)
            tick_ns += 2_000_000_000
            utc = "2026-04-01T12:00:02Z"
            assert observed.heartbeat(token) == {"observed_at": utc}
        finally:
            resume.set()
        assert first.result(timeout=5) == {"observed_at": initial}
    assert observed.snapshot(token)["members"][0]["observed_at"] == utc
