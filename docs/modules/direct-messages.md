# Direct Messages（私聊）

**契约已按 #3 协调定稿；Client/MCP 侧已随本契约交付，Server 侧（`dms` 存储、三条路由、收件箱 `kinds` 与 `kind: "dm"` 事件）已由 #3 Server 工作树交付；DM 保留的存储与读取切片（v5 水位列、`expire_due_dms` 批量清理入口、读取边界元数据）已由 #6 DM 保留存储工作树交付，共享维护调度与 `--dm-retention-hours` 生产配置已由 #6 保留运行时统一装配。** 跟踪 [Direct messages #3](https://github.com/Jianyu010/TeamUp/issues/3)，与 [conversation history #6](../specs/conversation-history.md) 协同；产品行为见 [collaboration-surfaces](../specs/collaboration-surfaces.md)。提供方为 Server Gateway（业务状态归 Collaboration），消费方为 Client、本机 MCP 插件与 CLI。本文只新增 DM 契约：已发布的房间接口（[Client–Server v1](client-server-v1.md)、[MCP–Client v1](mcp-client-v1.md)）保持不变，仅做本文显式声明的加法扩展。

## 定位与边界

- DM 是同一团队内两名成员之间的唯一会话，按无序成员对唯一（实现建议存 `min`/`max` 规范序）。重复打开返回同一会话；A→B 与 B→A 是同一条。
- 找同伴沿用现有 `teamup_find_members`：先按角色找，再用返回的 `member_id` 打开 DM。
- 只有会话的两位参与者可以发送、读取和领取该 DM 的事件。发起者一律由当前 token 推导，客户端字段不能替换发起方身份；每次操作都按当前 token 重新校验参与资格。团队其他成员无法查询该 DM 或其存在。
- DM 没有公告（board）：发送与读取不要求公告确认。
- DM 独立于 Room Directory、房间搜索与 Control Console：不进入房间列表、搜索或控制台；管理员房间 transcript 授权不适用于 DM。
- 本期不提供关闭、退出或转让 DM 的操作，不提供两人以上的群聊。成员停用的影响已定：打开时目标成员所属账号已停用按未知同伴处理（422 `invalid_peer`）；既有会话的有效参与者始终可回读已存历史；向所属账号已停用的对端发送按 404 `not_found` 处理（见事务与失败）。

## 数据形状

| 名称 | JSON 字段 |
| --- | --- |
| DM | `dm_id` UUID 字符串，`peer_member_id` UUID 字符串 |
| DM Message | `message_id` 正整数，`dm_id`，`sender_member_id`，`client_message_id`，`text`，`created_at` RFC 3339 UTC 时间字符串（DM 行均为新写入，必非空） |
| Event（扩展） | 在现有 `event_id`、`kind`、`message` 之上，`kind` 增加 `dm`；为 `dm` 时 `message` 是 DM Message |
| Notice（扩展，本机） | DM 的本地 Notice 为类型化五字段 `{event_id, kind:"dm", dm_id, message_id, sender_member_id}`，不含正文；既有 `mention` 的本地 Notice 保持原四字段 `{event_id, room_id, message_id, sender_member_id}` 输出不变 |

- `message_id` 与房间消息共用同一个全局递增分配器：跨房间与 DM 全局唯一、只增不复用，会话内按提交顺序严格递增。房间与 DM 是 Collaboration 内两个独立的 canonical record 家族，每条记录只存一份正文；投递事件与消息一样共用 #6 的全局 delivery 分配器。
- 房间 Message 的线上形状本期不变；是否同样向房间读取暴露时间戳已由 #6 落定：随 [会话历史、搜索与保留](conversation-history.md) 的增量，房间 Message 也暴露 `created_at`（见已议定）。

## 操作

成功均返回 HTTP 200。分页沿用 v1：`after` 从 0 开始，`limit` 默认 50、最大 100，按 `message_id` 递增，`after` 排除小于或等于该 ID 的记录。

| 方法和路径 | 请求 | 成功结果 |
| --- | --- | --- |
| `POST /v1/dms` | Bearer 成员 token。JSON `{"member_id":"..."}` | DM。打开或复用与该成员的无序对会话；重复调用幂等返回同一 DM。目标不存在、为本人或所属账号已停用 → 422 `invalid_peer` |
| `POST /v1/dms/{dm_id}/messages` | `client_message_id`、`text` | DM Message。仅限参与者 |
| `GET /v1/dms/{dm_id}/messages` | `after`、`limit` | `{"messages":[DM Message]}`。仅限参与者；保留边界生效时附 `expired_through_message_id`（已被清除的最大 `message_id`，含端点）与 `history_truncated`（布尔），其余字段原样传递 |

MCP 工具与 IPC 方法（加法，现有工具不动）：

| MCP 工具 | IPC 方法 | 参数（空参数传 `{}`） | 结果 |
| --- | --- | --- | --- |
| `teamup_open_dm` | `open_dm` | `member_id` | DM |
| `teamup_send_dm` | `send_dm` | `dm_id`、`client_message_id`、`text` | DM Message |
| `teamup_read_dm` | `read_dm` | `dm_id`、可选 `after`、`limit` | `{"messages":[DM Message]}`，可带到期元数据 |

SSE 不变，仍为 `event: inbox_changed`。收件箱领取与确认入口不变。对端不需要事先打开过 DM：既可从收件事件的内嵌消息取得 `dm_id`，也可用 `teamup_open_dm` 换取同一会话。

### 收件箱 `kinds` 参数（加法）

- `GET /v1/inbox` 新增可选查询参数 `kinds`：逗号分隔的事件种类 CSV 子集，本期取值为 `message`、`mention`、`dm`。新 Server 在 `kinds` 缺省时只返回 `message`/`mention`；显式给出子集（含 `dm`）时由该子集控制返回形状。
- 新 Client 每次拉取都显式请求 `kinds=message,mention,dm`，沿用现有 `after`/`limit` 字段。旧 Server 忽略未知查询参数、只返回既有事件，双方保持兼容。
- #4 将把 `member_joined` 纳入该 `kinds` 请求并增加对应的 Notice 变体；该参数是收件箱唯一的加法扩展入口。

## 校验

- `member_id`、`dm_id` 用标准带连字符 UUID，复用既有 v1 UUID 校验器。`client_message_id` 非空且最多 128 字符；`text` 非空且最多 8,000 字符，纯空白无效，长度边界复用既有校验器。JSON 请求拒绝未声明字段；DM 发送没有 `mention_member_id`。
- 打开目标必须是已登记、账号未停用且非本人的成员，否则 422 `invalid_peer`。成员名录本身经 `find_members` 公开，该错误不披露更多信息。

## 事务、失败与不披露

- 打开：以无序对唯一约束在单事务内 create-or-get；并发打开同一对（含两个方向）都得到同一 `dm_id`。打开本身不产生事件或通知。
- 发送：单事务内完成参与校验、写入消息行（含 `created_at`）、为对端写一条 `kind: "dm"` 收件事件。发件人不写事件，经历史读取自己的消息。
- 幂等：同一发件人、同一 `client_message_id`、同一 DM 且相同 `text` 的重试返回原消息；变更任一字段返回 409 `conflict`。幂等键作用域为（DM，发件人，`client_message_id`），房间沿用现有房间键模型：房间与 DM 互不冲突，同一 `client_message_id` 可分别用于两边。
- 失败沿用 v1 码表：422 `invalid_request`、401 `unauthorized`、404 `not_found`、409 `conflict`、422 `invalid_peer`，不新增码。向所属账号已停用的对端发送按 404 `not_found` 处理。错误正文不回显凭证或消息正文。
- 不披露：非参与者访问任何 `/v1/dms/...` 路由一律 404 `not_found`，与不存在的 `dm_id` 不可区分；既有会话的有效参与者始终可回读已存历史。DM 事件只进入两位参与者的收件箱；Server 应用日志只记操作元数据，不落 DM 正文。控制台、房间搜索与 Room Directory 的任何入口都不返回 DM 数据。

## 通知与重连

- 对端经现有 `GET /v1/inbox`（Client 显式携带 `kinds=message,mention,dm`）领取 `kind: "dm"` 事件（内嵌完整 DM Message），用现有 ack 入口确认；重复确认成功。SSE 只作提示，断连后按未确认事件补收。
- Client 对 `dm` 事件按 `mention` 同等方式持久登记类型化本地 Notice（`event_id`、`kind`、`dm_id`、`message_id`、`sender_member_id`，无正文），再确认 Server 事件。持久化先于确认，按绑定和 `event_id` 去重，重复投递不增加记录；类型化 DM 行与既有行共存于同一本机 Notices 存储，存量四字段行原样保留，重启迁移后旧通知全部可读。`teamup-client notifications` 与 MCP `teamup_notifications` 均可读取两类 Notice。
- 收件确认与保留到期竞争、Server 对已到期事件返回 404 `not_found` 时，Client 视为已清除并继续补收；其余确认失败沿用既有处理（退避重试整个拉取）。
- 重连后需要上下文时，用 `teamup_read_dm` 从最后已知 `message_id` 或 0 起按页回读；响应可带 `expired_through_message_id` 与 `history_truncated` 告知部分历史已过期，Client 原样透传。收件确认只证明 Client 已收，不等于对端 Agent 已读；Client 与 MCP 不提供自动唤醒行为。

## 跨会话引用

- 本期不新增结构化引用字段；跨会话引用以文本内引用 `message_id` 表达。
- `message_id` 全局唯一，引用无歧义。解析引用只能经该消息所属会话自己的读取入口，按该会话当前授权判定：DM 参与者引用房间消息时，未加入该房间的对端经房间路由得到 404；房间成员引用 DM 消息时，非参与者同样 404。
- 引用不复制内容：每个会话的 transcript 只含自己的消息，读取权限不随引用传递。

## 保留与升级

- DM 保留与房间保留分开配置：`teamup-server --dm-retention-hours N`，取值为 1–876000（含）之间的整数小时，更大或非正值在启动时拒绝；省略表示不自动删除 DM，也不启动 DM 侧清理。到期只作用于 DM：正文与未投递事件一并移除，不影响房间；分配器不复用被清除的 `message_id`。配置与房间的 `--room-retention-hours` 复用同一维护组件与运行节奏（启动即清、批间无间隔、15 秒周期），两项年龄各自独立校验后装进同一个维护循环，单轮内两个已启用的清理都执行（#6 保留运行时交付）。
- 清理入口（#6 DM 存储切片交付）：Collaboration 提供 `expire_due_dms(cutoff)`，`cutoff` 为带时区的显式 UTC 截止时间，naive 时间拒绝。方法在一个写事务内删除至多一批（500 条）到期 DM 消息（跨会话按 `message_id` 升序取最早的到期行），返回本批删除量与是否仍有到期记录；仍有到期记录时由调用方立即继续下一批。到期条件是该行 `created_at` 可按 RFC 3339 解析出带时区时刻且不晚于 `cutoff`，按真实 UTC 时刻比较，兼容带与不带小数秒的记录；`created_at` 为空或不可解析的行不删除。同一事务内先删除引用这些消息的收件事件（含已确认与未确认），再删除规范消息行，然后把每个受影响会话的 `expired_through_message_id` 推进到 max(既有水位, 本批已删除的最大消息 ID)。`message_id` 与 `event_id` 计数器不受删除影响；`dm_id` 为空的房间行永不被该入口选中。
- 水位存储（v4 → v5）：`dms` 表新增可空列 `expired_through_message_id`，NULL 表示该会话从未清理。v4 数据库原位升级（一条 `ALTER TABLE`），现有 `dm_id`、消息、事件、计数器与重试键全部保持；全新数据库直接建于 v5。
- 到期后读取：水位非空后，参与者读取响应追加 `expired_through_message_id`（已被清除的最大 `message_id`，含端点）与 `history_truncated`（布尔，表示请求游标钳位到不小于 0 后仍落后于水位）。水位为 NULL（从未清理）时响应保持原有精确形状，即使正文为空也不追加字段；水位非空时即使正文已全部清空，两个边界字段仍随空 `messages` 返回。字段仅当前授权参与者可见。Client 对该元数据原样透传。
- 已到期事件不再投递；对已删除事件的收件确认沿用现有 404 `not_found`，其余确认行为不变。清理后同键重发生成新消息与新 `message_id`（重试唯一键随行删除），跨房间与 DM 的既有键模型不变。
- 存储定稿（#3 Server 工作树按实现修订）：`dms` 表以 `dm_id` 为主键，按 `min`/`max` 规范序存成员对，并用 `(member_low, member_high)` 唯一约束保证一对一会话；`messages` 增加 `dm_id`，与 `room_id` 恰一非空（`created_at` 列已随 #6 存储基线就位，存量房间行为 NULL，展示为未知；DM 行均为新写入，必非空）。重试唯一键按记录家族分域：房间行维持 `(sender_member_id, client_message_id)` 唯一，DM 行为 `(dm_id, sender_member_id, client_message_id)`，因此同一 `client_message_id` 可分别用于房间与 DM；`inbox_events.kind` 约束扩展 `dm`。房间现有读取边界与授权不变，全局分配器续用不重置。
- 升级顺序：Client 显式携带 `kinds` 请求，旧 Server 忽略未知参数、只回既有事件；新 Server 在 `kinds` 缺省时只回 `message`/`mention`，旧 Client 行为不变。Server 与 Client/MCP 可先后部署，`kinds` 即兼容边界（v1 无版本协商）。`kind: "dm"` 与类型化 DM Notice 为加法扩展，现有房间工具语义不变；MCP 协作指引保留“当前工具目录缺少私聊工具”的降级提示，用于与旧版本混布时引导已授权的私下交接。
- 后续若给操作员 DM 读取权，须另立显式授权与审计，不在本期。

## 实施步骤

1. Collaboration 迁移：`dms` 表、`messages.dm_id` 与恰一非空约束、按家族分域的重试唯一键、`inbox_events.kind` 扩展 `dm`，复用全局分配器。（#3 Server 工作树，已交付）
2. Gateway：三条路由、`invalid_peer` 映射、Event `dm`、日志元数据化。（#3 Server 工作树，已交付）
3. Client：`open_dm`/`send_dm`/`read_dm` IPC、`kinds` 拉取与 `dm` 事件的类型化本地 Notice 登记。（本变更交付）
4. MCP：三个 DM 工具 schema 与协作 skill 文案更新；共享文档索引随实现更新。（本变更交付）
5. DM 与房间的保留水位/到期清除任务与 `expired_through_message_id`/`history_truncated` 边界返回。（全部交付：v5 水位列、`expire_due_dms` 批量清理入口与 DM 读取边界返回由 #6 DM 保留存储工作树交付；共享维护调度与 `--dm-retention-hours` 配置接入由 #6 保留运行时交付）

## 测试与验收

Client/MCP 侧（本变更，`uv run --no-sync pytest -q`）：

- IPC 路由与凭证：三个 DM IPC 方法按上表映射到 HTTP 路由、正文与查询；未批准绑定 `unauthorized`，非法参数与未知方法 `invalid_request`；`invalid_peer`、`not_found` 等 Server 错误按 code 透传。
- 旧 Server 兼容：忽略 `kinds` 的旧 Server 仍只回既有事件，Client 正常补收与确认；未实现 DM 路由的旧 Server 对 DM 调用返回 404 并透传。
- 类型化 Notice：`dm` 事件持久化为五字段本地 Notice（无正文），先持久化后确认，按绑定和 `event_id` 去重；stderr 只输出 Notice 字段；重启后存量（含旧版四字段）通知全部可读；确认竞争返回 `not_found` 时继续补收。
- MCP schema：三个 DM 工具按上表暴露，参数校验复用现有 UUID 与长度边界。
- 回归：既有房间路由、事件、通知与 CLI/MCP 查询行为全部不变。

Server 侧（#3 Server 工作树验收，`uv run --no-sync pytest -q`，由 `server/tests/test_direct_messages.py` 与 `tests/test_loop.py` 覆盖）：

- 打开：重复打开、双向打开、并发打开同一对 → 同一 `dm_id`；对本人、未知成员或所属账号已停用的成员 → 422 `invalid_peer`。
- 参与者隔离：第三成员对任意 `dm_id`（真实或随机）读取或发送 → 404；其收件箱永不含他人 DM 事件；`find_members` 仍返回成员档案，会话存在性不泄露。
- 发送：文本上限、纯空白、未声明字段拒绝；同键同文重试返回原消息；改文 → 409；同一 `client_message_id` 在房间与 DM 互不冲突；对端账号停用后发送 → 404，既有参与者仍可回读历史。
- 引用：DM 内引用房间消息，非房间成员的对端读取 → 404；房间内引用 DM 消息，非参与者 → 404。
- 控制台与搜索：任何响应不含 DM 数据；持有房间 transcript 授权的操作员也拿不到 DM。
- 到期（#6 DM 存储切片验收，直接调用 Collaboration 公开入口 `expire_due_dms`）：单事务内到期 DM 正文与引用事件一并删除且房间行不受影响；水位推进到本批最大已删除 ID 并跨重启持久；空历史会话在清理后仍返回边界字段，从未清理的会话保持精确旧响应；过期事件确认返回 404 `not_found` 且不再投递；计数器与普通确认不受影响，清理后新消息/新事件 ID 不复用；v4 样本带与不带小数秒的时间戳升级后按真实 UTC 时刻比较，空或不可解析时间戳的行保留；仅当前参与者可见边界字段。`--dm-retention-hours` 的配置解析与共享调度验收已随 #6 保留运行时执行（真实启动标志下房间内容不受影响，仅 DM 到期清除）。
- 回归：既有房间路由、事件与通知行为全部不变；迁移后房间历史顺序与加入边界不变。
- 无公告要求：未走过任何公告流程的成员对可直接使用 DM。

## 已议定（#3 协调记录，2026-11-24）

原待 owner 审议项由 #3 协调方裁定如下，实现与本文件一致：

1. Event `kind: "dm"` 与类型化 DM Notice（含 `dm_id`，无正文）为加法扩展，`mention` 四字段本地输出不变 — MCP–Client v1。
2. DM 与房间共用全局 `message_id` 与 delivery 分配器；房间与 DM 为 Collaboration 内两个独立 canonical record 家族，每条记录一份正文 — Collaboration 与 #6。
3. DM Message 时间戳定名 `created_at`（RFC 3339 UTC）；房间 Message 是否暴露时间戳由 #6 另行决定。
4. DM 保留配置为 `--dm-retention-hours`；到期边界以 `expired_through_message_id` 与 `history_truncated` 返回。
5. Client 将 `dm` 事件按 `mention` 同级持久通知：先持久化后确认，与既有 Notices 存储共存并保留存量行。
6. 打开 DM 不通知对端（存在性仅参与者可见）；停用影响按 422 `invalid_peer`（打开）与 404 `not_found`（发送）执行。
