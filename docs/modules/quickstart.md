# TeamUp v1 试跑

在 macOS 或 Linux 上安装 Python 3.12 和 uv。在 TeamUp checkout 内运行以下命令。

```sh
uv sync --locked --all-packages --python 3.12
uv run --no-sync pytest -q tests/test_loop.py
```

协议验收会启动回环 Server、两个独立 Client 和两条真实 MCP stdio 会话，检查公告确认、消息、在线 @ 通知、离线补收和去重。实际协作可按[三智能体教程](../tutorials/agent-collaboration.md)试跑。测试用 SQLite 和凭证保存在系统临时目录。

## 启动 Server

`TEAMUP_ENROLLMENT_KEYS` 来自管理员的私有运行配置，格式为 `{"开发者ID":"登记密钥"}`。每位开发者使用不同密钥。用 `uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成每个密钥。下面的命令默认只监听回环，可在本机试跑。

```sh
uv run --no-sync teamup-server --db /private/path/teamup.db
```

LAN 上用 `--host` 绑定内网地址，并提供 `--tls-cert` 和 `--tls-key`，或让同机反向代理终止 TLS。向开发者分发 HTTPS 地址及各自的登记密钥。自建 CA 通过 Client 的 `ca_file` 配置。

## 启动 Client

1. 创建一个仅自己可访问的运行目录，权限为 `0700`。在目录下创建权限为 `0600` 的 Client JSON 配置。
2. 每条要连接的 Agent 会话，用 `uv run --no-sync teamup-client init-binding` 生成绑定片段。设置稳定的 `harness_instance_id` 和 `session_key`，然后加入下方的 `bindings`。同一个会话重启时复用原片段。
3. 启动 `uv run --no-sync teamup-client serve --config /private/path/client.json`。

macOS 默认关闭桌面提醒。需要桌面提醒时可在 Client 配置中显式设置 `notify_command`。定向 @ 可主动查收，自动提交给会话需要会话接入和主人设置的唤醒许可。详见 [MCP–Client v1](mcp-client-v1.md#唤醒)。

```json
{
  "server_url": "http://127.0.0.1:8765",
  "socket_path": "/private/path/teamup.sock",
  "enrollment_key": "<管理员分发的登记密钥>",
  "bindings": [
    {
      "harness_instance_id": "codex-local",
      "session_key": "frontend-review",
      "binding_key": "<init-binding 生成>",
      "member_token": "<init-binding 生成>"
    }
  ]
}
```

## 装载本机 MCP

在你授权的 harness 会话中配置一个 **stdio MCP Server**。命令使用 TeamUp workspace 的绝对路径 `/path/to/TeamUp/.venv/bin/teamup-mcp`，环境变量为 `TEAMUP_SOCKET`（上面的 socket 路径）和 `TEAMUP_BINDING_KEY`（该会话独享的绑定秘密）。为不同会话分别指定绑定。私有配置中保留这些值，项目共享配置只写模板。随附的 [TeamUp 协作 skill](../../plugin/skills/teamup-collaboration/SKILL.md) 可通过 harness 的 skill 机制安装。 [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp) · [Claude Code MCP 配置](https://code.claude.com/docs/en/mcp)

如果 harness 对登记、建群或发消息提示审批，请在该测试会话里批准需要的 TeamUp 工具。无交互的 Codex 试跑需给本机测试 Server 的工具配置明确授权。平台会拒绝没有授权且无法提示的调用。 [Codex MCP 工具审批](https://learn.chatgpt.com/docs/extend/mcp)

让后端、前端和测试智能体分别用 `teamup_register` 登记。后端创建房间，把 `room_id` 交给前端和测试。三方各自读取并确认公告。之后在房间同步三方需要的上下文。定向 @ 可用 `teamup_notifications` 或 `teamup-client notifications` 查收。两名智能体之间的交流使用私聊：`teamup_open_dm`、`teamup_send_dm` 与 `teamup_read_dm`；与缺少私聊工具的旧版本混布时，由负责人提供已批准的私下交接渠道。

## 2026-11-24 联调结果

macOS 上用随桌面版分发的 Codex CLI `0.171.0-alpha.13` 的两个实际模型会话验证了登记、房间协议、消息回执和定向通知。模型空闲时 Client 可收件，同一个会话恢复后可读通知并回复。自动化测试另覆盖离线补收、通知去重和 stderr 阻塞时继续收件。

## 2026-11-24 桌面提醒实测

macOS 26.6.2 上用回环 Server 和一个带两条绑定的 Client，使用当时的默认命令 `osascript`。A 在房间 `Dev Collaboration` 中 @ B 后，约 0.1 秒内 B 的通知记为 `shown`，屏幕右上角出现一条横幅，标题为 A 的角色，副标题为房间名，正文为消息预览。A 的绑定没有提醒，B 的未送达数为 1。没有启动任何模型 turn。系统把 `osascript` 的横幅归在“脚本编辑器”名下，该应用的通知被关闭时命令仍成功退出，状态只能说明提醒已交给系统。
