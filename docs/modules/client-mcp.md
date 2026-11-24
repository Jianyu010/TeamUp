# Client 和本地 MCP

## 安装与进程生命周期

Client 后台进程随开发者本机登录或手动启动，管理多个已登记的 Agent 会话。没有 Agent turn 或 MCP 进程时，Client 仍靠 SSE 收件和心跳，向用户提醒或根据本机策略发起唤醒。

安装 TeamUp 插件时，harness 获得本地 MCP stdio 启动配置及协作 skills。Agent 调工具时经该 MCP 进程查询或请求 Client。harness 用自己的 skill 装载机制读取 `plugin/skills/` 的内容。安装包将 Client、MCP 入口及相应插件文件一起交付。各 harness 插件清单和启动命令随实现提供。 [Codex MCP 连接方式](https://learn.chatgpt.com/docs/extend/mcp) · [Codex 插件](https://learn.chatgpt.com/docs/plugins)

## 会话绑定和工具边界

1. 开发者在 Client 中批准登记某个稳定会话或 bot 键。Client 保存其本机绑定和 TeamUp 凭证。
2. 启动 MCP 进程时，为该进程提供绑定到单个会话的授权句柄。Client 根据该句柄确认调用身份。工具输入中的目标成员和房间只用于指向协作对象。
3. 首版在 macOS 和 Linux 上采用用户私有 Unix socket 和受限目录作为 Client IPC。连接必须属于本机用户，并使用已批准的会话绑定。共享或受信任配置文件里只放启动方式和绑定引用，有效凭证由 Client 保管。
4. MCP 工具按产品能力提供成员查找、建群或加入、公告读取和确认、消息与协作请求、收件和健康查询。普通 Agent 可报告问题。处置操作由具备权限的管理员通过公开入口执行。具体 schema 在对应实现之前冻结。

如果一个 harness 无法为两条会话提供可区分的启动绑定，需要由主人显式登记和选择会话。MCP 握手的 `clientInfo` 是客户端元信息，不能充当会话凭证。

## 自动唤醒

SSE 消费与唤醒决策在常驻 Client 内执行。Client 向 Server 检查事件资格，向本机读取唤醒许可，然后才调用 Driver。Driver 的结果区分已受理、明确失败和结果未知。未知结果等待核对，避免重复启动同一请求。适配器确认和引用回复可在 TeamUp 查询，harness 的实际 turn 状态由本机 Driver 观察。

[Codex 桌面唤醒独立验证](codex-wake.md)
