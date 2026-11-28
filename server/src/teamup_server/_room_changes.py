"""SQLite change storage owned by Collaboration."""

import sqlite3
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as DecodeError
from hashlib import sha256
from hmac import compare_digest, digest
from os import stat_result
from secrets import token_bytes
from uuid import uuid4

_GROUP_RETENTION = 10_000
_PAGE_GROUPS = 100

_SCHEMA = (
    """
    CREATE TABLE server_sync_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        generation TEXT NOT NULL,
        cursor_secret BLOB NOT NULL,
        database_device TEXT NOT NULL,
        database_inode TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE room_membership_revisions (
        room_id TEXT NOT NULL,
        member_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        PRIMARY KEY (room_id, member_id)
    ) WITHOUT ROWID
    """,
    """
    INSERT INTO room_membership_revisions (room_id, member_id, revision)
    SELECT room_id, member_id, 1 FROM room_members
    """,
    """
    CREATE TABLE room_change_transaction (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        group_key TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE room_change_heads (
        room_id TEXT PRIMARY KEY,
        position INTEGER NOT NULL,
        group_key TEXT NOT NULL,
        last_reset INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE room_changes (
        room_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        collection TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        created INTEGER NOT NULL,
        PRIMARY KEY (room_id, position, collection, entity_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE VIEW record_room_change AS
    SELECT room_id, collection, entity_id, created FROM room_changes WHERE 0
    """,
    f"""
    CREATE TRIGGER room_change_record INSTEAD OF INSERT ON record_room_change BEGIN
        INSERT INTO room_change_heads (room_id, position, group_key)
        VALUES (NEW.room_id, 1, COALESCE(
            (SELECT group_key FROM room_change_transaction WHERE singleton = 1),
            lower(hex(randomblob(16)))
        ))
        ON CONFLICT (room_id) DO UPDATE SET
            position = position + 1, group_key = excluded.group_key
        WHERE group_key <> excluded.group_key;

        UPDATE room_change_heads SET last_reset = position
        WHERE room_id = NEW.room_id AND NEW.collection = 'reset';

        INSERT INTO room_changes (room_id, position, collection, entity_id, created)
        SELECT NEW.room_id, position, NEW.collection, NEW.entity_id, COALESCE(NEW.created, 0)
        FROM room_change_heads WHERE room_id = NEW.room_id
        ON CONFLICT (room_id, position, collection, entity_id) DO UPDATE SET
            created = MAX(created, excluded.created);

        DELETE FROM room_changes WHERE room_id = NEW.room_id AND position <= (
            SELECT position - {_GROUP_RETENTION}
            FROM room_change_heads WHERE room_id = NEW.room_id
        );
    END
    """,
)


def _changes(room: str, *entities: tuple[str, str]) -> str:
    return "\n".join(
        "INSERT INTO record_room_change (room_id, collection, entity_id) "
        f"VALUES ({room}, '{collection}', {entity});"
        for collection, entity in entities
    )


def _membership_change(row: str) -> str:
    return f"""
        INSERT INTO room_membership_revisions (room_id, member_id, revision)
        VALUES ({row}.room_id, {row}.member_id, 1)
        ON CONFLICT (room_id, member_id) DO UPDATE SET revision = revision + 1;
        {_changes(f"{row}.room_id", ("roster", f"{row}.member_id"), ("room", f"{row}.room_id"))}
    """


def _visibility_change(row: str) -> str:
    return f"""
        INSERT INTO record_room_change (room_id, collection, entity_id)
        SELECT t.room_id, 'reset', '' FROM tasks AS t WHERE task_id = {row}.task_id;
    """


_TRIGGERS = (
    (
        "room_insert",
        "INSERT ON rooms",
        "",
        _changes("NEW.room_id", ("room", "NEW.room_id"), ("board", "NEW.room_id")),
    ),
    (
        "room_update",
        "UPDATE OF name, archived_at, expired_through_message_id ON rooms",
        "",
        _changes("NEW.room_id", ("room", "NEW.room_id")),
    ),
    (
        "board_update",
        "UPDATE OF version, rules, protocol ON rooms",
        "",
        _changes("NEW.room_id", ("room", "NEW.room_id"), ("board", "NEW.room_id")),
    ),
    (
        "history_policy_update",
        "UPDATE OF history_policy ON rooms",
        "WHEN NEW.history_policy IS NOT OLD.history_policy",
        _changes("NEW.room_id", ("reset", "''")),
    ),
    ("membership_insert", "INSERT ON room_members", "", _membership_change("NEW")),
    ("membership_delete", "DELETE ON room_members", "", _membership_change("OLD")),
    (
        "membership_update",
        "UPDATE OF acked_board_version, responsibility ON room_members",
        "",
        _changes("NEW.room_id", ("roster", "NEW.member_id")),
    ),
    (
        "membership_floor_update",
        "UPDATE OF joined_after ON room_members",
        "WHEN NEW.joined_after IS NOT OLD.joined_after",
        _changes("NEW.room_id", ("reset", "''")),
    ),
    (
        "leadership_insert",
        "INSERT ON room_leadership",
        "",
        _changes("NEW.room_id", ("board", "NEW.room_id")),
    ),
    (
        "leadership_update",
        "UPDATE OF leader_member_id ON room_leadership",
        "WHEN NEW.leader_member_id IS NOT OLD.leader_member_id",
        _changes("NEW.room_id", ("board", "NEW.room_id")),
    ),
    (
        "child_insert",
        "INSERT ON child_rooms",
        "",
        _changes("NEW.room_id", ("room", "NEW.room_id")),
    ),
    (
        "child_expiry_update",
        "UPDATE OF expires_us ON child_rooms",
        "WHEN NEW.expires_us IS NOT OLD.expires_us",
        _changes("NEW.room_id", ("room", "NEW.room_id")),
    ),
    *(
        (
            f"message_{event.lower()}",
            f"{event} ON messages",
            f"WHEN {row}.room_id IS NOT NULL",
            _changes(f"{row}.room_id", ("messages", f"CAST({row}.message_id AS TEXT)")),
        )
        for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD"))
    ),
    (
        "task_insert",
        "INSERT ON tasks",
        "",
        """
        INSERT INTO record_room_change (room_id, collection, entity_id, created)
        VALUES (NEW.room_id, 'tasks', NEW.task_id, 1);
        """,
    ),
    (
        "task_update",
        "UPDATE ON tasks",
        "",
        _changes("NEW.room_id", ("tasks", "NEW.task_id")),
    ),
    (
        "task_delete",
        "DELETE ON tasks",
        "",
        _changes("OLD.room_id", ("reset", "''")),
    ),
    (
        "task_addressee_insert",
        "INSERT ON task_addressees",
        "",
        """
        INSERT INTO record_room_change (room_id, collection, entity_id)
        SELECT t.room_id, 'reset', '' FROM tasks AS t WHERE t.task_id = NEW.task_id
        AND NOT EXISTS (
            SELECT 1 FROM room_change_transaction AS tx
            JOIN room_change_heads AS head ON head.group_key = tx.group_key AND head.room_id = t.room_id
            JOIN room_changes AS change ON change.room_id = head.room_id AND change.position = head.position
            WHERE change.collection = 'tasks' AND change.entity_id = NEW.task_id AND change.created = 1
        );
        """,
    ),
    (
        "task_addressee_delete",
        "DELETE ON task_addressees",
        "",
        _visibility_change("OLD"),
    ),
    (
        "task_addressee_update",
        "UPDATE ON task_addressees",
        "",
        _visibility_change("OLD") + _visibility_change("NEW"),
    ),
)


def initialize_room_changes(connection: sqlite3.Connection, stat: stat_result) -> None:
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'server_sync_metadata'"
    ).fetchone()
    if existing is None:
        for statement in _SCHEMA:
            connection.execute(statement)
    for name, event, condition, changes in _TRIGGERS:
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS sync_{name} AFTER {event} {condition} BEGIN {changes} END"
        )
    # A backup stores the source file identity. A restored copy acquires a new
    # identity when published. Checking it before serving also covers a restored
    # file moved into the original path while the Server was stopped.
    connection.execute(
        """
        INSERT INTO server_sync_metadata VALUES (1, ?, ?, ?, ?)
        ON CONFLICT (singleton) DO UPDATE SET generation = excluded.generation,
            cursor_secret = excluded.cursor_secret, database_device = excluded.database_device,
            database_inode = excluded.database_inode
        WHERE database_device <> excluded.database_device OR database_inode <> excluded.database_inode
        """,
        (uuid4().hex, token_bytes(32), str(stat.st_dev), str(stat.st_ino)),
    )


def rotate_generation(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE server_sync_metadata SET generation = ?, cursor_secret = ? WHERE singleton = 1",
        (uuid4().hex, token_bytes(32)),
    )


def server_generation(connection: sqlite3.Connection) -> str:
    return str(
        connection.execute("SELECT generation FROM server_sync_metadata").fetchone()[0]
    )


def membership_revision(
    connection: sqlite3.Connection, room_id: str, member_id: str
) -> int:
    row = connection.execute(
        "SELECT revision FROM room_membership_revisions WHERE room_id = ? AND member_id = ?",
        (room_id, member_id),
    ).fetchone()
    return 0 if row is None else int(row[0])


def begin_room_change_group(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO room_change_transaction VALUES (1, ?)", (uuid4().hex,)
    )


def end_room_change_group(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM room_change_transaction WHERE singleton = 1")


def room_change_head(connection: sqlite3.Connection, room_id: str) -> tuple[int, int]:
    row = connection.execute(
        "SELECT position, last_reset FROM room_change_heads WHERE room_id = ?",
        (room_id,),
    ).fetchone()
    return (0, 0) if row is None else (int(row[0]), int(row[1]))


def cursor_key(connection: sqlite3.Connection, *scope: str) -> bytes:
    secret = connection.execute(
        "SELECT cursor_secret FROM server_sync_metadata"
    ).fetchone()[0]
    return digest(secret, "\x00".join(scope).encode(), sha256)


def encode_cursor(key: bytes, position: int) -> str:
    value = position.to_bytes(8, "big")
    return urlsafe_b64encode(value + digest(key, value, sha256)).decode().rstrip("=")


def decode_cursor(key: bytes, cursor: str, head: int, last_reset: int) -> int | None:
    if len(cursor) != 54:
        return None
    try:
        data = b64decode(cursor + "==", altchars=b"-_", validate=True)
    except (DecodeError, ValueError):
        return None
    position = int.from_bytes(data[:8], "big")
    if not (max(last_reset, head - _GROUP_RETENTION) <= position <= head):
        return None
    if not compare_digest(cursor, encode_cursor(key, position)):
        return None
    return position


def room_change_page(
    connection: sqlite3.Connection, room_id: str, position: int, head: int
) -> tuple[int, list[sqlite3.Row]]:
    # Positions are contiguous per room. The page never splits a transaction.
    through = min(position + _PAGE_GROUPS, head)
    rows = connection.execute(
        """
        SELECT collection, entity_id FROM room_changes
        WHERE room_id = ? AND position > ? AND position <= ?
        GROUP BY collection, entity_id ORDER BY MIN(position), collection, entity_id
        """,
        (room_id, position, through),
    ).fetchall()
    return through, rows
