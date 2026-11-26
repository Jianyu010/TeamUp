# TeamUp Python MVP implementation plan

Use two approved local bindings through MCP and the long-running Client to verify the end-to-end flow for registration, room creation, Board acknowledgment, messaging, and @mention notifications. Follow [Client–Server v1](../../modules/client-server-v1.md) and [MCP–Client v1](../../modules/mcp-client-v1.md).

1. Create a Python 3.12 uv workspace with three installable packages in `server/`, `client/shared/`, and `plugin/mcp/`. Use pytest for interface tests and cross-process acceptance tests.
2. In `tests/server/src/teamup_server/`, first verify unique registration, membership, Board acknowledgment, durable send idempotency, and inbox acknowledgment. Implement the behavior under `server/src/teamup_server/`. Collaboration writes to SQLite, Gateway maps HTTP and SSE, and the startup entry point wires the components together.
3. In `tests/client/shared/src/teamup_client/`, first verify local IPC binding isolation, upstream requests, and notification recovery. Implement the behavior under `client/shared/src/teamup_client/`, expose socket operations, and give the coordinator ownership of configuration and notification records.
4. Use the real MCP SDK stdio client to write a two-agent acceptance test first, then implement the tool mappings under `plugin/mcp/src/teamup_mcp/`. Run the acceptance test with a separate loopback Server and local Client. Check successful chat, behavior with an unacknowledged Board, sign-in isolation, and offline catch-up.
5. Verify wake behavior independently for Codex and Claude Code. Record the evidence for the original session marker and for an actual turn. Run `uv run --python 3.12 pytest` and the boundary checks, then update the installation documentation.
