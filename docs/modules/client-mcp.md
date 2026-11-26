# The Client and local MCP

[简体中文](client-mcp.zh-CN.md)

## Installation and process lifecycle

[MCP–Client v1](mcp-client-v1.md) documents the implemented tools, bindings, and local IPC. The Client runs in the background and manages multiple registered Agent sessions. In v1, it continues to receive events over SSE and display notifications even when no Agent turn or MCP process is running. A future version could add health heartbeats and wake sessions according to local policy.

Installing the TeamUp plugin gives the harness a launch configuration for local MCP over stdio and a set of collaboration skills. When an Agent calls a tool, the MCP process queries the Client or passes it a request. The harness loads content from `plugin/skills/` through its own skill-loading mechanism. The installer bundles the Client, the MCP entry point, and the corresponding plugin files. The implementation for each harness supplies its plugin manifest and launch command. [Connecting Codex to MCP](https://learn.chatgpt.com/docs/extend/mcp) · [Codex plugins](https://learn.chatgpt.com/docs/plugins)

## Session bindings and tool boundaries

1. The developer uses the Client to approve enrollment for a stable session or bot key. The Client stores the local binding and TeamUp credentials.
2. The MCP process starts with an authorization handle for an installation or a dedicated session. With an installation handle, the Client identifies the caller from the handle and the session ID supplied by the harness. Member and room IDs in tool inputs identify the collaboration targets.
3. The first release uses a per-user Unix socket and a restricted directory for Client IPC on macOS and Linux. The connection must belong to the local user and use an approved session binding. Shared or trusted configuration files contain only launch settings and binding references. The Client holds the actual credentials.
4. MCP tools support finding members, creating and joining rooms, reading and acknowledging boards, sending messages and collaboration requests, and querying the inbox and health status. Regular Agents can report issues. Authorized administrators carry out administrative actions through public interfaces. Each tool's schema is finalized before implementation.

If a harness cannot supply distinct launch bindings for two sessions, the owner must explicitly register and select the session. The MCP handshake's `clientInfo` is client metadata. It cannot serve as a session credential.

## Automatic wake

The Client daemon consumes SSE events and decides when to wake a session. It checks event eligibility with the Server and reads the local wake permission before calling the Driver. The Driver distinguishes an accepted submission, a definite failure, and an unknown result. An unknown result awaits verification to avoid starting the same request twice. TeamUp can show adapter acknowledgments and replies that reference the original request. The local Driver observes the actual harness turn state.

[Standalone validation of Codex desktop wake](codex-wake.md)
