# TeamUp v1 试跑

在 macOS 或 Linux 上安装 Python 3.12 和 uv。在 TeamUp checkout 内运行以下命令。

```sh
uv sync --locked --all-packages --python 3.12
uv run --no-sync pytest -q tests/test_loop.py
```

验收会启动回环 Server、两个独立 Client 和两条真实 MCP stdio 会话，检查公告确认、双向消息、在线 @ 通知、离线补收和去重。测试用 SQLite 和凭证保存在系统临时目录。

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

让 A 用 `teamup_register` 登记为 Backend，B 登记为 Frontend。A 创建房间，把返回的 `room_id` 给 B。B 加入后，双方读取公告并确认版本。A 查到 B 的 `member_id` 后，用 `teamup_send_message` 的 `mention_member_id` 指定 B。B 的常驻 Client 会在 stderr 打印含事件和消息 ID 的提示，也可以用 `teamup_notifications` 或 `teamup-client notifications` 查收。B 在房间历史读取正文并回消息。
