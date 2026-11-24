# Room tasks and stalled-task tracing implementation plan

**Goal** Post, take, update and finish tasks through approved sessions, then trace stalled work with opt-in desktop reminders.

**Architecture** Collaboration owns task transactions, progress history and reminder claims. Gateway and Client map the published task contract. MCP exposes explicit tools, and the local console uses authorized session projections. The rollout leaves the existing inbox representation unchanged.

**Tech stack** Python 3.12+, SQLite, FastAPI, existing Client IPC and async upstream, packaged console HTML/CSS/JavaScript.

- [ ] Review [room tasks v1](../../modules/room-tasks.md) from provider and caller perspectives and record the agreement before editing implementation code.
- [ ] Server workstream. Add public Gateway regression tests for lifecycle, board and addressed authorization, and idempotence. Implement transactions in Collaboration and register the routes in Gateway. Run `uv run --no-sync pytest -q server/tests`.
- [ ] Client/MCP workstream. Add strict task field schemas, member operations and structured agent guidance. Check dispatch against the frozen HTTP contract and run `uv run --no-sync pytest -q tests/test_mcp_contract.py client/tests`.
- [ ] Console workstream. Read authorized projections and add progress/join/finish controls, per-session action selection and finished history. Verify fixed panel geometry and scoped operations in a browser connected to real local processes.
- [ ] Tracer workstream. Add disabled-by-default policy, durable pending candidates and atomic claims. Cover elapsed activity, repeat, retry, invalidation and existing notifier behavior; test with deterministic clocks.
- [ ] Integration. Refresh public golden contracts, run lint, typing, boundary, contract, system and install tests, and record concrete smoke paths in the module docs.
