# MCP–Client v1

提供方为常驻 Client，消费方为本机 MCP 插件和 CLI。Agent harness 到插件使用 MCP stdio，插件到 Client 使用用户私有 Unix socket。Client 持有登记密钥和 TeamUp 成员 token。Agent 工具只收到公开协作数据。

新试用部署使用 [账号登录 v1](account-login-v1.md) 的本机登录命令。Agent 工具与 IPC 接口沿用本契约。

## 启动与绑定

`teamup-client init-binding` 在 stdout 生成一个绑定配置片段，其中含两个互相独立的 43 字符 base64url 随机秘密 `binding_key`、`member_token`，以及可编辑的 `harness_instance_id`、`session_key`。开发者把稳定标识与生成的两个秘密保存进私有配置，后续重启复用。

`teamup-client serve --config PATH` 启动常驻进程。配置必须为用户拥有、组和其他账号无访问权的 JSON 文件，包含：

- `server_url`，TeamUp HTTP 回环或 HTTPS 基础地址，不含尾路径。
- `socket_path`，用户私有目录中的绝对 Unix socket 路径。Client 在这个目录私有地保存自身通知状态。
- `enrollment_key`，管理员分发给本机开发者的登记密钥。
- `bindings`，本机批准的会话列表。每项包含 `binding_key`、`member_token`、`harness_instance_id`、`session_key`。每项的秘密与稳定标识对在配置中唯一。
- 可选 `ca_file`，自建 CA 的证书路径，用于 HTTPS 校验。

`teamup-mcp` 从环境变量 `TEAMUP_SOCKET`、`TEAMUP_BINDING_KEY` 取得本机 socket 和当前会话的绑定秘密。由拥有者为每个 harness 会话配置各自的启动方式。只获准的本机用户能连接 socket。绑定秘密按对该本机用户的授权凭证保管，私有仓库或团队共享配置也不要提交。

## MCP 协作指引

MCP 初始化说明与随附的协作 skill 会引导 Agent 按参与范围选入口。两名 Agent 的协作用私聊，三名及以上 Agent 需要共享任务上下文时用房间。人数指预计参与者，建群时创建者先加入。私聊经 `teamup_open_dm`/`teamup_send_dm`/`teamup_read_dm` 完成，可重复交接：同一成员对总是同一会话，重试沿用同一 `client_message_id`。不要用房间替代私聊，也不要把私聊正文复制进日志或其他会话。与旧版本混布、当前工具目录缺少私聊工具时，提示请求者提供已授权的私下交接方式。房间消息可被其他有权访问的成员读取，`@` 只指定通知对象。

## Unix socket 协议

每次调用使用一条连接和一条以换行结尾的 UTF-8 JSON 请求。请求为 `{"version":1,"binding_key":"...","method":"...","params":{}}`。响应为 `{"result":{}}` 或 `{"error":{"code":"...","message":"..."}}`，以换行结束，随后关闭连接。请求最大 128 KiB，客户端接收单次响应的上限为 16 MiB。IPC 不接受可替代绑定身份的字段。Python 封装位于 `teamup_client.ipc`，公开异步 `call(socket_path, binding_key, method, params)` 和异常 `ClientError`（`code`、`message`）。

| MCP 工具 | IPC 方法 | 参数（空参数传 `{}`） | 结果 |
| --- | --- | --- | --- |
| `teamup_register` | `register` | `role_description` | Member，以已批准绑定登记，可重试 |
| `teamup_find_members` | `find_members` | 可选 `q` | `{"members":[Member]}` |
| `teamup_create_room` | `create_room` | `name`、可选 `rules`、`protocol` | Room |
| `teamup_join_room` | `join_room` | `room_id` | Room |
| `teamup_get_board` | `get_board` | `room_id` | Board |
| `teamup_ack_board` | `ack_board` | `room_id`、`version` | `{"version":1}` |
| `teamup_send_message` | `send_message` | `room_id`、`client_message_id`、`text`、可选 `mention_member_id` | Message |
| `teamup_read_messages` | `read_messages` | `room_id`、可选 `after`、`limit` | `{"messages":[Message]}` |
| `teamup_open_dm` | `open_dm` | `member_id` | DM |
| `teamup_send_dm` | `send_dm` | `dm_id`、`client_message_id`、`text` | DM Message |
| `teamup_read_dm` | `read_dm` | `dm_id`、可选 `after`、`limit` | `{"messages":[DM Message]}`，可带到期元数据 |
| `teamup_notifications` | `notifications` | 可选 `after`、`limit` | `{"notifications":[Notice]}`，按 `event_id` 递增 |

Member、Room、Board、Message 与分页要求见 [Client–Server v1](client-server-v1.md)；DM、DM Message 与收件箱 `kinds` 参数见 [Direct Messages](direct-messages.md)。`mention` 的 Notice 只含 `event_id`、`room_id`、`message_id`、`sender_member_id`；`dm` 的 Notice 为类型化五字段 `{event_id, kind:"dm", dm_id, message_id, sender_member_id}`，不含正文，两类 Notice 在同一查询中按 `event_id` 递增混排。返回值由 MCP 作为结构化工具结果交付。无效绑定返回 `unauthorized`，协议错误、非法参数或未知方法返回 `invalid_request`，未登记返回 `not_registered`，上游不可达返回 `unavailable`。Server 业务错误透传 code（含 422 `invalid_peer`）。对 Agent 的工具失败携带同一 code 和简短原因。用户可用 `teamup-client notifications` 和相同的 `TEAMUP_SOCKET`、`TEAMUP_BINDING_KEY` 环境变量主动读取通知。

## 通知行为

Client 为已登记的绑定监听 SSE，在初连、重连和收到提示时拉取未确认事件。拉取显式携带 `kinds=message,mention,dm`（旧 Server 忽略该参数，只返回既有事件）。普通事件确认后可通过房间历史读取。收到 `mention` 时，Client 按绑定和 `event_id` 持久登记通知，再确认 Server 事件；收到 `dm` 时按同等方式持久登记类型化通知（含 `dm_id`，无正文），新类型与既有行共存于同一本机存储，存量行原样保留，重启迁移后全部可读。持久化先于确认。首次登记时异步尝试向 daemon stderr 打印只有 Notice 字段的 JSON 对象。日志管道阻塞或队列满时，stderr 提醒可能延迟或省略，收件确认和本机通知查询继续运行。重复投递不会增加通知记录。进程在保存后崩溃时，通知仍可通过 MCP 或 CLI 读取。确认与保留到期竞争、Server 对已到期事件返回 404 时，Client 视为已清除并继续补收；其余确认失败照旧退避重试。Server 和 IPC 在后续增加撤权能力时须约定相应的本地可见性变更。

首轮 @ 只发通知，桌面唤醒作为独立 harness 实测进行。远端聊天正文不会直接作为命令执行。
