# TeamUp on Windows over MCP

Open TeamUp's existing Client Console in a standalone Windows window using
Microsoft Edge app mode. This launcher connects to an existing authorized
TeamUp stdio MCP endpoint, such as a Client reached over SSH. It requires
Windows, Edge, PowerShell and Node.js 22 or newer. Codex delivery uses a
Windows Codex executable whose `queue` command supports `--resume`.

## Set up

1. Confirm your stdio TeamUp MCP connection already works. If configured in
   Codex, `codex mcp get teamup --json` shows the command and argument list.
   Obtain the existing TeamUp member ID from your approved binding.
2. Create `%LOCALAPPDATA%\TeamUpWindows\profile.json` in your private Windows
   profile. Use the exact command and argument array for your connection. The
   launcher does not set up SSH authentication or register a member. A profile
   might look like this (substitute your own values):

   ```json
   {
     "command": "C:/Windows/System32/OpenSSH/ssh.exe",
     "args": ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "your-ssh-alias", "teamup-mcp"],
     "memberId": "00000000-0000-0000-0000-000000000000",
     "port": 18766
   }
   ```

3. Run `client/desktop/windows/Open-TeamUp.ps1` from the checkout. An alternate
   profile or Node location can be supplied with `-ProfileDirectory` or
   `-NodeExecutable`. The console and its short-lived access token stay on
   the loopback interface.

The workspace shows joined rooms, the team directory, history, boards and tasks.
When the MCP endpoint does not expose a room roster, the room sidebar explicitly
shows the team directory and suppresses membership actions. Presence and team
public room discovery require the full Client console connection.

## Send mentions to Codex

Open **我的 @ / Codex** and enter the target Codex session UUID. Choose a room
and a sender to enable automatic delivery for new mentions, or send a selected
mention manually. Automatic delivery starts at the latest known event, excludes
DMs and remains off until explicitly enabled. The background helper must be
running. An optional `codexExe` profile field selects an absolute executable
path if the Windows desktop CLI cannot be discovered.

Codex may queue a message for a busy or unavailable thread without waking it.
The inbox distinguishes queue acceptance, resume requests and uncertain
submissions. It does not retry a submission whose result is unknown. Incoming
text is passed as quoted message data and cannot select the destination or
change the CLI arguments.

Run `node --test client/desktop/windows/console-api.test.mjs` for the adapter
checks. For a smoke test, open a joined room and its board and tasks, view the
team directory, then send a harmless test mention to an explicitly chosen
Codex chat.
