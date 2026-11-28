import sqlite3
import stat
from contextlib import closing
from dataclasses import replace

import pytest
from teamup_client._local import LocalError
from teamup_client._room_store import RoomChange, RoomScope, open_room_store


@pytest.fixture
def socket_path(tmp_path):
    directory = tmp_path / "profile"
    directory.mkdir(mode=0o700)
    return directory / "client.sock"


@pytest.fixture
def scope():
    return RoomScope("server", "generation", "account", "member", "room", "membership")


def message(message_id, text):
    return RoomChange("messages", message_id, {"text": text})


def test_reopens_with_committed_records_and_cursor(socket_path, scope):
    first = message("88", "Keep this message")
    expired = message("95", "Remove this message")
    with open_room_store(socket_path) as store:
        initial = store.begin(scope)
        saved = store.replace(scope, initial, "c42", [first, expired])
        assert saved is not None
        assert store.apply(scope, saved, "c44", [RoomChange("messages", "95", None)])

    with open_room_store(socket_path) as store:
        state = store.load(scope)
        assert state is not None
        assert state.cursor == "c44"
        assert state.records == (first,)


def test_empty_page_advances_cursor_and_preserves_records(socket_path, scope):
    row = message("88", "Keep this message")
    with open_room_store(socket_path) as store:
        saved = store.replace(scope, store.begin(scope), "c42", [row])
        assert saved is not None
        advanced = store.apply(scope, saved, "c44", [])
        state = store.load(scope)
        assert state is not None
        assert advanced == state.revision
        assert advanced != saved
        assert state.cursor == "c44"
        assert state.records == (row,)


def test_stale_revision_cannot_overwrite_a_later_batch(socket_path, scope):
    with open_room_store(socket_path) as first, open_room_store(socket_path) as second:
        initial = first.begin(scope)
        assert second.begin(scope) == initial
        assert first.apply(scope, initial, "c1", [message("1", "Committed")])
        assert second.apply(scope, initial, "stale", [message("2", "Too late")]) is None
        state = second.load(scope)
        assert state is not None
        assert state.cursor == "c1"
        assert state.records == (message("1", "Committed"),)


def test_invalidation_prevents_delayed_snapshot_from_restoring_rows(socket_path, scope):
    with open_room_store(socket_path) as store:
        initial = store.replace(
            scope, store.begin(scope), "c0", [message("0", "Old projection")]
        )
        assert initial is not None
        store.invalidate(scope)
        assert store.load(scope) is None
        assert store.apply(scope, initial, "c1", [message("1", "Old")]) is None
        reopened = store.begin(scope)
        assert reopened != initial
        state = store.load(scope)
        assert state is not None
        assert state.records == ()
        assert store.replace(scope, initial, "c1", [message("1", "Old")]) is None
        assert store.replace(scope, reopened, "c2", [])
        state = store.load(scope)
        assert state is not None
        assert state.cursor == "c2"
        assert state.records == ()


@pytest.mark.parametrize(
    "field",
    [
        "server_profile",
        "server_generation",
        "account_id",
        "member_id",
        "room_id",
        "membership_id",
    ],
)
def test_scope_isolates_data_and_revisions(socket_path, scope, field):
    other = replace(scope, **{field: "different"})
    row = message("1", "Private")
    with open_room_store(socket_path) as store:
        saved = store.replace(scope, store.begin(scope), "c1", [row])
        assert saved is not None
        assert store.load(other) is None
        store.begin(other)
        assert store.apply(other, saved, "c2", [row]) is None
        store.invalidate(other)
        state = store.load(scope)
        assert state is not None
        assert state.records == (row,)


def test_failed_batch_rolls_back_records_and_cursor(socket_path, scope):
    row = message("1", "Committed")
    with open_room_store(socket_path) as store:
        saved = store.replace(scope, store.begin(scope), "c1", [row])
        assert saved is not None
        database = socket_path.with_name("client.sock.rooms.sqlite3")
        with closing(sqlite3.connect(database)) as fault_connection:
            fault_connection.execute("""
                CREATE TRIGGER reject_record BEFORE INSERT ON room_records
                WHEN NEW.entity_id = '3'
                BEGIN
                    SELECT RAISE(ABORT, 'injected record write failure');
                END
            """)
        with pytest.raises(
            sqlite3.IntegrityError, match="injected record write failure"
        ):
            store.replace(
                scope,
                saved,
                "c2",
                [message("2", "Partial"), message("3", "Rejected")],
            )
        state = store.load(scope)
        assert state is not None
        assert state.revision == saved
        assert state.cursor == "c1"
        assert state.records == (row,)


def test_database_is_private(socket_path):
    with open_room_store(socket_path):
        path = socket_path.with_name("client.sock.rooms.sqlite3")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(LocalError, match="private"), open_room_store(socket_path):
        pytest.fail("opened a non-private room database")


def test_change_iterator_cannot_commit_a_partial_replacement(socket_path, scope):
    row = message("1", "Committed")
    with open_room_store(socket_path) as store:
        saved = store.replace(scope, store.begin(scope), "c1", [row])
        assert saved is not None

        def interrupted_page():
            yield message("2", "Partial")
            store.begin(replace(scope, room_id="other-room"))
            raise ValueError("Incomplete page")

        with pytest.raises(ValueError, match="Incomplete page"):
            store.replace(scope, saved, "c2", interrupted_page())
        state = store.load(scope)
        assert state is not None
        assert state.revision == saved
        assert state.cursor == "c1"
        assert state.records == (row,)
