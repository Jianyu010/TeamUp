# Direct Messages（私聊）

**契约提案，未实现。** 跟踪 [Direct messages #3](https://github.com/Jianyu010/TeamUp/issues/3)，与 [conversation history #6](../specs/conversation-history.md) 协同；产品行为见 [collaboration-surfaces](../specs/collaboration-surfaces.md)。提供方为 Server Gateway（业务状态归 Collaboration），消费方为 Client、本机 MCP 插件与 CLI。本文只新增 DM 契约：已发布的房间接口（[Client–Server v1](client-server-v1.md)、[MCP–Client v1](mcp-client-v1.md)）保持不变，仅做本文显式声明的加法扩展。

## 定位与边界

- DM 是同一团队内两名成员之间的唯一会话，按无序成员对唯一（实现建议存 `min`/`max` 规范序）。重复打开返回同一会话；A→B 与 B→A 是同一条。
- 找同件沿用现有 `teamup_find_members`：先按角色找，再用返回的 `member_id` 打开 DM。
- 只有会话的两位参与者可以发送、读取和领取该 DM 的事件，每次操作都按当前 token 重新校验参与资格。团队其他成员无法查询该 DM 或其存在。
- DM 没有公告（board）：发送与读取不要求公告确认。
- DM 独立于 Room Directory、房间搜索与 Control Console：不进入房间列表、搜索或控制台；管理员房间 transcript 授权不适用于 DM。
- 本期不提供关闭、退出或转让 DM 的操作，不提供两人以上的群聊；成员停用对 DM 的影响随停用契约另行评审。

## 数据形状

| 名称 | JSON 字段 |
| --- | --- |
| DM | `dm_id` UUID 字符串，`peer_member_id` UUID 字符串 |
| DM Message | `message_id` 正整数，`dm_id`，`sender_member_id`，`client_message_id`，`text`，`sent_at` RFC 3339 UTC 时间字符串（DM 行均为新写入，必非空） |
| Event（扩展） | 在现有 `event_id`、`kind`、`message` 之上，`kind` 增加 `dm`；为 `dm` 时 `message` 是 DM Message |
| Notice（扩展） | 在现有字段之上增加 `dm_id`（UUID 或 null）：房间事件 `dm_id` 为 null，DM 事件 `room_id` 为 null |

- `message_id` 与房间消息共用同一个全局递增分配器：跨房间与 DM 全局唯一、只增不复用，会话内按提交顺序严格递增。
- 房间 Message 的线上形状本期不变；是否同样向房间读取暴露 `sent_at` 由 #6 决定（见审议项）。

## 操作

成功均返回 HTTP 200。分页沿用 v1：`after` 从 0 开始，`limit` 默认 50、最大 100，按 `message_id` 递增，`after` 排除小于或等于该 ID 的记录。

| 方法和路径 | 请求 | 成功结果 |
| --- | --- | --- |
| `POST /v1/dms` | Bearer 成员 token。JSON `{"member_id":"..."}` | DM。打开或复用与该成员的无序对会话；重复调用幂等返回同一 DM。目标不存在或为本人 → 422 `invalid_peer` |
| `POST /v1/dms/{dm_id}/messages` | `client_message_id`、`text` | DM Message。仅限参与者 |
| `GET /v1/dms/{dm_id}/messages` | `after`、`limit` | `{"messages":[DM Message]}`。仅限参与者；保留边界生效时附 `oldest_available_message_id`（见保留） |

MCP 工具与 IPC 方法（加法，现有工具不动）：

| MCP 工具 | IPC 方法 | 参数（空参数传 `{}`） | 结果 |
| --- | --- | --- | --- |
| `teamup_open_dm` | `open_dm` | `member_id` | DM |
| `teamup_send_dm` | `send_dm` | `dm_id`、`client_message_id`、`text` | DM Message |
| `teamup_read_dm` | `read_dm` | `dm_id`、可选 `after`、`limit` | `{"messages":[DM Message]}` |

SSE 不变，仍为 `event: inbox_changed`。收件箱领取与确认入口不变。对端不需要事先打开过 DM：既可从收件事件的内嵌消息取得 `dm_id`，也可用 `teamup_open_dm` 换取同一会话。

## 校验

- `member_id`、`dm_id` 用标准带连字符 UUID。`client_message_id` 非空且最多 128 字符；`text` 非空且最多 8,000 字符，纯空白无效。JSON 请求拒绝未声明字段；DM 发送没有 `mention_member_id`。
- 打开目标必须是已登记成员且非本人，否则 422 `invalid_peer`。成员名录本身经 `find_members` 公开，该错误不披露更多信息。

## 事务、失败与不披露

- 打开：以无序对唯一约束在单事务内 create-or-get；并发打开同一对（含两个方向）都得到同一 `dm_id`。打开本身不产生事件或通知。
- 发送：单事务内完成参与校验、写入消息行（含 `sent_at`）、为对端写一条 `kind: "dm"` 收件事件。发件人不写事件，经历史读取自己的消息。
- 幂等：同一发件人、同一 `client_message_id`、同一 DM 且相同 `text` 的重试返回原消息；变更任一字段返回 409 `conflict`。幂等键作用域为（会话，发件人，`client_message_id`）：房间与 DM 互不冲突，同一 `client_message_id` 可分别用于两边。
- 错误沿用 v1 码表：422 `invalid_request`、401 `unauthorized`、404 `not_found`、409 `conflict`、422 `invalid_peer`，不新增码。错误正文不回显凭证或消息正文。
- 不披露：非参与者访问任何 `/v1/dms/...` 路由一律 404 `not_found`，与不存在的 `dm_id` 不可区分；DM 事件只进入两位参与者的收件箱；Server 应用日志只记操作元数据，不落 DM 正文。控制台、房间搜索与 Room Directory 的任何入口都不返回 DM 数据。

## 通知与重连

- 对端经现有 `GET /v1/inbox` 领取 `kind: "dm"` 事件（内嵌完整 DM Message），用现有 ack 入口确认；重复确认成功。SSE 只作提示，断连后按未确认事件补收。
- Client 对 `dm` 事件按 `mention` 同等方式持久登记本地 Notice（含 `dm_id`），再确认 Server 事件；重复投递不增加记录，`teamup-client notifications` 与 MCP `teamup_notifications` 均可读取。
- 重连后需要上下文时，用 `teamup_read_dm` 从最后已知 `message_id` 或 0 起按页回读。收件确认只证明 Client 已收，不等于对端 Agent 已读。

## 跨会话引用

- 本期不新增结构化引用字段；跨会话引用以文本内引用 `message_id` 表达。
- `message_id` 全局唯一，引用无歧义。解析引用只能经该消息所属会话自己的读取入口，按该会话当前授权判定：DM 参与者引用房间消息时，未加入该房间的对端经房间路由得到 404；房间成员引用 DM 消息时，非参与者同样 404。
- 引用不复制内容：每个会话的 transcript 只含自己的消息，读取权限不随引用传递。

## 保留与升级

- DM 保留与房间保留分开配置（建议 `TEAMUP_DM_RETENTION_DAYS` / `--dm-retention-days`；时长与默认值待 owner 与 #6 商定，未配置即不限期）。到期只作用于 DM：正文与未投递事件一并移除，不影响房间；分配器不复用被清除的 `message_id`。
- 到期后读取：若 `after` 落在被清除范围，响应附 `oldest_available_message_id`，调用方据此得知部分历史已过期。
- 存储指引（实现期定稿）：`dms` 表以无序对唯一；`messages` 增加 `dm_id`（与 `room_id` 恰一非空）和 `sent_at`（存量房间行为 NULL，展示为未知；新写入必填）。房间现有读取边界与授权不变，全局分配器续用不重置。
- 升级顺序：契约评审通过后 Server 与 Client/MCP 同一版本交付（v1 无版本协商）。`kind: "dm"` 与 Notice 的 `dm_id` 为加法扩展，旧房间工具语义不变；MCP 协作指引中“当前版本未提供私聊工具”的降级提示随本能力移除。
- 后续若给操作员 DM 读取权，须另立显式授权与审计，不在本期。

## 实施步骤（待契约批准后）

1. Collaboration 迁移：`dms` 表、`messages.dm_id`/`sent_at` 列与约束，复用全局分配器。
2. Gateway：三条路由、`invalid_peer` 映射、Event `dm`、日志元数据化。
3. Client：`open_dm`/`send_dm`/`read_dm` IPC 与 `dm` 事件的本地 Notice 登记。
4. MCP：三个工具 schema 与协作 skill 文案更新；共享文档索引随实现更新。
5. DM 到期清除任务与 `oldest_available_message_id` 边界返回。

## 测试与验收

- 打开：重复打开、双向打开、并发打开同一对 → 同一 `dm_id`；对本人或未知成员 → 422 `invalid_peer`。
- 参与者隔离：第三成员对任意 `dm_id`（真实或随机）读取或发送 → 404；其收件箱永不含他人 DM 事件；`find_members` 仍返回成员档案，会话存在性不泄露。
- 发送：文本上限、纯空白、未声明字段拒绝；同键同文重试返回原消息；改文 → 409；同一 `client_message_id` 在房间与 DM 互不冲突。
- 通知与重连：对端收到 `dm` 事件并确认；SSE 断开后补收；Client 本地 Notice 含 `dm_id` 且去重；确认后经 `teamup_read_dm` 回读与事件一致。
- 引用：DM 内引用房间消息，非房间成员的对端读取 → 404；房间内引用 DM 消息，非参与者 → 404。
- 控制台与搜索：任何响应不含 DM 数据；持有房间 transcript 授权的操作员也拿不到 DM。
- 回归：既有房间路由、事件与通知行为全部不变；迁移后房间历史顺序与加入边界不变。
- 无公告要求：未走过任何公告流程的成员对可直接使用 DM。

## 待 owner 审议的选择

1. Event `kind: "dm"` 与 Notice `dm_id` 的加法扩展 — MCP–Client v1 owner。
2. DM 与房间共用全局 `message_id` 分配器（单表 canonical transcript）— Collaboration 与 #6 owner。
3. DM Message 暴露 `sent_at` 而房间 Message 暂不暴露的对齐时点 — #6 owner。
4. DM 保留时长、配置名与 `oldest_available_message_id` 边界字段形态 — #6 owner。
5. Client 将 `dm` 事件按 `mention` 同级持久通知 — Client owner。
6. 打开 DM 不通知对端（存在性仅参与者可见），以及停用/撤销对 DM 的影响 — 产品 owner。
