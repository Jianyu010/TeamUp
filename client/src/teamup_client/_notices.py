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

    def record(self, scope, notice):
        with self._db:
            result = self._db.execute(
                """
                INSERT INTO notices (
                    binding_scope, event_id, room_id, message_id, sender_member_id
                ) VALUES (:scope, :event_id, :room_id, :message_id, :sender_member_id)
                ON CONFLICT (binding_scope, event_id) DO NOTHING
                """,
                {"scope": scope, **notice},
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
        fields = ("event_id", "room_id", "message_id", "sender_member_id")
        return [dict(zip(fields, row, strict=True)) for row in cursor]
