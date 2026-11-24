"""Transactional owner of TeamUp collaboration state."""

import logging
import os
import re
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from functools import partial
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from threading import Lock
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)
_SQLITE_MAX_ID = (1 << 63) - 1
_DEFAULT_RULES = "Share only information authorized for this room."
_DEFAULT_PROTOCOL = (
    "Coordinate work in the room and direct mentions to the intended teammate."
)
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43}")
_MEMBER = "member_id, role_description"
_MESSAGE = """
    m.message_id, m.room_id, m.sender_member_id, m.client_message_id, m.text, m.mention_member_id
"""
_INBOX = """
    FROM inbox_events AS e
    JOIN messages AS m ON m.message_id = e.message_id
    JOIN rooms AS r ON r.room_id = m.room_id
    JOIN room_members AS rm ON rm.room_id = r.room_id AND rm.member_id = e.member_id
    WHERE e.member_id = ? AND e.event_id > ? AND e.acknowledged = 0
        AND rm.acked_board_version = r.version
"""

_SCHEMA = (
    """
    CREATE TABLE members (
        member_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        harness_instance_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        role_description TEXT NOT NULL,
        token_digest BLOB NOT NULL UNIQUE,
        UNIQUE (owner_id, harness_instance_id, session_key)
    )
    """,
    """
    CREATE TABLE rooms (
        room_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        rules TEXT NOT NULL,
        protocol TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE room_members (
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        member_id TEXT NOT NULL REFERENCES members(member_id),
        joined_after INTEGER NOT NULL,
        read_board_version INTEGER,
        acked_board_version INTEGER,
        PRIMARY KEY (room_id, member_id)
    )
    """,
    """
    CREATE TABLE messages (
        message_id INTEGER PRIMARY KEY,
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        sender_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_message_id TEXT NOT NULL,
        text TEXT NOT NULL,
        mention_member_id TEXT REFERENCES members(member_id),
        UNIQUE (sender_member_id, client_message_id)
    )
    """,
    "CREATE INDEX room_history ON messages (room_id, message_id)",
    """
    CREATE TABLE inbox_events (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER NOT NULL REFERENCES messages(message_id),
        kind TEXT NOT NULL CHECK (kind IN ('message', 'mention')),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        UNIQUE (member_id, message_id)
    )
    """,
    """
    CREATE INDEX pending_inbox ON inbox_events (member_id, event_id) WHERE acknowledged = 0
    """,
)

ChangeListener = Callable[[], None]
ObserveInbox = Callable[[ChangeListener], AbstractContextManager[None]]


class CollaborationError(Exception):
    """A rejection represented by a public collaboration error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _credential_digest(token: str) -> bytes | None:
    if _CREDENTIAL.fullmatch(token) is None:
        return None
    raw = urlsafe_b64decode(token + "=")
    if urlsafe_b64encode(raw).decode("ascii").rstrip("=") != token:
        return None
    return sha256(raw).digest()


def _enrollment_digests(keys: Mapping[str, str]) -> dict[str, bytes]:
    digests = {}
    used = set()
    for owner, token in keys.items():
        if (
            not isinstance(owner, str)
            or not owner.strip()
            or not isinstance(token, str)
        ):
            raise ValueError("Invalid enrollment key configuration")
        digest = _credential_digest(token)
        if digest is None or digest in used:
            raise ValueError(
                "Enrollment keys must be distinct 32-byte base64url credentials"
            )
        digests[owner] = digest
        used.add(digest)
    return digests


def _member(connection: sqlite3.Connection, token: str) -> sqlite3.Row:
    digest = _credential_digest(token)
    record = connection.execute(
        f"SELECT {_MEMBER} FROM members WHERE token_digest = ?", (digest,)
    ).fetchone()
    if record is None:
        raise CollaborationError("unauthorized")
    return record


def _room_member(
    connection: sqlite3.Connection, room_id: str, member_id: str
) -> sqlite3.Row:
    record = connection.execute(
        """
        SELECT r.version, r.rules, r.protocol, rm.read_board_version,
            rm.acked_board_version, rm.joined_after
        FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
        WHERE rm.room_id = ? AND rm.member_id = ?
        """,
        (room_id, member_id),
    ).fetchone()
    if record is None:
        raise CollaborationError("not_found")
    return record


def _require_current_board(authorization: sqlite3.Row) -> None:
    if authorization["acked_board_version"] != authorization["version"]:
        raise CollaborationError("board_ack_required")


class Collaboration:
    """Public operations and post-commit inbox subscriptions."""

    def __init__(self, db_path: str | Path, enrollment_keys: Mapping[str, str]):
        self._db_path = Path(db_path).resolve()
        self._enrollment_keys = _enrollment_digests(enrollment_keys)
        self._listeners: dict[str, set[ChangeListener]] = {}
        self._listeners_lock = Lock()
        with suppress(FileExistsError):
            os.close(
                os.open(self._db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            )
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        with self._transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
            elif version != 1:
                raise ValueError("Unsupported TeamUp database schema version")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection

    def _enrollment_owner(self, token: str) -> str | None:
        digest = _credential_digest(token)
        owner = None
        if digest is not None:
            for candidate, expected in self._enrollment_keys.items():
                if compare_digest(digest, expected):
                    owner = candidate
        return owner

    def enroll(
        self,
        enrollment_key: str,
        *,
        harness_instance_id: str,
        session_key: str,
        role_description: str,
        member_token: str,
    ) -> dict:
        owner = self._enrollment_owner(enrollment_key)
        if owner is None:
            raise CollaborationError("unauthorized")
        digest = _credential_digest(member_token)
        if digest is None or self._enrollment_owner(member_token) is not None:
            raise CollaborationError("invalid_request")
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                f"""
                SELECT {_MEMBER}, token_digest FROM members
                WHERE owner_id = ? AND harness_instance_id = ? AND session_key = ?
                """,
                (owner, harness_instance_id, session_key),
            ).fetchone()
            if existing is not None:
                if not compare_digest(existing["token_digest"], digest):
                    raise CollaborationError("conflict")
                return {key: existing[key] for key in ("member_id", "role_description")}
            if connection.execute(
                "SELECT 1 FROM members WHERE token_digest = ?", (digest,)
            ).fetchone():
                raise CollaborationError("conflict")
            member_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO members (
                    member_id, owner_id, harness_instance_id, session_key,
                    role_description, token_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id,
                    owner,
                    harness_instance_id,
                    session_key,
                    role_description,
                    digest,
                ),
            )
        return {"member_id": member_id, "role_description": role_description}

    def get_member(self, token: str) -> dict:
        with self._transaction() as connection:
            return dict(_member(connection, token))

    def search_members(self, token: str, query: str) -> list[dict]:
        with self._transaction() as connection:
            _member(connection, token)
            connection.create_function(
                "teamup_casefold", 1, str.casefold, deterministic=True
            )
            rows = connection.execute(
                f"""
                SELECT {_MEMBER} FROM members
                WHERE instr(teamup_casefold(role_description), ?) > 0
                ORDER BY member_id LIMIT 100
                """,
                (query.casefold(),),
            )
            return [dict(record) for record in rows]

    def create_room(
        self, token: str, name: str, rules: str | None, protocol: str | None
    ) -> dict:
        room_id = str(uuid4())
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            connection.execute(
                "INSERT INTO rooms VALUES (?, ?, 1, ?, ?)",
                (
                    room_id,
                    name,
                    rules if rules is not None else _DEFAULT_RULES,
                    protocol if protocol is not None else _DEFAULT_PROTOCOL,
                ),
            )
            connection.execute(
                "INSERT INTO room_members (room_id, member_id, joined_after) VALUES (?, ?, 0)",
                (room_id, member_id),
            )
        return {"room_id": room_id, "name": name}

    def join_room(self, token: str, room_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            room = connection.execute(
                "SELECT room_id, name FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise CollaborationError("not_found")
            connection.execute(
                """
                INSERT INTO room_members (room_id, member_id, joined_after)
                VALUES (?, ?, COALESCE((SELECT MAX(message_id) FROM messages WHERE room_id = ?), 0))
                ON CONFLICT (room_id, member_id) DO NOTHING
                """,
                (room_id, member_id, room_id),
            )
        return dict(room)

    def read_board(self, token: str, room_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            board = _room_member(connection, room_id, member_id)
            if board["read_board_version"] != board["version"]:
                connection.execute(
                    "UPDATE room_members SET read_board_version = ? WHERE room_id = ? AND member_id = ?",
                    (board["version"], room_id, member_id),
                )
            return {
                "room_id": room_id,
                **{key: board[key] for key in ("version", "rules", "protocol")},
            }

    def confirm_board(self, token: str, room_id: str, version: int) -> None:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            board = _room_member(connection, room_id, member_id)
            if version != board["version"]:
                raise CollaborationError("conflict")
            if board["read_board_version"] != version:
                raise CollaborationError("board_read_required")
            if version == board["acked_board_version"]:
                return
            connection.execute(
                "UPDATE room_members SET acked_board_version = ? WHERE room_id = ? AND member_id = ?",
                (version, room_id, member_id),
            )
            revealed = connection.execute(
                f"SELECT 1 {_INBOX} AND m.room_id = ? LIMIT 1",
                (member_id, 0, room_id),
            ).fetchone()
        if revealed:
            self._notify((member_id,))

    def send_message(
        self,
        token: str,
        room_id: str,
        client_message_id: str,
        text: str,
        mention_member_id: str | None,
    ) -> dict:
        with self._transaction(write=True) as connection:
            sender = _member(connection, token)["member_id"]
            _require_current_board(_room_member(connection, room_id, sender))
            existing = connection.execute(
                f"""
                SELECT {_MESSAGE} FROM messages AS m
                WHERE sender_member_id = ? AND client_message_id = ?
                """,
                (sender, client_message_id),
            ).fetchone()
            if existing is not None:
                stored = (
                    existing["room_id"],
                    existing["text"],
                    existing["mention_member_id"],
                )
                if stored != (room_id, text, mention_member_id):
                    raise CollaborationError("conflict")
                return dict(existing)
            if mention_member_id is not None:
                is_recipient = connection.execute(
                    """
                    SELECT 1 FROM room_members
                    WHERE room_id = ? AND member_id = ? AND member_id != ?
                    """,
                    (room_id, mention_member_id, sender),
                ).fetchone()
                if is_recipient is None:
                    raise CollaborationError("invalid_mention")
            message_id = connection.execute(
                """
                INSERT INTO messages (
                    room_id, sender_member_id, client_message_id, text, mention_member_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (room_id, sender, client_message_id, text, mention_member_id),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO inbox_events (member_id, message_id, kind)
                SELECT member_id, ?, CASE WHEN member_id = ? THEN 'mention' ELSE 'message' END
                FROM room_members WHERE room_id = ? AND member_id != ?
                """,
                (message_id, mention_member_id, room_id, sender),
            )
            visible_recipients = [
                row["member_id"]
                for row in connection.execute(
                    """
                    SELECT rm.member_id FROM room_members AS rm JOIN rooms AS r USING (room_id)
                    WHERE rm.room_id = ? AND rm.member_id != ? AND rm.acked_board_version = r.version
                    """,
                    (room_id, sender),
                )
            ]
        self._notify(visible_recipients)
        return {
            "message_id": message_id,
            "room_id": room_id,
            "sender_member_id": sender,
            "client_message_id": client_message_id,
            "text": text,
            "mention_member_id": mention_member_id,
        }

    def history(self, token: str, room_id: str, after: int, limit: int) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            membership = _room_member(connection, room_id, member_id)
            _require_current_board(membership)
            cursor = min(max(after, membership["joined_after"]), _SQLITE_MAX_ID)
            rows = connection.execute(
                f"""
                SELECT {_MESSAGE} FROM messages AS m
                WHERE room_id = ? AND message_id > ? ORDER BY message_id LIMIT ?
                """,
                (room_id, cursor, limit),
            )
            return [dict(record) for record in rows]

    def inbox(self, token: str, after: int, limit: int) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            rows = connection.execute(
                f"""
                SELECT e.event_id, e.kind, {_MESSAGE} {_INBOX}
                ORDER BY e.event_id LIMIT ?
                """,
                (member_id, min(after, _SQLITE_MAX_ID), limit),
            )
            events = []
            for record in rows:
                message = dict(record)
                event = {
                    "event_id": message.pop("event_id"),
                    "kind": message.pop("kind"),
                }
                events.append({**event, "message": message})
            return events

    def acknowledge_event(self, token: str, event_id: int) -> None:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            if event_id > _SQLITE_MAX_ID:
                raise CollaborationError("not_found")
            event = connection.execute(
                """
                SELECT m.room_id, e.acknowledged FROM inbox_events AS e
                JOIN messages AS m ON m.message_id = e.message_id
                WHERE e.event_id = ? AND e.member_id = ?
                """,
                (event_id, member_id),
            ).fetchone()
            if event is None:
                raise CollaborationError("not_found")
            room = _room_member(connection, event["room_id"], member_id)
            _require_current_board(room)
            if event["acknowledged"]:
                return
            connection.execute(
                "UPDATE inbox_events SET acknowledged = 1 WHERE event_id = ?",
                (event_id,),
            )
        self._notify((member_id,))

    def inbox_changes(self, token: str) -> ObserveInbox:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
        return partial(self._observe, member_id)

    @contextmanager
    def _observe(self, member_id: str, listener: ChangeListener) -> Iterator[None]:
        with self._listeners_lock:
            self._listeners.setdefault(member_id, set()).add(listener)
        try:
            yield
        finally:
            with self._listeners_lock:
                listeners = self._listeners[member_id]
                listeners.remove(listener)
                if not listeners:
                    del self._listeners[member_id]

    def _notify(self, members: Iterable[str]) -> None:
        with self._listeners_lock:
            listeners = [
                listener
                for member in members
                for listener in self._listeners.get(member, ())
            ]
        for listener in listeners:
            try:
                listener()
            except Exception:
                _LOGGER.exception("An inbox subscription listener failed")
