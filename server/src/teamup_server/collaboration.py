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
from uuid import uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError

_LOGGER = logging.getLogger(__name__)
_SQLITE_MAX_ID = (1 << 63) - 1
_DEFAULT_RULES = "Share only information authorized for this room."
_DEFAULT_PROTOCOL = (
    "Coordinate work in the room and direct mentions to the intended teammate."
)
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43}")
_USERNAME = re.compile(r"[a-z][a-z0-9_-]{2,63}")
_SESSION_SECONDS = 8 * 3600
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_CHECKS_PER_WINDOW = 60
_LOGIN_FAILURES_PER_USERNAME = 5
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2, memory_cost=19 * 1024, parallelism=1, type=Type.ID
)
_MEMBER = "member_id, role_description"
_MESSAGE = """
    m.message_id, m.room_id, m.sender_member_id, m.client_message_id, m.text, m.mention_member_id, m.created_at
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
_TASK = """
    t.task_id, t.task_number, t.room_id, t.creator_member_id, t.title,
    t.description, t.status, t.version, t.created_at, t.updated_at,
    t.started_at, t.last_progress_at, t.finished_at
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
_TRACER_TASK_BATCH = 100
_REMINDER_PAGE_SCAN = 1000

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
            CHECK (history_policy IN ('retained', 'since_joining'))
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
        created_at TEXT,
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
        expires_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX account_sessions_owner ON account_sessions (owner_id)",
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


ChangeListener = Callable[[], None]
ObserveInbox = Callable[[ChangeListener], AbstractContextManager[None]]


class CollaborationError(Exception):
    """A rejection represented by a public collaboration error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
            rm.read_board_version, rm.acked_board_version, rm.joined_after
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


def _task_fingerprint(values: Iterable[object]) -> str:
    canonical = json.dumps(
        list(values), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _task_row(
    connection: sqlite3.Connection, room_id: str, task_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT {_TASK} FROM tasks AS t WHERE t.room_id = ? AND t.task_id = ?",
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
    enabled = connection.execute(
        """
        SELECT 1 FROM members AS m
        WHERE m.member_id = ? AND NOT EXISTS (
            SELECT 1 FROM accounts AS a
            WHERE a.owner_id = m.owner_id AND a.disabled = 1
        )
        """,
        (member_id,),
    ).fetchone()
    if enabled is None:
        return False
    membership = connection.execute(
        """
        SELECT 1 FROM room_members AS rm JOIN rooms AS r USING (room_id)
        WHERE rm.room_id = ? AND rm.member_id = ?
            AND rm.acked_board_version = r.version
        """,
        (task["room_id"], member_id),
    ).fetchone()
    if membership is None:
        return False
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
        row[0]
        for row in connection.execute(
            """
            SELECT tp.member_id
            FROM task_participants AS tp
            JOIN tasks AS t ON t.task_id = tp.task_id
            JOIN members AS m ON m.member_id = tp.member_id
            JOIN room_members AS rm
                ON rm.room_id = ? AND rm.member_id = tp.member_id
            JOIN rooms AS r ON r.room_id = rm.room_id
            WHERE tp.task_id = ?
                AND rm.acked_board_version = r.version
                AND NOT EXISTS (
                    SELECT 1 FROM accounts AS a
                    WHERE a.owner_id = m.owner_id AND a.disabled = 1
                )
                AND (
                    t.creator_member_id = tp.member_id
                    OR NOT EXISTS (
                        SELECT 1 FROM task_addressees AS a
                        WHERE a.task_id = t.task_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM task_addressees AS a
                        WHERE a.task_id = t.task_id
                            AND a.member_id = tp.member_id
                    )
                )
            ORDER BY tp.member_id
            """,
            (task["room_id"], task["task_id"]),
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


def _task_projection(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    stall_after: int | None,
    now: datetime,
) -> dict:
    task_id = task["task_id"]
    return {
        "task_id": task_id,
        "task_number": task["task_number"],
        "room_id": task["room_id"],
        "creator_member_id": task["creator_member_id"],
        "title": task["title"],
        "description": task["description"],
        "addressed_member_ids": [
            row[0]
            for row in connection.execute(
                "SELECT member_id FROM task_addressees WHERE task_id = ? ORDER BY member_id",
                (task_id,),
            )
        ],
        "participant_member_ids": [
            row[0]
            for row in connection.execute(
                "SELECT member_id FROM task_participants WHERE task_id = ? ORDER BY member_id",
                (task_id,),
            )
        ],
        "status": task["status"],
        "version": task["version"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "started_at": task["started_at"],
        "last_progress_at": task["last_progress_at"],
        "finished_at": task["finished_at"],
        "stalled_since": _task_stalled_since(task, stall_after, now),
    }


def _authorized_task(
    connection: sqlite3.Connection, token: str, room_id: str, task_id: str
) -> tuple[str, sqlite3.Row]:
    member_id = _member(connection, token)["member_id"]
    membership = _room_member(connection, room_id, member_id)
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
            if version in (0, 1, 2):
                connection.execute("PRAGMA user_version = 3")
            elif version != 3:
                raise ValueError("Unsupported TeamUp database schema version")
            for statement in _TASK_SCHEMA:
                connection.execute(statement)

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

    def _now_datetime(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _now(self) -> str:
        return _rfc3339(self._now_datetime())

    def _projection(self, connection: sqlite3.Connection, task: sqlite3.Row) -> dict:
        return _task_projection(
            connection,
            task,
            stall_after=self._task_stall_after,
            now=self._now_datetime(),
        )

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

    def login(self, username: str, password: str) -> dict:
        _validate_username(username)
        _validate_password(password)
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
        session = self._issue_account_session(account["owner_id"], digest)
        with self._login_checks_lock:
            check.succeeded = True
        return session

    def _issue_account_session(self, owner_id: str, password_digest: str) -> dict:
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
                "INSERT INTO account_sessions VALUES (?, ?, ?)",
                (digest, owner_id, expires_at),
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
            SELECT a.owner_id, a.disabled, s.expires_at
            FROM account_sessions AS s JOIN accounts AS a USING (owner_id)
            WHERE s.token_digest = ?
            """,
            (digest,),
        ).fetchone()
        if session is not None:
            if session["disabled"] or session["expires_at"] <= int(time()):
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
        return {"room_id": room_id, "name": name, "history_policy": history_policy}

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
                    _rfc3339_utc_now(),
                ),
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

    def history(
        self,
        token: str,
        room_id: str,
        after: int,
        limit: int,
        before: int | None = None,
    ) -> list[dict]:
        with self._transaction() as connection:
            membership = _authorized_membership(connection, token, room_id)
            rows = _room_transcript(
                connection,
                room_id,
                after=min(max(after, _transcript_floor(membership)), _SQLITE_MAX_ID),
                before=None if before is None or before > _SQLITE_MAX_ID else before,
                limit=limit,
                newest_first=before is not None,
            )
            return [dict(record) for record in rows]

    def search(
        self, token: str, room_id: str, query: str, after: int, limit: int
    ) -> list[dict]:
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
            return [
                {
                    "message_id": row["message_id"],
                    "sender_member_id": row["sender_member_id"],
                    "created_at": row["created_at"],
                    "snippet": _snippet(row["body"], needle, row["hit"]),
                }
                for row in rows
            ]

    def create_task(
        self,
        token: str,
        room_id: str,
        client_task_id: str,
        title: str,
        description: str | None,
        addressed_member_ids: Iterable[str] | None,
    ) -> dict:
        with self._transaction(write=True) as connection:
            member_id = _member(connection, token)["member_id"]
            membership = _room_member(connection, room_id, member_id)
            _require_current_board(membership)
            raw_addressees = list(addressed_member_ids or ())
            fingerprint = _task_fingerprint(
                ("create", room_id, title, description, sorted(raw_addressees))
            )
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
                return self._projection(connection, task)
            addressees = _validated_addressees(connection, room_id, raw_addressees)
            now = self._now()
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
            return self._projection(connection, task)

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
                WHERE t.room_id = ? AND t.task_number > ?{status_filter}
                    AND {_VISIBLE_TASK}
                ORDER BY t.task_number
                LIMIT ?
                """,
                values,
            ).fetchall()
            next_after = rows[limit - 1]["task_number"] if len(rows) > limit else None
            return {
                "tasks": [self._projection(connection, task) for task in rows[:limit]],
                "next_after": next_after,
            }

    def get_task(self, token: str, room_id: str, task_id: str) -> dict:
        with self._transaction() as connection:
            _, task = _authorized_task(connection, token, room_id, task_id)
            return self._projection(connection, task)

    def join_task(self, token: str, room_id: str, task_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id)
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
                return self._projection(connection, task)
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
            return self._projection(connection, joined)

    def report_progress(
        self,
        token: str,
        room_id: str,
        task_id: str,
        client_progress_id: str,
        note: str,
    ) -> dict:
        with self._transaction(write=True) as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id)
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
                return self._projection(connection, task)
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
            return self._projection(connection, updated)

    def finish_task(self, token: str, room_id: str, task_id: str) -> dict:
        with self._transaction(write=True) as connection:
            member_id, task = _authorized_task(connection, token, room_id, task_id)
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
                return self._projection(connection, task)
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
            return self._projection(connection, finished)

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
                WHERE t.status = 'open' AND t.started_at IS NOT NULL
                    AND t.task_id > ?
                ORDER BY t.task_id
                LIMIT ?
                """,
                (self._scan_cursor, _TRACER_TASK_BATCH),
            ).fetchall()
            if len(tasks) < _TRACER_TASK_BATCH:
                self._scan_cursor = ""
            elif tasks:
                self._scan_cursor = tasks[-1]["task_id"]
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
            return _reminder_projection(reminder)

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
