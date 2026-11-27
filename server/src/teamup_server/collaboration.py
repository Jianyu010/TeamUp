"""Transactional owner of TeamUp collaboration state."""

import json
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
from typing import NoReturn
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError

from .ops_metrics import ObservationSource, SourceSnapshot

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
        AND EXISTS (
            SELECT 1 FROM rooms AS r WHERE r.room_id = messages.room_id AND r.archived_at IS NULL
        )
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
_MAINTENANCE_SCOPES = ("maintenance_read", "logs_read")
_LOG_COMPONENTS = ("host", "gateway", "collaboration")
_LOG_WINDOW_MICROS = 3_600_000_000
_LOG_RESULT_LIMIT = 500
_OBSERVATION_FIELDS = (
    "db_size_bytes",
    "schema_version",
    "sqlite_busy_errors_total",
    "transactions_total",
    "transaction_duration_seconds_total",
    "members_stored",
    "team_rooms_stored",
    "messages_stored",
    "pending_inbox_events",
    "oldest_pending_age_seconds",
    "login_failures_total",
    "rate_limited_total",
    "authorization_denials_total",
)
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2, memory_cost=19 * 1024, parallelism=1, type=Type.ID
)
_MEMBER = "member_id, owner_id, role_description"
_MEMBER_PROFILE = """
    m.member_id, m.role_description, m.description, a.username AS owner_username
"""
_PROFILE_TABLES = "members AS m LEFT JOIN accounts AS a ON a.owner_id = m.owner_id"
_MESSAGE = """
    m.message_id, m.room_id, m.sender_member_id, m.client_message_id, m.text, m.mention_member_id, m.created_at
"""
# The first target remains in messages for compatibility with existing rows.
# An indexed, bounded lookup projects the remaining targets in request order.
_ADDITIONAL_MENTIONS = """
    (SELECT json_group_array(member_id) FROM (
        SELECT member_id FROM room_message_additional_mentions
        WHERE message_id = m.message_id ORDER BY position
    )) AS additional_mentions
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
        AND rm.acked_board_version = r.version AND r.archived_at IS NULL
"""
_TASK = """
    t.task_id, t.task_number, t.room_id, t.creator_member_id, t.title,
    t.description, t.status, t.version, t.created_at, t.updated_at,
    t.started_at, t.last_progress_at, t.finished_at, a.closing_criteria, a.eta
"""
_VISIBLE_TASK = """
    (
        t.creator_member_id = ?
        OR NOT EXISTS (
            SELECT 1 FROM task_addressees AS a WHERE a.task_id = t.task_id
        )
        OR EXISTS (
            SELECT 1 FROM task_addressees AS a
            WHERE a.task_id = t.task_id AND a.member_id = ?
        )
    )
"""
_ROOM_IDLE_MICROS = 24 * 60 * 60 * 1_000_000
_ROOM_ARCHIVE_BATCH = 100
_HEARTBEAT_MICROS = 45_000_000
_LEADERSHIP_GRACE = 120_000_000
_CHALLENGE_INTERVAL = 30 * 60_000_000
_LEADERSHIP_BATCH = 100
_LEADERSHIP_TERMS = re.compile(
    r"\b(?:lead|leader|coordinator|manager|supervisor|director|captain|chief)\b|负责人|组长|队长|主管|协调员",
    re.IGNORECASE,
)
_TRACER_TASK_BATCH = 100
_REMINDER_PAGE_SCAN = 1000

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

_LIFECYCLE_SCHEMA = (
    "ALTER TABLE room_members ADD COLUMN membership_id TEXT NOT NULL DEFAULT ''",
    "UPDATE room_members SET membership_id = lower(hex(randomblob(16)))",
    """
    CREATE TABLE retired_room_access (
        member_id TEXT NOT NULL REFERENCES members(member_id),
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        after_message_id INTEGER NOT NULL,
        through_message_id INTEGER NOT NULL,
        PRIMARY KEY (member_id, room_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE room_departures (
        event_id INTEGER PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
    )
    """,
    """
    CREATE INDEX pending_departures ON room_departures (member_id, event_id)
    WHERE acknowledged = 0
    """,
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

_TASK_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        task_number INTEGER NOT NULL CHECK (task_number > 0),
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        creator_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_task_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL CHECK (status IN ('open', 'finished')),
        version INTEGER NOT NULL CHECK (version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        last_progress_at TEXT,
        finished_at TEXT,
        UNIQUE (room_id, task_number),
        UNIQUE (creator_member_id, client_task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_addressees (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        member_id TEXT NOT NULL REFERENCES members(member_id),
        PRIMARY KEY (task_id, member_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_participants (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        member_id TEXT NOT NULL REFERENCES members(member_id),
        joined_at TEXT NOT NULL,
        PRIMARY KEY (task_id, member_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        actor_member_id TEXT NOT NULL REFERENCES members(member_id),
        kind TEXT NOT NULL CHECK (kind IN ('created', 'joined', 'progress', 'finished')),
        created_at TEXT NOT NULL,
        note TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS task_room_order ON tasks (room_id, task_number)",
    "CREATE INDEX IF NOT EXISTS task_event_order ON task_events (task_id, event_id)",
    """
    CREATE TABLE IF NOT EXISTS task_retries (
        actor_member_id TEXT NOT NULL REFERENCES members(member_id),
        retry_kind TEXT NOT NULL CHECK (retry_kind IN ('create', 'progress')),
        client_key TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        fingerprint TEXT NOT NULL,
        PRIMARY KEY (actor_member_id, retry_kind, client_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_reminders (
        reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        member_id TEXT NOT NULL REFERENCES members(member_id),
        anchor_event_id INTEGER NOT NULL,
        stalled_since TEXT NOT NULL,
        created_at TEXT NOT NULL,
        claimed_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS task_reminder_pending
    ON task_reminders (task_id, member_id, anchor_event_id)
    WHERE claimed_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS task_reminder_member_page
    ON task_reminders (member_id, reminder_id)
    WHERE claimed_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS task_reminder_anchor
    ON task_reminders (task_id, member_id, anchor_event_id, reminder_id)
    """,
    "CREATE INDEX IF NOT EXISTS task_tracer_scan ON tasks (status, task_id)",
)

_CHILD_LEASE = timedelta(hours=5)
_CHILD_NOTICE_US = 15 * 60 * 1_000_000
_CHILD_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS child_rooms (
        room_id TEXT PRIMARY KEY REFERENCES rooms(room_id),
        parent_room_id TEXT NOT NULL REFERENCES rooms(room_id) CHECK (parent_room_id != room_id),
        creator_member_id TEXT NOT NULL REFERENCES members(member_id),
        client_request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_us INTEGER NOT NULL,
        offered_epoch INTEGER,
        renewed_from_us INTEGER,
        renewed_by TEXT REFERENCES members(member_id),
        UNIQUE (creator_member_id, client_request_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS child_rooms_parent ON child_rooms (parent_room_id, room_id)",
    "CREATE INDEX IF NOT EXISTS child_rooms_deadline ON child_rooms (expires_us, room_id)",
    """
    CREATE TABLE IF NOT EXISTS task_origins (
        successor_task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
        source_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id)
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

_MAINTENANCE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS operator_maintenance_grants (
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        scope TEXT NOT NULL CHECK (scope IN ('maintenance_read', 'logs_read')),
        PRIMARY KEY (owner_id, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS log_access_audit (
        owner_id TEXT NOT NULL REFERENCES accounts(owner_id),
        window_since TEXT NOT NULL,
        window_until TEXT NOT NULL,
        component TEXT CHECK (component IN ('host', 'gateway', 'collaboration')),
        result_count INTEGER NOT NULL CHECK (result_count BETWEEN 0 AND 500),
        at TEXT NOT NULL
    )
    """,
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


def _migrate_room_tasks(connection: sqlite3.Connection) -> None:
    """Install the task feature schema without taking a shared migration number."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS room_task_schema (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL CHECK (version > 0)
        )
        """
    )
    row = connection.execute(
        "SELECT version FROM room_task_schema WHERE singleton = 1"
    ).fetchone()
    if row is None:
        for statement in _TASK_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO room_task_schema (singleton, version) VALUES (1, 1)"
        )
    elif row["version"] != 1:
        raise ValueError("Unsupported room task schema version")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_assignments (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
            closing_criteria TEXT NOT NULL,
            eta TEXT
        )
        """
    )


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


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value)


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


def _profile_text(value: str | None, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value == value.strip()
        and value.isprintable()
    )


def _profile_name(value: str | None) -> bool:
    return value is not None and _profile_text(value, 200) and len(value.split()) <= 3


def _member_profile(row: sqlite3.Row) -> dict:
    profile = {field: row[field] for field in ("member_id", "role_description")}
    if row["description"] is not None:
        profile["name"] = row["role_description"]
        profile["description"] = row["description"]
    if row["owner_username"] is not None:
        profile["owner_username"] = row["owner_username"]
    return profile


def _read_member_profile(connection: sqlite3.Connection, member_id: str) -> dict:
    row = connection.execute(
        f"SELECT {_MEMBER_PROFILE} FROM {_PROFILE_TABLES} WHERE m.member_id = ?",
        (member_id,),
    ).fetchone()
    return _member_profile(row)


def _member(
    connection: sqlite3.Connection, token: str, *, include_retired: bool = False
) -> sqlite3.Row:
    digest = _credential_digest(token)
    fields = f"{_MEMBER}, retired" if include_retired else _MEMBER
    record = connection.execute(
        f"""
        SELECT {fields} FROM members WHERE token_digest = ? AND (retired = 0 OR ?)
            AND NOT EXISTS (
                SELECT 1 FROM accounts
                WHERE accounts.owner_id = members.owner_id AND disabled = 1
            )
        """,
        (digest, include_retired),
    ).fetchone()
    if record is None:
        raise CollaborationError("unauthorized")
    return record


def _room_member(
    connection: sqlite3.Connection, room_id: str, member_id: str, *, active: bool = False
) -> sqlite3.Row:
    record = connection.execute(
        """
        SELECT r.name, r.version, r.rules, r.protocol, r.history_policy,
            r.expired_through_message_id, r.archived_at,
            r.archive_reason, r.archive_roster, r.last_chat_at, rm.read_board_version,
            c.parent_room_id, c.created_at, c.expires_us, c.expires_us <= teamup_now_us() AS lease_expired,
            rm.acked_board_version, rm.joined_after, rm.membership_id
        FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
        LEFT JOIN child_rooms AS c ON c.room_id = r.room_id
        WHERE rm.room_id = ? AND rm.member_id = ?
        """,
        (room_id, member_id),
    ).fetchone()
    if record is None:
        raise CollaborationError("not_found")
    if active and (record["archived_at"] is not None or record["lease_expired"]):
        raise CollaborationError("room_archived")
    return record


def _room_leader(connection: sqlite3.Connection, room_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM room_leadership WHERE room_id = ?", (room_id,)
    ).fetchone()


def _has_room_leadership(connection: sqlite3.Connection, room_id: str, member_id: str) -> bool:
    return connection.execute(
        """
        SELECT 1 FROM room_leadership AS l JOIN room_members AS rm
            ON rm.room_id = l.room_id AND rm.member_id = l.leader_member_id
        WHERE l.room_id = ? AND l.leader_member_id = ?
        """,
        (room_id, member_id),
    ).fetchone() is not None


def _child_metadata(row: sqlite3.Row | None) -> dict:
    if row is None or row["parent_room_id"] is None:
        return {}
    return {
        "parent_room_id": row["parent_room_id"],
        "created_at": row["created_at"],
        "expires_at": _rfc3339(_EPOCH + timedelta(microseconds=row["expires_us"])),
    }


def _child_row(connection: sqlite3.Connection, room_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM child_rooms WHERE room_id = ?", (room_id,)).fetchone()


def _archive_metadata(row: sqlite3.Row) -> dict:
    if row["archived_at"] is None:
        return {}
    return {key: row[key] for key in ("archived_at", "archive_reason")}


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
        WHERE member_id = ? AND retired = 0 AND NOT EXISTS (
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
    connection: sqlite3.Connection, token: str, room_id: str, *, active: bool = False
) -> sqlite3.Row:
    member_id = _member(connection, token)["member_id"]
    membership = _room_member(connection, room_id, member_id, active=active)
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


def _room_message(row: sqlite3.Row) -> dict:
    message = {key: row[key] for key in _ROOM_EVENT_MESSAGE}
    if row["additional_mentions"] != "[]":
        message["mention_member_ids"] = [
            row["mention_member_id"],
            *json.loads(row["additional_mentions"]),
        ]
    return message


def _retired_sender(row: sqlite3.Row) -> dict:
    return {"sender_retired": True} if row["sender_retired"] else {}


def _room_transcript(
    connection: sqlite3.Connection,
    room_id: str,
    *,
    after: int = 0,
    before: int | None = None,
    through: int | None = None,
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
    if through is not None:
        bounds += " AND message_id <= ?"
        bound_values.append(through)
    rows = connection.execute(
        f"""
        SELECT {_MESSAGE}, {_ADDITIONAL_MENTIONS}, sender.retired AS sender_retired
        FROM messages AS m JOIN members AS sender ON sender.member_id = m.sender_member_id
        WHERE m.room_id = ? AND {bounds}
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


def _task_fingerprint(values: Iterable[object]) -> str:
    canonical = json.dumps(
        list(values), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _task_row(
    connection: sqlite3.Connection, room_id: str, task_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        f"""
        SELECT {_TASK} FROM tasks AS t
        LEFT JOIN task_assignments AS a USING (task_id)
        WHERE t.room_id = ? AND t.task_id = ?
        """,
        (room_id, task_id),
    ).fetchone()


def _task_visible(
    connection: sqlite3.Connection, task: sqlite3.Row, member_id: str
) -> bool:
    if task["creator_member_id"] == member_id:
        return True
    addressed = connection.execute(
        "SELECT 1 FROM task_addressees WHERE task_id = ? LIMIT 1", (task["task_id"],)
    ).fetchone()
    if addressed is None:
        return True
    return (
        connection.execute(
            """
            SELECT 1 FROM task_addressees
            WHERE task_id = ? AND member_id = ?
            """,
            (task["task_id"], member_id),
        ).fetchone()
        is not None
    )


def _task_anchor_event_id(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> int | None:
    """The first join or latest progress event that opens an activity period."""
    if task["last_progress_at"] is not None:
        query = """
            SELECT MAX(event_id) FROM task_events
            WHERE task_id = ? AND kind = 'progress'
        """
    else:
        query = """
            SELECT MIN(event_id) FROM task_events
            WHERE task_id = ? AND kind = 'joined'
        """
    return connection.execute(query, (task["task_id"],)).fetchone()[0]


def _task_stall_at(task: sqlite3.Row, stall_after: int | None) -> datetime | None:
    if stall_after is None or task["status"] != "open":
        return None
    anchor = task["last_progress_at"] or task["started_at"]
    if anchor is None:
        return None
    try:
        return _parse_rfc3339(anchor) + timedelta(seconds=stall_after)
    except OverflowError:
        # An interval beyond the representable calendar cannot have elapsed.
        return None


def _task_stalled_since(
    task: sqlite3.Row, stall_after: int | None, now: datetime
) -> str | None:
    stalled_at = _task_stall_at(task, stall_after)
    if stalled_at is None or now < stalled_at:
        return None
    return _rfc3339(stalled_at)


def _reminder_eligible(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    member_id: str,
) -> bool:
    if task["status"] != "open":
        return False
    if not _task_visible(connection, task, member_id):
        return False
    if _available_member(connection, member_id) is None:
        return False
    membership = connection.execute(
        """
        SELECT 1 FROM room_members AS rm JOIN rooms AS r USING (room_id)
        WHERE rm.room_id = ? AND rm.member_id = ?
            AND rm.acked_board_version = r.version AND r.archived_at IS NULL
        """,
        (task["room_id"], member_id),
    ).fetchone()
    if membership is None:
        return False
    if task["closing_criteria"] is not None and member_id == task["creator_member_id"]:
        return True
    return (
        connection.execute(
            """
            SELECT 1 FROM task_participants
            WHERE task_id = ? AND member_id = ?
            """,
            (task["task_id"], member_id),
        ).fetchone()
        is not None
    )


def _task_reminder_recipients(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> list[str]:
    return [
        row["member_id"]
        for row in connection.execute(
            """
            WITH candidates(member_id) AS (
                SELECT member_id FROM task_participants WHERE task_id = ?
                UNION
                SELECT ? WHERE ? IS NOT NULL
            )
            SELECT c.member_id
            FROM candidates AS c
            JOIN members AS m ON m.member_id = c.member_id
            JOIN room_members AS rm
                ON rm.room_id = ? AND rm.member_id = c.member_id
            JOIN rooms AS r ON r.room_id = rm.room_id
            WHERE rm.acked_board_version = r.version AND m.retired = 0
                AND NOT EXISTS (
                    SELECT 1 FROM accounts AS a
                    WHERE a.owner_id = m.owner_id AND a.disabled = 1
                )
                AND (
                    c.member_id = ?
                    OR NOT EXISTS (
                        SELECT 1 FROM task_addressees AS a WHERE a.task_id = ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM task_addressees AS a
                        WHERE a.task_id = ? AND a.member_id = c.member_id
                    )
                )
            ORDER BY c.member_id
            """,
            (
                task["task_id"], task["creator_member_id"], task["closing_criteria"],
                task["room_id"], task["creator_member_id"], task["task_id"], task["task_id"],
            ),
        )
    ]


def _reminder_projection(reminder: sqlite3.Row) -> dict:
    return {
        "reminder_id": reminder["reminder_id"],
        "room_id": reminder["room_id"],
        "task_id": reminder["task_id"],
        "title": reminder["title"],
        "stalled_since": reminder["stalled_since"],
    }


def _task_members(
    connection: sqlite3.Connection, task_ids: list[str]
) -> dict[str, tuple[list[str], list[str]]]:
    members: dict[str, tuple[list[str], list[str]]] = {
        task_id: ([], []) for task_id in task_ids
    }
    if not task_ids:
        return members
    placeholders = ", ".join("?" for _ in task_ids)
    for task_id, member_id, kind in connection.execute(
        f"""
        SELECT task_id, member_id, 0 FROM task_addressees
        WHERE task_id IN ({placeholders})
        UNION ALL
        SELECT task_id, member_id, 1 FROM task_participants
        WHERE task_id IN ({placeholders})
        ORDER BY member_id
        """,
        (*task_ids, *task_ids),
    ):
        members[task_id][kind].append(member_id)
    return members


def _task_projection(
    task: sqlite3.Row,
    addressed: list[str],
    participants: list[str],
    *,
    stall_after: int | None,
    now: datetime,
) -> dict:
    task_id = task["task_id"]
    projection = {
        "task_id": task_id,
        "task_number": task["task_number"],
        "room_id": task["room_id"],
        "creator_member_id": task["creator_member_id"],
        "title": task["title"],
        "description": task["description"],
        "addressed_member_ids": addressed,
        "participant_member_ids": participants,
        "status": task["status"],
        "version": task["version"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "started_at": task["started_at"],
        "last_progress_at": task["last_progress_at"],
        "finished_at": task["finished_at"],
        "stalled_since": _task_stalled_since(task, stall_after, now),
    }
    if task["closing_criteria"] is not None:
        projection["assignment"] = {
            "closing_criteria": task["closing_criteria"],
            "eta": task["eta"],
        }
    return projection


def _authorized_task(
    connection: sqlite3.Connection, token: str, room_id: str, task_id: str, *, active: bool = False
) -> tuple[str, sqlite3.Row]:
    member_id = _member(connection, token)["member_id"]
    membership = _room_member(connection, room_id, member_id, active=active)
    task = _task_row(connection, room_id, task_id)
    if task is None or not _task_visible(connection, task, member_id):
        raise CollaborationError("not_found")
    _require_current_board(membership)
    return member_id, task


def _validated_addressees(
    connection: sqlite3.Connection,
    room_id: str,
    addressed_member_ids: Iterable[str] | None,
) -> list[str]:
    addressees = list(addressed_member_ids or ())
    if len(addressees) > 100 or len(set(addressees)) != len(addressees):
        raise CollaborationError("invalid_request")
    for member_id in addressees:
        if (
            connection.execute(
                "SELECT 1 FROM room_members WHERE room_id = ? AND member_id = ?",
                (room_id, member_id),
            ).fetchone()
            is None
        ):
            raise CollaborationError("invalid_request")
    return sorted(addressees)


def _add_task_event(
    connection: sqlite3.Connection,
    task_id: str,
    actor_member_id: str,
    kind: str,
    created_at: str,
    note: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO task_events (task_id, actor_member_id, kind, created_at, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, actor_member_id, kind, created_at, note),
    )


class Collaboration:
    """Public operations and post-commit inbox subscriptions."""

    def __init__(
        self,
        db_path: str | Path,
        enrollment_keys: Mapping[str, str],
        *,
        task_stall_after: int | None = None,
        task_reminder_interval: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        if (task_stall_after is None) != (task_reminder_interval is None):
            raise ValueError(
                "task_stall_after and task_reminder_interval are both required"
            )
        if (
            task_stall_after is not None
            and task_reminder_interval is not None
            and min(task_stall_after, task_reminder_interval) <= 0
        ):
            raise ValueError("Task tracing durations must be positive")
        self._task_stall_after = task_stall_after
        self._task_reminder_interval = task_reminder_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scan_cursor = ""
        self._db_path = Path(db_path).resolve()
        self._enrollment_keys = _enrollment_digests(enrollment_keys)
        self._listeners: dict[str, set[ChangeListener]] = {}
        self._listeners_lock = Lock()
        self._login_checks: deque[_LoginCheck] = deque()
        self._login_checks_lock = Lock()
        self._password_checks = BoundedSemaphore(4)
        self._unknown_password_digest = _PASSWORD_HASHER.hash(token_bytes(32))
        self._metrics_lock = Lock()
        self._sqlite_busy_errors_total = 0
        self._transactions_total = 0
        self._transaction_duration_seconds_total = 0.0
        self._login_failures_total = 0
        self._rate_limited_total = 0
        self._authorization_denials = dict.fromkeys(
            ("missing_session", "wrong_purpose", "inactive_session", "missing_grant"), 0
        )
        self._maintenance_observations = ObservationSource(
            "collaboration", _OBSERVATION_FIELDS, clock=monotonic, timestamp=self._now
        )
        with suppress(FileExistsError):
            os.close(
                os.open(self._db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            )
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        with self._transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= 12:
                raise ValueError("Unsupported TeamUp database schema version")
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
            if version in (0, 1, 2, 3, 4, 5, 6):
                connection.execute(
                    """
                    CREATE TABLE room_message_additional_mentions (
                        message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
                        position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 15),
                        member_id TEXT NOT NULL REFERENCES members(member_id),
                        PRIMARY KEY (message_id, position),
                        UNIQUE (message_id, member_id)
                    )
                    """
                )
            if version < 8:
                connection.execute(
                    """
                    ALTER TABLE members ADD COLUMN retired INTEGER NOT NULL DEFAULT 0
                        CHECK (retired IN (0, 1))
                    """
                )
            if version < 9:
                for statement in _LIFECYCLE_SCHEMA:
                    connection.execute(statement)
            _migrate_room_tasks(connection)
            if "creator_member_id" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(rooms)")
            }:
                connection.execute(
                    "ALTER TABLE rooms ADD COLUMN creator_member_id TEXT REFERENCES members(member_id)"
                )
            if version < 10:
                # Room creation has always logged its initial join since activity tracking
                # began. The activity migration backfilled earlier rooms without timestamps.
                connection.execute(
                    """
                    UPDATE rooms SET creator_member_id = (
                        SELECT CASE WHEN kind = 'member_joined' AND at IS NOT NULL
                            THEN actor_member_id END
                        FROM room_activity
                        WHERE room_activity.room_id = rooms.room_id
                        ORDER BY activity_id LIMIT 1
                    )
                    WHERE creator_member_id IS NULL
                    """
                )
            if version < 11:
                for table, column in (
                    ("members", "description"),
                    ("room_members", "responsibility"),
                ):
                    if column not in {
                        row["name"]
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} TEXT"
                        )
                connection.execute("PRAGMA user_version = 11")
            for statement in _MAINTENANCE_SCHEMA:
                connection.execute(statement)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_admin_grants (
                    owner_id TEXT PRIMARY KEY REFERENCES accounts (owner_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS room_invitation_requests (
                    sender_member_id TEXT NOT NULL REFERENCES members(member_id),
                    client_request_id TEXT NOT NULL,
                    room_id TEXT NOT NULL REFERENCES rooms(room_id),
                    peer_member_id TEXT NOT NULL REFERENCES members(member_id),
                    message_id INTEGER UNIQUE REFERENCES messages(message_id) ON DELETE SET NULL,
                    PRIMARY KEY (sender_member_id, client_request_id)
                )
                """
            )
            if version < 12:
                self._migrate_room_archive(connection)
                connection.execute("PRAGMA user_version = 12")
            for statement in _CHILD_SCHEMA:
                connection.execute(statement)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS room_control_notices (
                    notice_id INTEGER PRIMARY KEY,
                    member_id TEXT NOT NULL REFERENCES members(member_id),
                    room_id TEXT NOT NULL REFERENCES rooms(room_id),
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    leadership_epoch INTEGER,
                    expires_at TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS room_control_notices_pending
                ON room_control_notices (member_id, notice_id) WHERE acknowledged = 0
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS room_control_notices_scope
                ON room_control_notices (room_id, kind, member_id, leadership_epoch, expires_at)
                """
            )

            if not _has_table(connection, "room_leadership"):
                self._migrate_room_leadership(connection)

    def _migrate_room_leadership(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE room_leadership (
                room_id TEXT PRIMARY KEY REFERENCES rooms(room_id),
                leader_member_id TEXT REFERENCES members(member_id),
                previous_member_id TEXT REFERENCES members(member_id),
                blocked_membership_id TEXT,
                leadership_epoch INTEGER NOT NULL DEFAULT 1,
                challenge_at INTEGER NOT NULL,
                next_check_at INTEGER,
                pending_kind TEXT CHECK (pending_kind IN ('challenge', 'defer')),
                deadline INTEGER,
                leave_after INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute("CREATE INDEX leadership_due ON room_leadership (next_check_at, room_id)")
        connection.execute("CREATE INDEX leadership_by_member ON room_leadership (leader_member_id)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leadership_heartbeats (
                member_id TEXT PRIMARY KEY REFERENCES members(member_id), observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO room_leadership (
                room_id, leader_member_id, previous_member_id, challenge_at, next_check_at
            )
            SELECT r.room_id,
                CASE WHEN m.retired = 0 AND COALESCE(a.disabled, 0) = 0 THEN rm.member_id END,
                r.creator_member_id, ?, ?
            FROM rooms AS r LEFT JOIN room_members AS rm
                ON rm.room_id = r.room_id AND rm.member_id = r.creator_member_id
            LEFT JOIN members AS m ON m.member_id = rm.member_id
            LEFT JOIN accounts AS a ON a.owner_id = m.owner_id
            """,
            ( _utc_micros(self._now_datetime()) + _CHALLENGE_INTERVAL,
              _utc_micros(self._now_datetime()) + _LEADERSHIP_GRACE ),
        )

    def _initialize_room_leadership(
        self, connection: sqlite3.Connection, room_id: str, member_id: str
    ) -> None:
        now = _utc_micros(self._now_datetime())
        connection.execute(
            """
            INSERT INTO room_leadership (room_id, leader_member_id, challenge_at, next_check_at)
            VALUES (?, ?, ?, ?)
            """,
            (room_id, member_id, now + _CHALLENGE_INTERVAL, now + _LEADERSHIP_GRACE),
        )

    def _migrate_room_archive(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(rooms)")}
        if "last_chat_at" in columns:
            return
        now = _utc_micros(self._now_datetime())
        connection.execute("ALTER TABLE rooms ADD COLUMN last_chat_at INTEGER NOT NULL DEFAULT 0")
        connection.execute("ALTER TABLE rooms ADD COLUMN archived_at TEXT")
        connection.execute("ALTER TABLE rooms ADD COLUMN archive_reason TEXT")
        connection.execute("ALTER TABLE rooms ADD COLUMN archive_roster TEXT")
        _register_expiry_clock(connection)
        connection.execute(
            """
            UPDATE rooms SET last_chat_at = COALESCE(
                (
                    SELECT MAX(stamp) FROM (
                        SELECT teamup_expiry_micros(created_at) AS stamp
                        FROM messages WHERE room_id = rooms.room_id
                        UNION ALL
                        SELECT teamup_expiry_micros(at) AS stamp FROM room_activity
                        WHERE room_id = rooms.room_id AND kind = 'message'
                            AND message_id IS NULL
                    )
                ),
                CASE WHEN NOT EXISTS (SELECT 1 FROM messages WHERE room_id = rooms.room_id)
                    AND NOT EXISTS (
                        SELECT 1 FROM room_activity WHERE room_id = rooms.room_id AND kind = 'message'
                    ) THEN (
                        SELECT CASE WHEN kind = 'member_joined' THEN teamup_expiry_micros(at) END
                        FROM room_activity WHERE room_id = rooms.room_id
                        ORDER BY activity_id LIMIT 1
                    )
                END,
                ?
            )
            """,
            (now,),
        )
        connection.execute(
            """
            CREATE INDEX rooms_inactivity ON rooms (last_chat_at, room_id)
            WHERE archived_at IS NULL
            """
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function("teamup_now_us", 0, lambda: _utc_micros(self._now_datetime()))
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        started = monotonic()
        with self._metrics_lock:
            self._transactions_total += 1
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
        except sqlite3.Error as error:
            self._record_sqlite_busy(error)
            raise
        finally:
            duration = monotonic() - started
            with self._metrics_lock:
                self._transaction_duration_seconds_total += duration

    def _record_sqlite_busy(self, error: sqlite3.Error) -> None:
        # The low byte of an extended SQLite result code is its primary code.
        if getattr(error, "sqlite_errorcode", 0) & 0xFF in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):
            with self._metrics_lock:
                self._sqlite_busy_errors_total += 1

    def _now_datetime(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _now(self) -> str:
        return _rfc3339(self._now_datetime())

    def _projection(self, connection: sqlite3.Connection, task: sqlite3.Row, member_id: str) -> dict:
        return self._projections(connection, [task], member_id)[0]

    def _projections(
        self, connection: sqlite3.Connection, tasks: list[sqlite3.Row], member_id: str
    ) -> list[dict]:
        if not tasks:
            return []
        ids = [task["task_id"] for task in tasks]
        marks = ", ".join("?" for _ in ids)
        origins = {
            row["successor_task_id"]: {"room_id": row["room_id"], "task_id": row["task_id"]}
            for row in connection.execute(
                f"""
                SELECT o.successor_task_id, t.room_id, t.task_id
                FROM task_origins AS o JOIN tasks AS t ON t.task_id = o.source_task_id
                JOIN rooms AS r ON r.room_id = t.room_id
                JOIN room_members AS rm ON rm.room_id = r.room_id
                WHERE o.successor_task_id IN ({marks}) AND rm.member_id = ?
                    AND rm.acked_board_version = r.version AND {_VISIBLE_TASK}
                """,
                (*ids, member_id, member_id, member_id),
            )
        }
        members = _task_members(connection, ids)
        now = self._now_datetime()
        projections = [
            _task_projection(
                task,
                *members[task["task_id"]],
                stall_after=self._task_stall_after,
                now=now,
            )
            for task in tasks
        ]
        for projection in projections:
            if projection["task_id"] in origins:
                projection["origin"] = origins[projection["task_id"]]
        return projections

    def _new_password_digest(self, password: str, operator_token: str | None) -> str:
        _validate_password(password, minimum=12)
        if operator_token is not None:
            # Reject unauthorized callers before starting expensive password hashing.
            with self._transaction() as connection:
                self._account_admin(connection, operator_token)
        with self._password_checks:
            return _PASSWORD_HASHER.hash(password)

    def create_account(
        self, username: str, password: str, *, operator_token: str | None = None
    ) -> None:
        _validate_username(username)
        digest = self._new_password_digest(password, operator_token)
        with self._transaction(write=True) as connection:
            if operator_token is not None:
                self._account_admin(connection, operator_token)
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
            return [dict(row) for row in self._accounts(connection)]

    @staticmethod
    def _accounts(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
        return iter(
            connection.execute(
                "SELECT username, owner_id, disabled FROM accounts ORDER BY username"
            )
        )

    def reset_account_password(
        self, username: str, password: str, *, operator_token: str | None = None
    ) -> None:
        _validate_username(username)
        digest = self._new_password_digest(password, operator_token)
        with self._transaction(write=True) as connection:
            account = self._managed_account(connection, username, operator_token)
            if account["disabled"]:
                raise CollaborationError("conflict")
            connection.execute(
                "UPDATE accounts SET password_digest = ? WHERE owner_id = ?",
                (digest, account["owner_id"]),
            )
            self._revoke_account_sessions(connection, account["owner_id"])

    def disable_account(
        self, username: str, *, operator_token: str | None = None
    ) -> None:
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._managed_account(connection, username, operator_token)[
                "owner_id"
            ]
            connection.execute(
                "UPDATE accounts SET disabled = 1 WHERE owner_id = ?", (owner_id,)
            )
            self._revoke_account_sessions(connection, owner_id)
            notified: set[str] = set()
            for member in connection.execute("SELECT member_id FROM members WHERE owner_id = ?", (owner_id,)):
                self._recover_member_rooms(connection, member["member_id"], notified)
        self._notify(notified)

    def _managed_account(
        self, connection: sqlite3.Connection, username: str, operator_token: str | None
    ) -> sqlite3.Row:
        if operator_token is not None:
            self._account_admin(connection, operator_token)
        account = self._account(connection, username)
        if operator_token is not None and self._is_operator(
            connection, account["owner_id"]
        ):
            raise CollaborationError("forbidden")
        return account

    def set_account_admin(self, username: str, *, granted: bool) -> None:
        _validate_username(username)
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            if granted:
                connection.execute(
                    "INSERT INTO account_admin_grants (owner_id) VALUES (?) ON CONFLICT DO NOTHING",
                    (owner_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM account_admin_grants WHERE owner_id = ?", (owner_id,)
                )

    @staticmethod
    def _is_account_admin(connection: sqlite3.Connection, owner_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM account_admin_grants WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _is_operator(connection: sqlite3.Connection, owner_id: str) -> bool:
        return (
            connection.execute(
                """
            SELECT 1 FROM operator_grants WHERE owner_id = ?
            UNION ALL SELECT 1 FROM operator_maintenance_grants WHERE owner_id = ?
            UNION ALL SELECT 1 FROM account_admin_grants WHERE owner_id = ?
            LIMIT 1
            """,
                (owner_id, owner_id, owner_id),
            ).fetchone()
            is not None
        )

    def _account_admin(self, connection: sqlite3.Connection, token: str) -> None:
        actor = self._console_operator(connection, token)
        self._require_grant(connection, actor, "console_read")
        if not self._is_account_admin(connection, actor):
            self._deny_console("forbidden", "missing_grant")

    def console_accounts(self, token: str) -> dict:
        with self._transaction() as connection:
            self._account_admin(connection, token)
            accounts = []
            for account in self._accounts(connection):
                owner_id = account["owner_id"]
                active = not account["disabled"]
                standard = not self._is_operator(connection, owner_id)
                accounts.append(
                    {
                        "username": account["username"],
                        "disabled": not active,
                        "can_reset_password": active and standard,
                        "can_disable": active and standard,
                    }
                )
            return {"accounts": accounts}

    def account_capabilities(self, token: str) -> dict:
        with self._transaction() as connection:
            owner_id = self._enrollment_owner(connection, token, allow_legacy=False)
            console_read = connection.execute(
                "SELECT 1 FROM operator_grants WHERE owner_id = ? AND scope = 'console_read'",
                (owner_id,),
            ).fetchone()
            return {"admin": console_read is not None}

    def console_capabilities(self, token: str) -> dict:
        with self._transaction() as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            maintenance = connection.execute(
                "SELECT 1 FROM operator_maintenance_grants WHERE owner_id = ? AND scope IN ('maintenance_read', 'logs_read')",
                (owner_id,),
            ).fetchone()
            return {
                "maintenance": maintenance is not None,
                "accounts": self._is_account_admin(connection, owner_id),
            }

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

    def set_maintenance_grant(self, username: str, scope: str, granted: bool) -> None:
        _validate_username(username)
        if scope not in _MAINTENANCE_SCOPES or type(granted) is not bool:
            raise CollaborationError("invalid_request")
        with self._transaction(write=True) as connection:
            owner_id = self._account(connection, username)["owner_id"]
            if granted:
                connection.execute(
                    """
                    INSERT INTO operator_maintenance_grants (owner_id, scope)
                    VALUES (?, ?) ON CONFLICT DO NOTHING
                    """,
                    (owner_id, scope),
                )
            else:
                connection.execute(
                    "DELETE FROM operator_maintenance_grants WHERE owner_id = ? AND scope = ?",
                    (owner_id, scope),
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
        try:
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
        except CollaborationError as error:
            with self._metrics_lock:
                if error.code == "rate_limited":
                    self._rate_limited_total += 1
                elif error.code == "unauthorized":
                    self._login_failures_total += 1
            raise
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
            return self._insert_account_session(
                connection, owner_id, purpose, int(time()) + _SESSION_SECONDS
            )

    def issue_console_session(self, token: str) -> dict:
        with self._transaction(write=True) as connection:
            session = self._enrollment_session(connection, _credential_digest(token))
            if session is None:
                raise CollaborationError("unauthorized")
            owner_id = session["owner_id"]
            self._require_grant(connection, owner_id, "console_read")
            issued = self._insert_account_session(
                connection,
                owner_id,
                "console",
                min(session["expires_at"], int(time()) + 3600),
            )
            return {**issued, "username": session["username"]}

    def _insert_account_session(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        purpose: str,
        expires_at: int,
    ) -> dict:
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

    @staticmethod
    def _enrollment_session(
        connection: sqlite3.Connection, digest: bytes | None
    ) -> sqlite3.Row | None:
        session = connection.execute(
            """
            SELECT a.owner_id, a.username, a.disabled, s.expires_at, s.purpose
            FROM account_sessions AS s JOIN accounts AS a USING (owner_id)
            WHERE s.token_digest = ?
            """,
            (digest,),
        ).fetchone()
        if session is not None and (
            session["disabled"]
            or session["expires_at"] <= int(time())
            or session["purpose"] != "enrollment"
        ):
            raise CollaborationError("unauthorized")
        return session

    def _enrollment_owner(
        self, connection: sqlite3.Connection, token: str, *, allow_legacy: bool = True
    ) -> str:
        digest = _credential_digest(token)
        session = self._enrollment_session(connection, digest)
        if session is not None:
            return session["owner_id"]
        if not allow_legacy:
            raise CollaborationError("unauthorized")
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
        member_token: str,
        role_description: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict:
        if name is not None or description is not None:
            if (
                role_description is not None
                or not _profile_name(name)
                or not _profile_text(description, 160)
            ):
                raise CollaborationError("invalid_request")
        elif role_description is None:
            raise CollaborationError("invalid_request")
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
                """
                SELECT member_id, token_digest, retired FROM members
                WHERE owner_id = ? AND harness_instance_id = ? AND session_key = ?
                """,
                (owner, harness_instance_id, session_key),
            ).fetchone()
            if existing is not None:
                if existing["retired"]:
                    raise CollaborationError("member_retired")
                if not compare_digest(existing["token_digest"], digest):
                    raise CollaborationError("conflict")
                return _read_member_profile(connection, existing["member_id"])
            if connection.execute(
                "SELECT 1 FROM members WHERE token_digest = ?", (digest,)
            ).fetchone():
                raise CollaborationError("conflict")
            if name is None:
                raise CollaborationError("invalid_request")

            member_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO members (
                    member_id, owner_id, harness_instance_id, session_key,
                    role_description, token_digest, description
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id,
                    owner,
                    harness_instance_id,
                    session_key,
                    name,
                    digest,
                    description,
                ),
            )
            return _read_member_profile(connection, member_id)

    def _owned_member(
        self, connection: sqlite3.Connection, token: str, member_id: str
    ) -> sqlite3.Row:
        owner = self._enrollment_owner(connection, token, allow_legacy=False)
        member = connection.execute(
            "SELECT retired, role_description FROM members WHERE owner_id = ? AND member_id = ?",
            (owner, member_id),
        ).fetchone()
        if member is None:
            raise CollaborationError("not_found")
        return member

    def rename_member(self, token: str, member_id: str, role_description: str) -> dict:
        with self._transaction(write=True) as connection:
            member = self._owned_member(connection, token, member_id)
            if member["retired"]:
                raise CollaborationError("member_retired")
            if role_description == member["role_description"]:
                return _read_member_profile(connection, member_id)
            if not _profile_name(role_description):
                raise CollaborationError("invalid_request")
            connection.execute(
                "UPDATE members SET role_description = ? WHERE member_id = ?",
                (role_description, member_id),
            )
            return _read_member_profile(connection, member_id)

    def set_room_responsibility(
        self,
        token: str,
        room_id: str,
        member_id: str,
        membership_id: str,
        description: str | None,
    ) -> dict:
        if description is not None and not _profile_text(description, 160):
            raise CollaborationError("invalid_request")
        with self._transaction(write=True) as connection:
            if self._owned_member(connection, token, member_id)["retired"]:
                raise CollaborationError("member_retired")
            current = _room_member(connection, room_id, member_id, active=True)
            if current["membership_id"] != membership_id:
                raise CollaborationError("membership_changed")
            connection.execute(
                "UPDATE room_members SET responsibility = ? WHERE room_id = ? AND member_id = ?",
                (description, room_id, member_id),
            )
        return {"description": description}

    def _retire(self, connection: sqlite3.Connection, member_id: str, notified: set[str]) -> None:
        self._recover_member_rooms(connection, member_id, notified)
        connection.execute(
            """
            INSERT INTO retired_room_access (
                member_id, room_id, after_message_id, through_message_id
            )
            SELECT rm.member_id, rm.room_id,
                CASE WHEN r.history_policy = 'retained' THEN 0 ELSE rm.joined_after END,
                (SELECT last_id FROM id_counters WHERE counter = 'message_id')
            FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
            WHERE rm.member_id = ? AND rm.acked_board_version = r.version
            """,
            (member_id,),
        )
        connection.execute(
            "UPDATE members SET retired = 1 WHERE member_id = ?", (member_id,)
        )
        connection.execute("DELETE FROM room_members WHERE member_id = ?", (member_id,))
        connection.execute(
            "DELETE FROM inbox_events WHERE member_id = ? AND acknowledged = 0",
            (member_id,),
        )
        connection.execute(
            "DELETE FROM room_departures WHERE member_id = ? AND acknowledged = 0",
            (member_id,),
        )
        connection.execute(
            "DELETE FROM task_reminders WHERE member_id = ? AND claimed_at IS NULL",
            (member_id,),
        )

    def retire_member(self, token: str, member_id: str) -> dict:
        notified = {member_id}
        with self._transaction(write=True) as connection:
            if not self._owned_member(connection, token, member_id)["retired"]:
                self._retire(connection, member_id, notified)
        self._notify(notified)
        return {"member_id": member_id, "retired": True}

    @staticmethod
    def _retirement_receipt(member: sqlite3.Row) -> dict:
        return {
            "member_id": member["member_id"],
            "role_description": member["role_description"],
            "retired": True,
        }

    def retire_self(self, token: str) -> dict:
        with self._transaction(write=True) as connection:
            member = _member(connection, token, include_retired=True)
            notified = {member["member_id"]}
            if not member["retired"]:
                self._retire(connection, member["member_id"], notified)
        self._notify(notified)
        return self._retirement_receipt(member)

    def member_retirement(self, token: str) -> dict:
        with self._transaction() as connection:
            member = _member(connection, token, include_retired=True)
            if not member["retired"]:
                raise CollaborationError("not_found")
        return self._retirement_receipt(member)

    def _authorize_archive(
        self, connection: sqlite3.Connection, token: str, member_id: str
    ) -> None:
        if not self._owned_member(connection, token, member_id)["retired"]:
            raise CollaborationError("not_found")

    def list_retired_conversations(self, token: str, member_id: str) -> dict:
        with self._transaction() as connection:
            self._authorize_archive(connection, token, member_id)
            conversations = connection.execute(
                """
                SELECT 'room' AS kind, r.room_id AS conversation_id, r.name AS label
                FROM retired_room_access AS a JOIN rooms AS r ON r.room_id = a.room_id
                WHERE a.member_id = ?
                UNION ALL
                SELECT 'dm', d.dm_id, peer.role_description
                FROM dms AS d JOIN members AS peer ON peer.member_id =
                    CASE WHEN d.member_low = ? THEN d.member_high ELSE d.member_low END
                WHERE ? IN (d.member_low, d.member_high)
                ORDER BY kind DESC, label, conversation_id
                """,
                (member_id, member_id, member_id),
            )
            return {"conversations": [dict(row) for row in conversations]}

    def retired_history(
        self,
        token: str,
        member_id: str,
        kind: str,
        conversation_id: str,
        before: int | None,
        limit: int,
    ) -> dict:
        upper = None if before is None or before > _SQLITE_MAX_ID else before
        with self._transaction() as connection:
            self._authorize_archive(connection, token, member_id)
            if kind == "room":
                bounds = connection.execute(
                    """
                    SELECT after_message_id, through_message_id
                    FROM retired_room_access WHERE member_id = ? AND room_id = ?
                    """,
                    (member_id, conversation_id),
                ).fetchone()
                if bounds is None:
                    raise CollaborationError("not_found")
                rows = _room_transcript(
                    connection,
                    conversation_id,
                    after=bounds["after_message_id"],
                    through=bounds["through_message_id"],
                    before=upper,
                    limit=limit,
                    newest_first=True,
                )
                return {
                    "messages": [
                        {**_room_message(row), **_retired_sender(row)} for row in rows
                    ]
                }
            if kind != "dm":
                raise CollaborationError("invalid_request")
            _dm_participant(connection, conversation_id, member_id)
            rows = connection.execute(
                f"""
                SELECT {_DM_MESSAGE}, sender.retired AS sender_retired
                FROM messages AS m JOIN members AS sender
                    ON sender.member_id = m.sender_member_id
                WHERE m.dm_id = ? AND (m.message_id < ? OR ? IS NULL)
                ORDER BY m.message_id DESC LIMIT ?
                """,
                (conversation_id, upper, upper, limit),
            ).fetchall()
            return {
                "messages": [
                    {
                        **{field: row[field] for field in _DM_EVENT_MESSAGE},
                        **_retired_sender(row),
                    }
                    for row in reversed(rows)
                ]
            }

    def get_member(self, token: str) -> dict:
        with self._transaction() as connection:
            return _read_member_profile(
                connection, _member(connection, token)["member_id"]
            )

    def authorized_presence_members(self, token: str) -> list[str]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            rows = connection.execute(
                """
                SELECT ? AS member_id
                UNION
                SELECT m.member_id
                FROM room_members AS own
                JOIN room_members AS peer ON peer.room_id = own.room_id
                JOIN members AS m ON m.member_id = peer.member_id
                WHERE own.member_id = ? AND NOT EXISTS (
                    SELECT 1 FROM accounts WHERE owner_id = m.owner_id AND disabled = 1
                )
                ORDER BY member_id
                """,
                (member_id, member_id),
            )
            return [row["member_id"] for row in rows]

    def search_members(self, token: str, query: str) -> list[dict]:
        with self._transaction() as connection:
            _member(connection, token)
            connection.create_function(
                "teamup_casefold", 1, str.casefold, deterministic=True
            )
            rows = connection.execute(
                f"""
                SELECT {_MEMBER_PROFILE} FROM {_PROFILE_TABLES}
                WHERE m.retired = 0 AND (
                    instr(teamup_casefold(m.role_description), :query) > 0
                    OR instr(teamup_casefold(COALESCE(m.description, '')), :query) > 0
                )
                ORDER BY m.member_id LIMIT 100
                """,
                {"query": query.casefold()},
            )
            return [_member_profile(row) for row in rows]

    def console_maintenance_snapshot(self, token: str) -> SourceSnapshot:
        with self._transaction() as connection:
            self._maintenance_operator(connection, token, "maintenance_read")
            return self._maintenance_observations.read(
                partial(self._collect_maintenance_values, connection)
            )

    def _collect_maintenance_values(
        self, connection: sqlite3.Connection
    ) -> dict | None:
        try:
            sampled_at = self._now_datetime()
            db_size = self._db_path.stat().st_size
            with suppress(FileNotFoundError):
                wal = self._db_path.with_name(f"{self._db_path.name}-wal")
                db_size += wal.stat().st_size
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM members) AS members_stored,
                    (SELECT COUNT(*) FROM rooms) AS team_rooms_stored,
                    (SELECT COUNT(*) FROM messages WHERE room_id IS NOT NULL)
                        AS messages_stored
                """
            ).fetchone()
            pending = self._pending_room_load(connection, sampled_at)
        except OSError:
            return None
        except sqlite3.Error as error:
            self._record_sqlite_busy(error)
            return None

        with self._metrics_lock:
            return {
                **dict(counts),
                **pending,
                "db_size_bytes": db_size,
                "schema_version": schema_version,
                "sqlite_busy_errors_total": self._sqlite_busy_errors_total,
                "transactions_total": self._transactions_total,
                "transaction_duration_seconds_total": self._transaction_duration_seconds_total,
                "login_failures_total": self._login_failures_total,
                "rate_limited_total": self._rate_limited_total,
                "authorization_denials_total": self._authorization_denials.copy(),
            }

    def console_room_load(self, token: str, room_id: str) -> dict:
        with self._transaction() as connection:
            self._maintenance_operator(connection, token, "maintenance_read")
            self._console_room(connection, room_id)
            sampled_at = self._now_datetime()
            room = {
                "room_id": room_id,
                "sampled_at": None,
                "state": "unavailable",
                "member_count": None,
                "messages_stored": None,
                "pending_inbox_events": None,
                "oldest_pending_age_seconds": None,
                "last_activity_at": None,
                "load_rates": None,
            }
            try:
                pending = self._pending_room_load(connection, sampled_at, room_id)
                counts = connection.execute(
                    """
                    SELECT
                        COALESCE(json_array_length(r.archive_roster),
                            (SELECT COUNT(*) FROM room_members WHERE room_id = r.room_id)
                        ) AS member_count,
                        (SELECT COUNT(*) FROM messages WHERE room_id = :room_id)
                            AS messages_stored,
                        (SELECT teamup_expiry_micros(at) FROM room_activity
                            WHERE room_id = :room_id AND teamup_expiry_micros(at) IS NOT NULL
                            ORDER BY activity_id DESC LIMIT 1) AS last_activity_micros
                    FROM rooms AS r WHERE r.room_id = :room_id
                    """,
                    {"room_id": room_id},
                ).fetchone()
            except sqlite3.Error as error:
                self._record_sqlite_busy(error)
                return {"room": room}

            last_activity = counts["last_activity_micros"]
            room.update(
                **pending,
                sampled_at=_rfc3339(sampled_at),
                state="fresh",
                member_count=counts["member_count"],
                messages_stored=counts["messages_stored"],
                last_activity_at=(
                    _rfc3339(_EPOCH + timedelta(microseconds=last_activity))
                    if last_activity is not None
                    else None
                ),
            )
            return {"room": room}

    @staticmethod
    def _pending_room_load(
        connection: sqlite3.Connection, sampled_at: datetime, room_id: str | None = None
    ) -> dict:
        _register_expiry_clock(connection)
        room_filter = "" if room_id is None else "AND r.room_id = ?"
        parameters = () if room_id is None else (room_id,)
        pending = connection.execute(
            f"""
            SELECT COUNT(*) AS pending_inbox_events,
                MIN(teamup_expiry_micros(m.created_at)) AS oldest_micros
            FROM inbox_events AS e JOIN messages AS m USING (message_id)
            JOIN rooms AS r ON r.room_id = m.room_id
            WHERE e.acknowledged = 0 AND r.archived_at IS NULL {room_filter}
            """,
            parameters,
        ).fetchone()
        oldest = pending["oldest_micros"]
        return {
            "pending_inbox_events": pending["pending_inbox_events"],
            "oldest_pending_age_seconds": (
                max(0.0, (_utc_micros(sampled_at) - oldest) / 1_000_000)
                if oldest is not None
                else None
            ),
        }

    def authorize_console_logs(self, token: str) -> None:
        with self._transaction() as connection:
            self._maintenance_operator(connection, token, "logs_read")

    def audit_console_logs(
        self,
        token: str,
        *,
        since: str,
        until: str,
        component: str | None,
        result_count: int,
    ) -> None:
        with self._transaction(write=True) as connection:
            operator_id = self._maintenance_operator(connection, token, "logs_read")
            start = _expiry_micros(since)
            end = _expiry_micros(until)
            if (
                start is None
                or end is None
                or not 0 < end - start <= _LOG_WINDOW_MICROS
                or (component is not None and component not in _LOG_COMPONENTS)
                or type(result_count) is not int
                or not 0 <= result_count <= _LOG_RESULT_LIMIT
            ):
                raise CollaborationError("invalid_request")
            connection.execute(
                """
                INSERT INTO log_access_audit (
                    owner_id, window_since, window_until, component, result_count, at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operator_id,
                    _rfc3339(_EPOCH + timedelta(microseconds=start)),
                    _rfc3339(_EPOCH + timedelta(microseconds=end)),
                    component,
                    result_count,
                    self._now(),
                ),
            )

    def _maintenance_operator(
        self, connection: sqlite3.Connection, token: str, scope: str
    ) -> str:
        owner_id = self._console_operator(connection, token)
        self._require_grant(connection, owner_id, "console_read")
        grant = connection.execute(
            "SELECT 1 FROM operator_maintenance_grants WHERE owner_id = ? AND scope = ?",
            (owner_id, scope),
        ).fetchone()
        if grant is None:
            self._deny_console("forbidden", "missing_grant")
        return owner_id

    def list_console_rooms(self, token: str) -> dict:
        with self._transaction() as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            rows = connection.execute(
                """
                SELECT r.room_id, r.name, r.archived_at, r.archive_reason,
                    COALESCE(json_array_length(r.archive_roster),
                        (SELECT COUNT(*) FROM room_members AS rm WHERE rm.room_id = r.room_id)
                    ) AS member_count,
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
                        **_archive_metadata(row),
                    }
                    for row in rows
                ]
            }

    def console_room_detail(self, token: str, room_id: str) -> dict:
        with self._transaction() as connection:
            owner_id = self._console_operator(connection, token)
            self._require_grant(connection, owner_id, "console_read")
            room = self._console_room(connection, room_id)
            roster = (
                json.loads(room["archive_roster"]) if room["archived_at"] is not None
                else self._room_roster(connection, room_id)
            )
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
                    "member_count": len(roster),
                    **_archive_metadata(room),
                **_child_metadata(room),
                    "last_activity": _activity_summary(room_last, "last_"),
                },
                "members": [
                    {
                        "member_id": row["member_id"],
                        "role_description": row["role_description"],
                        "board_state": (
                            "acknowledged"
                            if row["acked_board_version"] == room["version"]
                            else "awaiting"
                        ),
                        "last_activity": _activity_summary(
                            latest.get(row["member_id"]), "last_"
                        ),
                        "harness_state": "unknown",
                    }
                    for row in roster
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
                SELECT {_MESSAGE}, mem.role_description AS sender_role_description,
                    mem.retired AS sender_retired
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
                        **_retired_sender(row),
                    }
                    for row in page
                ],
                "has_more": has_more,
            }

    def _console_room(
        self, connection: sqlite3.Connection, room_id: str
    ) -> sqlite3.Row:
        room = connection.execute(
            """
            SELECT room_id, name, version, archived_at, archive_reason, archive_roster
            FROM rooms WHERE room_id = ?
            """,
            (room_id,)
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
        if session is None:
            self._deny_console("unauthorized", "missing_session")
        if session["purpose"] != "console":
            self._deny_console("unauthorized", "wrong_purpose")
        if session["disabled"] or session["expires_at"] <= int(time()):
            self._deny_console("unauthorized", "inactive_session")
        return session["owner_id"]

    def _deny_console(self, code: str, reason: str) -> NoReturn:
        with self._metrics_lock:
            self._authorization_denials[reason] += 1
        raise CollaborationError(code)

    def _require_grant(
        self,
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
            self._deny_console("forbidden", "missing_grant")

    def create_room(
        self,
        token: str,
        name: str,
        rules: str | None,
        protocol: str | None,
        history_policy: str = "since_joining",
    ) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            return self._insert_room(
                connection, member_id, name, rules, protocol, history_policy, self._now_datetime()
            )

    def _insert_room(
        self, connection: sqlite3.Connection, member_id: str, name: str, rules: str | None,
        protocol: str | None, history_policy: str, now: datetime,
    ) -> dict:
        if history_policy not in ("retained", "since_joining"):
            raise CollaborationError("invalid_request")
        room_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO rooms (
                room_id, name, version, rules, protocol, history_policy, creator_member_id, last_chat_at
            )
            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                name,
                rules if rules is not None else _DEFAULT_RULES,
                protocol if protocol is not None else _DEFAULT_PROTOCOL,
                history_policy,
                member_id,
                _utc_micros(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO room_members (room_id, member_id, joined_after, membership_id)
            VALUES (?, ?, 0, ?)
            """,
            (room_id, member_id, uuid4().hex),
        )
        _record_activity(
            connection, room_id, "member_joined", member_id, _rfc3339(now)
        )
        self._initialize_room_leadership(connection, room_id, member_id)
        return {"room_id": room_id, "name": name, "history_policy": history_policy}

    def create_linked_room(
        self, token: str, parent_room_id: str, name: str, rules: str | None, protocol: str | None,
        history_policy: str, client_request_id: str,
    ) -> dict:
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            raise CollaborationError("invalid_request")
        fingerprint = _task_fingerprint((parent_room_id, name, rules, protocol, history_policy))
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            parent = _room_member(connection, parent_room_id, member_id, active=True)
            if parent["parent_room_id"] is not None:
                raise CollaborationError("conflict")
            existing = connection.execute(
                "SELECT * FROM child_rooms WHERE creator_member_id = ? AND client_request_id = ?",
                (member_id, client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise CollaborationError("conflict")
                existing_room = _room_member(connection, existing["room_id"], member_id)
                return {
                    "room_id": existing["room_id"], "name": existing_room["name"],
                    "history_policy": existing_room["history_policy"], **_child_metadata(existing),
                }
            now = self._now_datetime()
            room = self._insert_room(connection, member_id, name, rules, protocol, history_policy, now)
            connection.execute(
                """
                INSERT INTO child_rooms (
                    room_id, parent_room_id, creator_member_id, client_request_id,
                    fingerprint, created_at, expires_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (room["room_id"], parent_room_id, member_id, client_request_id, fingerprint,
                 _rfc3339(now), _utc_micros(now + _CHILD_LEASE)),
            )
            return {**room, **_child_metadata(_child_row(connection, room["room_id"]))}

    def list_child_rooms(self, token: str, room_id: str, after: str | None, limit: int) -> dict:
        if not 1 <= limit <= 100:
            raise CollaborationError("invalid_request")
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            _room_member(connection, room_id, member_id)
            rows = connection.execute(
                """
                SELECT r.room_id, r.name, r.history_policy, r.archived_at, r.archive_reason,
                    c.parent_room_id, c.created_at, c.expires_us
                FROM child_rooms AS c JOIN rooms AS r USING (room_id)
                WHERE c.parent_room_id = ? AND c.room_id > ? AND (
                    r.archived_at IS NULL OR EXISTS (
                        SELECT 1 FROM room_members WHERE room_id = r.room_id AND member_id = ?
                    )
                ) ORDER BY c.room_id LIMIT ?
                """,
                (room_id, after or "", member_id, limit + 1),
            ).fetchall()
            rooms = [
                {**{k: row[k] for k in ("room_id", "name", "history_policy")},
                 **_child_metadata(row), **_archive_metadata(row)} for row in rows[:limit]
            ]
            return {"rooms": rooms, "next_after": rooms[-1]["room_id"] if len(rows) > limit else None}

    def extend_room_lease(
        self, token: str, room_id: str, seconds: int, expected_expires_at: str,
    ) -> dict:
        expected = _expiry_micros(expected_expires_at)
        if type(seconds) is not int or not 1 <= seconds <= 3600 or expected is None:
            raise CollaborationError("invalid_request")
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            room = _room_member(connection, room_id, member_id)
            child = _child_row(connection, room_id)
            if child is None or room["archived_at"] is not None:
                raise CollaborationError("conflict")
            if not _has_room_leadership(connection, room_id, member_id):
                raise CollaborationError("forbidden")
            expires = child["expires_us"]
            remaining = expires - _utc_micros(self._now_datetime())
            if remaining <= 0:
                raise CollaborationError("conflict")
            extended = expected + seconds * 1_000_000
            if (expected == child["renewed_from_us"] and member_id == child["renewed_by"]
                    and extended == expires):
                return {"room_id": room_id, **_child_metadata(child)}
            if remaining > _CHILD_NOTICE_US or expires != expected:
                raise CollaborationError("conflict")
            connection.execute(
                """
                UPDATE child_rooms SET expires_us = ?, offered_epoch = NULL,
                    renewed_from_us = ?, renewed_by = ? WHERE room_id = ?
                """,
                (extended, expected, member_id, room_id),
            )
            return {"room_id": room_id, **_child_metadata(_child_row(connection, room_id))}

    def record_leadership_heartbeat(self, token: str) -> None:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            now = _utc_micros(self._now_datetime())
            connection.execute(
                """
                INSERT INTO leadership_heartbeats (member_id, observed_at) VALUES (?, ?)
                ON CONFLICT (member_id) DO UPDATE SET observed_at = MAX(observed_at, excluded.observed_at)
                """,
                (member_id, now),
            )
            connection.execute(
                """
                UPDATE room_leadership SET next_check_at = ?
                WHERE leader_member_id IS NULL AND next_check_at IS NULL AND room_id IN (
                    SELECT room_id FROM room_members WHERE member_id = ?
                )
                """,
                (now, member_id),
            )

    @staticmethod
    def _leadership_actor(
        connection: sqlite3.Connection, token: str, room_id: str, epoch: int | None = None
    ) -> sqlite3.Row:
        actor = _member(connection, token)["member_id"]
        _room_member(connection, room_id, actor, active=True)
        state = _room_leader(connection, room_id)
        if state is None:
            raise CollaborationError("forbidden")
        if epoch is not None and state["leadership_epoch"] != epoch:
            raise CollaborationError("conflict")
        if state["leader_member_id"] != actor:
            raise CollaborationError("forbidden")
        return state

    @staticmethod
    def _rank_successors(connection: sqlite3.Connection, state: sqlite3.Row, now: int) -> list[dict]:
        room_id = state["room_id"]
        incumbent = state["leader_member_id"]
        previous = incumbent or state["previous_member_id"]
        _register_expiry_clock(connection)
        rows = connection.execute(
            f"""
            WITH work AS (
                SELECT msg.sender_member_id AS actor, msg.created_at
                FROM messages AS msg JOIN rooms AS r ON r.room_id = msg.room_id
                LEFT JOIN room_members AS viewer ON viewer.room_id = r.room_id AND viewer.member_id = :viewer
                WHERE r.room_id = :room AND (:automatic OR (
                    viewer.acked_board_version = r.version AND
                    (r.history_policy = 'retained' OR msg.message_id > viewer.joined_after)
                ))
                UNION ALL
                SELECT e.actor_member_id, e.created_at FROM task_events AS e JOIN tasks AS t USING (task_id)
                JOIN rooms AS r ON r.room_id = t.room_id
                LEFT JOIN room_members AS viewer ON viewer.room_id = r.room_id AND viewer.member_id = :viewer
                WHERE r.room_id = :room AND (:automatic OR viewer.acked_board_version = r.version)
                    AND NOT EXISTS (SELECT 1 FROM task_addressees AS ta WHERE ta.task_id = t.task_id)
            ), latest AS (
                SELECT actor, MAX(teamup_expiry_micros(created_at)) AS last_active FROM work GROUP BY actor
            )
            SELECT {_MEMBER_PROFILE}, rm.membership_id, rm.responsibility, latest.last_active,
                m.owner_id = (SELECT owner_id FROM members WHERE member_id = :previous) AS same_owner
            FROM {_PROFILE_TABLES} JOIN room_members AS rm ON rm.member_id = m.member_id
            JOIN leadership_heartbeats AS h ON h.member_id = m.member_id
            LEFT JOIN latest ON latest.actor = m.member_id
            WHERE rm.room_id = :room AND m.member_id IS NOT :viewer AND rm.membership_id IS NOT :blocked
                AND m.retired = 0 AND COALESCE(a.disabled, 0) = 0 AND h.observed_at > :cutoff
            ORDER BY m.member_id
            """,
            {"room": room_id, "viewer": incumbent, "automatic": incumbent is None, "previous": previous,
                 "blocked": state["blocked_membership_id"], "cutoff": now - _HEARTBEAT_MICROS},
        )
        ranked = []
        for row in rows:
            profile = _member_profile(row)
            if row["responsibility"] is not None:
                profile["description"] = row["responsibility"]
            leadership_term = bool(_LEADERSHIP_TERMS.search(
                f"{row['role_description']} {profile.get('description', '')}"
            ))
            same_owner = bool(row["same_owner"])
            active = row["last_active"]
            profile.update(
                membership_id=row["membership_id"],
                reason="leader_profile" if leadership_term else "same_owner" if same_owner else "recent_activity",
                last_active_at=_rfc3339(_EPOCH + timedelta(microseconds=active)) if active is not None else None,
            )
            ranked.append(((not leadership_term, not same_owner, active is None, -(active or 0), row["member_id"]), profile))
        return [profile for _, profile in sorted(ranked)]

    def room_leadership_candidates(self, token: str, room_id: str) -> dict:
        with self._transaction() as connection:
            state = self._leadership_actor(connection, token, room_id)
            return {
                "leadership_epoch": state["leadership_epoch"],
                "candidates": self._rank_successors(connection, state, _utc_micros(self._now_datetime())),
            }

    @staticmethod
    def _leadership_receipt(state: sqlite3.Row, *, left: bool = False) -> dict:
        return {
            "room_id": state["room_id"], "leader_member_id": state["leader_member_id"],
            "leadership_epoch": state["leadership_epoch"], "pending": state["pending_kind"] is not None,
            "left": left,
        }

    def _appoint_room_leader(
        self, connection: sqlite3.Connection, state: sqlite3.Row, successor: str | None, now: int,
        notified: set[str], *, block_unresponsive: bool = False,
    ) -> sqlite3.Row:
        room_id = state["room_id"]
        previous = state["leader_member_id"]
        blocked = None
        if block_unresponsive and previous is not None:
            blocked = _room_member(connection, room_id, previous)["membership_id"]
        updated = connection.execute(
            """
            UPDATE room_leadership SET leader_member_id = ?, previous_member_id = COALESCE(leader_member_id, previous_member_id),
                leadership_epoch = leadership_epoch + 1, blocked_membership_id = ?, pending_kind = NULL,
                deadline = NULL, leave_after = 0, challenge_at = ?, next_check_at = ?
            WHERE room_id = ? RETURNING *
            """,
            (successor, blocked, now + _CHALLENGE_INTERVAL, now + _LEADERSHIP_GRACE if successor else None, room_id),
        ).fetchone()
        if successor is not None:
            self._add_room_notice(
                connection, successor, room_id, "leadership_assigned",
                f"You now lead room {room_id}. Read its Board, review open tasks and manage the room.",
                leadership_epoch=updated["leadership_epoch"],
            )
            notified.add(successor)
        return updated

    def _departing_room_leader(
        self, connection: sqlite3.Connection, state: sqlite3.Row, notified: set[str]
    ) -> None:
        now = _utc_micros(self._now_datetime())
        candidates = self._rank_successors(connection, state, now)
        successor = candidates[0]["member_id"] if candidates else None
        self._appoint_room_leader(connection, state, successor, now, notified)

    def _recover_member_rooms(
        self, connection: sqlite3.Connection, member_id: str, notified: set[str]
    ) -> None:
        rows = connection.execute(
            """
            SELECT l.* FROM room_leadership AS l JOIN rooms AS r USING (room_id)
            WHERE l.leader_member_id = ? AND r.archived_at IS NULL
            """,
            (member_id,),
        ).fetchall()
        for state in rows:
            self._departing_room_leader(connection, state, notified)

    def _request_leadership_decision(
        self, connection: sqlite3.Connection, state: sqlite3.Row, kind: str, leave: bool, now: int,
        notified: set[str],
    ) -> sqlite3.Row:
        room_id = state["room_id"]
        deadline = now + _LEADERSHIP_GRACE
        updated = connection.execute(
            """
            UPDATE room_leadership SET leadership_epoch = leadership_epoch + 1, pending_kind = ?,
                deadline = ?, next_check_at = ?, leave_after = ? WHERE room_id = ? RETURNING *
            """,
            (kind, deadline, deadline, leave, room_id),
        ).fetchone()
        epoch = updated["leadership_epoch"]
        member_id = updated["leader_member_id"]
        if kind == "defer":
            notice = "leadership_deferred"
            instruction = (
                f"Choose a successor within two minutes. Use teamup_room_leadership_candidates for {room_id}, "
                f"then teamup_handoff_room_leadership with leadership_epoch {epoch}."
            )
        else:
            notice = "leadership_challenge"
            instruction = (
                f"Confirm you are available to lead {room_id} within two minutes. "
                f"Call teamup_confirm_room_leadership with room_id {room_id} and leadership_epoch {epoch}."
            )
        self._add_room_notice(connection, member_id, room_id, notice, instruction, leadership_epoch=epoch)
        notified.add(member_id)
        return updated

    def handoff_room_leadership(
        self, token: str, room_id: str, leadership_epoch: int, mode: str, *,
        member_id: str | None = None, membership_id: str | None = None, leave: bool = False,
    ) -> dict:
        if (
            type(leadership_epoch) is not int or leadership_epoch < 1 or type(leave) is not bool
            or mode not in ("choose", "auto", "defer")
            or (mode == "choose" and not (member_id and membership_id))
            or (mode != "choose" and (member_id is not None or membership_id is not None))
        ):
            raise CollaborationError("invalid_request")
        notified: set[str] = set()
        with self._transaction(write=True) as connection:
            state = self._leadership_actor(connection, token, room_id, leadership_epoch)
            now = _utc_micros(self._now_datetime())
            if state["deadline"] is not None and state["deadline"] <= now:
                raise CollaborationError("conflict")
            left = False
            if mode == "defer":
                if state["pending_kind"] != "defer":
                    state = self._request_leadership_decision(connection, state, "defer", leave, now, notified)
                elif leave != bool(state["leave_after"]):
                    raise CollaborationError("conflict")
            else:
                candidates = self._rank_successors(connection, state, now)
                if mode == "choose":
                    candidates = [
                        row for row in candidates
                        if (row["member_id"], row["membership_id"]) == (member_id, membership_id)
                    ]
                if not candidates:
                    raise CollaborationError("conflict")
                previous = state["leader_member_id"]
                left = bool(leave or state["leave_after"])
                state = self._appoint_room_leader(connection, state, candidates[0]["member_id"], now, notified)
                if left:
                    self._end_room_membership(connection, room_id, previous, None, notify=True)
                    notified.add(previous)
        self._notify(notified)
        return self._leadership_receipt(state, left=left)

    def confirm_room_leadership(self, token: str, room_id: str, leadership_epoch: int) -> dict:
        if type(leadership_epoch) is not int or leadership_epoch < 1:
            raise CollaborationError("invalid_request")
        with self._transaction(write=True) as connection:
            state = self._leadership_actor(connection, token, room_id, leadership_epoch)
            now = _utc_micros(self._now_datetime())
            if state["pending_kind"] != "challenge" or state["deadline"] <= now:
                raise CollaborationError("conflict")
            state = self._reset_leadership_challenge(connection, room_id, now)
        return self._leadership_receipt(state)

    @staticmethod
    def _reset_leadership_challenge(
        connection: sqlite3.Connection, room_id: str, now: int
    ) -> sqlite3.Row:
        return connection.execute(
            """
            UPDATE room_leadership SET leadership_epoch = leadership_epoch + 1,
                pending_kind = NULL, deadline = NULL, challenge_at = ?, next_check_at = ?
            WHERE room_id = ? RETURNING *
            """,
            (now + _CHALLENGE_INTERVAL, now + _LEADERSHIP_GRACE, room_id),
        ).fetchone()

    def _sweep_leader(
        self, connection: sqlite3.Connection, state: sqlite3.Row, now: int, notified: set[str]
    ) -> None:
        room_id = state["room_id"]
        incumbent = state["leader_member_id"]
        kind = state["pending_kind"]
        if incumbent is not None:
            heartbeat = connection.execute(
                "SELECT observed_at FROM leadership_heartbeats WHERE member_id = ?", (incumbent,)
            ).fetchone()
            lost = heartbeat is not None and heartbeat[0] + _LEADERSHIP_GRACE <= now
            if kind is None and not lost:
                if now >= state["challenge_at"]:
                    self._request_leadership_decision(connection, state, "challenge", False, now, notified)
                else:
                    connection.execute(
                        "UPDATE room_leadership SET next_check_at = ? WHERE room_id = ?",
                        (min(state["challenge_at"], (heartbeat[0] if heartbeat else now) + _LEADERSHIP_GRACE), room_id),
                    )
                return
        candidates = self._rank_successors(connection, state, now)
        successor = candidates[0]["member_id"] if candidates else None
        if kind == "challenge" and incumbent is not None and not lost:
            probe_received = connection.execute(
                """
                SELECT 1 FROM room_control_notices
                WHERE room_id = ? AND kind = 'leadership_challenge' AND member_id = ?
                    AND leadership_epoch = ? AND acknowledged = 1
                """,
                (room_id, incumbent, state["leadership_epoch"]),
            ).fetchone()
            if successor is None or probe_received is None:
                self._reset_leadership_challenge(connection, room_id, now)
                return
        if not successor and (kind == "defer" or incumbent is None):
            connection.execute(
                "UPDATE room_leadership SET next_check_at = ? WHERE room_id = ?",
                (now + _HEARTBEAT_MICROS if kind else None, room_id),
            )
            return
        self._appoint_room_leader(connection, state, successor, now, notified,
                                 block_unresponsive=kind == "challenge")
        if state["leave_after"]:
            self._end_room_membership(connection, room_id, incumbent, None, notify=True)
            notified.add(incumbent)

    def sweep_room_leadership(self) -> bool:
        notified: set[str] = set()
        with self._transaction(write=True) as connection:
            now = _utc_micros(self._now_datetime())
            rows = connection.execute(
                """
                SELECT l.* FROM room_leadership AS l JOIN rooms AS r USING (room_id)
                WHERE l.next_check_at <= ? AND r.archived_at IS NULL
                ORDER BY l.next_check_at, l.room_id LIMIT ?
                """,
                (now, _LEADERSHIP_BATCH),
            ).fetchall()
            for state in rows:
                self._sweep_leader(connection, state, now, notified)
        self._notify(notified)
        return len(rows) == _LEADERSHIP_BATCH

    def join_room(self, token: str, room_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            room = connection.execute(
                """SELECT r.room_id, r.name, r.archived_at, c.expires_us <= teamup_now_us() AS lease_expired
                   FROM rooms AS r LEFT JOIN child_rooms AS c USING (room_id) WHERE r.room_id = ?""",
                (room_id,)
            ).fetchone()
            if room is None:
                raise CollaborationError("not_found")
            if room["archived_at"] is not None or room["lease_expired"]:
                _room_member(connection, room_id, member_id, active=True)
            joined = connection.execute(
                """
                INSERT INTO room_members (room_id, member_id, joined_after, membership_id)
                VALUES (?, ?, COALESCE((SELECT MAX(message_id) FROM messages WHERE room_id = ?), 0), ?)
                ON CONFLICT (room_id, member_id) DO NOTHING
                """,
                (room_id, member_id, room_id, uuid4().hex),
            )
            if joined.rowcount:
                connection.execute(
                    "UPDATE room_leadership SET next_check_at = ? WHERE room_id = ? AND leader_member_id IS NULL",
                    (_utc_micros(self._now_datetime()), room_id),
                )
                _record_activity(
                    connection, room_id, "member_joined", member_id, _rfc3339_utc_now()
                )
        return {"room_id": room_id, "name": room["name"]}

    def leave_room(
        self,
        token: str,
        room_id: str,
        *,
        notify: bool = False,
        membership_id: str | None = None,
    ) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            if connection.execute(
                "SELECT 1 FROM rooms WHERE room_id = ? AND archived_at IS NOT NULL",
                (room_id,),
            ).fetchone():
                _room_member(connection, room_id, member_id, active=True)
            notified: set[str] = set()
            state = _room_leader(connection, room_id)
            if state is not None and state["leader_member_id"] == member_id:
                self._departing_room_leader(connection, state, notified)
            event_id = self._end_room_membership(
                connection, room_id, member_id, membership_id, notify=notify
            )
            if event_id is not None:
                notified.add(member_id)
        self._notify(notified)
        return (
            {"room_id": room_id, "departure_event_id": event_id}
            if notify
            else {"room_id": room_id}
        )

    def remove_room_member(
        self, token: str, room_id: str, member_id: str, membership_id: str
    ) -> dict:
        with self._transaction(write=True) as connection:
            actor = _member(connection, token)
            _room_member(connection, room_id, actor["member_id"], active=True)
            if not _has_room_leadership(connection, room_id, actor["member_id"]):
                raise CollaborationError("forbidden")
            notified: set[str] = set()
            state = _room_leader(connection, room_id)
            if state is not None and state["leader_member_id"] == member_id:
                self._departing_room_leader(connection, state, notified)
            event_id = self._end_room_membership(
                connection, room_id, member_id, membership_id, notify=True
            )
            if event_id is not None:
                notified.add(member_id)
        self._notify(notified)
        return {
            "room_id": room_id,
            "member_id": member_id,
            "departure_event_id": event_id,
        }

    @staticmethod
    def _end_room_membership(
        connection: sqlite3.Connection,
        room_id: str,
        member_id: str,
        membership_id: str | None,
        *,
        notify: bool,
    ) -> int | None:
        existing = connection.execute(
            "SELECT membership_id FROM room_members WHERE room_id = ? AND member_id = ?",
            (room_id, member_id),
        ).fetchone()
        if existing is None:
            return None
        if membership_id is not None and existing["membership_id"] != membership_id:
            raise CollaborationError("membership_changed")
        connection.execute(
            "DELETE FROM room_members WHERE room_id = ? AND member_id = ?",
            (room_id, member_id),
        )
        connection.execute(
            """
            DELETE FROM inbox_events WHERE member_id = ? AND acknowledged = 0
                AND EXISTS (
                    SELECT 1 FROM messages
                    WHERE message_id = inbox_events.message_id AND room_id = ?
                )
            """,
            (member_id, room_id),
        )
        connection.execute(
            """
            DELETE FROM task_reminders WHERE member_id = ? AND claimed_at IS NULL
                AND task_id IN (SELECT task_id FROM tasks WHERE room_id = ?)
            """,
            (member_id, room_id),
        )
        if not notify:
            return None
        event_id = _next_id(connection, "event_id")
        connection.execute(
            """
            INSERT INTO room_departures (event_id, member_id, room_id)
            VALUES (?, ?, ?)
            """,
            (event_id, member_id, room_id),
        )
        return event_id

    def list_rooms(self, token: str, *, include_membership: bool = False) -> list[dict]:
        with self._transaction() as connection:
            member = _member(connection, token)
            rows = connection.execute(
                """
                SELECT r.room_id, r.name, r.version AS current_board_version,
                    rm.acked_board_version, rm.membership_id, l.leader_member_id, l.leadership_epoch, (
                        SELECT MAX(m.message_id) FROM messages AS m
                        WHERE m.room_id = r.room_id
                    ) AS latest_message_id
                FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
                LEFT JOIN room_leadership AS l ON l.room_id = r.room_id
                WHERE rm.member_id = ? AND r.archived_at IS NULL
                ORDER BY r.room_id
                """,
                (member["member_id"],),
            )
            rooms = [dict(record) for record in rows]
            for room in rooms:
                if include_membership:
                    room.update(_child_metadata(_child_row(connection, room["room_id"])))
                    room["can_remove_members"] = room["leader_member_id"] == member["member_id"]
                else:
                    for field in ("membership_id", "leader_member_id", "leadership_epoch"):
                        room.pop(field)
            return rooms

    def room_directory(self, token: str, after: str | None, limit: int) -> dict:
        with self._transaction() as connection:
            _member(connection, token)
            # Every room currently supports self-service joining by team members.
            rows = connection.execute(
                """
                SELECT room_id, name, history_policy FROM rooms
                WHERE room_id > ? AND archived_at IS NULL ORDER BY room_id LIMIT ?
                """,
                (after or "", limit + 1),
            ).fetchall()
            rooms = [dict(row) for row in rows[:limit]]
            return {
                "rooms": rooms,
                "next_after": rooms[-1]["room_id"] if len(rows) > limit else None,
            }

    def room_detail(self, token: str, room_id: str) -> dict:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            room = _room_member(connection, room_id, member_id)
            count = (
                len(json.loads(room["archive_roster"]))
                if room["archived_at"] is not None
                else connection.execute(
                    "SELECT COUNT(*) FROM room_members WHERE room_id = ?", (room_id,)
                ).fetchone()[0]
            )
            return {
                "room_id": room_id,
                "name": room["name"],
                "history_policy": room["history_policy"],
                "current_board_version": room["version"],
                "member_count": count,
                **_archive_metadata(room),
                **_child_metadata(room),
            }

    def room_members(
        self, token: str, room_id: str, *, include_membership: bool = False
    ) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            room = _room_member(connection, room_id, member_id)
            if room["archived_at"] is not None:
                return json.loads(room["archive_roster"])
            return self._room_roster(connection, room_id, include_membership)

    @staticmethod
    def _room_roster(
        connection: sqlite3.Connection, room_id: str, include_membership: bool = False
    ) -> list[dict]:
        rows = connection.execute(
            f"""
            SELECT {_MEMBER_PROFILE}, rm.acked_board_version, rm.membership_id, rm.responsibility
            FROM {_PROFILE_TABLES} JOIN room_members AS rm ON m.member_id = rm.member_id
            WHERE rm.room_id = ? ORDER BY m.member_id
            """,
            (room_id,),
        )
        members = []
        for row in rows:
            profile = _member_profile(row)
            profile["acked_board_version"] = row["acked_board_version"]
            if row["responsibility"] is not None:
                profile["description"] = row["responsibility"]
            if include_membership:
                profile["membership_id"] = row["membership_id"]
            members.append(profile)
        return members

    def sweep_child_rooms(self) -> bool:
        notified: set[str] = set()
        with self._transaction(write=True) as connection:
            now = self._now_datetime()
            now_us = _utc_micros(now)
            expired = connection.execute(
                """
                SELECT c.room_id FROM child_rooms AS c JOIN rooms AS r USING (room_id)
                WHERE c.expires_us <= ? AND r.archived_at IS NULL
                ORDER BY c.expires_us, c.room_id LIMIT ?
                """,
                (now_us, _ROOM_ARCHIVE_BATCH),
            ).fetchall()
            for row in expired:
                _, members = self._archive_room(connection, row["room_id"], _rfc3339(now), "lease_expired")
                notified.update(members)
            offers = connection.execute(
                """
                SELECT c.*, l.leadership_epoch, l.leader_member_id FROM child_rooms AS c
                JOIN rooms AS r USING (room_id) JOIN room_leadership AS l USING (room_id)
                WHERE c.expires_us > ? AND c.expires_us <= ? AND r.archived_at IS NULL
                    AND l.leader_member_id IS NOT NULL AND c.offered_epoch IS NOT l.leadership_epoch
                ORDER BY c.expires_us, c.room_id LIMIT ?
                """,
                (now_us, now_us + _CHILD_NOTICE_US, _ROOM_ARCHIVE_BATCH),
            ).fetchall()
            for offer in offers:
                leader, room_id = offer["leader_member_id"], offer["room_id"]
                expires = _child_metadata(offer)["expires_at"]
                self._add_room_notice(
                    connection, leader, room_id, "child_lease_due",
                    f"Room {room_id} closes at {expires}. To continue, call teamup_extend_room_lease "
                    f"with expected_expires_at {expires} and 1 to 3600 seconds.",
                    leadership_epoch=offer["leadership_epoch"], expires_at=expires,
                )
                connection.execute(
                    "UPDATE child_rooms SET offered_epoch = ? WHERE room_id = ?",
                    (offer["leadership_epoch"], room_id),
                )
                notified.add(leader)
        self._notify(notified)
        return max(len(expired), len(offers)) == _ROOM_ARCHIVE_BATCH

    def _roll_up_child_tasks(
        self, connection: sqlite3.Connection, child: sqlite3.Row, now: str
    ) -> list[str]:
        parent, room_id = child["parent_room_id"], child["room_id"]
        tasks = connection.execute(
            """
            SELECT t.task_id, t.title, t.description, EXISTS (
                SELECT 1 FROM task_addressees AS a WHERE a.task_id = t.task_id
            ) AS restricted FROM tasks AS t
            WHERE t.room_id = ? AND t.status = 'open' ORDER BY t.task_number
            """,
            (room_id,),
        ).fetchall()
        if not tasks:
            return []
        actor = connection.execute(
            """
            SELECT COALESCE(l.leader_member_id, r.creator_member_id, ?)
            FROM rooms AS r LEFT JOIN room_leadership AS l USING (room_id) WHERE r.room_id = ?
            """,
            (child["creator_member_id"], parent),
        ).fetchone()[0]
        last_number = connection.execute(
            "SELECT COALESCE(MAX(task_number), 0) FROM tasks WHERE room_id = ?", (parent,)
        ).fetchone()[0]
        for number, task in enumerate(tasks, start=last_number + 1):
            successor = str(uuid4())
            title = "Coordinate continuation of restricted work" if task["restricted"] else task["title"]
            description = (
                "Contact the child room's team to coordinate access and continuation."
                if task["restricted"] else task["description"]
            )
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, task_number, room_id, creator_member_id, client_task_id,
                    title, description, status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
                """,
                (successor, number, parent, actor, str(uuid4()), title, description, now, now),
            )
            _add_task_event(connection, successor, actor, "created", now)
            connection.execute(
                "INSERT INTO task_origins (successor_task_id, source_task_id) VALUES (?, ?)",
                (successor, task["task_id"]),
            )
        recipients = [
            row[0] for row in connection.execute(
                """
                SELECT m.member_id FROM room_members AS rm JOIN members AS m USING (member_id)
                LEFT JOIN accounts AS a ON a.owner_id = m.owner_id
                WHERE rm.room_id = ? AND m.retired = 0 AND COALESCE(a.disabled, 0) = 0
                """, (parent,)
            )
        ]
        for member_id in recipients:
            self._add_room_notice(
                connection, member_id, parent, "child_tasks_rolled_up",
                f"Child room {room_id} closed. Review and join the continuation tasks in room {parent}.",
            )
        return recipients

    def _archive_room(
        self, connection: sqlite3.Connection, room_id: str, now: str, reason: str
    ) -> tuple[sqlite3.Row, list[str]]:
        members = Collaboration._room_roster(connection, room_id)
        record = connection.execute(
            """
            UPDATE rooms SET archived_at = ?, archive_reason = ?, archive_roster = ?
            WHERE room_id = ?
            RETURNING room_id, name, archived_at, archive_reason
            """,
            (now, reason, json.dumps(members, ensure_ascii=False), room_id),
        ).fetchone()
        notified = [member["member_id"] for member in members]
        child = _child_row(connection, room_id)
        if child is not None:
            notified.extend(self._roll_up_child_tasks(connection, child, now))
        return record, notified

    def archive_room(self, token: str, room_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member = _member(connection, token)
            room = _room_member(connection, room_id, member["member_id"])
            if not _has_room_leadership(connection, room_id, member["member_id"]):
                raise CollaborationError("forbidden")
            if room["archived_at"] is not None:
                return {
                    "room_id": room_id,
                    **{key: room[key] for key in ("name", "archived_at", "archive_reason")},
                }
            now = self._now_datetime()
            children = connection.execute(
                """
                SELECT c.room_id, c.expires_us FROM child_rooms AS c JOIN rooms AS r USING (room_id)
                WHERE c.parent_room_id = ? AND r.archived_at IS NULL
                """,
                (room_id,),
            ).fetchall()
            now_us = _utc_micros(now)
            if any(child["expires_us"] > now_us for child in children):
                raise CollaborationError("room_has_live_children")
            reason = "lease_expired" if room["expires_us"] is not None and room["expires_us"] <= now_us else "goal_reached"
            members = []
            for child in children:
                _, recipients = self._archive_room(connection, child["room_id"], _rfc3339(now), "lease_expired")
                members.extend(recipients)
            record, recipients = self._archive_room(connection, room_id, _rfc3339(now), reason)
            members.extend(recipients)
        self._notify(members)
        return dict(record)

    def archive_inactive_rooms(self) -> bool:
        members: set[str] = set()
        with self._transaction(write=True) as connection:
            now = self._now_datetime()
            rows = connection.execute(
                """
                SELECT room_id FROM rooms AS root
                WHERE archived_at IS NULL AND last_chat_at <= ?
                    AND NOT EXISTS (SELECT 1 FROM child_rooms WHERE room_id = root.room_id)
                    AND NOT EXISTS (
                        SELECT 1 FROM child_rooms AS c JOIN rooms AS r USING (room_id)
                        WHERE c.parent_room_id = root.room_id AND r.archived_at IS NULL
                    )
                ORDER BY last_chat_at, room_id LIMIT ?
                """,
                (_utc_micros(now) - _ROOM_IDLE_MICROS, _ROOM_ARCHIVE_BATCH),
            ).fetchall()
            for row in rows:
                _, notified = self._archive_room(
                    connection, row["room_id"], _rfc3339(now), "inactive"
                )
                members.update(notified)
        self._notify(members)
        return len(rows) == _ROOM_ARCHIVE_BATCH

    def list_archived_rooms(self, token: str, after: str | None, limit: int) -> dict:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            rows = connection.execute(
                """
                SELECT r.room_id, r.name, r.archived_at, r.archive_reason
                FROM room_members AS rm JOIN rooms AS r ON r.room_id = rm.room_id
                WHERE rm.member_id = ? AND r.archived_at IS NOT NULL AND r.room_id > ?
                ORDER BY r.room_id LIMIT ?
                """,
                (member_id, after or "", limit + 1),
            ).fetchall()
            rooms = [
                {**dict(row), **_child_metadata(_child_row(connection, row["room_id"]))}
                for row in rows[:limit]
            ]
            return {
                "rooms": rooms,
                "next_after": rooms[-1]["room_id"] if len(rows) > limit else None,
            }

    def view_board(self, token: str, room_id: str) -> dict:
        with self._transaction() as connection:
            member = _member(connection, token)
            board = _room_member(connection, room_id, member["member_id"])
            return {
                "room_id": room_id,
                **{key: board[key] for key in ("version", "rules", "protocol")},
                "can_edit": (
                    board["archived_at"] is None
                    and _has_room_leadership(connection, room_id, member["member_id"])
                ),
            }

    def publish_board(
        self, token: str, room_id: str, version: int, rules: str, protocol: str
    ) -> dict:
        with self._transaction(write=True) as connection:
            member = _member(connection, token)
            current = _room_member(connection, room_id, member["member_id"], active=True)
            if not _has_room_leadership(connection, room_id, member["member_id"]):
                raise CollaborationError("forbidden")
            if current["version"] != version:
                raise CollaborationError("conflict")
            board = connection.execute(
                """
                UPDATE rooms SET version = version + 1, rules = ?, protocol = ?
                WHERE room_id = ?
                RETURNING room_id, version, rules, protocol
                """,
                (rules, protocol, room_id),
            ).fetchone()
            return {**dict(board), "can_edit": True}

    def read_board(self, token: str, room_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            board = _room_member(connection, room_id, member_id)
            if (board["archived_at"] is None and not board["lease_expired"]
                    and board["read_board_version"] != board["version"]):
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
            if board["archived_at"] is not None or board["lease_expired"]:
                if version == board["acked_board_version"]:
                    return
                raise CollaborationError("room_archived")
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
        mention_member_id: str | None = None,
        mention_member_ids: list[str] | None = None,
    ) -> dict:
        with self._transaction(write=True) as connection:
            sender = _member(connection, token)["member_id"]
            room = _room_member(connection, room_id, sender, active=True)
            _require_current_board(room)
            targets = [] if mention_member_id is None else [mention_member_id]
            if mention_member_ids is not None:
                if (
                    mention_member_id is not None
                    or not 1 <= len(mention_member_ids) <= 16
                    or len(set(mention_member_ids)) != len(mention_member_ids)
                ):
                    raise CollaborationError("invalid_request")
                targets = mention_member_ids
            existing = connection.execute(
                f"""
                SELECT {_MESSAGE}, {_ADDITIONAL_MENTIONS} FROM messages AS m
                WHERE sender_member_id = ? AND client_message_id = ?
                    AND m.room_id IS NOT NULL
                """,
                (sender, client_message_id),
            ).fetchone()
            if existing is not None:
                message = _room_message(existing)
                primary = message["mention_member_id"]
                stored_targets = message.get(
                    "mention_member_ids", [primary] if primary else []
                )
                if (message["room_id"], message["text"], stored_targets) != (
                    room_id,
                    text,
                    targets,
                ):
                    raise CollaborationError("conflict")
                return message
            message, visible_recipients = self._write_room_message(
                connection, room_id, room, sender, client_message_id, text, targets,
                self._now_datetime(),
            )
        self._notify(visible_recipients)
        return message

    def _write_room_message(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        room: sqlite3.Row,
        sender: str,
        client_message_id: str,
        text: str,
        targets: list[str],
        now: datetime,
    ) -> tuple[dict, list[str]]:
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
        if not set(targets).issubset(recipients):
            raise CollaborationError("invalid_mention")
        stamp = _utc_micros(now)
        if stamp - room["last_chat_at"] >= _ROOM_IDLE_MICROS:
            raise CollaborationError("room_archived")
        created_at = _rfc3339(now)
        connection.execute(
            "UPDATE rooms SET last_chat_at = MAX(last_chat_at, ?) WHERE room_id = ?",
            (stamp, room_id),
        )
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
                targets[0] if targets else None,
                created_at,
            ),
        )
        connection.executemany(
            """
            INSERT INTO room_message_additional_mentions (message_id, position, member_id)
            VALUES (?, ?, ?)
            """,
            (
                (message_id, pos, member)
                for pos, member in enumerate(targets[1:], 1)
            ),
        )
        _record_activity(
            connection, room_id, "message", sender, created_at, message_id
        )
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
                    "mention" if recipient in targets else "message",
                ),
            )
        message = _room_message(
            connection.execute(
                f"SELECT {_MESSAGE}, {_ADDITIONAL_MENTIONS} FROM messages AS m WHERE m.message_id = ?",
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
        return message, visible_recipients

    @staticmethod
    def _get_or_create_dm(
        connection: sqlite3.Connection, member_id: str, peer_member_id: str
    ) -> str:
        member_low, member_high = sorted((member_id, peer_member_id))
        connection.execute(
            """
            INSERT INTO dms (dm_id, member_low, member_high) VALUES (?, ?, ?)
            ON CONFLICT (member_low, member_high) DO NOTHING
            """,
            (str(uuid4()), member_low, member_high),
        )
        dm_id: str = connection.execute(
            "SELECT dm_id FROM dms WHERE member_low = ? AND member_high = ?",
            (member_low, member_high),
        ).fetchone()["dm_id"]
        return dm_id

    def open_dm(self, token: str, peer_member_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            if peer_member_id == member_id:
                raise CollaborationError("invalid_peer")
            if _available_member(connection, peer_member_id) is None:
                raise CollaborationError("invalid_peer")
            dm_id = self._get_or_create_dm(connection, member_id, peer_member_id)
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
            message = self._write_dm(
                connection, dm_id, sender, peer, client_message_id, text
            )
        self._notify((peer,))
        return message

    @staticmethod
    def _write_dm(
        connection: sqlite3.Connection,
        dm_id: str,
        sender: str,
        peer: str,
        client_message_id: str,
        text: str,
    ) -> dict:
        message_id = _next_id(connection, "message_id")
        connection.execute(
            """
            INSERT INTO messages (
                message_id, room_id, dm_id, sender_member_id, client_message_id,
                text, mention_member_id, created_at
            ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?)
            """,
            (message_id, dm_id, sender, client_message_id, text, _rfc3339_utc_now()),
        )
        connection.execute(
            """
            INSERT INTO inbox_events (event_id, member_id, message_id, kind)
            VALUES (?, ?, ?, 'dm')
            """,
            (_next_id(connection, "event_id"), peer, message_id),
        )
        return dict(
            connection.execute(
                f"SELECT {_DM_MESSAGE} FROM messages AS m WHERE m.message_id = ?",
                (message_id,),
            ).fetchone()
        )

    def invite_to_room(
        self, token: str, room_id: str, member_id: str, client_request_id: str
    ) -> dict:
        if (
            not isinstance(client_request_id, str)
            or len(client_request_id) > 128
            or not client_request_id.strip()
        ):
            raise CollaborationError("invalid_request")
        try:
            client_request_id.encode("utf-8")
        except UnicodeEncodeError:
            raise CollaborationError("invalid_request") from None
        with self._transaction(write=True) as connection:
            sender = _member(connection, token)["member_id"]
            room = _room_member(connection, room_id, sender, active=True)
            if not _has_room_leadership(connection, room_id, sender):
                raise CollaborationError("forbidden")
            if member_id == sender:
                raise CollaborationError("invalid_request")
            prior = connection.execute(
                """
                SELECT room_id, peer_member_id, message_id FROM room_invitation_requests
                WHERE sender_member_id = ? AND client_request_id = ?
                """,
                (sender, client_request_id),
            ).fetchone()
            if prior is not None and (prior["room_id"], prior["peer_member_id"]) != (
                room_id,
                member_id,
            ):
                raise CollaborationError("conflict")
            if _available_member(connection, member_id) is None:
                raise CollaborationError("not_found")
            if prior is not None:
                if prior["message_id"] is None:
                    raise CollaborationError("not_found")
                return dict(
                    connection.execute(
                        f"SELECT {_DM_MESSAGE} FROM messages AS m WHERE m.message_id = ?",
                        (prior["message_id"],),
                    ).fetchone()
                )
            dm_id = self._get_or_create_dm(connection, sender, member_id)
            text = (
                f"You are invited to join the TeamUp room {room['name']!r} (room ID {room_id}). "
                "After you join, read and acknowledge its Board."
            )
            # Keep invitation request IDs independent of caller-chosen DM message IDs.
            message = self._write_dm(
                connection, dm_id, sender, member_id, str(uuid4()), text
            )
            connection.execute(
                """
                INSERT INTO room_invitation_requests (
                    sender_member_id, client_request_id, room_id, peer_member_id, message_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (sender, client_request_id, room_id, member_id, message["message_id"]),
            )
        self._notify((member_id,))
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

        The cutoff must be a timezone-aware datetime whose UTC
        normalization stays representable -- naive, non-datetime and
        offset-extreme values are rejected with ValueError before any
        transaction.

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
        try:
            cutoff_micros = _utc_micros(cutoff)
        except OverflowError:
            raise ValueError(
                "DM expiry cutoff cannot be normalized to representable UTC"
            ) from None
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
        *,
        membership_id: str | None = None,
        delivery: bool = False,
    ) -> dict:
        with self._transaction() as connection:
            membership = _authorized_membership(connection, token, room_id, active=delivery)
            if (
                membership_id is not None
                and membership["membership_id"] != membership_id
            ):
                raise CollaborationError("membership_changed")
            floor = (
                membership["joined_after"]
                if delivery
                else _transcript_floor(membership)
            )
            cursor = min(max(after, floor), _SQLITE_MAX_ID)
            upper = None if before is None or before > _SQLITE_MAX_ID else before
            rows = _room_transcript(
                connection,
                room_id,
                after=cursor,
                before=upper,
                limit=limit,
                newest_first=before is not None,
            )
            result = {
                "messages": [
                    {**_room_message(record), **_retired_sender(record)}
                    for record in rows
                ],
                **_expiry_metadata(
                    membership["expired_through_message_id"], cursor, upper
                ),
            }
            if delivery:
                result["delivery_board"] = {
                    "room_id": room_id,
                    "version": membership["version"],
                    "rules": membership["rules"],
                    "protocol": membership["protocol"],
                }
            return result

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

    def create_task(
        self,
        token: str,
        room_id: str,
        client_task_id: str,
        title: str,
        description: str | None,
        addressed_member_ids: Iterable[str] | None,
        assignment: dict[str, str | None] | None = None,
    ) -> dict:
        visible_recipients: list[str] = []
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            membership = _room_member(connection, room_id, member_id, active=True)
            _require_current_board(membership)
            raw_addressees = list(addressed_member_ids or ())
            fingerprint_values: list[object] = [
                "create", room_id, title, description, sorted(raw_addressees)
            ]
            assignment_details = (
                (assignment["closing_criteria"], assignment.get("eta"))
                if assignment is not None
                else None
            )
            if assignment_details is not None:
                fingerprint_values.extend(assignment_details)
            fingerprint = _task_fingerprint(fingerprint_values)
            retry = connection.execute(
                """
                SELECT task_id, fingerprint FROM task_retries
                WHERE actor_member_id = ? AND retry_kind = 'create'
                    AND client_key = ?
                """,
                (member_id, client_task_id),
            ).fetchone()
            if retry is not None:
                if retry["fingerprint"] != fingerprint:
                    raise CollaborationError("conflict")
                task = _task_row(connection, room_id, retry["task_id"])
                if task is None:
                    raise CollaborationError("not_found")
                return self._projection(connection, task, member_id)
            addressees = _validated_addressees(connection, room_id, raw_addressees)
            now_datetime = self._now_datetime()
            now = _rfc3339(now_datetime)
            task_id = str(uuid4())
            task_number = connection.execute(
                """
                SELECT COALESCE(MAX(task_number), 0) + 1 FROM tasks
                WHERE room_id = ?
                """,
                (room_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, task_number, room_id, creator_member_id,
                    client_task_id, title, description, status, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
                """,
                (
                    task_id,
                    task_number,
                    room_id,
                    member_id,
                    client_task_id,
                    title,
                    description,
                    now,
                    now,
                ),
            )
            for addressee in addressees:
                connection.execute(
                    "INSERT INTO task_addressees (task_id, member_id) VALUES (?, ?)",
                    (task_id, addressee),
                )
            if assignment_details is not None:
                targets = [addressee for addressee in addressees if addressee != member_id]
                if not 1 <= len(targets) <= 16 or any(
                    _available_member(connection, target) is None for target in targets
                ):
                    raise CollaborationError("invalid_request")
                closing_criteria, eta = assignment_details
                connection.execute(
                    "INSERT INTO task_assignments (task_id, closing_criteria, eta) VALUES (?, ?, ?)",
                    (task_id, closing_criteria, eta),
                )
                text = f"Task #{task_number} assigned. Read the task and join to accept."
                if eta is None:
                    text += " Record your ETA as task progress or reply privately."
                client_message_id = self._assignment_message_key(connection, member_id, task_id)
                _, visible_recipients = self._write_room_message(
                    connection, room_id, membership, member_id, client_message_id, text,
                    targets, now_datetime,
                )
            _add_task_event(connection, task_id, member_id, "created", now)
            connection.execute(
                """
                INSERT INTO task_retries (
                    actor_member_id, retry_kind, client_key, task_id, fingerprint
                ) VALUES (?, 'create', ?, ?, ?)
                """,
                (member_id, client_task_id, task_id, fingerprint),
            )
            task = _task_row(connection, room_id, task_id)
            if task is None:
                raise CollaborationError("not_found")
            result = self._projection(connection, task, member_id)
        if visible_recipients:
            self._notify(visible_recipients)
        return result

    @staticmethod
    def _assignment_message_key(
        connection: sqlite3.Connection, sender: str, task_id: str
    ) -> str:
        prefix = f"task-assignment-{task_id}"
        key = prefix
        # Ordinary room posts can use any valid retry key. Choose an unused key
        # under the same write lock rather than reusing an unrelated receipt.
        while connection.execute(
            """
            SELECT 1 FROM messages
            WHERE sender_member_id = ? AND client_message_id = ? AND room_id IS NOT NULL
            """,
            (sender, key),
        ).fetchone():
            key = f"{prefix}-{uuid4()}"
        return key

    def list_tasks(
        self,
        token: str,
        room_id: str,
        status: str | None = None,
        after: int = 0,
        limit: int = 50,
    ) -> dict:
        if status not in (None, "open", "finished"):
            raise CollaborationError("invalid_request")
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            membership = _room_member(connection, room_id, member_id)
            _require_current_board(membership)
            if limit <= 0:
                return {"tasks": [], "next_after": None}
            cursor = min(max(after, 0), _SQLITE_MAX_ID)
            status_filter = " AND t.status = ?" if status is not None else ""
            values: list[object] = [room_id, cursor]
            if status is not None:
                values.append(status)
            values.extend((member_id, member_id, limit + 1))
            rows = connection.execute(
                f"""
                SELECT {_TASK} FROM tasks AS t
                LEFT JOIN task_assignments AS a USING (task_id)
                WHERE t.room_id = ? AND t.task_number > ?{status_filter}
                    AND {_VISIBLE_TASK}
                ORDER BY t.task_number
                LIMIT ?
                """,
                values,
            ).fetchall()
            next_after = rows[limit - 1]["task_number"] if len(rows) > limit else None
            return {
                "tasks": self._projections(connection, rows[:limit], member_id),
                "next_after": next_after,
            }

    def get_task(self, token: str, room_id: str, task_id: str) -> dict:
        with self._transaction() as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id)
            return self._projection(connection, task, member_id)

    def join_task(self, token: str, room_id: str, task_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id, active=True)
            addressed = connection.execute(
                "SELECT 1 FROM task_addressees WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            if addressed is not None and (
                connection.execute(
                    """
                    SELECT 1 FROM task_addressees
                    WHERE task_id = ? AND member_id = ?
                    """,
                    (task_id, member_id),
                ).fetchone()
                is None
            ):
                raise CollaborationError("not_found")
            if (
                connection.execute(
                    """
                    SELECT 1 FROM task_participants
                    WHERE task_id = ? AND member_id = ?
                    """,
                    (task_id, member_id),
                ).fetchone()
                is not None
            ):
                return self._projection(connection, task, member_id)
            if task["status"] != "open":
                raise CollaborationError("conflict")
            now = self._now()
            connection.execute(
                """
                INSERT INTO task_participants (task_id, member_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (task_id, member_id, now),
            )
            connection.execute(
                """
                UPDATE tasks SET
                    started_at = COALESCE(started_at, ?),
                    version = version + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            _add_task_event(connection, task_id, member_id, "joined", now)
            joined = _task_row(connection, room_id, task_id)
            if joined is None:
                raise CollaborationError("not_found")
            return self._projection(connection, joined, member_id)

    def report_progress(
        self,
        token: str,
        room_id: str,
        task_id: str,
        client_progress_id: str,
        note: str,
    ) -> dict:
        with self._transaction(write=True) as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id, active=True)
            if (
                connection.execute(
                    """
                    SELECT 1 FROM task_participants
                    WHERE task_id = ? AND member_id = ?
                    """,
                    (task_id, member_id),
                ).fetchone()
                is None
            ):
                raise CollaborationError("not_found")
            fingerprint = _task_fingerprint(("progress", task_id, note))
            retry = connection.execute(
                """
                SELECT task_id, fingerprint FROM task_retries
                WHERE actor_member_id = ? AND retry_kind = 'progress'
                    AND client_key = ?
                """,
                (member_id, client_progress_id),
            ).fetchone()
            if retry is not None:
                if retry["fingerprint"] != fingerprint:
                    raise CollaborationError("conflict")
                return self._projection(connection, task, member_id)
            if task["status"] != "open":
                raise CollaborationError("conflict")
            now = self._now()
            connection.execute(
                """
                UPDATE tasks SET
                    last_progress_at = ?,
                    version = version + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            _add_task_event(connection, task_id, member_id, "progress", now, note)
            connection.execute(
                """
                DELETE FROM task_reminders
                WHERE task_id = ? AND claimed_at IS NULL
                """,
                (task_id,),
            )
            connection.execute(
                """
                INSERT INTO task_retries (
                    actor_member_id, retry_kind, client_key, task_id, fingerprint
                ) VALUES (?, 'progress', ?, ?, ?)
                """,
                (member_id, client_progress_id, task_id, fingerprint),
            )
            updated = _task_row(connection, room_id, task_id)
            if updated is None:
                raise CollaborationError("not_found")
            return self._projection(connection, updated, member_id)

    def finish_task(self, token: str, room_id: str, task_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id, active=True)
            participant = (
                connection.execute(
                    """
                    SELECT 1 FROM task_participants
                    WHERE task_id = ? AND member_id = ?
                    """,
                    (task_id, member_id),
                ).fetchone()
                is not None
            )
            if task["creator_member_id"] != member_id and not participant:
                raise CollaborationError("not_found")
            if task["status"] == "finished":
                return self._projection(connection, task, member_id)
            now = self._now()
            connection.execute(
                """
                UPDATE tasks SET
                    status = 'finished',
                    finished_at = ?,
                    version = version + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            _add_task_event(connection, task_id, member_id, "finished", now)
            connection.execute(
                "DELETE FROM task_reminders WHERE task_id = ?", (task_id,)
            )
            finished = _task_row(connection, room_id, task_id)
            if finished is None:
                raise CollaborationError("not_found")
            return self._projection(connection, finished, member_id)

    def task_history(
        self,
        token: str,
        room_id: str,
        task_id: str,
        after: int = 0,
        limit: int = 50,
    ) -> dict:
        with self._transaction() as connection:
            _authorized_task(connection, token, room_id, task_id)
            if limit <= 0:
                return {"events": [], "next_after": None}
            cursor = min(max(after, 0), _SQLITE_MAX_ID)
            rows = connection.execute(
                """
                SELECT event_id, task_id, actor_member_id, kind, created_at, note
                FROM task_events
                WHERE task_id = ? AND event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (task_id, cursor, limit + 1),
            ).fetchall()
            next_after = rows[limit - 1]["event_id"] if len(rows) > limit else None
            return {
                "events": [dict(row) for row in rows[:limit]],
                "next_after": next_after,
            }

    def scan_task_reminders(self) -> list[str]:
        """Commit pending reminders for one bounded, indexed page of open tasks."""
        stall_after = self._task_stall_after
        repeat = self._task_reminder_interval
        if stall_after is None or repeat is None:
            return []
        now = self._now_datetime()
        notified: set[str] = set()
        with self._transaction(write=True) as connection:
            tasks = connection.execute(
                f"""
                SELECT {_TASK} FROM tasks AS t
                LEFT JOIN task_assignments AS a USING (task_id)
                WHERE t.status = 'open' AND t.started_at IS NOT NULL
                    AND t.task_id > ?
                    AND EXISTS (
                        SELECT 1 FROM rooms AS r
                        WHERE r.room_id = t.room_id AND r.archived_at IS NULL
                    )
                ORDER BY t.task_id
                LIMIT ?
                """,
                (self._scan_cursor, _TRACER_TASK_BATCH),
            ).fetchall()
            next_cursor = (
                tasks[-1]["task_id"] if len(tasks) == _TRACER_TASK_BATCH else ""
            )
            for task in tasks:
                task_id = task["task_id"]
                anchor_event_id = _task_anchor_event_id(connection, task)
                if anchor_event_id is None:
                    connection.execute(
                        """
                        DELETE FROM task_reminders
                        WHERE task_id = ? AND claimed_at IS NULL
                        """,
                        (task_id,),
                    )
                    continue
                connection.execute(
                    """
                    DELETE FROM task_reminders
                    WHERE task_id = ? AND claimed_at IS NULL
                        AND anchor_event_id != ?
                    """,
                    (task_id, anchor_event_id),
                )
                stalled_at = _task_stall_at(task, stall_after)
                if stalled_at is None or now < stalled_at:
                    continue
                stalled_since = _rfc3339(stalled_at)
                for member_id in _task_reminder_recipients(connection, task):
                    previous = connection.execute(
                        """
                        SELECT claimed_at FROM task_reminders
                        WHERE task_id = ? AND member_id = ?
                            AND anchor_event_id = ?
                        ORDER BY reminder_id DESC LIMIT 1
                        """,
                        (task_id, member_id, anchor_event_id),
                    ).fetchone()
                    if previous is not None:
                        if previous["claimed_at"] is None:
                            continue
                        elapsed = (
                            now - _parse_rfc3339(previous["claimed_at"])
                        ).total_seconds()
                        if elapsed < repeat:
                            continue
                    inserted = connection.execute(
                        """
                        INSERT INTO task_reminders (
                            task_id, member_id, anchor_event_id,
                            stalled_since, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        RETURNING reminder_id
                        """,
                        (
                            task_id,
                            member_id,
                            anchor_event_id,
                            stalled_since,
                            _rfc3339(now),
                        ),
                    ).fetchone()
                    if inserted is not None:
                        notified.add(member_id)
        self._scan_cursor = next_cursor
        self._notify(sorted(notified))
        return sorted(notified)

    def _reminder_row_eligible(
        self,
        connection: sqlite3.Connection,
        reminder: sqlite3.Row,
        member_id: str,
        now: datetime,
    ) -> bool:
        if self._task_stall_after is None or reminder["status"] != "open":
            return False
        stalled_at = _task_stall_at(reminder, self._task_stall_after)
        if stalled_at is None or now < stalled_at:
            return False
        if _task_anchor_event_id(connection, reminder) != reminder["anchor_event_id"]:
            return False
        return _reminder_eligible(connection, reminder, member_id)

    def task_reminders(self, token: str, after: int = 0, limit: int = 50) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            if self._task_stall_after is None or limit <= 0:
                return []
            now = self._now_datetime()
            cursor = min(max(after, 0), _SQLITE_MAX_ID)
            reminders: list[dict] = []
            while len(reminders) < limit:
                rows = connection.execute(
                    f"""
                    SELECT r.reminder_id, r.task_id, r.anchor_event_id,
                        r.stalled_since, {_TASK}
                    FROM task_reminders AS r
                    JOIN tasks AS t ON t.task_id = r.task_id
                    LEFT JOIN task_assignments AS a ON a.task_id = t.task_id
                    WHERE r.member_id = ? AND r.claimed_at IS NULL
                        AND r.reminder_id > ?
                    ORDER BY r.reminder_id
                    LIMIT ?
                    """,
                    (member_id, cursor, _REMINDER_PAGE_SCAN),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    cursor = row["reminder_id"]
                    if self._reminder_row_eligible(connection, row, member_id, now):
                        reminders.append(_reminder_projection(row))
                        if len(reminders) >= limit:
                            break
                if len(rows) < _REMINDER_PAGE_SCAN:
                    break
            return reminders

    def claim_task_reminder(self, token: str, reminder_id: int) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            if self._task_stall_after is None or not 1 <= reminder_id <= _SQLITE_MAX_ID:
                raise CollaborationError("not_found")
            reminder = connection.execute(
                f"""
                SELECT r.reminder_id, r.task_id, r.anchor_event_id,
                    r.stalled_since, {_TASK}
                FROM task_reminders AS r
                JOIN tasks AS t ON t.task_id = r.task_id
                LEFT JOIN task_assignments AS a ON a.task_id = t.task_id
                WHERE r.reminder_id = ? AND r.member_id = ?
                    AND r.claimed_at IS NULL
                """,
                (reminder_id, member_id),
            ).fetchone()
            if reminder is None:
                raise CollaborationError("not_found")
            now = self._now_datetime()
            if not self._reminder_row_eligible(connection, reminder, member_id, now):
                raise CollaborationError("not_found")
            claimed = connection.execute(
                """
                UPDATE task_reminders SET claimed_at = ?
                WHERE reminder_id = ? AND claimed_at IS NULL
                RETURNING reminder_id
                """,
                (_rfc3339(now), reminder_id),
            ).fetchone()
            if claimed is None:
                raise CollaborationError("not_found")
            connection.execute(
                """
                DELETE FROM task_reminders
                WHERE task_id = ? AND member_id = ? AND claimed_at IS NOT NULL
                    AND reminder_id < ?
                """,
                (reminder["task_id"], member_id, reminder_id),
            )
            return _reminder_projection(reminder)

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
                    m.mention_member_id, m.created_at, {_ADDITIONAL_MENTIONS}
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
                                AND rm.acked_board_version = r.version AND r.archived_at IS NULL
                        )
                    )
                ORDER BY e.event_id LIMIT ?
                """,
                (member_id, min(after, _SQLITE_MAX_ID), *kind_filter, limit),
            )
            events = []
            for record in rows:
                message = (
                    {column: record[column] for column in _DM_EVENT_MESSAGE}
                    if record["kind"] == "dm"
                    else _room_message(record)
                )
                events.append(
                    {
                        "event_id": record["event_id"],
                        "kind": record["kind"],
                        "message": message,
                    }
                )
            if "room_left" in kind_filter:
                for departure in connection.execute(
                    """
                    SELECT event_id, room_id FROM room_departures
                    WHERE member_id = ? AND event_id > ? AND acknowledged = 0
                    ORDER BY event_id LIMIT ?
                    """,
                    (member_id, min(after, _SQLITE_MAX_ID), limit),
                ):
                    events.append({"kind": "room_left", **dict(departure)})
                events.sort(key=lambda event: event["event_id"])
            return events[:limit]

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
                departure = connection.execute(
                    "UPDATE room_departures SET acknowledged = 1 "
                    "WHERE event_id = ? AND member_id = ?",
                    (event_id, member_id),
                )
                if not departure.rowcount:
                    raise CollaborationError("not_found")
                return
            if event["dm_id"] is None:
                room = _room_member(connection, event["room_id"], member_id)
                if room["archived_at"] is not None and not event["acknowledged"]:
                    raise CollaborationError("not_found")
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
        values are rejected before any conversion, and so is one whose
        UTC normalization or cutoff subtraction leaves the representable
        calendar, before any transaction. The ID counters
        never move, so later messages and events keep new, higher IDs.
        """
        if max_age <= timedelta(0):
            raise ValueError("The retention age must be positive")
        if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
            raise ValueError("The expiry clock must be a timezone-aware datetime")
        try:
            moment = datetime.now(UTC) if now is None else now.astimezone(UTC)
            cutoff = _utc_micros(moment - max_age)
        except OverflowError:
            raise ValueError(
                "The expiry clock cannot be normalized to representable UTC"
            ) from None
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

    @staticmethod
    def _add_room_notice(
        connection: sqlite3.Connection,
        member_id: str,
        room_id: str,
        kind: str,
        text: str,
        *,
        leadership_epoch: int | None = None,
        expires_at: str | None = None,
    ) -> int:
        """Record a control notice in the transaction that changed the room."""
        if leadership_epoch is not None or expires_at is not None:
            previous = connection.execute(
                """
                SELECT notice_id FROM room_control_notices
                WHERE room_id = ? AND kind = ? AND member_id = ?
                    AND leadership_epoch IS ? AND expires_at IS ?
                """,
                (room_id, kind, member_id, leadership_epoch, expires_at),
            ).fetchone()
            if previous is not None:
                return int(previous["notice_id"])
        notice_id = _next_id(connection, "event_id")
        connection.execute(
            """
            INSERT INTO room_control_notices (
                notice_id, member_id, room_id, kind, text, leadership_epoch, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (notice_id, member_id, room_id, kind, text, leadership_epoch, expires_at),
        )
        return notice_id

    def room_notices(self, token: str, after: int = 0, limit: int = 50) -> dict:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            rows = connection.execute(
                """
                SELECT n.notice_id, n.kind, n.room_id, n.text, n.leadership_epoch, n.expires_at
                FROM room_control_notices n
                JOIN room_members rm ON rm.room_id = n.room_id AND rm.member_id = n.member_id
                JOIN rooms r ON r.room_id = n.room_id
                WHERE n.member_id = ? AND n.acknowledged = 0 AND n.notice_id > ?
                    AND r.archived_at IS NULL
                ORDER BY n.notice_id LIMIT ?
                """,
                (member_id, after, limit),
            )
            return {"notices": [{key: value for key, value in dict(row).items()
                                 if value is not None} for row in rows]}

    def ack_room_notice(self, token: str, notice_id: int) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            changed = connection.execute(
                """
                UPDATE room_control_notices SET acknowledged = 1
                WHERE notice_id = ? AND member_id = ?
                """,
                (notice_id, member_id),
            )
            if not changed.rowcount:
                raise CollaborationError("not_found")
        return {"notice_id": notice_id}

    def maintain_rooms(self) -> bool:
        children_due = self.sweep_child_rooms()
        archives_due = self.archive_inactive_rooms()
        leadership_due = self.sweep_room_leadership()
        return children_due or archives_due or leadership_due

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
