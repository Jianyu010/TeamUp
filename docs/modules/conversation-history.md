# 会话历史、搜索与保留

对应 [#6](https://github.com/Jianyu010/TeamUp/issues/6)。本文记录已协调的增量契约。实现分为房间历史与搜索、房间清理、私聊清理及控制台接入。下文的验收项是实现目标。

## 所有权

Collaboration 在 SQLite 保存规范消息和收件箱引用。Gateway 提供 HTTPS API，Client 和 MCP 转发经过鉴权的结果。控制台从同一房间记录读取，使用独立的房间正文授权。确认收件只更新投递状态，不删除消息。房间搜索直接查规范房间记录。私聊不进入目录、房间搜索或控制台。

## 记录和迁移

- Server 为每条新消息记录 UTC RFC 3339 `created_at`。升级时保留现有消息 ID、顺序和内容。旧消息没有时间戳，`created_at` 返回 null。
- 消息 ID 和收件事件 ID 分别全局递增。同一写事务内分配 ID、更新消息和写入相关投递。持久分配器从升级前的最大 ID 开始，删除记录以后仍不复用 ID。
- 新建房间可明确选择 `retained` 或 `since_joining`。现有房间及省略该字段的旧版建房请求都使用 `since_joining`。新工作流可明确选择 `retained`，并通过 [房间目录](../specs/collaboration-surfaces.md#room-directory) 在加入前展示当前策略。房间建立后，策略在本阶段保持固定。

## 房间读取和搜索

每次请求都以当前成员资格、账号状态和当前公告确认为准。`retained` 允许成员在确认公告后读取保留的加入前消息。`since_joining` 只允许读取本次加入后提交的消息。服务端先执行这些检查，再过滤或分页。旧游标不授予额外访问权。

| 入口 | 增量契约 |
| --- | --- |
| `POST /v1/rooms` | 请求增加可选 `history_policy`，值为 `retained` 或 `since_joining`。成功结果携带已保存的策略。 |
| `POST /v1/rooms/{room_id}/messages` | Message 增加 `created_at`。保留现有消息 ID 和重试语义。 |
| `GET /v1/rooms/{room_id}/messages` | 现有 `after` 低位排他边界，新增可选 `before` 高位排他边界。给出 `before` 时从其之前取最近一页，按 ID 递增展示；大于 SQLite 最大 ID 的 `before` 作为无上界处理，不排除合法的最大 ID。默认 limit 50，最大 100。Message 增加 `created_at`。 |
| `GET /v1/rooms/{room_id}/search` | `q` 为 1–200 个字符且不能全为空白。对房间内已获准的消息按大小写折叠做字面子串匹配（不使用 FTS），授权先于过滤和分页。按 ID 递增，`after` 和 `limit` 同历史；结果 `{"matches":[Match]}`，空页合法。 |
| Match | `message_id`、`sender_member_id`、`created_at` 和最多 200 个原文字符的普通文本 `snippet`。片段引用原始正文，保留原大小写与 Unicode（如 Straße 保持 Straße），围绕首次折叠匹配截取并包含被匹配的原始字符；折叠扩展（如 ß、İ）的偏移安全映射回原文字符。授权房间的过滤与限量先在 SQL 内完成，再为结果行生成片段；正文不随结果复制。可用稳定消息 ID 读取上下文。 |

MCP/IPC 在既有 `teamup_create_room` / `create_room` 增加可选 `history_policy`，在 `teamup_read_messages` / `read_messages` 增加可选 `before`，并增加 `teamup_search_room` / `search_room`，参数为 `room_id`、`q`、可选 `after` 与 `limit`。按 v1 返回验证、授权及公告错误。API 和 IPC 均使用已发布成员凭证边界。

## 到期与恢复

- 房间和私聊独立配置小时数，分别使用 `--room-retention-hours` 和 `--dm-retention-hours`。两者都必须是 1–876000（含）之间的整数小时，更大或非正值在启动时拒绝。省略表示不自动删除，也不启动任何清理调度。无时间戳的升级记录保留。到期以 Collaboration 清理事务的提交为准，配置启用后在启动和周期维护时运行。
- Collaboration 提供唯一的清理入口 `expire_due(max_age, now=None)`：在一个写事务内删除至多一批（500 条）到期房间消息，并返回本批删除量与是否仍有到期记录；消息 ID 与收件事件 ID 计数器不受删除影响。消息到期条件是 `created_at + max_age ≤ 本次事务取定的当前 UTC 时间`，按真实 UTC 时间比较，兼容带与不带小数秒的 RFC 3339 记录；`created_at` 为空或不可解析的记录不删除。不复制历史或正文。
- 独立的维护组件只负责调度：以守护线程在启动后立即调用一次清理，返回仍有到期记录时不等待、立即继续下一批直到清完，之后每 15 秒重新检查一次；停止时立即唤醒并退出。调度不阻塞 SSE，也不在两项配置都省略时运行。组合根只装配该组件。房间与私聊到期复用同一维护组件与水位、批量、比较语义：`--room-retention-hours` 与 `--dm-retention-hours` 各自独立校验后装进同一个维护循环，单轮内两个已启用的清理都执行，不因一方仍有到期记录而跳过或延迟另一方，任一方仍有到期记录即立即继续下一批。
- Collaboration 在 rooms 表持久保存每个房间的 `expired_through_message_id` 水位列，NULL 表示该房间从未清理。清理在同一事务内删去到期消息和引用它的全部投递（含已确认与未确认），并把水位推进到已删除的最大消息 ID。空历史仍保留这项边界。最早尚存的消息不能证明发生过删除。
- 对已发生删除的房间，授权历史和搜索响应追加 `expired_through_message_id` 与 `history_truncated`；从未清理的房间保持原有响应形状。`history_truncated` 表示请求游标与授权的加入位置合并后，仍落后于本次请求覆盖范围内的到期边界：历史与搜索的开放读为合并游标 < 水位，历史带 `before` 时为合并游标 < min(水位, before − 1)。该字段只表达续读游标与到期边界的差距，不承诺边界之下具体删除了哪些消息。旧客户端可以继续读取现有 `messages`/`matches` 数组。
- 收件箱重连仍补收可授权的未确认事件。已经到期的事件不再投递，确认已到期事件沿用现有 `not_found` 响应。消息存在时，同键同内容的授权重试返回原消息。清理后再次使用同键可创建消息，用新的 ID。
- [私聊契约](direct-messages.md) 定义参与权限和操作。私聊复用时间戳、ID、边界和清理语义。[控制台规格](../specs/collaboration-surfaces.md#control-console) 定义房间正文授权和无正文审计。控制台通过 Collaboration 的规范房间查询读取同一批消息。

## 验收

- 通过公开接口验证两个房间策略、公告门槛、房间范围、带文字上下文的搜索和基于 ID 的分页。
- 使用 v0 与账号试点数据库样本验证升级。原历史位置和旧消息 ID 保留，时间未知如实返回。
- 使用独立的房间和 DM 到期配置，检查历史、搜索和相关未收投递一致变化，空历史仍能报告缺失，后续消息与事件 ID 均不复用。
- 按已记录的运行节奏（启动即清、批间无间隔、15 秒周期、500 条一批）验证清理在真实 UTC 时间比较下到达并提交，省略配置时不产生任何清理或调度。
- 验证重试、确认收件、越权查询和已撤权后的旧游标；记录可复现的测试环境与结果。
