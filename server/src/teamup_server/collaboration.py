"""Transactional owner of TeamUp collaboration state."""

import logging
import os
import re
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from secrets import token_bytes
from threading import BoundedSemaphore, Lock
from time import monotonic, time
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError

_LOGGER = logging.getLogger(__name__)
_SQLITE_MAX_ID = (1 << 63) - 1
_EXPIRE_BATCH = 500
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DM_DUE_MESSAGES = """
    SELECT message_id, dm_id FROM messages
    WHERE dm_id IS NOT NULL AND created_at IS NOT NULL
        AND teamup_expiry_micros(created_at) <= ?
"""
_ROOM_DUE_MESSAGES = """
    SELECT message_id, room_id FROM messages
    WHERE room_id IS NOT NULL AND created_at IS NOT NULL
        AND teamup_expiry_micros(created_at) <= ?
"""
_DEFAULT_RULES = "Share only information authorized for this room."
_DEFAULT_PROTOCOL = (
    "Coordinate work in the room and direct mentions to the intended teammate."
)
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43}")
_USERNAME = re.compile(r"[a-z][a-z0-9_-]{2,63}")
_SESSION_SECONDS = 8 * 3600
_DM_EXPIRY_BATCH = 500
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_CHECKS_PER_WINDOW = 60
_LOGIN_FAILURES_PER_USERNAME = 5
_SESSION_PURPOSES = ("enrollment", "console")
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2, memory_cost=19 * 1024, parallelism=1, type=Type.ID
)
_MEMBER = "member_id, role_description"
_MESSAGE = """
    m.message_id, m.room_id, m.sender_member_id, m.client_message_id, m.text, m.mention_member_id, m.created_at
"""
_DM_MESSAGE = """
    m.message_id, m.dm_id, m.sender_member_id, m.client_message_id, m.text, m.created_at
"""
_SNIPPET = 200
_SNIPPET_LEAD = 80
_INBOX = """
    FROM inbox_events AS e
    JOIN messages AS m ON m.message_id = e.message_id
    JOIN rooms AS r ON r.room_id = m.room_id
    JOIN room_members AS rm ON rm.room_id = r.room_id AND rm.member_id = e.member_id
    WHERE e.member_id = ? AND e.event_id > ? AND e.acknowledged = 0
        AND rm.acked_board_version = r.version
"""
_DM_EVENT_MESSAGE = (
    "message_id",
    "dm_id",
    "sender_member_id",
    "client_message_id",
    "text",
    "created_at",
)
_ROOM_EVENT_MESSAGE = (
    "message_id",
    "room_id",
    "sender_member_id",
    "client_message_id",
    "text",
    "mention_member_id",
    "created_at",
)

_DMS_TABLE = """
    CREATE TABLE dms (
        dm_id TEXT PRIMARY KEY,
        member_low TEXT NOT NULL REFERENCES members(member_id),
        member_high TEXT NOT NULL REFERENCES members(member_id),
        expired_through_message_id INTEGER,
        UNIQUE (member_low, member_high),
        CHECK (member_low < member_high)
    )
"""

_DM_V4_DMS_TABLE = """
    CREATE TABLE dms (
        dm_id TEXT PRIMARY KEY,
        member_low TEXT NOT NULL REFERENCES members(member_id),
        member_high TEXT NOT NULL REFERENCES members(member_id),
        UNIQUE (member_low, member_high),
        CHECK (member_low < member_high)
    )
"""

_MESSAGES_TABLE = """
    CREATE TABLE messages (
        message_id INTEGER PRIMARY KEY,
        room_id TEXT REFERENCES rooms(room_id),
        dm_id TEXT REFERENCES dms(dm_id),
        sender_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_message_id TEXT NOT NULL,
        text TEXT NOT NULL,
        mention_member_id TEXT REFERENCES members(member_id),
        created_at TEXT,
        CHECK ((room_id IS NULL) <> (dm_id IS NULL))
    )
"""

_MESSAGE_INDEXES = (
    "CREATE INDEX room_history ON messages (room_id, message_id)",
    "CREATE INDEX dm_history ON messages (dm_id, message_id)",
    """
    CREATE UNIQUE INDEX room_message_retry ON messages (sender_member_id, client_message_id)
        WHERE room_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX dm_message_retry ON messages (dm_id, sender_member_id, client_message_id)
        WHERE dm_id IS NOT NULL
    """,
)

_INBOX_EVENTS_TABLE = """
    CREATE TABLE inbox_events (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER NOT NULL REFERENCES messages(message_id),
        kind TEXT NOT NULL CHECK (kind IN ('message', 'mention', 'dm')),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        UNIQUE (member_id, message_id)
    )
"""

_PENDING_INBOX_INDEX = "CREATE INDEX pending_inbox ON inbox_events (member_id, event_id) WHERE acknowledged = 0"

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
        protocol TEXT NOT NULL,
        history_policy TEXT NOT NULL DEFAULT 'since_joining'
            CHECK (history_policy IN ('retained', 'since_joining')),
        expired_through_message_id INTEGER
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
    _DMS_TABLE,
    _MESSAGES_TABLE,
    *_MESSAGE_INDEXES,
    _INBOX_EVENTS_TABLE,
    _PENDING_INBOX_INDEX,
    """
    CREATE TABLE id_counters (
        counter TEXT PRIMARY KEY CHECK (counter IN ('message_id', 'event_id')),
        last_id INTEGER NOT NULL CHECK (last_id >= 0)
    )
    """,
    "INSERT INTO id_counters (counter, last_id) VALUES ('message_id', 0), ('event_id', 0)",
)

_ACCOUNT_SCHEMA = (
    """
    CREATE TABLE accounts (
        owner_id TEXT NOT NULL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_digest TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
    )
    """,
    """
    CREATE TABLE account_sessions (
        token_digest BLOB NOT NULL PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        expires_at INTEGER NOT NULL,
        purpose TEXT NOT NULL DEFAULT 'enrollment'
            CHECK (purpose IN ('enrollment', 'console'))
    )
    """,
    "CREATE INDEX account_sessions_owner ON account_sessions (owner_id)",
)

_GRANT_SCHEMA = (
    """
    CREATE TABLE operator_grants (
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        scope TEXT NOT NULL CHECK (scope IN ('console_read', 'transcript_read')),
        room_id TEXT REFERENCES rooms(room_id),
        CHECK (
            (scope = 'console_read' AND room_id IS NULL)
            OR (scope = 'transcript_read' AND room_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX operator_grant_identity
        ON operator_grants (owner_id, scope, ifnull(room_id, ''))
    """,
    "CREATE INDEX operator_grants_owner ON operator_grants (owner_id)",
)

_HISTORY_SCHEMA = (
    """
    ALTER TABLE rooms ADD COLUMN history_policy TEXT NOT NULL
        DEFAULT 'since_joining' CHECK (history_policy IN ('retained', 'since_joining'))
    """,
    "ALTER TABLE messages ADD COLUMN created_at TEXT",
    """
    CREATE TABLE id_counters (
        counter TEXT PRIMARY KEY CHECK (counter IN ('message_id', 'event_id')),
        last_id INTEGER NOT NULL CHECK (last_id >= 0)
    )
    """,
)

_READ_SCHEMA = (

    """
    CREATE TABLE room_activity (
        activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        kind TEXT NOT NULL CHECK (
            kind IN ('member_joined', 'board_acknowledged', 'message', 'delivery')
        ),
        at TEXT,
        actor_member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER REFERENCES messages(message_id) ON DELETE SET NULL,
        CHECK (
            kind IN ('message', 'delivery')
            OR (kind IN ('member_joined', 'board_acknowledged')
                AND message_id IS NULL)
        )
    )
    """,
    "CREATE INDEX room_activity_room ON room_activity (room_id, activity_id)",
    "CREATE INDEX room_activity_actor ON room_activity (actor_member_id, activity_id)",
    """
    CREATE TABLE transcript_access_audit (
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        action TEXT NOT NULL CHECK (action = 'transcript_read'),
        at TEXT NOT NULL,
        page_before INTEGER,
        page_limit INTEGER NOT NULL
    )
    """,
    "CREATE INDEX transcript_access_audit_room ON transcript_access_audit (room_id)",
)

_ACTIVITY = "activity_id, room_id, kind, at, message_id, actor_member_id"

_RETENTION_SCHEMA = (
    "ALTER TABLE rooms ADD COLUMN expired_through_message_id INTEGER",
    "ALTER TABLE dms ADD COLUMN expired_through_message_id INTEGER",
)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _auth_preview_v3(connection: sqlite3.Connection) -> bool:
    """Disambiguate the two v3 drafts by their table columns.

    The #6 history v3 draft carries ``rooms.history_policy`` and no operator
    columns; the published #5 auth preview (e495544) v3 carries
    ``account_sessions.purpose`` and ``operator_grants`` but no history
    columns. Both report ``user_version`` 3, so only the columns separate
    them, and only this schema migration inspects them.
    """
    purposes = {
        row[1] for row in connection.execute("PRAGMA table_info(account_sessions)")
    }
    return "purpose" in purposes and _has_table(connection, "operator_grants")


_MIGRATED_MESSAGES_TABLE = """
    CREATE TABLE migrated_messages (
        message_id INTEGER PRIMARY KEY,
        room_id TEXT REFERENCES rooms(room_id),
        dm_id TEXT REFERENCES dms(dm_id),
        sender_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_message_id TEXT NOT NULL,
        text TEXT NOT NULL,
        mention_member_id TEXT REFERENCES members(member_id),
        created_at TEXT,
        CHECK ((room_id IS NULL) <> (dm_id IS NULL))
    )
"""

_MIGRATED_INBOX_EVENTS_TABLE = """
    CREATE TABLE migrated_inbox_events (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        message_id INTEGER NOT NULL REFERENCES migrated_messages(message_id),
        kind TEXT NOT NULL CHECK (kind IN ('message', 'mention', 'dm')),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        UNIQUE (member_id, message_id)
    )
"""


def _migrate_dm_schema(connection: sqlite3.Connection) -> None:
    """Grow the canonical record families by the DM family, in place.

    The messages table gains a nullable dm_id (exactly one of room_id and
    dm_id is set), the per-sender retry key becomes family-scoped unique
    indexes, and inbox events learn the "dm" kind. Existing rows keep
    their IDs, order and columns. A v0 upgrade sample may predate the
    inbox_events table, so its rows are carried over only when present.
    The dms table is created in the published DM v4 shape; the composed
    retention step adds the expiry watermark columns afterwards.
    """
    connection.execute(_DM_V4_DMS_TABLE)
    connection.execute(_MIGRATED_MESSAGES_TABLE)
    connection.execute(
        """
        INSERT INTO migrated_messages (
            message_id, room_id, dm_id, sender_member_id, client_message_id, text,
            mention_member_id, created_at
        )
        SELECT message_id, room_id, NULL, sender_member_id, client_message_id, text,
            mention_member_id, created_at
        FROM messages
        """
    )
    carried_inbox = False
    if _has_table(connection, "inbox_events"):
        connection.execute(_MIGRATED_INBOX_EVENTS_TABLE)
        connection.execute(
            """
            INSERT INTO migrated_inbox_events (
                event_id, member_id, message_id, kind, acknowledged
            )
            SELECT event_id, member_id, message_id, kind, acknowledged
            FROM inbox_events
            """
        )
        connection.execute("DROP TABLE inbox_events")
        carried_inbox = True
    connection.execute("DROP TABLE messages")
    connection.execute("ALTER TABLE migrated_messages RENAME TO messages")
    if carried_inbox:
        connection.execute("ALTER TABLE migrated_inbox_events RENAME TO inbox_events")
    else:
        connection.execute(_INBOX_EVENTS_TABLE)
    for statement in _MESSAGE_INDEXES:
        connection.execute(statement)
    connection.execute(_PENDING_INBOX_INDEX)


def _seed_id_counters(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO id_counters (counter, last_id)
        SELECT 'message_id', COALESCE(MAX(message_id), 0) FROM messages
        """
    )
    has_events = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inbox_events'"
    ).fetchone()
    last_event_id = (
        connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) FROM inbox_events"
        ).fetchone()[0]
        if has_events is not None
        else 0
    )
    connection.execute(
        "INSERT INTO id_counters (counter, last_id) VALUES ('event_id', ?)",
        (last_event_id,),
    )


def _next_id(connection: sqlite3.Connection, counter: str) -> int:
    return connection.execute(
        "UPDATE id_counters SET last_id = last_id + 1 WHERE counter = ? RETURNING last_id",
        (counter,),
    ).fetchone()[0]


def _rfc3339_utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _rfc3339_to_unix(value: str | None) -> int | None:
    if value is None:
        return None
    return int(datetime.fromisoformat(value).timestamp())


def _activity_summary(row: sqlite3.Row | None, prefix: str = "") -> dict | None:
    if row is None or row[f"{prefix}kind"] is None:
        return None
    return {
        "kind": row[f"{prefix}kind"],
        "at": _rfc3339_to_unix(row[f"{prefix}at"]),
        "message_id": row[f"{prefix}message_id"],
    }


def _backfill_room_activity(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO room_activity (room_id, kind, at, actor_member_id, message_id)
        SELECT room_id, 'member_joined', NULL, member_id, NULL FROM room_members
        ORDER BY room_id, joined_after, member_id
        """
    )
    connection.execute(
        """
        INSERT INTO room_activity (room_id, kind, at, actor_member_id, message_id)
        SELECT room_id, 'board_acknowledged', NULL, member_id, NULL
        FROM room_members WHERE acked_board_version IS NOT NULL
        ORDER BY room_id, member_id
        """
    )
    connection.execute(
        """
        INSERT INTO room_activity (room_id, kind, at, actor_member_id, message_id)
        SELECT room_id, 'message', created_at, sender_member_id, message_id
        FROM messages WHERE room_id IS NOT NULL ORDER BY message_id
        """
    )
    has_events = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inbox_events'"
    ).fetchone()
    if has_events is not None:
        connection.execute(
            """
            INSERT INTO room_activity (room_id, kind, at, actor_member_id, message_id)
            SELECT m.room_id, 'delivery', NULL, e.member_id, m.message_id
            FROM inbox_events AS e JOIN messages AS m ON m.message_id = e.message_id
            WHERE e.acknowledged = 1 AND m.room_id IS NOT NULL
            ORDER BY e.event_id
            """
        )


def _record_activity(
    connection: sqlite3.Connection,
    room_id: str,
    kind: str,
    actor_member_id: str,
    at: str | None,
    message_id: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO room_activity (room_id, kind, at, actor_member_id, message_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (room_id, kind, at, actor_member_id, message_id),
    )


def _utc_micros(moment: datetime) -> int:
    """Microseconds since the UTC epoch for a timezone-aware moment."""
    return (moment.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


def _expiry_micros(stamp: object) -> int | None:
    """Microseconds since the UTC epoch, or None when the stamp is unusable.

    Both retention families share this SQL-side clock and the single
    parser behind it: a ``created_at`` without an explicit offset, or
    with any unusable value, never expires -- the policy fails closed.
    That includes offset extremes whose UTC conversion overflows the
    representable calendar: they stay unusable instead of aborting
    the scan.
    """
    moment = _parse_timestamp(stamp)
    if moment is None:
        return None
    try:
        return _utc_micros(moment)
    except OverflowError:
        return None


def _register_expiry_clock(connection: sqlite3.Connection) -> None:
    """Publish the shared timestamp policy to SQL for one connection."""
    connection.create_function(
        "teamup_expiry_micros", 1, _expiry_micros, deterministic=True
    )


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an RFC 3339 timestamp with an explicit offset, else return None.

    This is the single timestamp policy for both retention families:
    naive, missing, and otherwise unusable values never expire.
    """

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


ChangeListener = Callable[[], None]
ObserveInbox = Callable[[ChangeListener], AbstractContextManager[None]]


class CollaborationError(Exception):
    """A rejection represented by a public collaboration error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RetentionPass:
    """One committed retention batch and whether more expired work is due."""

    expired_messages: int
    expired_events: int
    more_due: bool


@dataclass
class _LoginCheck:
    username: str
    started: float
    succeeded: bool = False


def _validate_username(username: str) -> None:
    if _USERNAME.fullmatch(username) is None:
        raise CollaborationError("invalid_request")


def _validate_password(password: str, *, minimum: int = 0) -> None:
    if not minimum <= len(password) <= 128:
        raise CollaborationError("invalid_request")
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise CollaborationError("invalid_request") from None


def _validate_room_id(room_id: str) -> str:
    if not isinstance(room_id, str):
        raise CollaborationError("invalid_request")
    try:
        return str(UUID(room_id))
    except ValueError:
        raise CollaborationError("invalid_request") from None


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
        f"""
        SELECT {_MEMBER} FROM members WHERE token_digest = ?
            AND NOT EXISTS (
                SELECT 1 FROM accounts
                WHERE accounts.owner_id = members.owner_id AND disabled = 1
            )
        """,
        (digest,),
    ).fetchone()
    if record is None:
        raise CollaborationError("unauthorized")
    return record


def _room_member(
    connection: sqlite3.Connection, room_id: str, member_id: str
) -> sqlite3.Row:
    record = connection.execute(
        """
        SELECT r.version, r.rules, r.protocol, r.history_policy,
            r.expired_through_message_id, rm.read_board_version,
            rm.acked_board_version, rm.joined_after
        FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
        WHERE rm.room_id = ? AND rm.member_id = ?
        """,
        (room_id, member_id),
    ).fetchone()
    if record is None:
        raise CollaborationError("not_found")
    return record


def _dm_participant(
    connection: sqlite3.Connection, dm_id: str, member_id: str
) -> sqlite3.Row:
    record = connection.execute(
        """
        SELECT member_low, member_high, expired_through_message_id FROM dms
        WHERE dm_id = ? AND ? IN (member_low, member_high)
        """,
        (dm_id, member_id),
    ).fetchone()
    if record is None:
        raise CollaborationError("not_found")
    return record


def _available_member(
    connection: sqlite3.Connection, member_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT member_id FROM members
        WHERE member_id = ? AND NOT EXISTS (
            SELECT 1 FROM accounts
            WHERE accounts.owner_id = members.owner_id AND disabled = 1
        )
        """,
        (member_id,),
    ).fetchone()


def _require_current_board(authorization: sqlite3.Row) -> None:
    if authorization["acked_board_version"] != authorization["version"]:
        raise CollaborationError("board_ack_required")


def _authorized_membership(
    connection: sqlite3.Connection, token: str, room_id: str
) -> sqlite3.Row:
    member_id = _member(connection, token)["member_id"]
    membership = _room_member(connection, room_id, member_id)
    _require_current_board(membership)
    return membership


def _transcript_floor(membership: sqlite3.Row) -> int:
    """The oldest message ID a current membership is authorized to page from."""
    if membership["history_policy"] == "retained":
        return 0
    return membership["joined_after"]


def _expiry_metadata(
    expired_through: int | None, cursor: int, upper: int | None
) -> dict:
    """The resume-cursor gap appended after a room's first committed prune."""
    if expired_through is None:
        return {}
    return {
        "expired_through_message_id": expired_through,
        "history_truncated": (
            cursor < expired_through
            if upper is None
            else cursor < min(expired_through, upper - 1)
        ),
    }


def _room_transcript(
    connection: sqlite3.Connection,
    room_id: str,
    *,
    after: int = 0,
    before: int | None = None,
    limit: int = 50,
    newest_first: bool = False,
) -> list[sqlite3.Row]:
    """One ascending page of the canonical room transcript owned by Collaboration.

    ``newest_first`` picks the page closest below ``before`` (or the latest page
    when no upper bound is given) instead of the page following ``after``.
    """
    if limit <= 0:
        return []
    bounds = "message_id > ?"
    bound_values: list[object] = [after]
    if before is not None:
        bounds += " AND message_id < ?"
        bound_values.append(before)
    rows = connection.execute(
        f"""
        SELECT {_MESSAGE} FROM messages AS m
        WHERE room_id = ? AND {bounds}
        ORDER BY message_id {"DESC" if newest_first else ""}
        LIMIT ?
        """,
        (room_id, *bound_values, limit),
    ).fetchall()
    if newest_first:
        rows.reverse()
    return rows


def _snippet(body: str, needle: str, hit: int) -> str:
    """A bounded window of the original body around the first folded match.

    ``hit`` is the 1-based casefolded offset of ``needle``; folded offsets are
    mapped back to whole original characters so expansions such as ``ß`` and
    ``İ`` never split or reshape the quoted text.
    """
    folded_start = hit - 1
    folded_end = folded_start + len(needle)
    start = end = 0
    offset = 0
    for index, character in enumerate(body):
        width = len(character.casefold())
        if offset < folded_end and offset + width > folded_start:
            if not end:
                start = index
            end = index + 1
        elif end:
            break
        offset += width
    length = end - start
    if length <= _SNIPPET - _SNIPPET_LEAD:
        lead = _SNIPPET_LEAD
    else:
        lead = max(_SNIPPET - length, 0)
    window = max(start - lead, 0)
    return body[window : window + _SNIPPET]


class Collaboration:
    """Public operations and post-commit inbox subscriptions."""

    def __init__(self, db_path: str | Path, enrollment_keys: Mapping[str, str]):
        self._db_path = Path(db_path).resolve()
        self._enrollment_keys = _enrollment_digests(enrollment_keys)
        self._listeners: dict[str, set[ChangeListener]] = {}
        self._listeners_lock = Lock()
        self._login_checks: deque[_LoginCheck] = deque()
        self._login_checks_lock = Lock()
        self._password_checks = BoundedSemaphore(4)
        self._unknown_password_digest = _PASSWORD_HASHER.hash(token_bytes(32))
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
            if version in (0, 1):
                for statement in _ACCOUNT_SCHEMA:
                    connection.execute(statement)
            if version in (1, 2):
                for statement in _HISTORY_SCHEMA:
                    connection.execute(statement)
                _seed_id_counters(connection)
            preview = version == 3 and _auth_preview_v3(connection)
            if preview:
                # The published #5 auth preview v3 predates the #6 history layer:
                # apply the missing columns and seed durable IDs before the DM
                # rebuild, keeping the preview's purposes, sessions, grants and
                # room data.
                for statement in _HISTORY_SCHEMA:
                    connection.execute(statement)
                _seed_id_counters(connection)
            if version in (1, 2, 3):
                _migrate_dm_schema(connection)
            if version in (2, 3, 4) and not preview:
                # Console migration: legacy sessions keep enrollment semantics.
                connection.execute(
                    """
                    ALTER TABLE account_sessions
                        ADD COLUMN purpose TEXT NOT NULL DEFAULT 'enrollment'
                    """
                )
            if version in (1, 2, 3, 4, 5):
                # The composed retention step: every published pre-v6 start
                # (including Console v5) gains the expiry watermarks in place.
                for statement in _RETENTION_SCHEMA:
                    connection.execute(statement)
            if version in (0, 1, 2, 3, 4):
                if not preview:
                    for statement in _GRANT_SCHEMA:
                        connection.execute(statement)
                for statement in _READ_SCHEMA:
                    connection.execute(statement)
                _backfill_room_activity(connection)
            if version in (0, 1, 2, 3, 4, 5):
                connection.execute("PRAGMA user_version = 6")
            elif version != 6:
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

    def create_account(self, username: str, password: str) -> None:
        _validate_username(username)
        _validate_password(password, minimum=12)
        digest = _PASSWORD_HASHER.hash(password)
        with self._transaction(write=True) as connection:
            created = connection.execute(
                """
                INSERT INTO accounts (owner_id, username, password_digest) VALUES (?, ?, ?)
                ON CONFLICT (username) DO NOTHING
                """,
                (str(uuid4()), username, digest),
            )
            if not created.rowcount:
                raise CollaborationError("conflict")

    def list_accounts(self) -> list[dict]:
        with self._transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT username, owner_id, disabled FROM accounts ORDER BY username"
                )
            ]

    def reset_account_password(self, username: str, password: str) -> None:
        _validate_username(username)
        _validate_password(password, minimum=12)
        digest = _PASSWORD_HASHER.hash(password)
        with self._transaction(write=True) as connection:
            account = self._account(connection, username)
            if account["disabled"]:
                raise CollaborationError("conflict")
            connection.execute(
                "UPDATE accounts SET password_digest = ? WHERE owner_id = ?",
                (digest, account["owner_id"]),
            )
            self._revoke_account_sessions(connection, account["owner_id"])

    def disable_account(self, username: str) -> None:
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            connection.execute(
                "UPDATE accounts SET disabled = 1 WHERE owner_id = ?", (owner_id,)
            )
            self._revoke_account_sessions(connection, owner_id)

    def grant_console_read(self, username: str) -> None:
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            connection.execute(
                """
                INSERT INTO operator_grants (owner_id, scope, room_id)
                VALUES (?, 'console_read', NULL) ON CONFLICT DO NOTHING
                """,
                (owner_id,),
            )

    def revoke_console_read(self, username: str) -> None:
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            connection.execute(
                "DELETE FROM operator_grants WHERE owner_id = ? AND scope = 'console_read'",
                (owner_id,),
            )

    def grant_transcript_read(self, username: str, room_id: str) -> None:
        room_id = _validate_room_id(room_id)
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            if (
                connection.execute(
                    "SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)
                ).fetchone()
                is None
            ):
                raise CollaborationError("not_found")
            connection.execute(
                """
                INSERT INTO operator_grants (owner_id, scope, room_id)
                VALUES (?, 'transcript_read', ?) ON CONFLICT DO NOTHING
                """,
                (owner_id, room_id),
            )

    def revoke_transcript_read(self, username: str, room_id: str) -> None:
        room_id = _validate_room_id(room_id)
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            if (
                connection.execute(
                    "SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)
                ).fetchone()
                is None
            ):
                raise CollaborationError("not_found")
            connection.execute(
                """
                DELETE FROM operator_grants
                WHERE owner_id = ? AND scope = 'transcript_read' AND room_id = ?
                """,
                (owner_id, room_id),
            )

    @staticmethod
    def _account(connection: sqlite3.Connection, username: str) -> sqlite3.Row:
        account = connection.execute(
            "SELECT owner_id, disabled FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        if account is None:
            raise CollaborationError("not_found")
        return account

    @staticmethod
    def _revoke_account_sessions(connection: sqlite3.Connection, owner_id: str) -> None:
        # Keep issued digests so expired or revoked login tokens cannot become member tokens.
        connection.execute(
            "UPDATE account_sessions SET expires_at = 0 WHERE owner_id = ? AND expires_at != 0",
            (owner_id,),
        )

    def _start_password_check(self, username: str) -> _LoginCheck:
        with self._login_checks_lock:
            now = monotonic()
            checks = self._login_checks
            while checks and now - checks[0].started >= _LOGIN_WINDOW_SECONDS:
                checks.popleft()
            if (
                len(checks) >= _LOGIN_CHECKS_PER_WINDOW
                or sum(
                    check.username == username and not check.succeeded
                    for check in checks
                )
                >= _LOGIN_FAILURES_PER_USERNAME
            ):
                raise CollaborationError("rate_limited")
            check = _LoginCheck(username, now)
            checks.append(check)
            return check

    def login(self, username: str, password: str, purpose: str = "enrollment") -> dict:
        _validate_username(username)
        _validate_password(password)
        if purpose not in _SESSION_PURPOSES:
            raise CollaborationError("invalid_request")
        check = self._start_password_check(username)
        with self._transaction() as connection:
            account = connection.execute(
                """
                SELECT owner_id, password_digest FROM accounts
                WHERE username = ? AND disabled = 0
                """,
                (username,),
            ).fetchone()
        digest = (
            account["password_digest"] if account else self._unknown_password_digest
        )
        with self._password_checks:
            try:
                _PASSWORD_HASHER.verify(digest, password)
            except VerifyMismatchError:
                raise CollaborationError("unauthorized") from None
        if account is None:
            raise CollaborationError("unauthorized")
        session = self._issue_account_session(account["owner_id"], digest, purpose)
        with self._login_checks_lock:
            check.succeeded = True
        return session

    def _issue_account_session(
        self, owner_id: str, password_digest: str, purpose: str
    ) -> dict:
        with self._transaction(write=True) as connection:
            if not connection.execute(
                """
                SELECT 1 FROM accounts
                WHERE owner_id = ? AND password_digest = ? AND disabled = 0
                """,
                (owner_id, password_digest),
            ).fetchone():
                raise CollaborationError("unauthorized")
            expires_at = int(time()) + _SESSION_SECONDS
            while True:
                raw = token_bytes(32)
                digest = sha256(raw).digest()
                if self._legacy_owner(digest) is not None:
                    continue
                collision = connection.execute(
                    """
                    SELECT 1 FROM members WHERE token_digest = ?
                    UNION ALL SELECT 1 FROM account_sessions WHERE token_digest = ?
                    """,
                    (digest, digest),
                ).fetchone()
                if collision is None:
                    break
            connection.execute(
                """
                INSERT INTO account_sessions (token_digest, owner_id, expires_at, purpose)
                VALUES (?, ?, ?, ?)
                """,
                (digest, owner_id, expires_at, purpose),
            )
        return {
            "access_token": urlsafe_b64encode(raw).decode("ascii").rstrip("="),
            "expires_at": expires_at,
        }

    def _legacy_owner(self, digest: bytes | None) -> str | None:
        owner = None
        if digest is not None:
            for candidate, expected in self._enrollment_keys.items():
                if compare_digest(digest, expected):
                    owner = candidate
        return owner

    def _enrollment_owner(self, connection: sqlite3.Connection, token: str) -> str:
        digest = _credential_digest(token)
        session = connection.execute(
            """
            SELECT a.owner_id, a.disabled, s.expires_at, s.purpose
            FROM account_sessions AS s JOIN accounts AS a USING (owner_id)
            WHERE s.token_digest = ?
            """,
            (digest,),
        ).fetchone()
        if session is not None:
            if (
                session["disabled"]
                or session["expires_at"] <= int(time())
                or session["purpose"] != "enrollment"
            ):
                raise CollaborationError("unauthorized")
            return session["owner_id"]
        owner = self._legacy_owner(digest)
        if (
            owner is None
            or connection.execute(
                """
            SELECT 1 FROM accounts WHERE owner_id = ?
            UNION ALL SELECT 1 FROM members WHERE token_digest = ?
            """,
                (owner, digest),
            ).fetchone()
        ):
            raise CollaborationError("unauthorized")
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
        digest = _credential_digest(member_token)
        with self._transaction(write=True) as connection:
            owner = self._enrollment_owner(connection, enrollment_key)
            if (
                digest is None
                or self._legacy_owner(digest) is not None
                or connection.execute(
                    "SELECT 1 FROM account_sessions WHERE token_digest = ?", (digest,)
                ).fetchone()
            ):
                raise CollaborationError("invalid_request")
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

    def list_console_rooms(self, token: str) -> dict:
        with self._transaction() as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            rows = connection.execute(
                """
                SELECT r.room_id, r.name,
                    (SELECT COUNT(*) FROM room_members AS rm WHERE rm.room_id = r.room_id)
                        AS member_count,
                    la.kind AS last_kind, la.at AS last_at,
                    la.message_id AS last_message_id
                FROM rooms AS r
                LEFT JOIN room_activity AS la ON la.activity_id = (
                    SELECT MAX(activity_id) FROM room_activity WHERE room_id = r.room_id
                )
                ORDER BY r.room_id
                """
            )
            return {
                "rooms": [
                    {
                        "room_id": row["room_id"],
                        "name": row["name"],
                        "member_count": row["member_count"],
                        "last_activity": _activity_summary(row, "last_"),
                    }
                    for row in rows
                ]
            }

    def console_room_detail(self, token: str, room_id: str) -> dict:
        with self._transaction() as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            room = self._console_room(connection, room_id)
            member_rows = connection.execute(
                """
                SELECT m.member_id, m.role_description, rm.acked_board_version, r.version
                FROM room_members AS rm
                JOIN members AS m ON m.member_id = rm.member_id
                JOIN rooms AS r ON r.room_id = rm.room_id
                WHERE rm.room_id = ?
                ORDER BY m.member_id
                """,
                (room_id,),
            ).fetchall()
            room_last = connection.execute(
                """
                SELECT kind AS last_kind, at AS last_at, message_id AS last_message_id
                FROM room_activity
                WHERE room_id = ? ORDER BY activity_id DESC LIMIT 1
                """,
                (room_id,),
            ).fetchone()
            latest = {
                row["actor_member_id"]: row
                for row in connection.execute(
                    """
                    SELECT a.actor_member_id, a.kind AS last_kind, a.at AS last_at,
                        a.message_id AS last_message_id
                    FROM room_activity AS a
                    JOIN (
                        SELECT actor_member_id, MAX(activity_id) AS activity_id
                        FROM room_activity WHERE room_id = ?
                        GROUP BY actor_member_id
                    ) AS recent ON recent.activity_id = a.activity_id
                    """,
                    (room_id,),
                )
            }
            granted = connection.execute(
                """
                SELECT 1 FROM operator_grants
                WHERE owner_id = ? AND scope = 'transcript_read' AND room_id = ?
                """,
                (owner_id, room_id),
            ).fetchone()
            return {
                "room": {
                    "room_id": room_id,
                    "name": room["name"],
                    "member_count": len(member_rows),
                    "last_activity": _activity_summary(room_last, "last_"),
                },
                "members": [
                    {
                        "member_id": row["member_id"],
                        "role_description": row["role_description"],
                        "board_state": (
                            "acknowledged"
                            if row["acked_board_version"] == row["version"]
                            else "awaiting"
                        ),
                        "last_activity": _activity_summary(
                            latest.get(row["member_id"]), "last_"
                        ),
                        "harness_state": "unknown",
                    }
                    for row in member_rows
                ],
                "transcript_allowed": granted is not None,
            }

    def console_timeline(
        self, token: str, room_id: str, after: int, limit: int
    ) -> dict:
        with self._transaction() as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            self._console_room(connection, room_id)
            rows = connection.execute(
                f"""
                SELECT {_ACTIVITY} FROM room_activity
                WHERE room_id = ? AND activity_id > ?
                ORDER BY activity_id LIMIT ?
                """,
                (room_id, min(max(after, 0), _SQLITE_MAX_ID), limit),
            ).fetchall()
            return {"activities": [self._activity_payload(row) for row in rows]}

    def console_transcript(
        self, token: str, room_id: str, before: int | None, limit: int
    ) -> dict:
        with self._transaction(write=True) as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            self._console_room(connection, room_id)
            self._require_grant(connection, owner_id, "transcript_read", room_id)
            cursor = None if before is None or before > _SQLITE_MAX_ID else before
            boundary = "" if cursor is None else "AND m.message_id < ?"
            parameters = (room_id,) if cursor is None else (room_id, cursor)
            rows = connection.execute(
                f"""
                SELECT {_MESSAGE}, mem.role_description AS sender_role_description
                FROM messages AS m
                JOIN members AS mem ON mem.member_id = m.sender_member_id
                WHERE m.room_id = ? {boundary}
                ORDER BY m.message_id DESC LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = list(reversed(rows[:limit]))
            connection.execute(
                """
                INSERT INTO transcript_access_audit (
                    owner_id, room_id, action, at, page_before, page_limit
                ) VALUES (?, ?, 'transcript_read', ?, ?, ?)
                """,
                (owner_id, room_id, _rfc3339_utc_now(), cursor, limit),
            )
            return {
                "messages": [
                    {
                        "message_id": row["message_id"],
                        "room_id": row["room_id"],
                        "sender_member_id": row["sender_member_id"],
                        "sender_role_description": row["sender_role_description"],
                        "text": row["text"],
                        "mention_member_id": row["mention_member_id"],
                        "created_at": _rfc3339_to_unix(row["created_at"]),
                    }
                    for row in page
                ],
                "has_more": has_more,
            }

    def _console_room(
        self, connection: sqlite3.Connection, room_id: str
    ) -> sqlite3.Row:
        room = connection.execute(
            "SELECT room_id, name FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if room is None:
            raise CollaborationError("not_found")
        return room

    @staticmethod
    def _activity_payload(row: sqlite3.Row) -> dict:
        return {
            "activity_id": row["activity_id"],
            "room_id": row["room_id"],
            "kind": row["kind"],
            "at": _rfc3339_to_unix(row["at"]),
            "message_id": row["message_id"],
            "actor_member_id": row["actor_member_id"],
        }

    def _console_operator(self, connection: sqlite3.Connection, token: str) -> str:
        digest = _credential_digest(token)
        session = connection.execute(
            """
            SELECT a.owner_id, a.disabled, s.expires_at, s.purpose
            FROM account_sessions AS s JOIN accounts AS a USING (owner_id)
            WHERE s.token_digest = ?
            """,
            (digest,),
        ).fetchone()
        if session is None or session["purpose"] != "console":
            raise CollaborationError("unauthorized")
        if session["disabled"] or session["expires_at"] <= int(time()):
            raise CollaborationError("unauthorized")
        return session["owner_id"]

    @staticmethod
    def _require_grant(
        connection: sqlite3.Connection,
        owner_id: str,
        scope: str,
        room_id: str | None = None,
    ) -> None:
        granted = connection.execute(
            "SELECT 1 FROM operator_grants WHERE owner_id = ? AND scope = ? AND room_id IS ?",
            (owner_id, scope, room_id),
        ).fetchone()
        if granted is None:
            raise CollaborationError("forbidden")

    def create_room(
        self,
        token: str,
        name: str,
        rules: str | None,
        protocol: str | None,
        history_policy: str = "since_joining",
    ) -> dict:
        if history_policy not in ("retained", "since_joining"):
            raise CollaborationError("invalid_request")
        room_id = str(uuid4())
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            connection.execute(
                """
                INSERT INTO rooms (room_id, name, version, rules, protocol, history_policy)
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (
                    room_id,
                    name,
                    rules if rules is not None else _DEFAULT_RULES,
                    protocol if protocol is not None else _DEFAULT_PROTOCOL,
                    history_policy,
                ),
            )
            connection.execute(
                "INSERT INTO room_members (room_id, member_id, joined_after) VALUES (?, ?, 0)",
                (room_id, member_id),
            )
            _record_activity(
                connection, room_id, "member_joined", member_id, _rfc3339_utc_now()
            )
        return {"room_id": room_id, "name": name, "history_policy": history_policy}

    def join_room(self, token: str, room_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            room = connection.execute(
                "SELECT room_id, name FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise CollaborationError("not_found")
            joined = connection.execute(
                """
                INSERT INTO room_members (room_id, member_id, joined_after)
                VALUES (?, ?, COALESCE((SELECT MAX(message_id) FROM messages WHERE room_id = ?), 0))
                ON CONFLICT (room_id, member_id) DO NOTHING
                """,
                (room_id, member_id, room_id),
            )
            if joined.rowcount:
                _record_activity(
                    connection, room_id, "member_joined", member_id, _rfc3339_utc_now()
                )
        return dict(room)

    def list_rooms(self, token: str) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            rows = connection.execute(
                """
                SELECT r.room_id, r.name, r.version AS current_board_version,
                    rm.acked_board_version, (
                        SELECT MAX(m.message_id) FROM messages AS m
                        WHERE m.room_id = r.room_id
                    ) AS latest_message_id
                FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
                WHERE rm.member_id = ?
                ORDER BY r.room_id
                """,
                (member_id,),
            )
            return [dict(record) for record in rows]

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
            _record_activity(
                connection, room_id, "board_acknowledged", member_id, _rfc3339_utc_now()
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
                    AND m.room_id IS NOT NULL
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
            created_at = _rfc3339_utc_now()
            message_id = _next_id(connection, "message_id")
            connection.execute(
                """
                INSERT INTO messages (
                    message_id, room_id, sender_member_id, client_message_id, text,
                    mention_member_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    room_id,
                    sender,
                    client_message_id,
                    text,
                    mention_member_id,
                    created_at,
                ),
            )
            _record_activity(
                connection, room_id, "message", sender, created_at, message_id
            )
            recipients = [
                row["member_id"]
                for row in connection.execute(
                    """
                    SELECT member_id FROM room_members
                    WHERE room_id = ? AND member_id != ? ORDER BY member_id
                    """,
                    (room_id, sender),
                )
            ]
            for recipient in recipients:
                connection.execute(
                    """
                    INSERT INTO inbox_events (event_id, member_id, message_id, kind)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        _next_id(connection, "event_id"),
                        recipient,
                        message_id,
                        "mention" if recipient == mention_member_id else "message",
                    ),
                )
            message = dict(
                connection.execute(
                    f"SELECT {_MESSAGE} FROM messages AS m WHERE m.message_id = ?",
                    (message_id,),
                ).fetchone()
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
        return message

    def open_dm(self, token: str, peer_member_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            if peer_member_id == member_id:
                raise CollaborationError("invalid_peer")
            if _available_member(connection, peer_member_id) is None:
                raise CollaborationError("invalid_peer")
            member_low, member_high = sorted((member_id, peer_member_id))
            connection.execute(
                """
                INSERT INTO dms (dm_id, member_low, member_high) VALUES (?, ?, ?)
                ON CONFLICT (member_low, member_high) DO NOTHING
                """,
                (str(uuid4()), member_low, member_high),
            )
            dm_id = connection.execute(
                "SELECT dm_id FROM dms WHERE member_low = ? AND member_high = ?",
                (member_low, member_high),
            ).fetchone()["dm_id"]
            return {"dm_id": dm_id, "peer_member_id": peer_member_id}

    def send_dm(
        self, token: str, dm_id: str, client_message_id: str, text: str
    ) -> dict:
        with self._transaction(write=True) as connection:
            sender = _member(connection, token)["member_id"]
            dm = _dm_participant(connection, dm_id, sender)
            peer = dm["member_high"] if sender == dm["member_low"] else dm["member_low"]
            existing = connection.execute(
                f"""
                SELECT {_DM_MESSAGE} FROM messages AS m
                WHERE m.dm_id = ? AND m.sender_member_id = ? AND m.client_message_id = ?
                """,
                (dm_id, sender, client_message_id),
            ).fetchone()
            if existing is not None:
                if existing["text"] != text:
                    raise CollaborationError("conflict")
                return dict(existing)
            if _available_member(connection, peer) is None:
                raise CollaborationError("not_found")
            message_id = _next_id(connection, "message_id")
            connection.execute(
                """
                INSERT INTO messages (
                    message_id, room_id, dm_id, sender_member_id, client_message_id,
                    text, mention_member_id, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    message_id,
                    dm_id,
                    sender,
                    client_message_id,
                    text,
                    _rfc3339_utc_now(),
                ),
            )
            connection.execute(
                """
                INSERT INTO inbox_events (event_id, member_id, message_id, kind)
                VALUES (?, ?, ?, 'dm')
                """,
                (_next_id(connection, "event_id"), peer, message_id),
            )
            message = dict(
                connection.execute(
                    f"SELECT {_DM_MESSAGE} FROM messages AS m WHERE m.message_id = ?",
                    (message_id,),
                ).fetchone()
            )
        self._notify((peer,))
        return message

    def dm_history(self, token: str, dm_id: str, after: int, limit: int) -> dict:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            dm = _dm_participant(connection, dm_id, member_id)
            watermark = dm["expired_through_message_id"]
            cursor = min(max(after, 0), _SQLITE_MAX_ID)
            rows = connection.execute(
                f"""
                SELECT {_DM_MESSAGE} FROM messages AS m
                WHERE m.dm_id = ? AND m.message_id > ? ORDER BY m.message_id LIMIT ?
                """,
                (dm_id, cursor, limit),
            )
            history = {"messages": [dict(record) for record in rows]}
            if watermark is not None:
                history["expired_through_message_id"] = watermark
                history["history_truncated"] = cursor < watermark
            return history

    def expire_due_dms(self, cutoff: datetime) -> tuple[int, bool]:
        """Delete one bounded batch of DM messages due at ``cutoff``.

        Candidate selection runs in the owner's read transaction with the
        aware-UTC cutoff applied SQL-side and a bounded result, so the
        potentially linear scan never holds the write lock. One atomic
        write transaction then rechecks the selected IDs -- still
        present, still DM rows, still due at the same cutoff -- and
        deletes the survivors together with the inbox events referencing
        them, advancing each affected conversation's
        ``expired_through_message_id`` watermark to the greatest ID this
        pass actually deleted. A concurrent expiry that removed or
        re-dated candidates between the two transactions only drops them
        from the recheck: the returned count reflects exactly this
        pass's deletions and no watermark regresses. A row is due when
        its ``created_at`` parses as an RFC 3339 timestamp with an
        explicit offset at or before ``cutoff``; rows without a usable
        offset survive. Room rows are never selected. The ID counters
        keep their allocated values. Returns the number of deleted
        messages and whether more due rows remain.
        """
        if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
            raise ValueError("DM expiry cutoff must be a timezone-aware datetime")
        cutoff_micros = _utc_micros(cutoff)
        with self._transaction() as connection:
            _register_expiry_clock(connection)
            candidates = connection.execute(
                f"{_DM_DUE_MESSAGES} ORDER BY message_id LIMIT ?",
                (cutoff_micros, _DM_EXPIRY_BATCH + 1),
            ).fetchall()
        if not candidates:
            return 0, False
        more_due = len(candidates) > _DM_EXPIRY_BATCH
        selected = [row["message_id"] for row in candidates[:_DM_EXPIRY_BATCH]]
        selected_placeholders = ",".join("?" * len(selected))
        with self._transaction(write=True) as connection:
            _register_expiry_clock(connection)
            due = connection.execute(
                f"{_DM_DUE_MESSAGES} AND message_id IN ({selected_placeholders})"
                " ORDER BY message_id",
                (cutoff_micros, *selected),
            ).fetchall()
            if not due:
                return 0, more_due
            message_ids = [row["message_id"] for row in due]
            placeholders = ",".join("?" * len(message_ids))
            connection.execute(
                f"DELETE FROM inbox_events WHERE message_id IN ({placeholders})",
                message_ids,
            )
            connection.execute(
                f"DELETE FROM messages WHERE message_id IN ({placeholders})",
                message_ids,
            )
            watermarks: dict[str, int] = {}
            for row in due:
                watermarks[row["dm_id"]] = max(
                    watermarks.get(row["dm_id"], 0), row["message_id"]
                )
            for dm_id, watermark in watermarks.items():
                connection.execute(
                    """
                    UPDATE dms SET expired_through_message_id =
                        MAX(COALESCE(expired_through_message_id, 0), ?)
                    WHERE dm_id = ?
                    """,
                    (watermark, dm_id),
                )
            return len(due), more_due

    def history(
        self,
        token: str,
        room_id: str,
        after: int,
        limit: int,
        before: int | None = None,
    ) -> dict:
        with self._transaction() as connection:
            membership = _authorized_membership(connection, token, room_id)
            cursor = min(max(after, _transcript_floor(membership)), _SQLITE_MAX_ID)
            upper = None if before is None or before > _SQLITE_MAX_ID else before
            rows = _room_transcript(
                connection,
                room_id,
                after=cursor,
                before=upper,
                limit=limit,
                newest_first=before is not None,
            )
            return {
                "messages": [dict(record) for record in rows],
                **_expiry_metadata(
                    membership["expired_through_message_id"], cursor, upper
                ),
            }

    def search(
        self, token: str, room_id: str, query: str, after: int, limit: int
    ) -> dict:
        with self._transaction() as connection:
            membership = _authorized_membership(connection, token, room_id)
            cursor = min(max(after, _transcript_floor(membership)), _SQLITE_MAX_ID)
            connection.create_function(
                "teamup_casefold", 1, str.casefold, deterministic=True
            )
            needle = query.casefold()
            rows = connection.execute(
                """
                SELECT m.message_id, m.sender_member_id, m.created_at, m.text AS body,
                    instr(teamup_casefold(m.text), ?) AS hit
                FROM messages AS m
                WHERE m.room_id = ? AND m.message_id > ?
                    AND instr(teamup_casefold(m.text), ?) > 0
                ORDER BY m.message_id
                LIMIT ?
                """,
                (needle, room_id, cursor, needle, limit),
            ).fetchall()
            return {
                "matches": [
                    {
                        "message_id": row["message_id"],
                        "sender_member_id": row["sender_member_id"],
                        "created_at": row["created_at"],
                        "snippet": _snippet(row["body"], needle, row["hit"]),
                    }
                    for row in rows
                ],
                **_expiry_metadata(
                    membership["expired_through_message_id"], cursor, None
                ),
            }

    def inbox(
        self, token: str, after: int, limit: int, kinds: Iterable[str]
    ) -> list[dict]:
        kind_filter = tuple(kinds)
        if not kind_filter:
            raise CollaborationError("invalid_request")
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            rows = connection.execute(
                f"""
                SELECT e.event_id, e.kind, m.message_id, m.room_id, m.dm_id,
                    m.sender_member_id, m.client_message_id, m.text,
                    m.mention_member_id, m.created_at
                FROM inbox_events AS e
                JOIN messages AS m ON m.message_id = e.message_id
                WHERE e.member_id = ? AND e.event_id > ? AND e.acknowledged = 0
                    AND e.kind IN ({", ".join("?" * len(kind_filter))})
                    AND (
                        EXISTS (
                            SELECT 1 FROM dms AS d
                            WHERE d.dm_id = m.dm_id
                                AND e.member_id IN (d.member_low, d.member_high)
                        )
                        OR EXISTS (
                            SELECT 1 FROM room_members AS rm
                            JOIN rooms AS r ON r.room_id = rm.room_id
                            WHERE rm.room_id = m.room_id AND rm.member_id = e.member_id
                                AND rm.acked_board_version = r.version
                        )
                    )
                ORDER BY e.event_id LIMIT ?
                """,
                (member_id, min(after, _SQLITE_MAX_ID), *kind_filter, limit),
            )
            events = []
            for record in rows:
                message_columns = (
                    _DM_EVENT_MESSAGE if record["kind"] == "dm" else _ROOM_EVENT_MESSAGE
                )
                events.append(
                    {
                        "event_id": record["event_id"],
                        "kind": record["kind"],
                        "message": {
                            column: record[column] for column in message_columns
                        },
                    }
                )
            return events

    def acknowledge_event(self, token: str, event_id: int) -> None:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            if event_id > _SQLITE_MAX_ID:
                raise CollaborationError("not_found")
            event = connection.execute(
                """
                SELECT m.room_id, m.dm_id, m.message_id, e.acknowledged
                FROM inbox_events AS e
                JOIN messages AS m ON m.message_id = e.message_id
                WHERE e.event_id = ? AND e.member_id = ?
                """,
                (event_id, member_id),
            ).fetchone()
            if event is None:
                raise CollaborationError("not_found")
            if event["dm_id"] is None:
                room = _room_member(connection, event["room_id"], member_id)
                _require_current_board(room)
            else:
                _dm_participant(connection, event["dm_id"], member_id)
            if event["acknowledged"]:
                return
            connection.execute(
                "UPDATE inbox_events SET acknowledged = 1 WHERE event_id = ?",
                (event_id,),
            )
            if event["dm_id"] is None:
                # DM acknowledgements never create room activity.
                _record_activity(
                    connection,
                    event["room_id"],
                    "delivery",
                    member_id,
                    _rfc3339_utc_now(),
                    event["message_id"],
                )
        self._notify((member_id,))

    def expire_due(
        self, max_age: timedelta, now: datetime | None = None
    ) -> RetentionPass:
        """Commit one bounded batch of expired room messages and deliveries.

        Candidate selection runs in the owner's read transaction with the
        aware-UTC cutoff applied SQL-side and a bounded result, so the
        potentially linear scan never holds the write lock. One atomic
        write transaction then rechecks the selected IDs -- still
        present, still room rows, still due at the same cutoff -- and
        deletes the survivors together with every inbox reference and
        the owning rooms' watermark advances. A concurrent expiry that
        removed or re-dated candidates between the two transactions only
        drops them from the recheck: the pass counts exactly the messages
        it deleted, each watermark advances only to a message this pass
        actually deleted, and no watermark regresses. Messages recorded
        at or before ``now - max_age`` expire; records without a usable
        RFC 3339 offset stay untouched. An explicitly supplied ``now``
        must be a timezone-aware datetime -- naive or non-datetime
        values are rejected before any conversion. The ID counters
        never move, so later messages and events keep new, higher IDs.
        """
        if max_age <= timedelta(0):
            raise ValueError("The retention age must be positive")
        if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
            raise ValueError("The expiry clock must be a timezone-aware datetime")
        moment = datetime.now(UTC) if now is None else now.astimezone(UTC)
        cutoff = _utc_micros(moment - max_age)
        with self._transaction() as connection:
            _register_expiry_clock(connection)
            candidates = connection.execute(
                f"{_ROOM_DUE_MESSAGES} ORDER BY message_id LIMIT ?",
                (cutoff, _EXPIRE_BATCH),
            ).fetchall()
        if not candidates:
            return RetentionPass(0, 0, False)
        more_due = len(candidates) == _EXPIRE_BATCH
        selected = [row["message_id"] for row in candidates]
        selected_placeholders = ",".join("?" * len(selected))
        with self._transaction(write=True) as connection:
            _register_expiry_clock(connection)
            due = connection.execute(
                f"{_ROOM_DUE_MESSAGES} AND message_id IN ({selected_placeholders})"
                " ORDER BY message_id",
                (cutoff, *selected),
            ).fetchall()
            if not due:
                return RetentionPass(0, 0, more_due)
            expired = [row["message_id"] for row in due]
            placeholders = ",".join("?" * len(expired))
            expired_events = connection.execute(
                f"DELETE FROM inbox_events WHERE message_id IN ({placeholders})",
                expired,
            ).rowcount
            connection.execute(
                f"DELETE FROM messages WHERE message_id IN ({placeholders})",
                expired,
            )
            watermarks: dict[str, int] = {}
            for row in due:
                watermarks[row["room_id"]] = row["message_id"]
            connection.executemany(
                """
                UPDATE rooms SET expired_through_message_id
                    = MAX(COALESCE(expired_through_message_id, 0), ?)
                WHERE room_id = ?
                """,
                [(through, room_id) for room_id, through in watermarks.items()],
            )
            return RetentionPass(len(due), expired_events, more_due)

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
