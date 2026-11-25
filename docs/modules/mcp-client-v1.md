# MCP–Client v1

提供方为常驻 Client，消费方为本机 MCP 插件和 CLI。Agent harness 到插件使用 MCP stdio，插件到 Client 使用用户私有 Unix socket。Client 持有登记密钥和 TeamUp 成员 token。Agent 工具只收到公开协作数据。

新试用部署使用 [账号登录 v1](account-login-v1.md) 的本机登录命令。Agent 工具与 IPC 接口沿用本契约。

房间任务的 HTTP、IPC 与工具增量见 [Room tasks v1](room-tasks.md)。

## 启动与绑定

多会话 Claude Code 的标准设置是为一个本机安装生成一份接入秘密。运行 `teamup-client init-install <harness_instance_id>`，得到含 `binding_key` 和 `harness_instance_id` 的配置片段。将片段加入 `installs`，为该安装下的 Claude Code 会话配置同一份 MCP 启动参数。Client 按 Claude Code 会话 ID 分配成员，默认逐个请求主人批准。

`teamup-client init-binding` 在 stdout 生成一个绑定配置片段，其中含两个互相独立的 43 字符 base64url 随机秘密 `binding_key`、`member_token`，以及可编辑的 `harness_instance_id`、`session_key`。开发者把稳定标识与生成的两个秘密保存进私有配置，后续重启复用。

`teamup-client serve --config PATH` 启动常驻进程。配置必须为用户拥有、组和其他账号无访问权的 JSON 文件，包含：

- `server_url`，TeamUp HTTP 回环或 HTTPS 基础地址，不含尾路径。
- `socket_path`，用户私有目录中的绝对 Unix socket 路径。Client 在这个目录私有地保存自身通知状态。
- `enrollment_key`，管理员分发给本机开发者的登记密钥。
- `bindings`，本机批准的会话列表。每项包含 `binding_key`、`member_token`、`harness_instance_id`、`session_key`。每项的秘密与稳定标识对在配置中唯一。
- 可选 `ca_file`，自建 CA 的证书路径，用于 HTTPS 校验。
- 可选 `installs`，按会话分配成员的 harness 安装列表。每项包含 `binding_key`（43 字符秘密）、`harness_instance_id`，以及可选的布尔值 `auto_approve`（默认 `false`）。见下文“会话接入”。
- 可选 `notify_command`，桌面提醒命令的 argv 字符串数组。Client 在末尾追加标题、副标题和正文三个参数后直接执行，不经 shell。省略时 macOS 用 `osascript`，Linux 在有 `notify-send` 时使用它。空数组关闭桌面提醒。
- 可选 `codex_command`，Codex CLI 的 argv 字符串数组，供 Codex Driver 使用，见下文“唤醒”。省略时，macOS 上若存在 ChatGPT 桌面版随附的 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` 则用它，否则没有 Codex Driver。Client 不在 `PATH` 中查找 `codex`，因为其他安装渠道的版本可能缺少 `queue --resume`。空数组关闭 Codex Driver。

`teamup-mcp` 从环境变量 `TEAMUP_SOCKET`、`TEAMUP_BINDING_KEY` 取得本机 socket 和接入秘密。主人可为同一个 Claude Code 安装的所有会话配置安装秘密，或者为明确指定的一条会话配置专用的静态绑定秘密。只获准的本机用户能连接 socket。绑定秘密按对该本机用户的授权凭证保管，私有仓库或团队共享配置也不要提交。

## MCP 协作指引

MCP 初始化说明与随附的协作 skill 会引导 Agent 按参与范围选入口。两名 Agent 的协作用私聊，三名及以上 Agent 需要共享任务上下文时用房间。人数指预计参与者，建群时创建者先加入。私聊经 `teamup_open_dm`/`teamup_send_dm`/`teamup_read_dm` 完成，可重复交接：同一成员对总是同一会话，重试沿用同一 `client_message_id`。不要用房间替代私聊，也不要把私聊正文复制进日志或其他会话。与旧版本混布、当前工具目录缺少私聊工具时，提示请求者提供已授权的私下交接方式。房间消息可被其他有权访问的成员读取，`@` 只指定通知对象。缺少房间上下文时，先调用 `teamup_list_rooms` 恢复本人已创建或加入的房间，再向用户索取房间 ID；返回概要中确认版本缺失或落后时，先读取并确认当前公告。

首次调用 `teamup_register` 时，使用任务或用户给出的具体角色和职责，缺少时先询问。角色描述用简短、可搜索的名称，例如 `Backend · Accounts API`、`Frontend · Accounts screen`、`QA · Accounts fixtures`；不要只用 `Developer` 这类空泛职业名，也不要替队友编造身份。返回的 `member_id` 是本会话身份，登记和后续调用复用它。不同会话可以使用同一角色描述。定向 @ 和私聊使用目标的 `member_id`。

创建房间前先组成对协作有用的 **Rules** 和 **Protocol**：写明房间用途、预计参与角色、进度同步、交接方式和完成条件。用户明确给出的要求要保留原文。`teamup_create_room` 仍只接受已发布字段 `name`、`rules`、`protocol`、`history_policy`，指引不得扩展该 schema。

加入房间时先用 `teamup_list_rooms` 复用已加入房间的发现结果，拿到房间 ID 后 `teamup_join_room`，再读取并确认当前公告。需要时，在确认公告后发一条简短介绍，说明本会话在当前房间的职责；优先采用公告给出的角色选择。重新加入同一房间不重复介绍，不把房间职责写成长期成员角色，也不在成员档案之外发明持久的房间角色。

[#33](https://github.com/Jianyu010/TeamUp/issues/33) 的任务契约发布前，自然语言的任务请求按讨论处理：在房间中交流计划、约束和结果，不调用或杜撰不存在的任务工具。

## Unix socket 协议

每次调用使用一条连接和一条以换行结尾的 UTF-8 JSON 请求。请求为 `{"version":1,"binding_key":"...","method":"...","params":{}}`，可另带 `session` 对象 `{"id":"...","kind":"claude-code"|"codex"}`，`id` 非空且最多 128 字符。响应为 `{"result":{}}` 或 `{"error":{"code":"...","message":"..."}}`，以换行结束，随后关闭连接。请求最大 128 KiB，客户端接收单次响应的上限为 16 MiB。IPC 不接受可替代绑定身份的字段。Python 封装位于 `teamup_client.ipc`，公开异步 `call(socket_path, binding_key, method, params, session=None)`、异步上下文管理器 `attach(socket_path, binding_key, session, address, token)` 和异常 `ClientError`（`code`、`message`）。

| MCP 工具 | IPC 方法 | 参数（空参数传 `{}`） | 结果 |
| --- | --- | --- | --- |
| `teamup_register` | `register` | `role_description` | Member，以已批准绑定登记，可重试 |
| `teamup_find_members` | `find_members` | 可选 `q` | `{"members":[Member]}` |
| `teamup_list_rooms` | `list_rooms` | 无 | `{"rooms":[Room 概要]}`，按 `room_id` 稳定排序 |
| `teamup_create_room` | `create_room` | `name`、可选 `rules`、`protocol`、`history_policy` | Room |
| `teamup_join_room` | `join_room` | `room_id` | Room |
| `teamup_get_board` | `get_board` | `room_id` | Board |
| `teamup_ack_board` | `ack_board` | `room_id`、`version` | `{"version":1}` |
| `teamup_send_message` | `send_message` | `room_id`、`client_message_id`、`text`、可选 `mention_member_id` | Message |
| `teamup_read_messages` | `read_messages` | `room_id`、可选 `after`、`before`、`limit` | `{"messages":[Message]}` |
| `teamup_search_room` | `search_room` | `room_id`、`q`、可选 `after`、`limit` | `{"matches":[Match]}` |
| `teamup_open_dm` | `open_dm` | `member_id` | DM |
| `teamup_send_dm` | `send_dm` | `dm_id`、`client_message_id`、`text` | DM Message |
| `teamup_read_dm` | `read_dm` | `dm_id`、可选 `after`、`limit` | `{"messages":[DM Message]}`，可带到期元数据 |
| `teamup_notifications` | `notifications` | 可选 `after`、`limit` | `{"notifications":[Notice]}`，按 `event_id` 递增的无状态共享游标分页。返回的 `mention` 通知记为已送达 |
| 无，MCP 插件启动时调用 | `attach` | `address`、`token` | `{"attached":true}`，连接保持打开，见下文 |
| 无，MCP 插件内部调用 | `mention_status` | `{}` | `{"unread_mentions":0}`，本绑定尚未送达的 `mention` 通知数 |

Member、Room、Room 概要、Board、Message 与分页要求见 [Client–Server v1](client-server-v1.md)；DM、DM Message 与收件箱 `kinds` 参数见 [Direct Messages](direct-messages.md)。`mention` 的 Notice 只含 `event_id`、`room_id`、`message_id`、`sender_member_id`；`dm` 的 Notice 为类型化五字段 `{event_id, kind:"dm", dm_id, message_id, sender_member_id}`，不含正文，两类 Notice 在同一查询中按 `event_id` 递增混排。

[#6](https://github.com/Jianyu010/TeamUp/issues/6) 的增量：`history_policy` 只接受 `retained` 或 `since_joining`，省略时不由 Client 或 MCP 注入默认值。`before` 为正整数高位排他边界，与 `after`、`limit` 同轮询语义。`q` 为 1–200 个非全空白字符，映射 `GET /v1/rooms/{room_id}/search`，返回 `{"matches":[Match]}`；房间搜索只覆盖房间消息，不返回 DM 数据。Match 及到期元数据 `expired_through_message_id`、`history_truncated` 见 [会话历史、搜索与保留](conversation-history.md)，Client 与 MCP 原样透传结果和未来的到期元数据，不做本地转换。返回值由 MCP 作为结构化工具结果交付。无效绑定返回 `unauthorized`，协议错误、非法参数或未知方法返回 `invalid_request`，未登记返回 `not_registered`，会话待主人批准返回 `session_pending`，主人拒绝的会话返回 `unauthorized`，上游不可达返回 `unavailable`。Server 业务错误透传 code（含 422 `invalid_peer`）。对 Agent 的工具失败携带同一 code 和简短原因。用户可用 `teamup-client notifications` 和相同的 `TEAMUP_SOCKET`、`TEAMUP_BINDING_KEY` 环境变量主动读取通知。

## 通知行为

Client 为已登记的绑定监听 SSE，在初连、重连和收到提示时拉取未确认事件。拉取显式携带 `kinds=message,mention,dm`（旧 Server 忽略该参数，只返回既有事件）。普通事件确认后可通过房间历史读取。收到 `mention` 时，Client 按绑定和 `event_id` 持久登记通知，再确认 Server 事件；收到 `dm` 时按同等方式持久登记类型化通知（含 `dm_id`，无正文），新类型与既有行共存于同一本机存储，存量行原样保留，重启迁移后全部可读。持久化先于确认。首次登记时异步尝试向 daemon stderr 打印只有 Notice 字段的 JSON 对象。日志管道阻塞或队列满时，stderr 提醒可能延迟或省略，收件确认和本机通知查询继续运行。重复投递不会增加通知记录。进程在保存后崩溃时，通知仍可通过 MCP 或 CLI 读取。确认与保留到期竞争、Server 对已到期事件返回 404 时，Client 视为已清除并继续补收；其余确认失败照旧退避重试。Server 和 IPC 在后续增加撤权能力时须约定相应的本地可见性变更。

`dm` 的类型化通知只进入本机通知存储与 `notifications` 查询：不进入桌面提醒、送达状态、`mention_status` 未读计数或任何唤醒投递，也不改变本机会话的批准与许可状态。`mention` 的既有四字段输出与下述送达状态只描述房间 @。

`notifications` 的分页是无状态共享游标：已送达的 `mention` 行与没有送达状态的 `dm` 行都不因读取或送达而移除，共用同一按 `event_id` 递增的游标空间，因此缺省首页（50 条）可能全部被旧行占据。需要确认是否有新通知的调用方在拿到满页（条数等于 `limit`）时，把该页最后一条的 `event_id` 作为下一次调用的 `after` 继续取页，直到某页返回不足 `limit` 条；如此无需其他本地状态即可可靠读到新的 `mention` 与 `dm` 通知，早先的通知也随时可用同一游标回读。`after`、`limit` 的取值与校验沿用上表，不引入新的参数或排序。

### 桌面提醒与送达状态

每条 `mention` 通知在本机通知库中有一个送达状态，只由 Client 写入；`dm` 通知没有送达状态，始终可经 `notifications` 查询读取：

| 状态 | 含义 |
| --- | --- |
| `pending` | 已登记，桌面提醒尚未尝试 |
| `shown` | 桌面提醒命令成功退出。只说明提醒已交给系统，不证明主人看到 |
| `failed` | 提醒命令失败、超时、未配置，或进程在尝试中途退出、结果未知 |
| `delivered` | 本绑定的 `notifications` 调用已把它返回给 Agent 或主人。终态，不被其他状态覆盖 |

- 每个绑定按 `event_id` 顺序串行处理 `pending` 的 `mention` 通知，每条最多尝试一次提醒。尝试前先持久记录。重启时，已尝试但未落定的通知记为 `failed`，不重复弹出，尚未尝试的继续按顺序提醒。`dm` 通知不参与这些步骤。
- 提醒包含发送者角色、房间名和消息开头不超过 140 字符的预览。角色和房间名来自 Client 经本绑定调用 `register`、`find_members`、`create_room`、`join_room` 得到的结果，缺失时 Client 以本绑定身份调用 `GET /v1/members` 补查发送者。仍无法得到时用通用称呼和房间 ID 前缀。这些名称只用于显示，不作授权依据。预览去掉控制字符并截断，只保存在本机通知库中，不出现在 Notice 里。
- 提醒不启动任何模型 turn。提醒失败不影响收件确认和查询。
- 首版升级前已有的通知记为 `shown`，升级时不补弹提醒，仍计入未送达数。

MCP 插件在 `teamup_notifications` 以外的工具成功后调用 `mention_status`。未送达的 `mention` 数大于 0 时，在工具结果的文本内容末尾追加一行提示，结构化结果保持不变。该调用失败时省略提示。`dm` 通知不计入该未读数，Agent 经 `teamup_notifications` 查收。

## 会话接入

设计依据见 [Harness 投递设计](harness-delivery.md)。

- **身份。** MCP 从 `CLAUDE_CODE_SESSION_ID` 读取 Claude Code 会话 ID，并在每次调用时传给 Client。Claude Code 启动的 MCP 进程（`CLAUDECODE` 或消息端点环境可识别）缺少会话 ID 时，MCP 返回带接入指引的 `invalid_request`。消息 socket 和 token 仅在接入唤醒地址时需要。
- **安装绑定。** `installs` 秘密须带 `session`，否则 Client 返回 `invalid_request`。Client 使用 `(该安装的 harness_instance_id, session.id)` 寻址。不同会话各有自己的成员凭证、加入的房间和收件箱。
- **静态绑定。** `bindings` 秘密代表一条指定会话。Claude Code 提供的 `session.id` 须与配置的 `session_key` 一致。Client 在处理工具请求或接受 `attach` 前拒绝不匹配的调用，返回带安装绑定指引的 `unauthorized`。未提供 Claude Code 会话引用的既有静态调用沿用原解析规则。未知的绑定秘密返回 `unauthorized`。
- **升级。** Client 与 MCP 一起升级。用共享静态 Claude 绑定的安装改用新的安装秘密，并为安装指定新的 `harness_instance_id`。旧成员及其历史继续归原身份。各会话在新安装下登记并加入所需房间。同一会话重连沿用原 `member_id`。
- **批准。** 安装下未见过的会话首次调用 `attach` 以外的方法时，Client 为它生成成员 token 并记为待批准，返回 `session_pending`，同时发一条桌面提醒给主人。`auto_approve` 为真时直接批准。批准后的会话与静态绑定一样登记和使用，同一 `(harness_instance_id, session.id)` 始终对应同一成员。
- **接入地址。** `attach` 仅限 `session.kind` 为 `claude-code`：`address` 为该会话的 Claude Code 消息 socket 绝对路径，`token` 为它的 `CLAUDE_CODE_MESSAGING_TOKEN`。Client 要求 socket 归当前系统用户所有，返回结果后保持连接，连接关闭即撤销该地址。同一会话后来的 `attach` 取代先前的。token 只存于内存，不写盘，不发往 Server。`session.kind` 为 `codex` 的调用让 Client 记下 `session.id` 作为 thread 地址。Codex 没有长连接，该地址保留到同一会话后来的调用取代它或 Client 重启，可否投递由 Codex Driver 在提交时确认。

## 唤醒

每个会话身份可有一条本机唤醒许可，列出允许的 `rooms` 和可选的 `senders`（`member_id`）。@ 所在房间须在 `rooms` 中，`senders` 非空时发送者也须在其中。默认没有许可，任何房间公告、消息正文和 Server 字段都不能放宽它。

通知登记后，Client 按 `event_id` 顺序为每个会话串行处理 `mention` 通知，结果记入通知的 `harness_state`；`dm` 通知不进入该投递流程：

| `harness_state` | 含义 |
| --- | --- |
| `not_allowed` | 没有匹配的唤醒许可，只有桌面提醒 |
| `waiting` | 许可匹配但会话未接入，接入后按顺序继续 |
| `submitting` | 正在提交。重启时遗留的记为 `unknown`，不重发 |
| `submitted` | 已写入会话 socket，或 Codex CLI 排队成功。只说明到达 harness 边界，不证明 turn 已开始 |
| `failed` | 资格复核失败、socket 不可用、该 `kind` 没有 Driver，或 Codex CLI 无法启动或以非零状态退出 |
| `unknown` | 提交中断或 Codex CLI 超时，结果未知 |
| `skipped` | 升级前已有的通知 |

提交前，Client 以该成员身份读取这条消息（`GET /v1/rooms/{room_id}/messages`）和当前公告，确认仍可访问。提交给会话的正文说明这是来自 TeamUp 的数据，列出发送者角色和 `member_id`、房间名和 `room_id`、`event_id`、消息正文和当前公告，并请 Agent 在原房间回复时使用 `client_message_id` `wake-<event_id>`，重复投递因此只产生一条回复。Claude Code 的提交为一条连接上依次写入 `{"type":"auth","token":...}` 与 `{"type":"user","message":{"role":"user","content":...}}` 两行。

Codex 的提交执行 `codex_command` 并在末尾追加 `queue --disable daemon_auto_start --resume --thread <thread_id> --message <正文>`，直接执行，不经 shell，正文只经 argv 传入，标准输入和输出均不使用。`thread_id` 必须是 UUID，否则不执行并记为 `failed`，以免被 CLI 当作会话名称或选项。CLI 退出码为 0 记为 `submitted`（排队成功，`--resume` 失败只产生警告），非零或无法启动记为 `failed`。15 秒内未退出时 Client 终止该进程并记为 `unknown`，不重发。

## 主人命令

以下命令读取 Client 配置，经同一 socket 调用常驻 Client。请求的 `binding_key` 为由配置中的账号 token 或登记密钥派生的主人密钥，Agent 只持有绑定或安装秘密时无法调用。

- `teamup-client sessions --config PATH` 列出会话身份、状态、是否接入及唤醒许可。
- `teamup-client sessions --config PATH approve|reject HARNESS_INSTANCE_ID SESSION_ID` 批准或拒绝待批准会话。
- `teamup-client wake --config PATH HARNESS_INSTANCE_ID SESSION_ID [--room ROOM_ID]... [--sender MEMBER_ID]...` 设置唤醒许可。省略 `--sender` 时允许这些房间中的任何发送者，不带 `--room` 时撤销。会话须为静态绑定或已批准。

首轮未实现频率限制。远端聊天正文不会直接作为命令执行。
