# Room archive implementation plan

**Goal** Recycle rooms after goal completion or 24 hours of silence while keeping history and the closing roster.

**Architecture** Collaboration owns the archive transaction and authorization. Gateway and member clients use the published room contract. The existing systemd backup continues to capture the full database over SSH.

**Tech stack** Python, FastAPI, SQLite, TeamUp Client and MCP, pytest, and systemd.

1. Add failing lifecycle tests in `tests/server/src/teamup_server/test_room_archive.py` for creator authorization, retries, message delivery, archived transcripts and roster, inactivity, restarts, and competing writes. Implement migration and transactions in `server/src/teamup_server/collaboration.py`, and add the Gateway routes in `server/src/teamup_server/gateway.py`. Run the focused server tests.
2. Wire the inactivity sweep in `server/src/teamup_server/maintenance.py` and `main.py`. Verify that passing former retention flags leaves both families of history intact. Keep compatibility reads for past expiration watermarks.
3. Add archive operations and the read-only archive view through the member Client and MCP surface. Update the contract snapshots and the room and retention documentation. Run `uv run --no-sync pytest -q tests/server tests/contract tests/system tests/scripts/ops tests/test_boundaries.py` and the lint and type checks.
4. Open the linked PR and monitor the normal merge gate. Deploy the merged release with a pre-upgrade snapshot. Take and verify a new hp-laptop generation and run an isolated restore through authenticated APIs. Clean up this change’s branch, worktree, and temporary restore copies after verification.
