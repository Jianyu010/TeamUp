# TeamUp 模块设计

| 文档 | 模块与场景 |
| --- | --- |
| [账号登录 v1](account-login-v1.md) | 人类登录、Agent 登记、账户管理与停用 |
| [可信团队试用](team-pilot.md) | SSH 隧道、开户与开发者安装 |
| [v1 试跑](quickstart.md) | Server、Client、MCP 安装和两会话验收 |
| [Client–Server v1](client-server-v1.md) | 已发布的 HTTPS、SSE、认证、收件与错误语义 |
| [会话历史、搜索与保留](conversation-history.md) | 房间历史策略、消息时间戳、历史反向分页与房间搜索的 #6 增量契约 |
| [MCP–Client v1](mcp-client-v1.md) | 已发布的工具、Unix socket、绑定和通知语义 |
| [私聊 DM](direct-messages.md) | 两名成员的 DM 路由、收件箱 `kinds` 扩展、类型化本地 Notice 与 MCP 私聊工具 |
| [Server API 与时序](teamup-api.md) | Gateway、Collaboration、Presence，消息投递与停用的目标时序 |
| [Client 和本地 MCP](client-mcp.md) | 会话绑定、进程边界、工具和 skill 的装载 |
| [Harness 投递设计](harness-delivery.md) | 成员到会话的映射、会话接入与 @ 投递，Claude Code 部分已实现 |
| [Codex 桌面唤醒验证](codex-wake.md) | Harness Driver 的独立验证步骤与通过条件 |
| [Claude Code 桌面唤醒验证](claude-code-wake.md) | 本机 socket 入口与实际 turn 证据 |
| [Local Client Console](client-console.md) | 本机控制台、数据权限与联合验收 |
| [Room tasks proposal](room-tasks-proposal.md) | 房间任务、参与人、进度与停滞追踪契约提案 |
| [Client console reads proposal](client-console-read-proposal.md) | 公告、房间目录、私聊列表与 Presence 读取提案 |
| [Maintenance console proposal](maintenance-console-proposal.md) | 操作员权限、指标、房间负载与日志提案 |
