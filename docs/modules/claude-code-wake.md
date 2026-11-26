# Verifying Wake for Claude Code Desktop Sessions

## Method

Claude Code uses a per-session Unix socket for local cross-session messages. In a supported session, find the peer address in `/status` or read `CLAUDE_CODE_MESSAGING_SOCKET` using the test session's terminal tool. For macOS sessions that run under the same system user, the documentation says external connections can use the socket and the recipient still enforces its inbound permissions. [Claude Code local inbox](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)

1. Open a local Claude Desktop Code test session with no sensitive content. Send an initial marker to verify the target. Choose a mode with interactive permission prompts, such as Manual.
2. Read the local socket path for that test session only. Record the test environment and session state. From the connecting process, verify that the same system user owns the socket.
3. While the target is idle, use the local socket to send a test message with a unique marker and no side effects. The experiment uses newline-delimited JSON with `type` set to `user` and `message` containing `role` and `content`. Record separate evidence for the write to the socket and the reply in the original desktop window. [Local message frame reproduction](https://github.com/anthropics/claude-code/issues/103048)
4. Count wake as successful when the original session starts a turn and echoes the target marker without anyone submitting a new prompt. A socket write or receipt only establishes that the message reached the corresponding interface. Test busy and unloaded sessions separately.

## Observations from 2026-11-24

The environment ran macOS 26.6.2 and Claude Desktop `6.28899.6`. The test session used the bundled Claude Code `2.1.330`, and the separately installed CLI was `2.1.312`. The desktop app was signed in. The standalone CLI's OAuth session had expired, so `--resume` failed before the model ran. The desktop test session's `/status` did not show a peer address. Its terminal tool provided the local socket path.

**Wake worked in a real, idle desktop session.** After a request containing the marker was written to the user-confirmed socket under the same system user, the original session started a turn automatically and echoed the exact marker. The local test log showed the initial reply, the socket request, and the wake reply in that order. All three entries carried the same session ID. A desktop screenshot also showed the input labeled as a cross-session message and the exact reply. Busy, ended, and unloaded Claude Desktop sessions remain unverified. TeamUp v1 sends notifications for @ mentions. Harness wake is still being verified separately.
