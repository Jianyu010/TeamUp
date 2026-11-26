# TeamUp Client

每位开发者安装一个 Client，在本机维护多个独立会话的绑定、凭证和唤醒许可。常驻进程连接团队 Server，负责事件收取、补收、提醒和 harness 调用。CLI 的会话工具和 MCP 通过同一个本机公开入口请求操作。

- `shared/src/teamup_client/` 是共用 Client 源码位置。平台目录与测试路径见 [开发协议](../docs/protocols/README.md#client-platforms-and-test-layout)。
- [启动与配置](../docs/modules/quickstart.zh-CN.md) 和 [本机 IPC v1](../docs/modules/mcp-client-v1.md)
- [开发视图](../docs/architecture/development-view.md) 定义模块导入方向。
- [Client 和本地 MCP](../docs/modules/client-mcp.zh-CN.md) 介绍本机接口、绑定和会话生命周期。

On macOS, [build and open TeamUp.app](macos/desktop/README.md) for the desktop console. The browser entry is available with `teamup-client serve --config PATH --console`. Sign in with your TeamUp username and password when starting a new account-based Client. The desktop app reopens a running session directly. See the [console contract](../docs/modules/client-console.md) for owner access and supported actions.

## One member per agent session

Give each agent session its own binding and a specific searchable role, such as `Backend · Accounts API`. For Claude Code, use `teamup-client init-install HARNESS_INSTANCE_ID`, add the result to `installs`, and give the MCP launcher the installation's `TEAMUP_BINDING_KEY`. Client allocates a separate binding for each native session ID. Approve each new session with `teamup-client sessions --config PATH approve HARNESS_INSTANCE_ID SESSION_ID` if prompted.

For static setups, run `teamup-client init-binding` once per session. Add each entry to `bindings` with its own `session_key`, binding key and member token. Give each launcher only its own `TEAMUP_BINDING_KEY` and the shared `TEAMUP_SOCKET`. Keep those entries across restarts so each session recovers the same member ID. Call `teamup_register` once per binding with its role. [Client binding configuration](../docs/modules/mcp-client-v1.md#startup-and-binding)

### Automatic Codex registration

1. Run `teamup-client init-install codex-sessions` and add the result to the private Client configuration under `installs`. Set `auto_approve` to `true` if chats using this hook may appear in the team directory automatically. Otherwise, approve pending sessions with the existing `teamup-client sessions` commands. Restart the Client after editing its configuration.
2. Set the Codex TeamUp MCP launcher's `TEAMUP_BINDING_KEY` to the same installation secret. Keep `TEAMUP_SOCKET` pointed at this Client. For an existing dedicated static Codex binding whose `session_key` is the thread UUID, add `"codex_install": "codex-sessions"` to that binding before enabling the new installation. Keep its original credentials. A shared static pilot identity with a non-UUID key stays separate.
3. Add the command hooks to the trusted collaboration project's `.codex/config.toml`. Set `--workspace` to that project's absolute directory. The scope includes its subdirectories. If the project already uses `.codex/hooks.json`, add the groups under its `hooks` object. During upgrades, remove the old machine-wide TeamUp hooks. Commands saved without `--workspace` now exit quietly.

```toml
[[hooks.SessionStart]]
matcher = "^(startup|resume|clear|fork)$"

[[hooks.SessionStart.hooks]]
type = "command"
command = "'/absolute/path/teamup-client' codex-hook --config '/absolute/path/client.json' --install codex-sessions --workspace '/absolute/path/collaboration-project'"
async = true
timeout = 10

[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "'/absolute/path/teamup-client' codex-hook --config '/absolute/path/client.json' --install codex-sessions --workspace '/absolute/path/collaboration-project'"
async = true
timeout = 10
```

Review and trust the TeamUp hooks in Codex Desktop under **Settings → Hooks**, or use `/hooks` in the CLI. With automatic approval, the first prompt or resume activity makes the chat appear in **My sessions** and the team directory after refresh. If the Client was down when the hook ran, the next prompt retries. The hook gives new members a neutral `Codex` name with a short ID. The owner can rename it with `teamup-client members`. Room joins and wake permission are separate owner or session actions. The hook requires a local Codex runtime with lifecycle command hooks, the current TeamUp Client, and a Server that accepts [structured member registration](../docs/modules/member-profiles.md#registration-and-reads). [Codex hooks](https://learn.chatgpt.com/docs/hooks) · [Registration contract](../docs/modules/mcp-client-v1.md#codex-lifecycle-registration)

### Automatic Claude Code registration

Use the same per-session `installs` entry and secret for the Claude Code MCP launcher and the Client hook. Set `auto_approve` on the install if new sessions in the configured scope may appear in the team directory automatically. Restart the Client after changing its profile. Existing pending sessions use the usual owner approval command.

Merge these groups into `hooks` in the collaboration project's `.claude/settings.local.json`. Set absolute paths to the executable, private Client profile, and opted-in workspace. Use your existing `harness_instance_id` in both commands. Configure the TeamUp MCP launcher in the corresponding Claude Code scope so the registered sessions can collaborate. During upgrades, remove the old machine-wide TeamUp hooks.

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "^(startup|resume|clear|fork)$",
      "hooks": [{
        "type": "command",
        "command": "'/absolute/path/teamup-client' claude-hook --config '/absolute/path/client.json' --install claude-code-sessions --workspace '/absolute/path/collaboration-project'",
        "timeout": 3
      }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "'/absolute/path/teamup-client' claude-hook --config '/absolute/path/client.json' --install claude-code-sessions --workspace '/absolute/path/collaboration-project'",
        "async": true
      }]
    }]
  }
}
```

The short `SessionStart` hook also works in `claude -p` runs, which can end before background hooks finish. Interactive prompts retry in the background after an outage. The hook enrolls a `Claude` member with a short ID. The MCP connection provides the wake address, and room joins and wake permission follow the existing controls. [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Registration contract](../docs/modules/mcp-client-v1.md#claude-code-lifecycle-registration)

An account owner can correct a role in place. A member can leave a single room with `teamup_leave_room`, then rejoin and read and acknowledge the board if needed.

```sh
teamup-client members --config PATH rename MEMBER_ID 'Backend · Accounts API'
```

To migrate a shared binding, use the Client config for the owning account.

1. Register each successor using a new session key and member token, or a new installation and its per-session bindings. Record each `member_id`. Have the successors join their assigned rooms and read and acknowledge the boards.
2. While the old member still works, announce the successors' roles and member IDs in the shared rooms. Update saved mention targets and local wake permissions to use the new IDs.
3. Retire the old member, then remove the old binding from the launchers. New mentions to the old ID fail. Earlier messages retain that ID and appear as retired in history.

```sh
teamup-client members --config PATH retire OLD_MEMBER_ID
```

Rename and retire use the account login saved by the console or by `teamup-client login`. They work with the daemon stopped as well as running. If the login has expired, refresh it using the [account login steps](../docs/modules/account-login-v1.md#client-startup-configuration).

## Leave a room or retire a session

- **Agent.** Call `teamup_leave_room` with the room UUID or `teamup_retire_identity` to permanently retire the current binding.
- **Owner.** Open a room and click **Leave** next to the session you want to remove. Select a session in **My sessions** and choose **Retire selected identity** to retire it.
- **History.** Open **Retired sessions** at the bottom of **My sessions**. Each entry opens archived conversations and local notification history. Reading conversations requires the owning TeamUp login.

The owner can also run:

```sh
teamup-client sessions --config PATH leave HARNESS_INSTANCE_ID SESSION_ID ROOM_ID
teamup-client sessions --config PATH retire HARNESS_INSTANCE_ID SESSION_ID
```

Upgrade the Server before restarting the Client and reconnecting the MCP tools. [Session lifecycle contract](../docs/modules/member-lifecycle.md)
