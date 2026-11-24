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
from datetime import UTC, datetime
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
_DM_MESSAGE = """
    m.message_id, m.dm_id, m.sender_member_id, m.client_message_id, m.text, m.created_at
"""
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


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


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
    """
    connection.execute(_DMS_TABLE)
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


def _dm_participant(
    connection: sqlite3.Connection, dm_id: str, member_id: str
) -> sqlite3.Row:
    record = connection.execute(
        """
        SELECT member_low, member_high FROM dms
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
            if version in (1, 2, 3):
                _migrate_dm_schema(connection)
            if version in (0, 1, 2, 3):
                connection.execute("PRAGMA user_version = 4")
            elif version != 4:
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

    def dm_history(self, token: str, dm_id: str, after: int, limit: int) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            _dm_participant(connection, dm_id, member_id)
            cursor = min(max(after, 0), _SQLITE_MAX_ID)
            rows = connection.execute(
                f"""
                SELECT {_DM_MESSAGE} FROM messages AS m
                WHERE m.dm_id = ? AND m.message_id > ? ORDER BY m.message_id LIMIT ?
                """,
                (dm_id, cursor, limit),
            )
            return [dict(record) for record in rows]

    def history(self, token: str, room_id: str, after: int, limit: int) -> list[dict]:
        with self._transaction() as connection:
            member_id = _member(connection, token)["member_id"]
            membership = _room_member(connection, room_id, member_id)
            _require_current_board(membership)
            floor = (
                0
                if membership["history_policy"] == "retained"
                else membership["joined_after"]
            )
            cursor = min(max(after, floor), _SQLITE_MAX_ID)
            rows = connection.execute(
                f"""
                SELECT {_MESSAGE} FROM messages AS m
                WHERE room_id = ? AND message_id > ? ORDER BY message_id LIMIT ?
                """,
                (room_id, cursor, limit),
            )
            return [dict(record) for record in rows]

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
                        m.dm_id IS NOT NULL
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
                SELECT m.room_id, m.dm_id, e.acknowledged FROM inbox_events AS e
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
