"""Client-owned SQLite room projections and command intents."""

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import astuple, dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ._local import private_directory, private_file


@dataclass(frozen=True)
class RoomScope:
    server_profile: str
    server_generation: str
    account_id: str
    member_id: str
    room_id: str
    membership_id: str


@dataclass(frozen=True)
class RoomChange:
    collection: str
    entity_id: str
    record: dict[str, object] | None


@dataclass(frozen=True)
class RoomProjection:
    revision: str
    cursor: str | None
    records: tuple[RoomChange, ...]


@dataclass(frozen=True)
class CommandScope:
    server_profile: str
    account_id: str
    member_id: str
    room_id: str

    @property
    def actor(self) -> str:
        return json.dumps(astuple(self)[:3], separators=(",", ":"))


@dataclass(frozen=True)
class StoredCommand:
    operation_id: str
    room_id: str
    generation: str
    membership_id: str | None
    intent: str
    request: dict | None
    attempts: int
    blocked: bool
    receipt: dict | None


@contextmanager
def open_room_store(socket_path: Path) -> Iterator["RoomStore"]:
    private_directory(socket_path.parent)
    path = socket_path.with_name(f"{socket_path.name}.rooms.sqlite3")
    os.close(private_file(path, create=True))
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA secure_delete = ON")
        db.executescript("""
            BEGIN;
            CREATE TABLE IF NOT EXISTS room_scopes (
                scope TEXT PRIMARY KEY,
                revision TEXT NOT NULL,
                cursor TEXT
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS room_records (
                scope TEXT NOT NULL REFERENCES room_scopes(scope) ON DELETE CASCADE,
                collection TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                record TEXT NOT NULL,
                PRIMARY KEY (scope, collection, entity_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS room_commands (
                operation_id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                room_id TEXT NOT NULL,
                generation TEXT NOT NULL,
                membership_id TEXT,
                kind TEXT NOT NULL,
                retry_key TEXT,
                intent TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                request TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                automatic_attempted INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                rejection TEXT,
                receipt TEXT,
                released INTEGER NOT NULL DEFAULT 0
            ) WITHOUT ROWID;
            CREATE UNIQUE INDEX IF NOT EXISTS room_command_queue
                ON room_commands (actor, room_id)
                WHERE blocked = 0 AND rejection IS NULL AND receipt IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS room_command_retry_key
                ON room_commands (actor, kind, retry_key)
                WHERE retry_key IS NOT NULL AND rejection IS NULL;
            CREATE INDEX IF NOT EXISTS room_command_intent
                ON room_commands (actor, room_id, kind, intent);
            COMMIT;
        """)
        yield RoomStore(db)


def _scope_key(scope: RoomScope) -> str:
    return json.dumps(astuple(scope), separators=(",", ":"))


class RoomStore:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def find_command(
        self, scope: CommandScope, kind: str, retry_key: str | None, intent: str
    ) -> StoredCommand | None:
        selector = (
            ("retry_key = ?", (retry_key,))
            if retry_key is not None
            else (
                "room_id = ? AND intent = ? AND retry_key IS NULL AND released = 0",
                (scope.room_id, intent),
            )
        )
        return self._get_command(
            f"actor = ? AND kind = ? AND rejection IS NULL AND {selector[0]}",
            (scope.actor, kind, *selector[1]),
        )

    def pending_command(self, scope: CommandScope) -> StoredCommand | None:
        return self._get_command(
            "actor = ? AND room_id = ? AND blocked = 0 AND rejection IS NULL AND receipt IS NULL",
            (scope.actor, scope.room_id),
        )

    def reconnect_queue(self, scope: CommandScope, limit: int) -> list[tuple[str, str]]:
        rows = self._db.execute(
            """
            SELECT room_id, operation_id FROM room_commands
            WHERE actor = ? AND blocked = 0 AND rejection IS NULL
                AND receipt IS NULL AND automatic_attempted = 0
            ORDER BY room_id LIMIT ?
            """,
            (scope.actor, limit),
        )
        return [(room_id, operation_id) for room_id, operation_id in rows]

    def _get_command(self, where: str, params: tuple) -> StoredCommand | None:
        row = self._db.execute(
            f"""
            SELECT operation_id, room_id, generation, membership_id, intent,
                request, attempts, blocked, receipt, request_fingerprint
            FROM room_commands WHERE {where}
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        operation_id, room_id, generation, membership_id, intent, request, attempts, blocked, receipt, fingerprint = row
        if request is not None and sha256(request.encode()).hexdigest() != fingerprint:
            raise ValueError("Stored room command does not match its fingerprint")
        return StoredCommand(
            operation_id, room_id, generation, membership_id, intent,
            json.loads(request) if request is not None else None, attempts, bool(blocked),
            json.loads(receipt) if receipt is not None else None,
        )

    def stage_command(
        self,
        scope: CommandScope,
        generation: str,
        membership_id: str | None,
        retry_key: str | None,
        intent: str,
        body: dict,
        *,
        blocked: bool = False,
    ) -> StoredCommand | None:
        request = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        operation_id = body["operation_id"]
        with self._db:
            inserted = self._db.execute(
                """
                INSERT OR IGNORE INTO room_commands (
                    operation_id, actor, room_id, generation, membership_id, kind,
                    retry_key, intent, request_fingerprint, request, blocked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id, scope.actor, scope.room_id, generation, membership_id,
                    body["kind"], retry_key, intent, sha256(request.encode()).hexdigest(),
                    None if blocked else request,
                    blocked,
                ),
            )
            if inserted.rowcount == 1:
                self._db.execute(
                    """
                    UPDATE room_commands SET request = NULL
                    WHERE actor = ? AND room_id = ? AND kind = ? AND intent = ?
                        AND rejection IS NOT NULL AND request IS NOT NULL
                    """,
                    (scope.actor, scope.room_id, body["kind"], intent),
                )
        if inserted.rowcount != 1:
            return None
        return StoredCommand(
            operation_id, scope.room_id, generation, membership_id, intent,
            None if blocked else json.loads(request), 0, blocked, None,
        )

    def mark_command_dispatch(self, command: StoredCommand, *, automatic: bool = False) -> bool:
        with self._db:
            updated = self._db.execute(
                """
                UPDATE room_commands SET attempts = attempts + 1,
                    automatic_attempted = MAX(automatic_attempted, ?)
                WHERE operation_id = ? AND attempts = ? AND blocked = 0
                    AND rejection IS NULL AND receipt IS NULL
                    AND (NOT ? OR automatic_attempted = 0)
                """,
                (automatic, command.operation_id, command.attempts, automatic),
            )
        return updated.rowcount == 1

    def save_command_receipt(self, operation_id: str, receipt: dict) -> None:
        with self._db:
            self._db.execute(
                """
                UPDATE room_commands SET receipt = ?, request = NULL
                WHERE operation_id = ? AND attempts > 0
                    AND receipt IS NULL AND rejection IS NULL
                """,
                (json.dumps(receipt, allow_nan=False), operation_id),
            )

    def command_result_current(self, operation_id: str) -> bool:
        return self._db.execute(
            """
            SELECT 1 FROM room_commands
            WHERE operation_id = ? AND receipt IS NOT NULL AND blocked = 0
            """,
            (operation_id,),
        ).fetchone() is not None

    def release_command_result(self, operation_id: str) -> bool:
        with self._db:
            result = self._db.execute(
                """
                UPDATE room_commands SET released = 1
                WHERE operation_id = ? AND receipt IS NOT NULL AND blocked = 0
                """,
                (operation_id,),
            )
        return result.rowcount == 1

    def reject_command(self, operation_id: str, code: str, *, keep_draft: bool = False) -> None:
        with self._db:
            self._db.execute(
                """
                UPDATE room_commands SET rejection = ?,
                    request = CASE WHEN ? THEN request ELSE NULL END
                WHERE operation_id = ? AND attempts = 1 AND receipt IS NULL
                """,
                (code, keep_draft, operation_id),
            )

    def block_command(self, operation_id: str, *, redact: bool = False) -> None:
        with self._db:
            self._db.execute(
                """
                UPDATE room_commands SET blocked = 1, request = CASE WHEN ? THEN NULL ELSE request END
                WHERE operation_id = ?
                """,
                (redact, operation_id),
            )

    def invalidate_commands(
        self,
        server_profile: str,
        account_id: str,
        member_id: str,
        *,
        room_id: str | None = None,
        membership_id: str | None = None,
        keep_self_leave: bool = False,
    ) -> None:
        scope = CommandScope(server_profile, account_id, member_id, room_id or "")
        with self._db:
            self._db.execute(
                """
                UPDATE room_commands SET request = NULL, blocked = 1
                WHERE actor = ? AND (? IS NULL OR room_id = ?)
                    AND (? IS NULL OR membership_id = ?)
                    AND (NOT ? OR kind != 'leave_room')
                    AND (request IS NOT NULL OR blocked = 0)
                """,
                (
                    scope.actor, room_id, room_id, membership_id, membership_id,
                    keep_self_leave,
                ),
            )

    def begin(self, scope: RoomScope) -> str:
        key = _scope_key(scope)
        with self._db:
            self._db.execute(
                """
                INSERT INTO room_scopes (scope, revision) VALUES (?, ?)
                ON CONFLICT (scope) DO NOTHING
                """,
                (key, uuid4().hex),
            )
            row = self._db.execute(
                "SELECT revision FROM room_scopes WHERE scope = ?", (key,)
            ).fetchone()
        return str(row[0])

    def load(self, scope: RoomScope) -> RoomProjection | None:
        rows = self._db.execute(
            """
            SELECT s.revision, s.cursor, r.collection, r.entity_id, r.record
            FROM room_scopes AS s LEFT JOIN room_records AS r USING (scope)
            WHERE s.scope = ?
            ORDER BY r.collection, r.entity_id
            """,
            (_scope_key(scope),),
        ).fetchall()
        if not rows:
            return None
        revision, cursor, *_ = rows[0]
        records = tuple(
            RoomChange(collection, entity_id, json.loads(record))
            for _, _, collection, entity_id, record in rows
            if record is not None
        )
        return RoomProjection(revision, cursor, records)

    def is_current(self, scope: RoomScope, revision: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM room_scopes WHERE scope = ? AND revision = ?",
            (_scope_key(scope), revision),
        ).fetchone() is not None

    def replace(
        self,
        scope: RoomScope,
        revision: str,
        cursor: str,
        changes: Iterable[RoomChange],
    ) -> str | None:
        return self._write(scope, revision, cursor, changes, replace=True)

    def apply(
        self,
        scope: RoomScope,
        revision: str,
        cursor: str,
        changes: Iterable[RoomChange],
    ) -> str | None:
        return self._write(scope, revision, cursor, changes, replace=False)

    def _write(
        self,
        scope: RoomScope,
        revision: str,
        cursor: str,
        changes: Iterable[RoomChange],
        *,
        replace: bool,
    ) -> str | None:
        key = _scope_key(scope)
        next_revision = uuid4().hex
        # Prepare the whole page before opening a transaction. Iterators belong to callers.
        records = [
            (
                change.collection,
                change.entity_id,
                None
                if change.record is None
                else json.dumps(change.record, allow_nan=False),
            )
            for change in changes
        ]
        with self._db:
            updated = self._db.execute(
                """
                UPDATE room_scopes SET revision = ?, cursor = ?
                WHERE scope = ? AND revision = ?
                """,
                (next_revision, cursor, key, revision),
            )
            if updated.rowcount != 1:
                return None
            if replace:
                self._db.execute("DELETE FROM room_records WHERE scope = ?", (key,))
            for collection, entity_id, record in records:
                identity = (key, collection, entity_id)
                if record is None:
                    self._db.execute(
                        """
                        DELETE FROM room_records
                        WHERE scope = ? AND collection = ? AND entity_id = ?
                        """,
                        identity,
                    )
                else:
                    self._db.execute(
                        """
                        INSERT INTO room_records (scope, collection, entity_id, record)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (scope, collection, entity_id) DO UPDATE
                        SET record = excluded.record
                        """,
                        (*identity, record),
                    )
        return next_revision

    def invalidate(self, scope: RoomScope) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM room_scopes WHERE scope = ?", (_scope_key(scope),)
            )

    def invalidate_room(
        self,
        server_profile: str,
        account_id: str,
        member_id: str,
        room_id: str | None = None,
        *,
        keep: RoomScope | None = None,
    ) -> tuple[RoomScope, ...]:
        retained = _scope_key(keep) if keep is not None else None
        with self._db:
            rows = self._db.execute(
                """
                DELETE FROM room_scopes
                WHERE json_extract(scope, '$[0]') = ?
                    AND json_extract(scope, '$[2]') = ?
                    AND json_extract(scope, '$[3]') = ?
                    AND (? IS NULL OR json_extract(scope, '$[4]') = ?)
                    AND (? IS NULL OR scope != ?)
                RETURNING scope
                """,
                (server_profile, account_id, member_id, room_id, room_id, retained, retained),
            ).fetchall()
        return tuple(RoomScope(*json.loads(key)) for (key,) in rows)
