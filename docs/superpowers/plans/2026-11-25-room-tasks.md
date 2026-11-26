# Room tasks and stalled-task tracing implementation plan

**Goal** Post, take, update and finish tasks through approved sessions, then trace stalled work with opt-in desktop reminders.

**Architecture** Collaboration owns task transactions, progress history and reminder claims. Gateway and Client map the published task contract. MCP exposes explicit tools, and the local console uses authorized session projections. The rollout leaves the existing inbox representation unchanged.

**Tech stack** Python 3.12+, SQLite, FastAPI, existing Client IPC and async upstream, packaged console HTML/CSS/JavaScript.

- [x] Review [room tasks v1](../../modules/room-tasks.md) from provider and caller perspectives and record the agreement before editing implementation code.
- [x] Server workstream. Add public Gateway regression tests for lifecycle, board and addressed authorization, and idempotence. Implement transactions in Collaboration and register the routes in Gateway. Run `uv run --no-sync pytest -q tests/server`.
- [x] Client/MCP workstream. Add strict task field schemas, member operations and structured agent guidance. Check dispatch against the frozen HTTP contract and run `uv run --no-sync pytest -q tests/plugin/mcp/src/teamup_mcp/test_mcp_contract.py tests/client/shared`.
- [x] Console workstream. Read authorized projections and add progress/join/finish controls, per-session action selection and finished history. Verify fixed panel geometry and scoped operations in a browser connected to real local processes.
- [x] Tracer workstream. Add disabled-by-default policy, durable pending candidates and atomic claims. Cover elapsed activity, repeat, retry, invalidation and existing notifier behavior; test with deterministic clocks.
- [x] Integration. Refresh public golden contracts, run lint, typing, boundary, contract, system and install tests, and record concrete smoke paths in the module docs.

The lifecycle passed the local Server, Client, MCP system test and a real browser run on 2026-11-25. The browser covered taking, progress, finishing, history, and activity-wire color.

Provider review was recorded by GLM-5.3 (hp-laptop commit `637f925`). Client and MCP review was recorded by DeepSeek-V4.1-Flash-1017 (hp-laptop commit `8942b56`). The integration incorporated the route contract and snapshot changes, combined the provider and caller in one stack, and verified the additive task migration on the merged DM schema.

The enabled tracer passed the real Server and Client smoke run with a 2-second threshold and 60-second repeat interval. The browser showed a stalled task and completed it after progress. The configured test notifier recorded one desktop reminder. Deterministic tests covered repeat intervals, concurrent claims, progress and finish invalidation, and restart persistence. A slow reminder also leaves the room inbox free to process new hints.

Final validation on the merged DM base covered 112 Client, Server, system, install, contract, boundary, MCP and loop tests. Ruff, mypy, and the live browser and notifier smoke run passed.
