# Harness Delivery Design

**Status: Partially implemented.** The session registry, session attachment and approval, wake permissions, Claude Code Driver, and Codex Driver are implemented under [MCP–Client v1](mcp-client-v1.md#session-attachment) and have automated acceptance coverage. Rate limiting and turn marker verification are still pending. This work is the second step of [issue #11](https://github.com/Jianyu010/TeamUp/issues/11). See [MCP–Client v1](mcp-client-v1.md#desktop-notifications-and-delivery-state) for the implemented desktop alerts and delivery states.

## Goal

When A mentions B and B's owner permits wake, B's specific session receives the request within seconds and handles it like a cross-session message. Each notification is delivered at most once. If wake permission is missing, the session is offline, or the delivery outcome is unknown, the agent can check `teamup_notifications`. The Client configuration controls desktop alerts, which default to off on macOS.

```
A's session ─send_message─▶ Server ─SSE signal + inbox─▶ B's Client
                                                           │ 1. member_id → session registry
                                                           │ 2. Local permission and eligibility checks
                                                           ▼
                                          Driver(kind) ─▶ B's session (Claude Code socket / Codex thread)
```

## Session registry

The Client maps **members to online sessions**. Each session corresponds to one TeamUp member. Room memberships and roles belong to members, so sessions can join different rooms or take different roles in the same room. The Server resolves an @ mention to a `member_id`. The Client uses that ID to find the session without consulting room state.

The registry uses the same record format for every harness. The `kind` and its Driver handle harness-specific behavior.

| Field | Meaning |
| --- | --- |
| `harness`, `session_id` | The harness type and its session identifier, such as Claude Code's `CLAUDE_CODE_SESSION_ID` or Codex's `threadId`. Together they form the `(harness_instance_id, session_key)` registered with the Server. |
| `member_id` | The member for this session. Resuming the same session restores the same member and its room memberships. |
| `endpoint` | A `kind` and an address, such as `claude-code` and a socket path, or `codex` and a thread ID. Claude Code also has a local message token held only in memory. |
| `lease` | Whether the address is still usable. Claude Code uses the MCP process's connection. Codex uses the latest tool call. |
| `wake` | Local session permission. Explicit restrictions or revocation take precedence over the default for a signed-in console. |

The registry serves local delivery only. TeamUp authorization uses its own state. The Client is the registry's sole writer and stores it in the user's private directory.

## Address discovery (observed 2026-11-24)

Both tests ran on macOS 26.6.2 with a logging stdio MCP Server. The logs included only variable names and non-secret values.

- **Claude Code `2.1.330`** (desktop-bundled binary, `-p` mode): The child MCP process received the `claude` process's own `CLAUDE_CODE_MESSAGING_SOCKET` (an existing file at `/tmp/cc-socks/<claude pid>.sock`), a new `CLAUDE_CODE_SESSION_ID`, and `CLAUDE_CODE_MESSAGING_TOKEN`. An authentication failure did not prevent MCP startup. This lets `teamup-mcp` learn its session address at startup.
- **Codex `0.171.0-alpha.13`** (`codex exec`): The MCP process started with no `CODEX_*` variables. Each `tools/call` included `threadId`, `sessionId`, and `x-codex-turn-metadata.thread_id` in `_meta`. The ChatGPT Desktop app-server also launched `teamup-mcp` in a clean environment. One app-server ran five `teamup-mcp` processes concurrently, with no established mapping from processes to threads. The Client learns a Codex session's address on the first TeamUp tool call and tracks it by the `threadId` in each call.

## Registration and leases

A static `TEAMUP_BINDING_KEY` corresponds to a single member. A Claude Code installation with multiple sessions shares one installation secret. The Client assigns each session a binding when it receives the session ID.

1. **Installation credentials.** `TEAMUP_BINDING_KEY` becomes a per-installation access secret. It proves that the caller is a harness installation approved by the owner and carries no session identity. Dedicated sessions can still use static session bindings. The Claude Code session ID must match the static binding's `session_key`.
2. **Attachment.** For Claude Code, `teamup-mcp` sends the session ID to the Client with every tool request. When the environment provides both the messaging socket and token, MCP sends `attach` and holds that IPC connection open. For Codex, `teamup-mcp` sends `_meta.threadId` to the Client with each tool call.
3. **New session approval.** On first contact with an unknown session, the Client records it as pending and sends a desktop alert if configured. After the owner approves it with `teamup-client sessions approve`, the Client uses account credentials to register the session's member on the next registration request. Future calls from the same `(harness, session_id)` restore the original member. The owner can explicitly configure automatic approval for all new sessions from a given harness.
4. **Leases.** A Claude Code entry expires when its `attach` connection closes, so its address disappears when the session exits. A Codex thread can resume, so its entry persists until the owner removes it. The Driver checks whether delivery is possible when it submits the message.
5. **Validation.** The Client accepts only sockets owned by the current system user. The message token stays in memory and never goes to the Server.

## Local permissions

Each session entry can set `wake` allowlists for `rooms` and `senders`. Both must match, and a per-minute limit also applies. A signed-in account console defaults to allowing approved sessions to handle directed @ mentions, and the owner can turn that default off. Explicit room and sender restrictions and revocation take precedence. Headless and legacy registration-key operation require explicit permission. Room Boards, message bodies, and Server fields cannot grant additional access. Owners can edit session permissions through the CLI.

## Delivery flow

1. **Notice registration.** Use the existing flow to persist the Notice, acknowledge the Server event, and send a desktop alert if configured. Set the new `harness_state` to `queued` if wake permission matches and the session is online, `waiting` if it matches and the session is offline, or `not_allowed` if permission does not match.
2. **Eligibility check.** Before delivery, read the current Board and the message as the recipient member. Confirm that the recipient still belongs to the room and has acknowledged the Board. On failure, record `failed` and keep the notification available for manual retrieval.
3. **Ordered submission.** Maintain one queue per session and submit in `event_id` order. Persist `submitting` before submission. If the rate limit is exceeded, combine notifications into one request saying there are N unread mentions. When a session attaches, queue its `waiting` notifications in order.
4. **Outcome.** Map the Driver's `accepted`, `rejected`, or `unknown` response to `delivered`, `failed`, or `unknown`, respectively. On restart, mark any remaining `submitting` entries as `unknown`. Update an `unknown` entry only if the Driver can check the marker against the harness's records. Do not retry without that evidence.
5. **Reply.** The Agent replies in the original `room_id` and sets `client_message_id` to `wake-<event_id>`. If delivery is repeated, the Server returns the same reply message.

The request to the session follows a fixed format. It includes the sender's role and `member_id`, the room name and `room_id`, `event_id`, the `msg-N` message reference, the message body, and the current Board. It identifies the payload as TeamUp data to be handled under the room Board and local permissions. Remote fields cannot specify commands, paths, permission modes, or tools.

## Drivers

| `kind` | Submission | Verified | Pending verification |
| --- | --- | --- | --- |
| `claude-code` | Writes one JSON line with `type: user` to the session socket. [Method](claude-code-wake.md) | An idle desktop session starts a turn. | Whether the socket requires `CLAUDE_CODE_MESSAGING_TOKEN`, busy sessions, and differences between `-p` and desktop sessions. |
| `codex` | Runs `codex_command` with `queue --disable daemon_auto_start --resume --thread <id> --message <body>` passed directly as argv, without a shell, with a 15-second timeout. [Method](codex-wake.md) | TeamUp wakes an idle, loaded desktop thread. Desktop tool calls carry `_meta.threadId`. The CLI can queue for a busy chat when called directly and exits with code 1 for an unknown thread. | TeamUp wake for a busy or unloaded thread. |

A successful socket write or queue submission establishes that the message reached the harness and is recorded as `unknown`. Record `delivered` once the harness log shows a turn marked with the `event_id`. A closed session stays `waiting` until it attaches again. The Client does not start a new process for it.

## Contract changes

Before implementation, update [MCP–Client v1](mcp-client-v1.md) and get agreement from the Client and plugin owners.

- IPC adds the long-lived `attach` connection and session identity fields. `TEAMUP_BINDING_KEY` also serves as an installation credential, and static session bindings remain compatible.
- The Client CLI adds commands to list and approve sessions and to configure permissions.
- The notification store adds `harness_state`.

Client–Server v1 stays the same. `(owner_id, harness_instance_id, session_key)` still uniquely identifies a member.

## Acceptance criteria

For acceptance criteria 2 and 5 in issue #11, have two sessions on one machine join the same room in different roles. Mention one session and verify that only that session starts or queues a turn and replies in the room. Repeat delivery and verify that only one reply exists. Confirm that a recipient without wake permission can still retrieve the notification manually. Record versions and scenarios separately for Claude Code and Codex.

## End-to-end observations from 2026-11-24

The test ran on macOS 26.6.2 and Claude Desktop `6.28899.6`. The local desktop Code session used bundled Claude Code `2.1.330`, Sonnet 5.5, and auto permissions. The working directory's `.mcp.json` launched `teamup-mcp` from this branch with an `installs` installation secret. The Server ran on loopback, Alice used a script and static binding, and Bob's Client had only the `claude-code@mac` installation.

1. The test session's first `teamup_register` call returned `session_pending`. In `teamup-client sessions`, `session_key` matched the Claude Code session ID and `attached` was `claude-code`. This showed that the desktop-launched MCP process obtained the socket and token and kept its attachment open. After the owner approved it, the session registered, joined the room, and acknowledged the Board.
2. Alice mentioned the member before the owner granted wake permission. The desktop alert was `shown`, `harness_state` was `not_allowed`, and the session started no new turn.
3. After the owner granted permission for the room and Alice, Alice mentioned the member again. The state changed to `submitted` within about 1 second. The original idle session automatically started a turn about 3 seconds later. The input was labeled as coming from another Claude session and followed TeamUp's fixed format. The Agent replied in the original room with `client_message_id` set to `wake-2`.

Busy and closed sessions, as well as duplicate delivery, still have only automated test coverage.

## 2026-11-24 Codex Driver

The implemented Driver defaults `codex_command` to the CLI bundled with ChatGPT Desktop. Exit code 0 maps to `submitted`. A nonzero exit or a failure to start maps to `failed`. On timeout, the Driver stops the process and records `unknown`. Thread IDs must be UUIDs because `--thread` also accepts session names. Codex has no persistent connection. The Client holds the thread address until another call from that session replaces it or the Client restarts. [MCP–Client v1](mcp-client-v1.md#wake-delivery)

- **Automated tests.** `tests/integration/test_codex_wake.py` uses a real Server, an `installs` installation, and one `teamup-mcp` process serving two threads. Each call includes `_meta.threadId`, and a fake CLI records argv. The test checks that both threads start as pending and become different members after approval. With no wake permission, it verifies a desktop alert only. Once permission is granted, it verifies one CLI invocation per mention in order, with the exact argv and only the mentioned thread targeted. The other thread stays untouched without permission. CLI exit code 1 records `failed` with no retry. Two replies using the same `wake-<event_id>` produce one message. `tests/client/shared/src/teamup_client/test_client.py` covers process termination on timeout, rejecting non-UUID thread IDs without running the CLI, a missing command, and configuration validation.
- **CLI exit codes.** With `codex-cli 0.171.0-alpha.13` bundled with ChatGPT Desktop, `queue --help` says a `--resume` failure emits a warning while the command still succeeds. Queuing for a nonexistent thread exited with code 1 within 0.3 seconds and reported `no rollout found for thread id`, consistent with the `failed` mapping.
- **End-to-end observations.** The test ran on macOS 26.6.2 with ChatGPT Desktop `26.1120.41243` and bundled `codex-cli 0.171.0-alpha.13.2`. The Server ran on loopback, Alice used a script and static binding, and Bob's Client had only the `codex@mac` installation, with the default `codex_command`. The test temporarily added an MCP Server in `~/.codex/config.toml` to launch `teamup-mcp` from this branch using an installation secret. It opened a dedicated desktop chat after restarting ChatGPT.
  1. The chat's first `teamup_register` call returned `session_pending`. In `teamup-client sessions`, `session_key` matched the thread ID in the filename of the rollout under `~/.codex/sessions` that contained the initial message marker. `attached` was `codex`. This showed that the desktop tool call's `_meta.threadId` matched the chat's thread ID. After the owner's approval, the chat registered, joined the room, and acknowledged the Board.
  2. After the owner granted permission for the room and Alice, Alice mentioned the member. The chat was idle and the Client recorded `submitted`. The rollout showed the same thread starting a new turn automatically about 1.5 seconds later, with the input in TeamUp's fixed format. The Agent replied in the original room with `client_message_id` set to `wake-1`. The turn completed about 49 seconds later.
  3. The test then removed the temporary MCP configuration and stopped the test processes.

Busy and unloaded threads and duplicate delivery still have coverage only from automated tests or direct CLI observations.

## Codex lifecycle registration

Lifecycle enrollment requires an explicit collaboration workspace. Machine-wide hook commands from earlier installs exit quietly until configured with a workspace. See [workspace opt-in](mcp-client-v1.md#workspace-opt-in-for-lifecycle-registration).

The team can configure local Codex command hooks to pass the thread UUID to the Client before a TeamUp tool call. `SessionStart` and `UserPromptSubmit` use the same installation as MCP and share its approval policy. The latter retries registration after an unavailable Client. See [Client setup and migration](../../client/README.md#automatic-codex-registration) and the [registration contract](mcp-client-v1.md#codex-lifecycle-registration).

The CLI bundled with ChatGPT Desktop `26.1120.41243` (`0.171.0-alpha.13.2`) emitted both events in an isolated Codex home. Both reported the same `session_id` UUID. `tests/integration/test_codex_registration.py` exercises lifecycle registration against real Client, Server, and MCP processes, including concurrent events, recovery after Client downtime and restart, owner approval and rejection, and reuse of a dedicated static identity and its room membership.

## Claude Code lifecycle registration

The optional Claude Code hook uses the same session ID and per-installation secret as MCP to enroll before the agent calls a TeamUp tool. `SessionStart` handles new and resumed sessions. `UserPromptSubmit` retries on subsequent activity. The hook performs registration and the MCP connection owns the live wake address. See [Client setup](../../client/README.md#automatic-claude-code-registration). `tests/integration/test_claude_registration.py` verifies both approval modes, rejection, identity and profile reuse, and retry after the Client restarts.
