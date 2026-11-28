# Room Sync Integration Implementation Plan

**Goal.** Connect the approved room synchronization and durable-command contracts to the installed Client and Server.

**Architecture.** Collaboration authorizes canonical writes and provides a transactional change journal and generation. Gateway translates the wire interface. Client Room Sync validates and applies the room projection through the existing SQLite Room Store. The coordinator owns member verification and calls the durable-command interface. Existing Console and MCP operations keep their result contracts.

**Tech stack.** Python, SQLite, FastAPI, HTTPX, pytest and the existing nightly and weekly runners.

## Implementation sequence

- [x] Implement capability discovery, authorized baselines and paged changes in `server/src/teamup_server/collaboration.py` and `gateway.py`. Verify scope binding, archive transitions, legacy mutations, task visibility and journal resets in Server tests.
- [x] Wire `client/shared/src/teamup_client/_room_sync.py` into `main.py` and `_console_data.py`. Verify process restarts and authorized Console responses against the Server, then register the read path in nightly and weekly recovery.
- [x] Persist and reconcile confirmed members in Client state through `_coordinator.py` and `_notices.py`. Exercise missing and changed member IDs before a second action can use the binding.
- [x] Implement the typed command route and atomic receipts in Collaboration and Gateway, then connect Client outbox persistence and transport. Test commit-before-denial, denial-before-commit, actor and generation changes, and recoverable receipt retries on both sides of a restart.
- [x] Run the scheduled scenario entry points for available paths, check contract and system tests, and record observed results and outstanding coverage.

## Verification

On macOS with Python 3.13, the broad Client, Server, system, boundary and contract test run passed 482 tests with one skip. The narrower end-to-end and contract rerun after review fixes passed 219 tests. The final storage, session, contract, and boundary rerun passed 75 tests, including the keyed Leave regression. Console Node tests passed 47 cases. Ruff and mypy passed.

The installed scenarios NS-20A–L passed. The weekly agent runner passed WE-04 and WE-08 with scripted agents in dry-run mode. Model-free NP-17 recovered six independent Client projections after 10,001 writes through existing routes.

```sh
uv run --no-sync pytest -q tests/test_boundaries.py tests/client tests/server tests/system tests/contract
TEAMUP_SCENARIOS=nightly uv run --no-sync pytest -q tests/scenarios/nightly/test_ns20_room_sync.py tests/scenarios/nightly/test_ns20_room_commands.py
uv run --no-sync python -m tests.scenarios.weekly --dry-run --only WE-04,WE-08
uv run --no-sync python -m tests.scenarios.perf --suite weekly --only NP-17
```
