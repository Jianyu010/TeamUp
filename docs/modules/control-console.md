# Control Console（只读控制台）

**Stage 3（读路径，已按协调方裁定范围实施）。** 本期在 stage 2（操作员鉴权、账号级/房间级授权、静态托管，commit `e495544`）与 #6 存量基线（存储地基 commit `25fff63`，已并入本分支）之上交付控制台只读读路径：`GET /v1/console/rooms/{room_id}`（roster 详情）、`GET /v1/console/rooms/{room_id}/timeline`（body-free 活动投影）、`GET /v1/console/rooms/{room_id}/transcript`（规范房间记录 + 同事务无正文审计）。已交付的 #3 私聊服务端（`codex/teamup-3-direct-messages`，DM 家族表与收件路径）随后并入本分支：schema 先经 #3 达到 **v4**（`dms`、可空 `messages.room_id` 与恰一非空约束、按家族分域的重试索引、收件 `dm` kind），控制台层在其上收敛为 **v5**。已测试的 #6 房间与私聊到期运行时（`codex/teamup-6-retention` 顶端 `e86aa97`）随后经前向合并并入本分支：两项保留水位原位补列，收敛为最终 **v6**（见下文迁移表，含已发布控制台 v5 起点）。迁移支持 fresh、v1、v2、两种 v3 草稿（#6 历史 v3；已发布的 #5 鉴权预览 `e495544`，以表列在 schema 迁移内判别，见下文）、#3 DM v4 与控制台 v5。控制台读路径复用 #6 规范消息记录与所有权，成员端点形状不变；#3 并入的成员 DM 路径见[私聊契约](direct-messages.md)；#6 到期清理的运行时契约见[会话历史模块契约](conversation-history.md)。不进行线上部署。跟踪于 [Control Console](https://github.com/Jianyu010/TeamUp/issues/5)，产品行为见 [collaboration-surfaces](../specs/collaboration-surfaces.md)，历史与审计依据见 [conversation-history](../specs/conversation-history.md) 与 [会话历史模块契约](conversation-history.md)。本文是控制台的模块契约：操作员鉴权、只读 API、审计、实施与测试步骤。按 [开发协议](../protocols/README.md)，新路由、schema 和权限须先过契约评审再写代码。

## 范围与 owner

| 角色 | 组件 | 职责 |
| --- | --- | --- |
| 提供方 | Server Gateway | 发布 `/v1/console` 只读路由与错误语义；托管控制台静态资源（装配，不含业务逻辑） |
| 状态 owner | Collaboration | 权限检查、房间状态、消息历史、活动投影与审计记录的唯一 owner |
| 消费方 | Control Console Web UI | 人类操作员的浏览器端只读界面，只调用本文发布的入口 |

受影响的调用方：Console UI 与账号管理 CLI 子命令（stage 2 已交付）；#6 成员历史读取（共享规范消息记录与所有权，成员端点形状不变）；无其他既有调用方变更。成员 token、登记密钥与 [Client–Server v1](client-server-v1.md) 契约不变。

- 控制台只读：没有 composer、没有 join、没有任何写操作路由。打开控制台不产生任何聊天动作。
- 控制台只呈现 TeamUp 观察到的团队房间记录。DM 属于参与者私有会话，不出现在控制台 API 与界面：Timeline 投影与升级回填、roster 的 `last_activity` 与 transcript 读取都只覆盖 `room_id` 非空的规范房间行，DM 发送与 DM 收件确认不产生任何房间活动；即使操作员持有房间 `transcript_read`，也无法经 Timeline 或 transcript 追溯 DM 文本（已随 #3 服务端并入补契约测试）。
- harness 状态默认显示 **Unknown**。本期没有支持的 harness 报告通道，`harness_state` 固定为 `unknown`；不得用 SSE 或连接状态推断。
- 投递回执只证明某个 Client 收到事件，不证明 Agent 已看到或处理。

## 操作员鉴权

采用模型 A（复用账号登录试点；独立操作员凭证的备选方案已否决）。

**模型 A：复用账号登录试点。** 试点（commit `5f25dc6`，已集成入本工作树）提供账号、Argon2id 口令摘要、`POST /v1/auth/login` 签发 8 小时 `account_token`、登录限流、停用与重置口令的撤销语义。控制台在其上新增两份授权，由服务器本机账号管理 CLI 配置、Collaboration 持久化：

| 授权 | 粒度 | 允许 |
| --- | --- | --- |
| `console_read` | 账号 | 列出房间、查看 roster 与 Timeline（元数据，无消息正文） |
| `transcript_read` | 账号 × 房间 | 读取该房间 Chats；每次成功分页触发审计 |

浏览器登录获取 token 后以 Bearer 调用 `/v1/console/*`；无 `console_read` 的账号可以登录但被控制台路由拒绝。登录请求体为 `{"username":"…","password":"…","purpose":"console"}`，即 [账号登录 v1](account-login-v1.md) 的 `POST /v1/auth/login` 增加 `purpose` 字段（可选，唯一非空合法值为 `"console"`，语义详见该契约）；`purpose` 为 `console` 的会话签发的 token 只能调用控制台只读路由，**不能登记 Agent**（不能调用 `POST /v1/members`），也不能调用普通成员 API；登记用途登录 token、成员 token 与旧登记密钥不能授权任何控制台路由。响应仍为 `{"access_token":"…","expires_at":…}`（Unix 秒）。

### 授权配置（仅限服务器本机 CLI）

`teamup-server accounts --db PATH` 四个子命令，全部走 Collaboration 公开操作、在 Collaboration 事务内落库：

| 子命令 | 参数 | 行为 |
| --- | --- | --- |
| `grant-console` | `USERNAME` | 授予账号级 `console_read`；幂等 |
| `revoke-console` | `USERNAME` | 撤销账号级 `console_read`（不影响 `transcript_read`）；幂等；账号不存在返回 not_found |
| `grant-transcript` | `USERNAME ROOM_ID` | 授予该账号在该房间的 `transcript_read`；幂等；账号或房间不存在返回 not_found，房间 ID 非法返回 invalid_request |
| `revoke-transcript` | `USERNAME ROOM_ID` | 撤销该授权；幂等；账号或房间不存在返回 not_found，房间 ID 非法返回 invalid_request |

授权由 Collaboration 单一持有：`operator_grants` 表（`owner_id`、`scope` ∈ {`console_read`,`transcript_read`}、`room_id`：`console_read` 行为 NULL，`transcript_read` 行指向房间）。Server 网络入口不提供任何授权写接口。CLI 输出不含任何凭证。撤销/停用对下一页生效：每次请求都在使用事务内重查会话有效性与所需授权，撤销后携旧游标继续翻页返回 `forbidden`（或会话失效时 401）。

## 数据形状

| 名称 | JSON 字段 |
| --- | --- |
| Activity | `kind` 为 `message`、`member_joined`、`board_acknowledged` 或 `delivery`；`at` 为 Server 记录的 Unix 秒时间戳（整数）或 null（存量行无时间证据）；`message_id` 为正整数或 null（消息与投递类活动指向对应消息；消息被 #6 到期清理删除后链接清为 null，事件本身保留） |
| ConsoleRoom | `room_id` UUID 字符串，`name` 字符串，`member_count` 为非负整数，`last_activity` 为 Activity 或 null |
| ConsoleMember | `member_id` UUID 字符串，`role_description` 字符串，`board_state` 为 `awaiting` 或 `acknowledged`（相对当前公告版本），`last_activity` 为 Activity 或 null，`harness_state` 为 `unknown` |
| ConsoleActivity | `activity_id` 正整数，`room_id`，Activity 各字段，`actor_member_id`（投递活动的 actor 为确认收件的成员） |
| ConsoleMessage | `message_id` 正整数，`room_id`，`sender_member_id`，`sender_role_description`（读取时的登记角色），`text`，`mention_member_id`（UUID 字符串或 null），`created_at` 为 Server 记录的 Unix 秒时间戳（整数）或 null（存量行无时间证据，界面显示为未知） |
| TranscriptPage | `messages` 为按 `message_id` 递增的 ConsoleMessage 数组，`has_more` 为布尔（按当前游标是否还有更早消息） |

退役成员从当前名单移除，其历史消息仍包含原 `sender_member_id` 和角色描述，并追加 `sender_retired: true`。Chats 在角色旁显示 `retired`。访问 transcript 仍需要相同的房间授权并写入读取审计。

路径 UUID 使用标准带连字符拼写。文本沿用 [Client–Server v1](client-server-v1.md) 的长度与编码约束。控制台载荷中的 `at`/`created_at` 为 Unix 秒整数（消费方契约 `formatTime` 以数值解析）；#6 成员端点的 `created_at` 保持 RFC 3339 字符串，控制台投影读取规范行后换算，不另存副本。

## 操作

成功返回 HTTP 200。分页沿用已发布约定：`after` 从 0 开始，排除小于或等于该 ID 的记录，`limit` 默认 50、最大 100，按 ID 递增。所有控制台路由要求 Bearer 操作员凭证（`purpose:"console"` 会话、账号未停用、会话未到期未撤销）且账号持有 `console_read`；凭证缺一格（非 console 会话、成员 token、登记密钥、无效或过期）一律 401 `unauthorized`，凭证有效但缺 `console_read` 为 403 `forbidden`。目标房间不存在一律 404 `not_found`，判定次序固定为：凭证 401 → 账号级 `console_read` 403 → 房间存在性 404 → 房间级 `transcript_read` 403（末级仅 transcript 路由）。持有 `console_read` 的操作员（本就可列出全部房间）对不存在的房间得到 404，与 roster/timeline 一致；缺 `console_read` 的账号无论房间是否存在均为 403，不因 404/403 之差泄露房间存在性。

| 方法和路径 | 请求 | 成功结果 | 阶段 |
| --- | --- | --- | --- |
| `GET /v1/console/rooms` | — | `{"rooms":[ConsoleRoom]}`，按 `room_id` 稳定排序。只含团队房间，不含 DM。`last_activity` 为该房间最新一条 `room_activity` 投影（无活动为 null） | stage 2/3（本期起 `last_activity` 为真实投影） |
| `GET /v1/console/rooms/{room_id}` | — | `{"room":ConsoleRoom,"members":[ConsoleMember],"transcript_allowed":boolean}`，`members` 按 `member_id` 稳定排序；`transcript_allowed` 表示该账号当前是否持有此房间的 `transcript_read`，界面据此决定是否显示 Chats 数据与是否发起 transcript 请求 | **本期（stage 3）** |
| `GET /v1/console/rooms/{room_id}/timeline` | `after`、`limit` | `{"activities":[ConsoleActivity]}`，按 `activity_id` 递增；界面用 `after` 向后翻页，页行数小于 `limit` 即到尾。只含元数据，不含消息正文；`message_id` 字段照常返回（到期清理清空链接的事件为 null，无跳转目标），界面只在 `transcript_allowed` 为真时提供到 Chats 的跳转 | **本期（stage 3）** |
| `GET /v1/console/rooms/{room_id}/transcript` | `before`（可选，正整数排他上界）、`limit` | `TranscriptPage`。省略 `before` 返回**最新一页**（页内仍按 `message_id` 递增）并如实给出 `has_more`；携带 `before`（取当前已展示最旧消息的 `message_id`）返回更早的一页。大于 SQLite 最大 ID 的正 `before` 作为无上界处理（等同省略 `before`，不排除合法的最大 ID，与成员历史端点的既有边界一致），不报错。按 `message_id` 递增读取规范房间记录；每次成功响应在同一事务写入一条审计。要求目标房间的 `transcript_read` | **本期（stage 3）** |

鉴权助手在使用的同一事务内重查 console 会话有效性与所需授权（`console_read` 全路由；transcript 路由另查该房间的 `transcript_read`），授权不缓存、不作为投影使用。授权与持久化语义保持 stage 2 形态。

Transcript 翻页不使用 `after`：入口请求省略 `before` 取得最新页，之后仅以 `before` 向更早翻页，避免为取“最新一页”而从 0 扫描。已发布的成员历史端点不加 `before`。Timeline 无 `has_more` 字段，界面以短页判定到尾。

### 缓存

所有 `/v1/console/*` 成功（200）响应携带 `Cache-Control: no-store`：载荷可能随房间活动与授权变化，不得被任何共享或私有缓存复用。静态资源托管沿用 `no-cache`（见下文）。错误响应不设缓存头。

## 授权、审计与错误

- Collaboration 在每次 Transcript 请求上重查 `transcript_read`；撤销后携旧游标继续翻页返回 `forbidden`。停用/重置口令撤销会话后下一页返回 401。
- 审计行由 Collaboration 在读取事务内持久化：操作员 `owner_id`、`room_id`、`action`（`transcript_read`）、分页参数 `before`（可空；记录实际生效的排他上界，省略与超界开放上界均记 null）与 `limit`、Server 记录时间。审计不含消息正文，也不可从审计反推内容。
- 房间列表、roster 与 Timeline 读取不写审计。
- 错误沿用 `{"error":{"code":"...","message":"..."}}`。403 `forbidden`（凭证有效但缺少所需授权）已在 Gateway 错误表，仅控制台路由使用。401 `unauthorized`、404 `not_found`、422 `invalid_request`、429 `rate_limited` 沿用既有语义。
- 界面错误语义（消费方契约）：任一控制台路由返回 401 即清除内存会话并回到登录页；403 在对应区域显示无权限状态（rooms 区域显示账号无 `console_read`，Chats 显示无 `transcript_read`），不自动重试；404 显示房间不存在；429 与网络错误显示可重试的友好错误。分页中途的瞬时失败（429、网络错误等可重试失败）不清空已加载内容：Timeline 下一页或 Chats 更早页取数失败时，已渲染行原样保留，行尾就地显示错误与重试入口；重试按失败请求的同一游标重发（Timeline 用当前 `after`，Chats 用失败请求的 `before`，不回退到最新页），成功后正常续接分页。403 拒绝、切换房间、手动刷新与登出仍然清空此前已加载的私有内容（内存行与 DOM 一并清除）。

## 静态托管（stage 2 交付）

- Gateway 同源托管**恰好三个**路径：`GET /console/`（HTML）、`GET /console/console.css`、`GET /console/console.js`，与静态包 `server/src/teamup_server/console/` 内文件一一对应、字节一致。不做目录列举、不接受任何其他 `/console/…` 路径（一律 404 `not_found`），方法不符返回 405。`redirect_slashes` 保持关闭，`/console`（无斜杠）不是入口。
- 响应头：`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-cache`（无修订机制前避免陈旧资源）；媒体类型分别为 `text/html; charset=utf-8`、`text/css; charset=utf-8`、`text/javascript; charset=utf-8`。
- 静态路由不需鉴权，页面内容不含任何私有数据（token 只在 API 鉴权后经 `/v1/console/*` 返回）。控制台无写入 UI；静态托管只做装配，不含业务逻辑。

## Console UI 消费方契约（stage 2 确认；分页瞬时失败的重试语义随 #12 F3 修订）

提供方为 Server Gateway（路由与静态托管由 Server 会话实现），消费方为 Console Web UI（本仓库 `server/src/teamup_server/console/` 静态包，已交付）。记录调用场景、双方约定与兼容性：

1. **静态资源**：同源路径 `/console/`（HTML）、`/console/console.css`、`/console/console.js`，三个文件一一对应，不引入任何第三方脚本、样式、图片或字体；HTML 内资源仅以相对路径引用这两个同源文件。静态包内不含任何业务逻辑；Gateway 按上述精确路径装配托管。
2. **登录**：浏览器向同源 `POST /v1/auth/login` 提交 `{"username","password","purpose":"console"}`，收到 `{"access_token","expires_at"}`。
3. **token 生命周期**：access token 只存于页面内存（JS 变量），不写 cookie、localStorage、sessionStorage、IndexedDB，不出现在 URL 或查询串；仅以 `Authorization: Bearer` 头调用控制台 GET 路由（及上述登录 POST）；登出、收到 401 或 token 到期即从内存清除并回到登录页。控制台 UI 不持有成员 token 与登记密钥，也不调用登记入口。
4. **UI 请求纪律**：`transcript_allowed` 为假时不发起任何 transcript 请求，Chats 页签显示无权限；Timeline 以 `after` 翻页；Transcript 省略 `before` 取最新页，以 `before=<当前已展示最旧消息 ID>` 取更早页，`has_more` 为假时不再提供“更早”入口。时间字段（`at`、`created_at`）按 Unix 秒数值渲染，null 显示为未知。分页请求瞬时失败（429、网络错误等可重试失败）时保留已加载行，重试以失败请求的同一游标重发（不重置、不回退到最新页）；授权拒绝、切换房间、手动刷新与登出仍清空已加载的私有内容。
5. **陈旧响应防线**：切换房间、手动刷新或登出时中止在途请求，且响应应用前校验所属房间游标，防止把 A 房间/已登出会话的数据渲染进 B 房间或已清空的界面。
6. **渲染安全**：所有用户文本一律经 `textContent` 渲染，不使用 `innerHTML` 等注入口；无任何站外 fetch 目标。
7. **状态与布局**：只读；房间列表（名称、成员数、最近活动时间）、roster（角色、公告确认状态、最近活动、harness **Unknown**）、Timeline 与 Chats 两个独立页签（各自含分页与友好的空态、加载态、错误态、无权限态）；键盘可达、响应式密度；内容与布局遵循 [mockup](../assets/teamup-control-console.excalidraw)。

Timeline 按 `msg-N` 显示活动关联的 `message_id`，例如 `msg-42`。关联 ID 为 null 时省略引用。见 [会话历史契约](conversation-history.md#记录和迁移)。

## 活动投影（Timeline 数据来源）

- `room_activity` 表：`activity_id` 全局单调递增（SQLite AUTOINCREMENT，删除后不复用）、`room_id`、`kind` ∈ {`member_joined`,`board_acknowledged`,`message`,`delivery`}、`at`（RFC 3339 文本或 null）、`actor_member_id`、`message_id`（消息与投递类活动指向对应消息，加入与确认活动恒为 null）。
- **到期兼容（#5/#6）**：`message_id` 外键保留并带 `ON DELETE SET NULL`；加入与确认活动仍由 schema CHECK 要求 `message_id` 为 null。#6 到期清理在同一清理事务内删去到期消息及其投递引用时，指向该消息的消息与投递活动把链接清为 null，body-free 事件行及其 `kind`、`at`、actor 照常保留；存活消息的链接与既有可见行为不变。正文的不可读由消息删除本身保证，活动行不因到期而删除。
- Collaboration 随四类业务写操作在**同一所有者事务**内追加投影行：建房者首加入与成员加入（`member_joined`，actor 为加入成员）、对当前公告版本的首次确认（`board_acknowledged`，actor 为确认成员，按版本去重）、消息受理（`message`，actor 为发送者，`at` 取该消息 `created_at`）、投递首次确认（`delivery`，actor 为确认收件的成员，指向对应消息）。幂等重试（加入冲突、重复确认、同键同文重发、重复确认收件）不追加行，投影与授权仍以规范成员、公告与消息记录为准。
- DM 永不进入该投影：DM 消息的 `messages.room_id` 为 NULL，发送与 DM 收件确认都不追加 `room_activity` 行；升级回填的消息与投递语句同样以 `room_id IS NOT NULL` 过滤。房间消息收件确认照旧记录 `delivery`。
- 该投影只服务呈现，不是授权依据；授权一律回查规范成员、公告与消息记录。消息与投递活动不含正文。
- 升级存量库时按规范行回填可推导的历史活动，回填次序为：成员加入 → 公告确认 → 消息（按 `message_id`）→ 投递确认（按 `event_id`）；无时间证据的旧行 `at` 为 null（消息行有 `created_at` 时如实取值）。回填行同样只含元数据，且不含任何 DM 行。

## 审计存储

`transcript_access_audit` 表：`owner_id`、`room_id`、`action`（固定 `transcript_read`）、`at`（RFC 3339 文本）、`page_before`（可空整数）、`page_limit`（整数）。由 Collaboration 在 transcript 读取的同一事务内写入，每次成功响应一行；不暴露查询路由，不含正文。租户数据除该无正文审计外不受控制台写入影响。

## Schema 与迁移

下表记录 #6 合入后的 v6 基线。后续 [#45](https://github.com/Jianyu010/TeamUp/issues/45) 在 **v7** 引入房间消息的有序附加目标子表。已有消息以旧列 `mention_member_id` 表示首个或唯一目标。房间消息到期清理时，附加目标随消息删除。操作员 `ConsoleMessage` 继续显示首个目标字段，房间成员读取遵循 [Client–Server v1](client-server-v1.md#房间消息的多个明确收件人45)。当前 **v8** 追加 [成员退役标记](client-server-v1.md#成员退役与离开房间47)。

v6 基线分层构成：v1/v2 为已发布基线；#6 历史层（`history_policy`/`created_at`/`id_counters`）叠加后为 v3 草稿；#3 DM 层（`dms` 表、可空 `messages.room_id` 与恰一非空约束、按家族分域的重试唯一索引、`inbox_events.kind` 扩展 `dm`）叠加为 **v4**；控制台层（`purpose` 会话列、`operator_grants`、`room_activity`、`transcript_access_audit`）收敛为 **v5**；最终 #6 保留层（rooms 与 dms 的 `expired_through_message_id` 可空水位列）在控制台基线之上收敛为 **v6**。两种 v3 草稿都报告 `user_version` 3，仅在 schema 迁移内以表列判别，运行时不依赖该判别：`account_sessions` 带 `purpose` 列且存在 `operator_grants` 表即已发布的 #5 鉴权预览 v3（`e495544`，无历史列）；存在 `rooms.history_policy` 而无操作员列即 #6 历史 v3；`messages` 带 DM 列即 #3 v4。此前 #6 独立保留工作树曾把同两列水位标为 v5，该形态从未发布，不设额外兼容分支：`user_version` 5 一律按已发布控制台 v5 处理。迁移次序（每行按序在同一个写事务内执行，最后统一回填活动投影并写 `user_version = 6`）：

| 起始版本 | 步骤 |
| --- | --- |
| 0（fresh） | 完整 schema（含 DM 表、历史列、计数器、`purpose`、授权、投影、审计表与两列保留水位）直接建到 v6；空库空回填 |
| 1 | 账号 schema（`account_sessions` 自带 `purpose`）→ 历史 schema（列 + 计数器播种）→ DM 重建 → 控制台 schema → 保留水位原位补列 → v6 |
| 2 | 历史 schema（列 + 计数器播种）→ DM 重建 → `account_sessions` 补 `purpose` 列（存量行记为登记用途）→ 控制台 schema → 保留水位原位补列 → v6 |
| 3（#6 历史 v3 草稿） | DM 重建 → `account_sessions` 补 `purpose` 列 → 控制台 schema → 保留水位原位补列 → v6 |
| 3（#5 鉴权预览 v3，`e495544`） | 先补缺失的 #6 历史 schema（列 + 计数器播种，早于 DM 重建）→ DM 重建 → 只补读 schema（投影与审计表）→ 保留水位原位补列；`purpose` 列、既有会话、`operator_grants` 与房间数据原样保留，不重授、不重置 → v6 |
| 4（#3 DM v4） | `account_sessions` 补 `purpose` 列 → 控制台 schema → 保留水位原位补列 → v6 |
| 5（已发布控制台 v5） | 保留水位原位补列（rooms 与 dms 各一条 `ALTER TABLE`，NULL 即从未清理）；`purpose`、授权、投影、审计与既有数据原样保留 → v6 |

DM 重建沿用 #3 契约：消息表重建为 `room_id`/`dm_id` 恰一非空并改挂按家族分域的重试索引，存量行保持 ID、顺序与各列值（`dms` 表按已发布 v4 形态重建，保留水位随后补列）；预览 v3 的回补历史列使房间缺省 `since_joining`，与旧版建房语义一致。所有升级路径随后执行活动回填，回填与运行期采集都排除 `room_id IS NULL` 的 DM 行。#6 到期清理（`--room-retention-hours`/`--dm-retention-hours`、`expire_due`/`expire_due_dms`、`expired_through_message_id` 边界）已随本期合并并入本分支：清理事务删除房间消息时，活动投影指向该消息的链接由 `room_id IS NOT NULL` 作用域内的清理事务经上文外键清为 null。

## 实施步骤

### stage 2（已实施，e495544）

1. ~~Collaboration schema v3：`purpose` 列、`operator_grants`。~~（并入 v5）
2. 账号管理 CLI 四个子命令。
3. `list_console_rooms` 单事务鉴权路径。
4. `POST /v1/auth/login` 可选 `purpose:"console"`；`GET /v1/console/rooms`；403 `forbidden` 入错误表；静态三路径托管。

### 本期（stage 3，已实施）

5. ~~v4 schema 与迁移（见上表）；`room_activity`、`transcript_access_audit` 两表与存量回填。~~（随 #3 并入改述为 v5，见下节）
6. 四类所有者事务内的投影写入（join/ack/send/delivery），幂等重试不重复。
7. Collaboration 读路径：`console_room_detail`（roster + `transcript_allowed`）、`console_timeline`（`after`/`limit` 递增分页）、`console_transcript`（`before`/`limit` 最新页与更早页、`has_more`、同事务审计）。房间列表 `last_activity` 改为真实投影。
8. Gateway 挂载三个路由；`/v1/console/*` 成功响应统一 `Cache-Control: no-store`；transcript 读取复用 #6 规范消息记录与所有权（同一 SQL 投影 + 读取时角色，恒以 `m.room_id = ?` 过滤），成员端点形状不变。

### #3 服务端并入（已实施）

9. v5 schema 与迁移（见上表）：在 #3 DM v4 之上收敛控制台层，覆盖 fresh/v1/v2/两种 v3/#3 v4；预览 v3 先补 #6 历史列并播种计数器，再做 DM 重建；预览的 `purpose`、会话、授权与房间数据原样保留。
10. DM 行排除出活动投影：升级回填与运行期事件活动采集都按 `room_id IS NOT NULL` 过滤，DM 发送与 DM 收件确认不追加 `room_activity` 行；transcript 维持 `m.room_id = ?` 过滤、`before`/`has_more` 翻页、独立的 `transcript_read` 授权与无正文读审计不变。

### #6 到期运行时并入（本合并，已实施）

11. 最终 v6 schema 与迁移（见上表）：在控制台 v5 之上原位补 rooms/dms 两列保留水位，覆盖 fresh/v1/v2/两种 v3/#3 v4/控制台 v5；#6 独立保留工作树曾把同两列水位标为 v5，该形态从未发布，不设兼容分支。
12. 到期清理与控制台共存：清理事务先删引用的收件事件再删房间消息，活动投影指向到期消息的链接经 `room_id IS NOT NULL` 作用域内清理事务的既有外键清为 null（事件行保留）；审计、授权与 DM 作用域不受清理影响；`--room-retention-hours`/`--dm-retention-hours` 各自独立启用、共用一个维护循环，两项都省略时不运行任何清理调度（运行时契约见[会话历史模块契约](conversation-history.md)）。

### 后续（另行契约修订）

13. #6 搜索 worker 的房间成员分页、到期清理对审计的影响（到期清理对 Timeline 链接的清空语义已随本期契约化并实现，见活动投影）。

## 测试步骤

Server 契约测试（`server/tests/test_console_server.py`；stage 2 项保持，stage 3 新增）：

1. （stage 2）无 `console_read` 的账号以 `purpose:"console"` 登录后访问 `/v1/console/rooms` 返回 403 `forbidden`；无效、过期、被撤销（停用/重置口令）的 console 会话返回 401。
2. （stage 2）跨用途隔离双向验证：console 会话 token 不能登记（`POST /v1/members` 401）、不能调用普通成员 API（401）、不能作为 `member_token`（422）；登记用途登录 token、成员 token、旧登记密钥调用控制台路由 401；省略 `purpose` 的登录保持既有行为。
3. （stage 2→本期更新）v2 存量库迁移：升级后旧会话按登记用途继续可用，控制台拒绝之，`user_version` 升至 **8**。
4. （stage 2）`grant-console`/`revoke-console` 经 CLI 生效（403↔200 幂等往返）；`grant-transcript`/`revoke-transcript` CLI 幂等、对缺失账号/房间报错。
5. （本期更新）rooms 载荷：按 `room_id` 稳定排序，字段恰为 `room_id`/`name`/`member_count`/`last_activity`，`last_activity` 为最新活动投影（无活动为 null）；读房间不产生收件事件、SSE 提示或房间写入。
6. （stage 2）静态托管三路径与安全响应头；其余 `/console/…` 404、方法不符 405。
7. （本期）roster 详情：`room`/`members`/`transcript_allowed` 字段精确（`board_state` 随确认状态、`harness_state` 恒 `unknown`、`member_id` 稳定排序、`last_activity` 为该成员最新活动）；房间不存在 404。
8. （本期）Timeline：四类活动字段精确且不含正文；`after`/`limit` 递增分页、短页即尾；加入/确认/重发/重复确认收件幂等重试不产生重复投影行。
9. （本期）Transcript：省略 `before` 取最新页（页内递增）、`before` 取更早页、`has_more` 如实；大于 SQLite 整数上界的 `before` 按无上界返回最新一页（非 500），审计 `page_before` 记 null；`sender_role_description` 为读取时登记角色；存量无时间戳消息 `created_at` 为 null；每次成功响应在同一事务写入一条审计（owner、room、action、分页参数、时间），审计行不含正文。
10. （本期）授权边界：仅元数据授权的操作员读不到消息正文（timeline 无正文、无 transcript 权限 403）；房间级 `transcript_read` 只对授权房间生效，跨房间 403；transcript 对不存在房间的次序为 console_read 之后、房间级授权之前（缺 `console_read` 未知房间 403、持 `console_read` 缺房 404，既有未授权房间仍 403）；撤销后携旧游标翻页 403、停用/过期会话 401；控制台读取不产生收件事件或 SSE 提示。
11. （本期）迁移：v2 样本与 #6 历史 v3 样本在升级过程中回投活动（次序与 null `at` 如契约）；消息 ID 与既有 #6 语义不受影响。
12. （本期）成功响应统一 `Cache-Control: no-store`。
13. （#3 并入）迁移：已发布的 #5 鉴权预览 v3（`e495544` 形态：`purpose` 列、`operator_grants`、无历史列）以代表性样本升级——预览的 `console`/`enrollment` purpose、既有会话、`console_read` 与 `transcript_read` 授权与房间数据保留，预置授权无需重授即可登录读房与读 transcript；补入的历史层使既有房间缺省 `since_joining`，DM 重建后即可私聊，ID 播种续旧最大值。#3 DM v4 样本升级：已确认的 DM 投递不回填房间活动，transcript 只含房间行，DM 历史仍仅参与者可读，ID 续旧最大值。
14. （#3 并入）DM 隔离：即使操作员同时持有 `console_read` 与该房间 `transcript_read`、且 DM 正常启用并交换、收件确认完毕，rooms/roster/Timeline/transcript 任何响应都不含 DM 文本或 DM `message_id`；DM 发送与确认不新增 `room_activity` 行。
15. （本期，#5/#6 兼容）到期清理兼容：临时库内写入房间消息与活动（含投递确认）后，以测试内直连 SQL（开启外键、先清收件引用，模拟 #6 清理事务）删除消息；删除成功，Timeline 仍呈现该事件的 `kind`、`at` 与 actor 且 `message_id` 为 null，存活消息的链接不受影响；带 `message_id` 的加入/确认活动插入仍被 schema CHECK 拒绝。
16. （#6 到期运行时并入）真实到期路径 × 已鉴权控制台：把已发布控制台 v5 形态样本（v6 减两列水位）在 `--room-retention-hours` 启动的真实服务器上升级到当前 schema 并等待到期提交；房间正文与未确认投递被清理，Timeline 的消息与投递活动保留元数据且 `message_id` 为 null，成员读取/确认已到期事件得 404 `not_found`，transcript 读取照常写无正文审计，DM 正文与 DM 读取边界不受房间到期影响（`user_version` 为 8）。

Console UI 回归测试：`server/tests/test_console_ui.py`（stage 2 交付的静态回归；随 #12 F3 追加瞬时失败保留行与同游标重试的静态断言）；`server/tests/test_console_ui_behavior.mjs`（#12 F3 行为回归，`node server/tests/test_console_ui_behavior.mjs` 运行，仅用 Node 内置模块，无新增依赖：在桩 DOM 与受控 fetch 下驱动真实 `console.js`，验证 Timeline 下一页与 Chats 更早页瞬时失败时保留已加载行、重试用同一 `after`/`before` 游标，以及 403 拒绝、切换房间与登出仍清空私有内容）。

## 验收

### stage 2（已验收）

- `purpose:"console"` 登录、授权与 `/v1/console/rooms` 元数据读取按本文生效；跨用途隔离双向成立。
- 静态包三路径经 Gateway 同源托管且带严格 CSP；无任意文件服务、无写入 UI。
- 打开控制台不产生任何聊天动作；授权检查由 Server 在使用事务内执行。

### 本期（stage 3）

- 控制台在 Timeline 呈现房间活动（body-free），在 Chats 呈现所选房间会话（按授权）。
- 缺 transcript 授权的操作员读不到消息正文；元数据授权不隐含正文授权；每次正文读取留下无正文审计。
- 撤销/停用对下一页生效；控制台读取不影响成员收件、SSE 与写路径语义。
- 迁移覆盖 fresh/v1/v2/两种 v3（#6 历史草稿与已发布的 #5 鉴权预览）/#3 DM v4/已发布控制台 v5/保留水位 v6/多目标 v7，当前 `user_version` 为 8。
- DM 不进入控制台：授权操作员无法经 Timeline/transcript 追溯 DM 文本或 DM `message_id`，DM 发送与确认不产生房间活动（已随 #3 并入交付并测试）。
- #6 到期清理与控制台共存：房间到期删除正文与未确认投递、Timeline 链接经外键清空且事件行保留、审计与 DM 作用域不受影响（已随 #6 到期运行时并入交付并测试）。

## 待跨 owner 评审的选择

1. ~~试点前置~~：工作树已含试点集成，按模型 A 推进。
2. ~~浏览器持有 `account_token` 的 scope 影响~~：已裁定 `purpose:"console"` 会话级限定。
3. ~~`forbidden` 403 与 `before` 翻页参数~~：已随本期路由交付。
4. ~~审计行含分页参数（无正文）~~：已按此实现（`page_before`/`page_limit`）。
5. ~~静态资源托管~~：已实现。
6. ~~活动投影表与存量回填策略~~：已按“规范行回填 + null `at`”实现。
7. `docs/modules/README.md` 索引已随本期增补条目。协调方。
