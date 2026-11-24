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
| Match | `message_id`、`sender_member_id`、`created_at` 和最多 200 个原文字符的普通文本 `snippet`。片段引用原始正文，保留原大小写与 Unicode（如 Straße 保持 Straße），围绕首次折叠匹配截取并包含被匹配的原始字符；大小写折叠可把查询字符扩展为多个字符（如 ß 折叠为 ss），使一次匹配映射回超过 200 个原文字符，此时片段从该匹配的原文起点引用至 200 个原文字符的上限，未引用的尾部不随片段返回。折叠扩展（如 ß、İ）的偏移安全映射回原文字符。授权房间的过滤与限量先在 SQL 内完成，再为结果行生成片段；正文不随结果复制。可用稳定消息 ID 按当前授权读取完整正文与上下文。 |

MCP/IPC 在既有 `teamup_create_room` / `create_room` 增加可选 `history_policy`，在 `teamup_read_messages` / `read_messages` 增加可选 `before`，并增加 `teamup_search_room` / `search_room`，参数为 `room_id`、`q`、可选 `after` 与 `limit`。按 v1 返回验证、授权及公告错误。API 和 IPC 均使用已发布成员凭证边界。

## 到期与恢复

- 房间和私聊独立配置小时数，分别使用 `--room-retention-hours` 和 `--dm-retention-hours`。省略表示不自动删除。无时间戳的升级记录保留。到期以 Collaboration 清理事务的提交为准，配置启用后在启动和周期维护时运行。
- 清理在一个事务内删去到期消息和引用它的投递，推进会话自己的 `expired_through_message_id` 到已删除的最大消息 ID。空历史仍保留这项边界。最早尚存的消息不能证明发生过删除。
- 对已发生删除的会话，授权历史和搜索响应追加 `expired_through_message_id` 与 `history_truncated`。后者表示请求游标与授权的加入位置合并后，仍落后于这次请求覆盖范围内的到期记录。旧客户端可以继续读取现有 `messages`/ `matches` 数组。
- 收件箱重连仍补收可授权的未确认事件。已经到期的事件不再投递。消息存在时，同键同内容的授权重试返回原消息。清理后再次使用同键可创建消息，用新的 ID。
- [私聊规格](../specs/collaboration-surfaces.md#direct-messages) 定义参与权限和操作。私聊复用时间戳、ID、边界和清理语义。[控制台规格](../specs/collaboration-surfaces.md#control-console) 定义房间正文授权和无正文审计。控制台通过 Collaboration 的规范房间查询读取同一批消息。

## 验收

- 通过公开接口验证两个房间策略、公告门槛、房间范围、带文字上下文的搜索和基于 ID 的分页。
- 使用 v0 与账号试点数据库样本验证升级。原历史位置和旧消息 ID 保留，时间未知如实返回。
- 使用独立的房间和 DM 到期配置，检查历史、搜索和相关未收投递一致变化，空历史仍能报告缺失，后续消息与事件 ID 均不复用。
- 验证重试、确认收件、越权查询和已撤权后的旧游标；记录可复现的测试环境与结果。
