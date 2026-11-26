# Verifying Wake for Codex Desktop Sessions

## Observations from 2026-11-24

The test environment ran macOS 26.6.2, ChatGPT Desktop `26.1117.41037`, and the bundled Codex CLI `0.171.0-alpha.13`. The separately installed Homebrew CLI `0.163.0` did not list `--resume` in its help. The tests used a dedicated desktop chat with no sensitive content and recorded the markers sent, target session ID, turn IDs, and completion status.

| Existing desktop session state | Result | Evidence |
| --- | --- | --- |
| Idle and loaded | Passed | From outside the desktop app, the test invoked the bundled CLI with `codex queue --disable daemon_auto_start --resume --thread … --message …`. The CLI reported that the message was queued and warned that it could not resume the session through the daemon. About 9 seconds later the original desktop chat automatically started a new turn, completed it, and echoed the test marker. |
| Busy and loaded | Passed | While the original session ran a 20-second test instruction with no side effects, the same CLI queued a second marker. The first turn completed before the same desktop session started a separate turn and echoed the second marker. The desktop app was in the background. |
| Unloaded or closed | Unverified | The target still reported `idle` after the desktop app had been in the background for over 60 seconds. The test could not connect to the default daemon control socket. The existing desktop app remained running. |

An isolated App Server used its private Unix socket to exercise an idle session, a busy session, and a session confirmed as `notLoaded`. The unloaded session stayed `notLoaded` when the test queued a message normally. With `--resume`, the same test thread processed the queued marker. These results apply to a self-managed App Server backend.

The TeamUp Codex Driver now uses the same CLI command to queue messages for a loaded desktop chat after the owner grants wake permission. These tests called the harness directly. [MCP–Client v1 contract](mcp-client-v1.md#wake-delivery) · [Harness delivery implementation notes](harness-delivery.md#2026-11-24-codex-driver) · [Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference#codex-queue) · [Codex App Server](https://learn.chatgpt.com/docs/app-server) · [Cross-process queue implementation](https://github.com/openai/codex/pull/39034)

## How to reproduce

1. Open two desktop sessions in a test project authorized by the owner. Record the desktop and CLI versions, the authorized daemon or backend, and the target session IDs. Use distinct test markers for the two sessions.
2. Use the CLI queue or a supported local App Server endpoint to submit a test message with no side effects. For an idle session, record the actual start and completion of the turn. For a busy session, record the order in which the prior turn ends and the new turn begins, and confirm that the other session stays unchanged.
3. For an unloaded session, establish its state first. Record the queue command's exit code, any automatic resume warning, and subsequent turn events. Try to resume the session when the version and backend support it and the owner has authorized the test.
4. Mark each version and scenario separately as verified or unverified. Count wake as supported only when **the original desktop session automatically starts a turn**.
