# Focused rooms implementation plan

> For agentic workers, apply subagent-driven development and test-driven development.

Read the [approved focused rooms contract](../../modules/focused-rooms.md). Start from current main and use independent branches from the contract commit.

- [ ] **Collaboration leadership (#124).** Add failing unit tests for ranked handoff, current authority, the departure and retirement paths, and crash recovery. Add durable leader and heartbeat state, generation checks and a bounded sweep to Collaboration. Include migration tests against an existing room and move all creator capability checks to the current leader.
- [ ] **Collaboration children (#123).** Add failing clock and concurrent closure tests. Add the immutable relationship and timestamps, idempotent creation and renewal, expiry sweep, archive integration, and transactional parent work. Cover restricted task content and absence of duplicate work across restarts.
- [ ] **DM invitations (#122).** Add a failing Collaboration unit test for private, leader-only, idempotent DM invitations. Share the ordinary DM mutation and delivery path with member sends. Verify authorization and unchanged legacy DM receipts.
- [ ] **HTTP and tool integration (#122–#125).** Add contract and cross-process tests. Wire Gateway handlers to Collaboration, extend the Client API, binding IPC and MCP. Cover notice delivery and validate changed contract goldens.
- [ ] **Console handoff (#125).** Add a failing shared console test for leader Leave, dismissal, manual selection, automatic selection, and deferred choice. Implement linked room and invitation actions plus the successor dialog through the established local Client write boundary.
- [ ] **Scheduled verification (#126).** Add a marked model-free end-to-end scenario, Client Console checks, and a pressure case with nightly and weekly profiles. Assert transactional outcomes and measure expiry lag and contention. Add a bounded real-agent scenario to weekly.
- [ ] **QA and release (#121).** Integrate in dependency order, review all code and permission changes, and run UT, ST, contract, architecture and scheduled scenario checks locally. Create a linked PR and follow the normal merge gate. Verify the rollout target, deployment and snapshot restore. Clean up task-owned branches and worktrees after verification.

Local baseline and release commands include `uv sync --locked --all-packages`, `uv run --no-sync pytest -q tests/server tests/client/shared tests/integration tests/plugin/mcp tests/system tests/contract`, `uv run --no-sync ruff check .`, `uv run --no-sync mypy`, and `npm --prefix client/macos/desktop test`. Run scenario and pressure entry points through the documented CI profiles.
