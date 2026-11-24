import hashlib
import os
import sqlite3
from contextlib import contextmanager

from ._local import private_file
from ._protocol import frame


def binding_scope(server_url, binding):
    # The stable binding survives rotation of its local handle, but a new member
    # token or server does not inherit an earlier member's notifications.
    identity = [
        server_url,
        binding.harness_instance_id,
        binding.session_key,
        binding.member_token,
    ]
    return hashlib.sha256(frame(identity)).hexdigest()


@contextmanager
def open_store(socket_path):
    path = socket_path.with_name(f"{socket_path.name}.notices.sqlite3")
    os.close(private_file(path, create=True))
    db = sqlite3.connect(path)
    try:
        yield NoticeStore(db)
    finally:
        db.close()


PENDING, SHOWN, DELIVERED, FAILED = "pending", "shown", "delivered", "failed"
# Notices recorded before delivery state existed already reached stderr, so the
# upgrade marks them shown instead of replaying a burst of desktop notices.
_DELIVERY_COLUMNS = {
    "state": "TEXT NOT NULL DEFAULT 'shown' "
    "CHECK (state IN ('pending', 'shown', 'delivered', 'failed'))",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "preview": "TEXT NOT NULL DEFAULT ''",
}
_FIELDS = ("event_id", "room_id", "message_id", "sender_member_id")


class NoticeStore:
    def __init__(self, db):
        self._db = db
        with db:
            db.execute("PRAGMA synchronous = FULL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS notices (
                    binding_scope TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    room_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_member_id TEXT NOT NULL,
                    PRIMARY KEY (binding_scope, event_id)
                ) WITHOUT ROWID
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(notices)")}
            for name, definition in _DELIVERY_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE notices ADD COLUMN {name} {definition}")
            # Display names only label desktop notices; they never authorize.
            db.execute("""
                CREATE TABLE IF NOT EXISTS room_names (
                    binding_scope TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    PRIMARY KEY (binding_scope, room_id)
                ) WITHOUT ROWID
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS member_roles (
                    binding_scope TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    role_description TEXT NOT NULL,
                    PRIMARY KEY (binding_scope, member_id)
                ) WITHOUT ROWID
            """)

    def record(self, scope, notice, preview=""):
        with self._db:
            result = self._db.execute(
                """
                INSERT INTO notices (
                    binding_scope, event_id, room_id, message_id, sender_member_id,
                    state, attempts, preview
                ) VALUES (
                    :scope, :event_id, :room_id, :message_id, :sender_member_id,
                    'pending', 0, :preview
                )
                ON CONFLICT (binding_scope, event_id) DO NOTHING
                """,
                {"scope": scope, "preview": preview, **notice},
            )
        return result.rowcount == 1

    def page(self, scope, after, limit):
        if after >= 2**63 - 1:
            return []
        cursor = self._db.execute(
            """
            SELECT event_id, room_id, message_id, sender_member_id
            FROM notices WHERE binding_scope = ? AND event_id > ?
            ORDER BY event_id LIMIT ?
            """,
            (scope, after, limit),
        )
        return [dict(zip(_FIELDS, row, strict=True)) for row in cursor]

    def mark_delivered(self, scope, event_ids):
        with self._db:
            self._db.executemany(
                "UPDATE notices SET state = 'delivered' "
                "WHERE binding_scope = ? AND event_id = ?",
                [(scope, event_id) for event_id in event_ids],
            )

    def unread(self, scope):
        (count,) = self._db.execute(
            "SELECT count(*) FROM notices "
            "WHERE binding_scope = ? AND state != 'delivered'",
            (scope,),
        ).fetchone()
        return count

    def state(self, scope, event_id):
        row = self._db.execute(
            "SELECT state, attempts FROM notices "
            "WHERE binding_scope = ? AND event_id = ?",
            (scope, event_id),
        ).fetchone()
        return row and {"state": row[0], "attempts": row[1]}

    def abandon_unsettled(self, scope):
        """An attempt that never settled has an unknown result: do not repeat it."""
        with self._db:
            self._db.execute(
                "UPDATE notices SET state = 'failed' "
                "WHERE binding_scope = ? AND state = 'pending' AND attempts > 0",
                (scope,),
            )

    def next_pending(self, scope):
        row = self._db.execute(
            """
            SELECT event_id, room_id, message_id, sender_member_id, preview
            FROM notices
            WHERE binding_scope = ? AND state = 'pending' AND attempts = 0
            ORDER BY event_id LIMIT 1
            """,
            (scope,),
        ).fetchone()
        return row and dict(zip((*_FIELDS, "preview"), row, strict=True))

    def begin_attempt(self, scope, event_id):
        with self._db:
            self._db.execute(
                "UPDATE notices SET attempts = attempts + 1 "
                "WHERE binding_scope = ? AND event_id = ?",
                (scope, event_id),
            )

    def settle(self, scope, event_id, shown):
        with self._db:
            self._db.execute(
                "UPDATE notices SET state = ? "
                "WHERE binding_scope = ? AND event_id = ? AND state = 'pending'",
                (SHOWN if shown else FAILED, scope, event_id),
            )

    def remember_room(self, scope, room_id, name):
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO room_names VALUES (?, ?, ?)",
                (scope, room_id, name),
            )

    def room_name(self, scope, room_id):
        row = self._db.execute(
            "SELECT name FROM room_names WHERE binding_scope = ? AND room_id = ?",
            (scope, room_id),
        ).fetchone()
        return row and row[0]

    def remember_members(self, scope, members):
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO member_roles VALUES (?, ?, ?)",
                [
                    (scope, member["member_id"], member["role_description"])
                    for member in members
                ],
            )

    def member_role(self, scope, member_id):
        row = self._db.execute(
            "SELECT role_description FROM member_roles "
            "WHERE binding_scope = ? AND member_id = ?",
            (scope, member_id),
        ).fetchone()
        return row and row[0]
