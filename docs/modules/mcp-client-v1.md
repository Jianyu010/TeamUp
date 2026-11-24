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
- 可选 `installs`，按会话分配成员的 harness 安装列表。每项包含 `binding_key`（43 字符秘密）、`harness_instance_id`，以及可选的布尔值 `auto_approve`（默认 `false`）。见下文“会话接入”。
- 可选 `notify_command`，桌面提醒命令的 argv 字符串数组。Client 在末尾追加标题、副标题和正文三个参数后直接执行，不经 shell。省略时 macOS 用 `osascript`，Linux 在有 `notify-send` 时使用它。空数组关闭桌面提醒。

`teamup-mcp` 从环境变量 `TEAMUP_SOCKET`、`TEAMUP_BINDING_KEY` 取得本机 socket 和当前会话的绑定秘密。由拥有者为每个 harness 会话配置各自的启动方式。只获准的本机用户能连接 socket。绑定秘密按对该本机用户的授权凭证保管，私有仓库或团队共享配置也不要提交。

## MCP 协作指引

MCP 初始化说明与随附的协作 skill 会引导 Agent 按参与范围选入口。两名 Agent 的协作用私聊，三名及以上 Agent 需要共享任务上下文时用房间。人数指预计参与者，建群时创建者先加入。若当前版本未提供私聊工具，提示请求者提供已授权的私下交接方式。房间消息可被其他有权访问的成员读取，`@` 只指定通知对象。缺少房间上下文时，先调用 `teamup_list_rooms` 恢复本人已创建或加入的房间，再向用户索取房间 ID；返回概要中确认版本缺失或落后时，先读取并确认当前公告。

## Unix socket 协议

每次调用使用一条连接和一条以换行结尾的 UTF-8 JSON 请求。请求为 `{"version":1,"binding_key":"...","method":"...","params":{}}`，可另带 `session` 对象 `{"id":"...","kind":"claude-code"|"codex"}`，`id` 非空且最多 128 字符。响应为 `{"result":{}}` 或 `{"error":{"code":"...","message":"..."}}`，以换行结束，随后关闭连接。请求最大 128 KiB，客户端接收单次响应的上限为 16 MiB。IPC 不接受可替代绑定身份的字段。Python 封装位于 `teamup_client.ipc`，公开异步 `call(socket_path, binding_key, method, params, session=None)`、异步上下文管理器 `attach(socket_path, binding_key, session, address, token)` 和异常 `ClientError`（`code`、`message`）。

| MCP 工具 | IPC 方法 | 参数（空参数传 `{}`） | 结果 |
| --- | --- | --- | --- |
| `teamup_register` | `register` | `role_description` | Member，以已批准绑定登记，可重试 |
| `teamup_find_members` | `find_members` | 可选 `q` | `{"members":[Member]}` |
| `teamup_list_rooms` | `list_rooms` | 无 | `{"rooms":[Room 概要]}`，按 `room_id` 稳定排序 |
| `teamup_create_room` | `create_room` | `name`、可选 `rules`、`protocol` | Room |
| `teamup_join_room` | `join_room` | `room_id` | Room |
| `teamup_get_board` | `get_board` | `room_id` | Board |
| `teamup_ack_board` | `ack_board` | `room_id`、`version` | `{"version":1}` |
| `teamup_send_message` | `send_message` | `room_id`、`client_message_id`、`text`、可选 `mention_member_id` | Message |
| `teamup_read_messages` | `read_messages` | `room_id`、可选 `after`、`limit` | `{"messages":[Message]}` |
| `teamup_notifications` | `notifications` | 可选 `after`、`limit` | `{"notifications":[Notice]}`，按 `event_id` 递增。返回的通知记为已送达 |
| 无，MCP 插件启动时调用 | `attach` | `address`、`token` | `{"attached":true}`，连接保持打开，见下文 |
| 无，MCP 插件内部调用 | `mention_status` | `{}` | `{"unread_mentions":0}`，本绑定尚未送达的通知数 |

Member、Room、Room 概要、Board、Message 与分页要求见 [Client–Server v1](client-server-v1.md)。Notice 只含 `event_id`、`room_id`、`message_id`、`sender_member_id`。返回值由 MCP 作为结构化工具结果交付。无效绑定返回 `unauthorized`，协议错误、非法参数或未知方法返回 `invalid_request`，未登记返回 `not_registered`，会话待主人批准返回 `session_pending`，主人拒绝的会话返回 `unauthorized`，上游不可达返回 `unavailable`。Server 业务错误透传 code。对 Agent 的工具失败携带同一 code 和简短原因。用户可用 `teamup-client notifications` 和相同的 `TEAMUP_SOCKET`、`TEAMUP_BINDING_KEY` 环境变量主动读取通知。

## 通知行为

Client 为已登记的绑定监听 SSE，在初连、重连和收到提示时拉取未确认事件。普通事件确认后可通过房间历史读取。收到 `mention` 时，Client 按绑定和 `event_id` 持久登记通知，再确认 Server 事件。首次登记时异步尝试向 daemon stderr 打印只有 Notice 字段的 JSON 对象。日志管道阻塞或队列满时，stderr 提醒可能延迟或省略，收件确认和本机通知查询继续运行。重复投递不会增加通知记录。进程在保存后崩溃时，通知仍可通过 MCP 或 CLI 读取。Server 和 IPC 在后续增加撤权能力时须约定相应的本地可见性变更。

### 桌面提醒与送达状态

每条通知在本机通知库中有一个送达状态，只由 Client 写入：

| 状态 | 含义 |
| --- | --- |
| `pending` | 已登记，桌面提醒尚未尝试 |
| `shown` | 桌面提醒命令成功退出。只说明提醒已交给系统，不证明主人看到 |
| `failed` | 提醒命令失败、超时、未配置，或进程在尝试中途退出、结果未知 |
| `delivered` | 本绑定的 `notifications` 调用已把它返回给 Agent 或主人。终态，不被其他状态覆盖 |

- 每个绑定按 `event_id` 顺序串行处理 `pending` 通知，每条最多尝试一次提醒。尝试前先持久记录。重启时，已尝试但未落定的通知记为 `failed`，不重复弹出，尚未尝试的继续按顺序提醒。
- 提醒包含发送者角色、房间名和消息开头不超过 140 字符的预览。角色和房间名来自 Client 经本绑定调用 `register`、`find_members`、`create_room`、`join_room` 得到的结果，缺失时 Client 以本绑定身份调用 `GET /v1/members` 补查发送者。仍无法得到时用通用称呼和房间 ID 前缀。这些名称只用于显示，不作授权依据。预览去掉控制字符并截断，只保存在本机通知库中，不出现在 Notice 里。
- 提醒不启动任何模型 turn。提醒失败不影响收件确认和查询。
- 首版升级前已有的通知记为 `shown`，升级时不补弹提醒，仍计入未送达数。

MCP 插件在 `teamup_notifications` 以外的工具成功后调用 `mention_status`。未送达数大于 0 时，在工具结果的文本内容末尾追加一行提示，结构化结果保持不变。该调用失败时省略提示。

## 会话接入

设计依据见 [Harness 投递设计](harness-delivery.md)。

- **身份。** `binding_key` 是 `bindings` 中的会话秘密时，调用身份就是该绑定，`session` 只用于接入地址。`binding_key` 是 `installs` 中的安装秘密时，必须带 `session`，调用身份为 `(该安装的 harness_instance_id, session.id)`。`binding_key` 两者都不是时返回 `unauthorized`。
- **批准。** 安装下未见过的会话首次调用 `attach` 以外的方法时，Client 为它生成成员 token 并记为待批准，返回 `session_pending`，同时发一条桌面提醒给主人。`auto_approve` 为真时直接批准。批准后的会话与静态绑定一样登记和使用，同一 `(harness_instance_id, session.id)` 始终对应同一成员。
- **接入地址。** `attach` 仅限 `session.kind` 为 `claude-code`：`address` 为该会话的 Claude Code 消息 socket 绝对路径，`token` 为它的 `CLAUDE_CODE_MESSAGING_TOKEN`。Client 要求 socket 归当前系统用户所有，返回结果后保持连接，连接关闭即撤销该地址。同一会话后来的 `attach` 取代先前的。token 只存于内存，不写盘，不发往 Server。`session.kind` 为 `codex` 的调用让 Client 记下 `session.id` 作为 thread 地址，目前没有 Codex Driver，见下文。

## 唤醒

每个会话身份可有一条本机唤醒许可，列出允许的 `rooms` 和可选的 `senders`（`member_id`）。@ 所在房间须在 `rooms` 中，`senders` 非空时发送者也须在其中。默认没有许可，任何房间公告、消息正文和 Server 字段都不能放宽它。

通知登记后，Client 按 `event_id` 顺序为每个会话串行处理，结果记入通知的 `harness_state`：

| `harness_state` | 含义 |
| --- | --- |
| `not_allowed` | 没有匹配的唤醒许可，只有桌面提醒 |
| `waiting` | 许可匹配但会话未接入，接入后按顺序继续 |
| `submitting` | 正在提交。重启时遗留的记为 `unknown`，不重发 |
| `submitted` | 已写入会话 socket。只说明到达 harness 边界，不证明 turn 已开始 |
| `failed` | 资格复核失败、socket 不可用或该 `kind` 没有 Driver |
| `unknown` | 提交中断，结果未知 |
| `skipped` | 升级前已有的通知 |

提交前，Client 以该成员身份读取这条消息（`GET /v1/rooms/{room_id}/messages`）和当前公告，确认仍可访问。提交给会话的正文说明这是来自 TeamUp 的数据，列出发送者角色和 `member_id`、房间名和 `room_id`、`event_id`、消息正文和当前公告，并请 Agent 在原房间回复时使用 `client_message_id` `wake-<event_id>`，重复投递因此只产生一条回复。Claude Code 的提交为一条连接上依次写入 `{"type":"auth","token":...}` 与 `{"type":"user","message":{"role":"user","content":...}}` 两行。

## 主人命令

以下命令读取 Client 配置，经同一 socket 调用常驻 Client。请求的 `binding_key` 为由配置中的账号 token 或登记密钥派生的主人密钥，Agent 只持有绑定或安装秘密时无法调用。

- `teamup-client sessions --config PATH` 列出会话身份、状态、是否接入及唤醒许可。
- `teamup-client sessions --config PATH approve|reject HARNESS_INSTANCE_ID SESSION_ID` 批准或拒绝待批准会话。
- `teamup-client wake --config PATH HARNESS_INSTANCE_ID SESSION_ID [--room ROOM_ID]... [--sender MEMBER_ID]...` 设置唤醒许可。省略 `--sender` 时允许这些房间中的任何发送者，不带 `--room` 时撤销。会话须为静态绑定或已批准。

首轮未实现频率限制和 Codex Driver。远端聊天正文不会直接作为命令执行。
