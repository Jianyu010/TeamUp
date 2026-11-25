import hashlib
import json
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
    # Earlier notices are never handed to a harness after an upgrade.
    "harness_state": "TEXT DEFAULT 'skipped'",
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
            # DM notices keep their own typed, body-free rows: they never enter
            # the mention delivery or wake machinery.
            db.execute("""
                CREATE TABLE IF NOT EXISTS dm_notices (
                    binding_scope TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    dm_id TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS sessions (
                    harness_instance_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    member_token TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'approved', 'rejected')),
                    PRIMARY KEY (harness_instance_id, session_key)
                ) WITHOUT ROWID
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS wake_permissions (
                    harness_instance_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    rooms TEXT NOT NULL,
                    senders TEXT NOT NULL,
                    PRIMARY KEY (harness_instance_id, session_key)
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
        fields = {
            key: value for key, value in notice.items() if key not in ("kind", "scope")
        }
        with self._db:
            if notice.get("kind") == "dm":
                result = self._db.execute(
                    """
                    INSERT INTO dm_notices (
                        binding_scope, event_id, dm_id, message_id, sender_member_id
                    ) VALUES (:scope, :event_id, :dm_id, :message_id, :sender_member_id)
                    ON CONFLICT (binding_scope, event_id) DO NOTHING
                    """,
                    {"scope": scope, **fields},
                )
            else:
                result = self._db.execute(
                    """
                    INSERT INTO notices (
                        binding_scope, event_id, room_id, message_id, sender_member_id,
                        state, attempts, preview, harness_state
                    ) VALUES (
                        :scope, :event_id, :room_id, :message_id, :sender_member_id,
                        'pending', 0, :preview, NULL
                    )
                    ON CONFLICT (binding_scope, event_id) DO NOTHING
                    """,
                    {"scope": scope, "preview": preview, **fields},
                )
        return result.rowcount == 1

    def page(self, scope, after, limit):
        if after >= 2**63 - 1:
            return []
        cursor = self._db.execute(
            """
            SELECT event_id, room_id, dm_id, message_id, sender_member_id
            FROM (
                SELECT event_id, room_id, NULL AS dm_id, message_id, sender_member_id
                FROM notices WHERE binding_scope = ? AND event_id > ?
                UNION ALL
                SELECT event_id, NULL AS room_id, dm_id, message_id, sender_member_id
                FROM dm_notices WHERE binding_scope = ? AND event_id > ?
            ) ORDER BY event_id LIMIT ?
            """,
            (scope, after, scope, after, limit),
        )
        rows = []
        for event_id, room_id, dm_id, message_id, sender in cursor:
            if dm_id is None:
                rows.append(
                    {
                        "event_id": event_id,
                        "room_id": room_id,
                        "message_id": message_id,
                        "sender_member_id": sender,
                    }
                )
            else:
                rows.append(
                    {
                        "event_id": event_id,
                        "kind": "dm",
                        "dm_id": dm_id,
                        "message_id": message_id,
                        "sender_member_id": sender,
                    }
                )
        return rows

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
            "SELECT state, attempts, harness_state FROM notices "
            "WHERE binding_scope = ? AND event_id = ?",
            (scope, event_id),
        ).fetchone()
        return row and {"state": row[0], "attempts": row[1], "harness_state": row[2]}

    def undecided(self, scope, limit=100):
        """Mentions not yet decided for the harness, oldest first."""
        cursor = self._db.execute(
            """
            SELECT event_id, room_id, message_id, sender_member_id
            FROM notices
            WHERE binding_scope = ?
                AND (harness_state IS NULL OR harness_state = 'waiting')
            ORDER BY event_id LIMIT ?
            """,
            (scope, limit),
        )
        return [dict(zip(_FIELDS, row, strict=True)) for row in cursor]

    def set_harness_state(self, scope, event_id, state):
        with self._db:
            self._db.execute(
                "UPDATE notices SET harness_state = ? "
                "WHERE binding_scope = ? AND event_id = ?",
                (state, scope, event_id),
            )

    def abandon_submitting(self, scope):
        """A submission that never settled has an unknown result: never resend."""
        with self._db:
            self._db.execute(
                "UPDATE notices SET harness_state = 'unknown' "
                "WHERE binding_scope = ? AND harness_state = 'submitting'",
                (scope,),
            )

    def session(self, harness_instance_id, session_key):
        row = self._db.execute(
            "SELECT member_token, state FROM sessions "
            "WHERE harness_instance_id = ? AND session_key = ?",
            (harness_instance_id, session_key),
        ).fetchone()
        if row is None:
            return None
        return {
            "harness_instance_id": harness_instance_id,
            "session_key": session_key,
            "member_token": row[0],
            "state": row[1],
        }

    def add_session(self, harness_instance_id, session_key, member_token, state):
        with self._db:
            self._db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?) "
                "ON CONFLICT (harness_instance_id, session_key) DO NOTHING",
                (harness_instance_id, session_key, member_token, state),
            )
        return self.session(harness_instance_id, session_key)

    def decide_session(self, harness_instance_id, session_key, state):
        with self._db:
            result = self._db.execute(
                "UPDATE sessions SET state = ? "
                "WHERE harness_instance_id = ? AND session_key = ?",
                (state, harness_instance_id, session_key),
            )
        return result.rowcount == 1

    def sessions(self):
        cursor = self._db.execute(
            "SELECT harness_instance_id, session_key, member_token, state "
            "FROM sessions ORDER BY harness_instance_id, session_key"
        )
        fields = ("harness_instance_id", "session_key", "member_token", "state")
        return [dict(zip(fields, row, strict=True)) for row in cursor]

    def set_wake(self, harness_instance_id, session_key, rooms, senders):
        with self._db:
            if rooms:
                self._db.execute(
                    "INSERT OR REPLACE INTO wake_permissions VALUES (?, ?, ?, ?)",
                    (
                        harness_instance_id,
                        session_key,
                        json.dumps(sorted(rooms)),
                        json.dumps(sorted(senders)),
                    ),
                )
            else:
                self._db.execute(
                    "DELETE FROM wake_permissions "
                    "WHERE harness_instance_id = ? AND session_key = ?",
                    (harness_instance_id, session_key),
                )

    def wake(self, harness_instance_id, session_key):
        row = self._db.execute(
            "SELECT rooms, senders FROM wake_permissions "
            "WHERE harness_instance_id = ? AND session_key = ?",
            (harness_instance_id, session_key),
        ).fetchone()
        return row and {"rooms": json.loads(row[0]), "senders": json.loads(row[1])}

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
