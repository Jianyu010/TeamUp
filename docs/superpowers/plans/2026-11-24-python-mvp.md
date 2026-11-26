# TeamUp Python 最小闭环实施计划

目标是用两条已获准的本机绑定，经 MCP 和常驻 Client 完成登记、建群、公告确认、收发和 @ 通知。依据 [Client–Server v1](../../modules/client-server-v1.md) 和 [MCP–Client v1](../../modules/mcp-client-v1.md)。

1. 建立 Python 3.12 的 uv workspace，包含 `server/`、`client/shared/` 和 `plugin/mcp/` 三个可安装包。用 pytest 管理端口测试及跨进程验收。
2. 在 `tests/server/src/teamup_server/` 先验证注册唯一性、成员资格、公告确认、持久发送幂等与收件确认。实现在 `server/src/teamup_server/`，由 Collaboration 写 SQLite，Gateway 做 HTTP 和 SSE 映射，启动入口负责装配。
3. 在 `tests/client/shared/src/teamup_client/` 先验证本机 IPC 绑定隔离、上游请求及通知恢复。实现在 `client/shared/src/teamup_client/`，公开 socket 调用，并让协调器持有配置和通知记录。
4. 先用实际 MCP SDK stdio 客户端写两个 Agent 的验收用例，再实现 `plugin/mcp/src/teamup_mcp/` 工具映射。验收从独立回环 Server 与本机 Client 出发，检查正向聊天、未确认公告、登录隔离和离线补收。
5. 核对 Codex 与 Claude Code 的独立唤醒证据，记录原会话标记与实际 turn 的证据等级。运行 `uv run --python 3.12 pytest` 和边界检查，同步安装文档。
