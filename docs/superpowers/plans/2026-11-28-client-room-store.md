# Client Room Store Implementation Plan

**Goal.** Provide a private SQLite adapter for authorized room projections and their change cursors.

**Architecture.** The adapter accepts a fully resolved scope from the future Room Sync caller. A scope includes the Server profile and generation, account, member, room, and membership. Snapshot replacement, delta application, and invalidation go through the same owner. The adapter uses a local revision to reject late writes and commits each page and cursor together. The Client–Server proposal governs runtime integration.

**Tech stack.** Python 3.12+, standard-library SQLite, pytest.

## Storage interface

Add `client/shared/src/teamup_client/_room_store.py`. Expose `open_room_store(socket_path)`, a `RoomScope` value, and a `RoomChange` value. The returned store has the following operations.

| Operation | Result |
| --- | --- |
| `begin(scope)` | Open or create a scope and return its current local revision. |
| `load(scope)` | Return the revision, cursor, and records, or `None` for an absent scope. |
| `replace(scope, revision, cursor, changes)` | Replace the records and save the cursor if the expected revision matches. Return the new revision or `None` on mismatch. |
| `apply(scope, revision, cursor, changes)` | Upsert or remove the affected records and save the cursor if the expected revision matches. Return the new revision or `None` on mismatch. |
| `invalidate(scope)` | Delete that scope and its records. Reopening generates a fresh revision. |

Use the existing private-file helper and a dedicated SQLite file beside the profile socket. Use foreign keys for row cleanup and one SQLite transaction for a revision check, record writes, and cursor advancement. A record with a null payload requests removal.

## Verification

- [x] Add storage tests in `tests/client/shared/src/teamup_client/test_room_store.py` that exercise reopen, exact removal, empty-page advancement, stale and invalidated revisions, cross-scope isolation, rollback of a failed page, and file permissions. Run the new tests before adding the adapter to confirm it is missing.
- [x] Implement the storage interface and run `uv run --no-sync pytest -q tests/client/shared/src/teamup_client/test_room_store.py`. Reproduce and fix a caller iterator committing a partial page, and exercise SQLite rollback with an injected record write failure.
- [x] Extend `tests/test_boundaries.py` so the new private file and schema stay behind the adapter. Run the Client tests, boundary tests, and static checks.

On macOS with Python 3.13, the following run passed 151 tests. Ruff and mypy passed, and the built Client wheel included the adapter.

```sh
uv run --no-sync pytest -q tests/test_boundaries.py tests/client/shared tests/contract tests/system/test_console_transcript.py tests/system/test_shared_binding_retirement.py
```

The next integration slice needs the approved Server feed and local view contract. Command outbox persistence has a separate lifetime from the projection and is outside this adapter slice.
