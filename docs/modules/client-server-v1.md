# Client–Server v1

本契约用于 TeamUp 最小闭环。提供方为 Server Gateway，业务状态归 Collaboration，消费方为 Client。本文锁定首轮登记、公共房间、公告确认、历史、定向通知及补收，并增量提供成员本人房间列表。私聊、邀请房间、公告发布、停用、健康和 harness 唤醒仍按 [MVP 设计](../specs/mvp.md) 演进。

新增的人类账号登录与停用规则见 [账号登录 v1](account-login-v1.md)。

## 传输与认证

JSON API 前缀为 `/v1`。请求与响应是 UTF-8 JSON。内网使用 HTTPS。开发环境允许回环地址 HTTP。客户端校验 TLS 证书。

管理员为每位开发者分配登记密钥，Server 将其映射到稳定的 `owner_id`。登记后，其他请求一律使用 `Authorization: Bearer <member_token>`。登记密钥和成员 token 都是各自独立的 32 随机字节经无填充 base64url 编码的 43 字符串。Client 为每个本机绑定生成并保留独立成员 token。Server 只保存成员 token 的摘要。Server 根据凭证决定当前成员，客户端字段不能替换发起方身份。

Server 启动读取 `TEAMUP_ENROLLMENT_KEYS` JSON 对象，键为 owner ID，值为登记密钥。支持命令 `teamup-server --db PATH [--host 127.0.0.1] [--port 8765] [--tls-cert PATH --tls-key PATH]`。声明非回环监听地址时需要 TLS。反向代理在同主机终止 TLS 时可连回环。

## 数据形状

| 名称 | JSON 字段 |
| --- | --- |
| Member | `member_id` UUID 字符串，`role_description` 字符串 |
| Room | `room_id` UUID 字符串，`name` 字符串 |
| Room 概要 | `room_id` UUID 字符串，`name` 字符串，`acked_board_version` 正整数或 null，`current_board_version` 正整数，`latest_message_id` 正整数或 null |
| Board | `room_id`，`version` 正整数，`rules`，`protocol` |
| Message | `message_id` 正整数，`room_id`，`sender_member_id`，`client_message_id`，`text`，`mention_member_id`（UUID 字符串或 null） |
| Event | `event_id` 正整数，`kind` 为 `message` 或 `mention`，`message` 为 Message |

路径 UUID 使用标准带连字符拼写。输入角色和房间名非空且最多 200 字符，`harness_instance_id`、`session_key`、`client_message_id` 非空且最多 128 字符，消息非空且最多 8,000 字符。各文本字段保留原文，纯空白无效。公告两部分各最多 8,000 字符。JSON 请求拒绝未声明字段。

## 操作

成功均返回 HTTP 200。列表 `after` 从 0 开始，`limit` 默认 50、最大 100，按 ID 递增。查询使用 `after` 排除小于或等于该 ID 的记录。

| 方法和路径 | 请求 | 成功结果 |
| --- | --- | --- |
| `POST /v1/members` | Bearer 登记密钥。JSON 含 `harness_instance_id`、`session_key`、`role_description`、`member_token` | Member。重复绑定且 token 相同返回原档案。不同 token 重复绑定，或 token 已绑定其他会话时冲突 |
| `GET /v1/members/me` | Bearer 成员 token | 当前 Member |
| `GET /v1/members?q=...` | 可选角色子串，默认空串 | `{"members":[Member]}`，按 ID 稳定排序，最多 100 条，大小写不敏感 |
| `POST /v1/rooms` | `{"name":"...","rules":"...","protocol":"..."}` | Room。创建者同时加入。公告初始版本为 1，两段文字可省略，各有默认值 |
| `GET /v1/rooms` | 无正文 | `{"rooms":[Room 概要]}`，按 `room_id` 稳定排序。仅含当前成员在 `room_members` 有行的房间，创建者经其既有成员关系包含在内。`acked_board_version` 为该成员已确认的版本，确认前为 null；`current_board_version` 为房间当前版本；`latest_message_id` 为该房间最新消息 ID，空房间为 null。概要属于成员范围数据，不要求当前公告已确认，也不提供按任意房间 ID 的查询、可加入状态或房间发现 |
| `POST /v1/rooms/{room_id}/join` | 无正文 | Room。当前只提供团队公开房间。重复加入保留原加入位置 |
| `GET /v1/rooms/{room_id}/board` | 无正文 | Board。加入后即可读取，Server 记录该成员取到的版本 |
| `POST /v1/rooms/{room_id}/board/ack` | `{"version":1}` | `{"version":1}`。确认限房间成员，要求当前版本，且该成员已先读取该版本 |
| `POST /v1/rooms/{room_id}/messages` | `client_message_id`、`text`、可选 `mention_member_id` | Message。定向 @ 由独立目标字段表达，目标必须是房间内其他成员 |
| `GET /v1/rooms/{room_id}/messages` | `after`、`limit` | `{"messages":[Message]}`。只读本次加入之后的历史 |
| `GET /v1/inbox` | `after`、`limit` | `{"events":[Event]}`。仅返回本人未确认且当前可读事件 |
| `POST /v1/inbox/{event_id}/ack` | 无正文 | `{"event_id":42}`。重复确认同一条已授权事件成功 |
| `GET /v1/events` | Bearer 成员 token，`Accept: text/event-stream` | SSE。就绪和保活可用注释。变更通知为 `event: inbox_changed`，`data: {}` |

## 事务与错误

- 成员按 `(owner_id, harness_instance_id, session_key)` 唯一，同一 IP 或同一个 harness 实例可有多个会话。稳定绑定和 token 由本机开发者授权配置。
- 确认当前公告前须通过读取入口取得该版本，未读取时返回 `board_read_required`。读发房间消息及领取房间消息事件要求当前公告已确认。未确认时，历史和发送返回 `board_ack_required`，收件箱暂缓返回该房间条目。收件箱分页游标只用于本轮扫描，重连和新提示均可从 0 重扫未确认项。
- 发送在同一个事务中做授权、保存消息并为当时的其他房间成员各写一条事件。指定目标收到 `mention`，其余成员收到 `message`，发件人通过历史读取自己的消息。相同发件人、相同 `client_message_id` 和相同房间、文本、@ 目标重试返回原消息。变更任一字段则冲突。
- SSE 只提供提示，重连或丢帧后从持久收件箱恢复。成功响应表示 Server 已接受请求。收到 SSE 不等于消息已确认或某个 harness 已唤醒。
- 错误 JSON 统一为 `{"error":{"code":"...","message":"..."}}`。无效输入为 422 `invalid_request`，无效凭证为 401 `unauthorized`，无访问权或资源不存在为 404 `not_found`，当前公告未确认时为 409 `board_ack_required`，尚未读取当前公告便提交确认为 409 `board_read_required`，重复键或版本冲突为 409 `conflict`。无效 @ 目标为 422 `invalid_mention`。错误正文不回显凭证。
