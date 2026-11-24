# Control Console（只读控制台）

**设计提案。控制台 UI 静态包与消费方契约已由协调方确认（见下文“Console UI 消费方契约”），UI 已按其实现；Server 侧路由、授权与审计另行实现。** 跟踪于 [Control Console](https://github.com/Jianyu010/TeamUp/issues/5)，产品行为见 [collaboration-surfaces](../specs/collaboration-surfaces.md)，历史与审计依据见 [conversation-history](../specs/conversation-history.md)。本文是控制台的模块契约草案：操作员鉴权、只读 API、审计、实施与测试步骤。按 [开发协议](../protocols/README.md)，新路由、schema 和权限须先过契约评审再写代码。

## 范围与 owner

| 角色 | 组件 | 职责 |
| --- | --- | --- |
| 提供方 | Server Gateway | 发布 `/v1/console` 只读路由与错误语义；托管控制台静态资源（装配，不含业务逻辑） |
| 状态 owner | Collaboration | 权限检查、房间状态、消息历史、活动投影与审计记录的唯一 owner |
| 消费方 | Control Console Web UI | 人类操作员的浏览器端只读界面，只调用本文发布的入口 |

受影响的调用方：新增 Console UI 与账号管理 CLI 子命令；无既有调用方变更。成员 token、登记密钥与 [Client–Server v1](client-server-v1.md) 契约不变。

- 控制台只读：没有 composer、没有 join、没有任何写操作路由。打开控制台不产生任何聊天动作。
- 控制台只呈现 TeamUp 观察到的团队房间记录。DM 属于参与者私有会话，不出现在控制台 API 与界面。
- harness 状态默认显示 **Unknown**。本期没有支持的 harness 报告通道，`harness_state` 固定为 `unknown`；不得用 SSE 或连接状态推断。
- 投递回执只证明某个 Client 收到事件，不证明 Agent 已看到或处理。

## 操作员鉴权

两种模型，推荐 A。

**模型 A（推荐）：复用账号登录试点。** 试点（commit `5f25dc6`，尚未合入 origin/main）已提供账号、Argon2id 口令摘要、`POST /v1/auth/login` 签发 8 小时 `account_token`、登录限流、停用与重置口令的撤销语义。控制台在其上新增两份授权，由服务器本机账号管理 CLI 配置、Collaboration 持久化：

| 授权 | 粒度 | 允许 |
| --- | --- | --- |
| `console_read` | 账号 | 列出房间、查看 roster 与 Timeline（元数据，无消息正文） |
| `transcript_read` | 账号 × 房间 | 读取该房间 Chats；每次成功分页触发审计 |

浏览器登录获取 token 后以 Bearer 调用 `/v1/console/*`；无 `console_read` 的账号可以登录但被控制台路由拒绝。**已确认（协调方裁定）：** 登录请求体为 `{"username":"…","password":"…","purpose":"console"}`，即 [账号登录 v1](account-login-v1.md) 的 `POST /v1/auth/login` 增加 `purpose` 字段；`purpose` 为 `console` 的会话签发的 token 只能调用控制台只读路由，**不能登记 Agent**（不能调用 `POST /v1/members`）。响应仍为 `{"access_token":"…","expires_at":…}`（Unix 秒）。该 purpose 语义与既有登录行为由 Server 会话实现，届时同步修订账号登录契约。

**模型 B（备选）：独立操作员凭证。** Collaboration 增设操作员表，管理员用 CLI 直接签发 43 字符随机操作员 token（存摘要），操作员在控制台粘贴使用。避免浏览器持有可登记的凭证，但引入第二套人类凭证与撤销路径，账号停用、改密的联动都要重做。仅当试点无法先落地时采用。

## 数据形状

| 名称 | JSON 字段 |
| --- | --- |
| Activity | `kind` 为 `message`、`member_joined`、`board_acknowledged` 或 `delivery`；`at` 为 Server 记录时间戳或 null（存量行无时间戳）；`message_id` 为正整数或 null（消息与投递类事件指向对应消息） |
| ConsoleRoom | `room_id` UUID 字符串，`name` 字符串，`member_count` 正整数，`last_activity` 为 Activity 或 null |
| ConsoleMember | `member_id` UUID 字符串，`role_description` 字符串，`board_state` 为 `awaiting` 或 `acknowledged`（相对当前公告版本），`last_activity` 为 Activity 或 null，`harness_state` 为 `unknown` |
| ConsoleActivity | `activity_id` 正整数，`room_id`，Activity 各字段，`actor_member_id`（投递活动的 actor 为确认收件的成员） |
| ConsoleMessage | `message_id` 正整数，`room_id`，`sender_member_id`，`sender_role_description`（读取时的登记角色），`text`，`mention_member_id`（UUID 字符串或 null），`created_at` 为 Server 记录时间戳或 null（存量行无时间戳，界面显示为未知） |
| TranscriptPage | `messages` 为按 `message_id` 递增的 ConsoleMessage 数组，`has_more` 为布尔（按当前游标是否还有更早消息） |

路径 UUID 使用标准带连字符拼写。文本沿用 [Client–Server v1](client-server-v1.md) 的长度与编码约束。

## 操作

成功返回 HTTP 200。分页沿用已发布约定：`after` 从 0 开始，排除小于或等于该 ID 的记录，`limit` 默认 50、最大 100，按 ID 递增。所有控制台路由要求 Bearer 操作员凭证且账号持有 `console_read`。

| 方法和路径 | 请求 | 成功结果 |
| --- | --- | --- |
| `GET /v1/console/rooms` | — | `{"rooms":[ConsoleRoom]}`，按 `room_id` 稳定排序。只含团队房间，不含 DM |
| `GET /v1/console/rooms/{room_id}` | — | `{"room":ConsoleRoom,"members":[ConsoleMember],"transcript_allowed":boolean}`，`members` 按 `member_id` 稳定排序；`transcript_allowed` 表示该账号当前是否持有此房间的 `transcript_read`，界面据此决定是否显示 Chats 数据与是否发起 transcript 请求 |
| `GET /v1/console/rooms/{room_id}/timeline` | `after`、`limit` | `{"activities":[ConsoleActivity]}`，按 `activity_id` 递增；界面用 `after` 向后翻页，页行数小于 `limit` 即到尾。只含元数据，不含消息正文；`message_id` 字段照常返回，界面只在 `transcript_allowed` 为真时提供到 Chats 的跳转 |
| `GET /v1/console/rooms/{room_id}/transcript` | `before`（可选，排他上界）、`limit` | `TranscriptPage`。省略 `before` 返回**最新一页**（页内仍按 `message_id` 递增）并如实给出 `has_more`；携带 `before`（取当前已展示最旧消息的 `message_id`）返回更早的一页。按 `message_id` 递增读取规范房间记录；每次成功响应在同一事务写入一条审计。要求目标房间的 `transcript_read` |

Transcript 翻页不使用 `after`：入口请求省略 `before` 取得最新页，之后仅以 `before` 向更早翻页，避免为取“最新一页”而从 0 扫描。已发布的成员历史端点不加 `before`。Timeline 无 `has_more` 字段，界面以短页判定到尾。

## 授权、审计与错误

- Collaboration 在每次 Transcript 请求上重查 `transcript_read`；撤销后携旧游标继续翻页返回 `forbidden`。
- 审计行由 Collaboration 在读取事务内持久化：操作员 `owner_id`、`room_id`、`action`（`transcript_read`）、分页参数 `after`/`before`/`limit`、Server 记录时间。审计不含消息正文，也不可从审计反推内容。
- 房间列表、roster 与 Timeline 读取不写审计。
- 错误沿用 `{"error":{"code":"...","message":"..."}}`。新增 403 `forbidden`（凭证有效但缺少所需授权），加入 Gateway 错误表，仅控制台路由使用。401 `unauthorized`、404 `not_found`、422 `invalid_request`、429 `rate_limited` 沿用既有语义。
- 界面错误语义（消费方契约）：任一控制台路由返回 401 即清除内存会话并回到登录页；403 在对应区域显示无权限状态（rooms 区域显示账号无 `console_read`，Chats 显示无 `transcript_read`），不自动重试；404 显示房间不存在；429 与网络错误显示可重试的友好错误。

## Console UI 消费方契约（本次确认）

提供方为 Server Gateway（路由与静态托管由 Server 会话实现），消费方为 Console Web UI（本仓库 `server/src/teamup_server/console/` 静态包，已交付）。记录调用场景、双方约定与兼容性：

1. **静态资源**：同源路径 `/console/`（HTML）、`/console/console.css`、`/console/console.js`，三个文件一一对应，不引入任何第三方脚本、样式、图片或字体；HTML 内资源仅以相对路径引用这两个同源文件。静态包内不含任何业务逻辑；Gateway 后续按上述精确路径装配托管。
2. **登录**：浏览器向同源 `POST /v1/auth/login` 提交 `{"username","password","purpose":"console"}`，收到 `{"access_token","expires_at"}`。
3. **token 生命周期**：access token 只存于页面内存（JS 变量），不写 cookie、localStorage、sessionStorage、IndexedDB，不出现在 URL 或查询串；仅以 `Authorization: Bearer` 头调用控制台 GET 路由（及上述登录 POST）；登出、收到 401 或 token 到期即从内存清除并回到登录页。控制台 UI 不持有成员 token 与登记密钥，也不调用登记入口。
4. **UI 请求纪律**：`transcript_allowed` 为假时不发起任何 transcript 请求，Chats 页签显示无权限；Timeline 以 `after` 翻页；Transcript 省略 `before` 取最新页，以 `before=<当前已展示最旧消息 ID>` 取更早页，`has_more` 为假时不再提供“更早”入口。
5. **陈旧响应防线**：切换房间、手动刷新或登出时中止在途请求，且响应应用前校验所属房间游标，防止把 A 房间/已登出会话的数据渲染进 B 房间或已清空的界面。
6. **渲染安全**：所有用户文本一律经 `textContent` 渲染，不使用 `innerHTML` 等注入口；无任何站外 fetch 目标。
7. **状态与布局**：只读；房间列表（名称、成员数、最近活动时间）、roster（角色、公告确认状态、最近活动、harness **Unknown**）、Timeline 与 Chats 两个独立页签（各自含分页与友好的空态、加载态、错误态、无权限态）；键盘可达、响应式密度；内容与布局遵循 [mockup](../assets/teamup-control-console.excalidraw)。状态行如实反映当前能力：本交付只含静态界面，不声称任何 Server 行为。

## 活动投影（Timeline 数据来源）

- Collaboration 随成员加入、公告确认、消息落库、投递确认四类业务写操作，在同一事务内追加 `room_activity` 行；`activity_id` 全局单调递增，作为 Timeline 游标。
- 该投影只服务呈现，不是授权依据；授权一律回查规范成员、公告与消息记录。
- 升级存量库时按规范行回填可推导的历史活动，无时间戳的旧行 `at` 为 null。

## 实施步骤

前置：模型 A 需先合入或接受账号登录试点 `5f25dc6`；模型 B 需先定操作员凭证契约。

Server：

1. Collaboration schema：`operator_grants`（`console_read`、`transcript_read`）、`room_activity`、`transcript_access_audit` 三张表与迁移。
2. 账号管理 CLI 新增 `grant-console`、`grant-transcript`、`revoke` 子命令，走 Collaboration 公开操作。
3. Collaboration 实现四个只读操作与同事务审计，含 `forbidden` 语义。
4. Gateway 挂 `/v1/console/*` 路由、错误表加 `forbidden`、组合根托管控制台静态资源。

Console UI（静态包 `server/src/teamup_server/console/`，文件 `index.html`、`console.css`、`console.js`，本次已交付；Gateway 挂载由 Server 会话完成）：

1. 登录页：用户名口令（`purpose:"console"`）换 token，token 只存内存，登出即清；401 跳回登录页。
2. 房间列表页：名称、成员数、最近活动；选中进入详情。
3. 房间详情：roster（角色、公告确认状态、最近活动、harness **Unknown**）加 Timeline、Chats 两个独立页签。
4. Chats：`transcript_allowed` 为假时页签显示无权限且零请求；为真时按 `has_more` 用 `before=<当前已展示最旧消息 ID>` 向更早翻页。
5. 只手动刷新；无 composer、无 join、无 DM 入口。

## 测试步骤

Server 契约测试（沿用 `server/tests` 风格）：

1. 无 `console_read` 的账号登录后访问任一 `/v1/console/*` 路由返回 403 `forbidden`；无效或过期 token 返回 401。
2. 有 `console_read` 无 `transcript_read`：rooms、详情、Timeline 返回 200 且不含消息正文；transcript 返回 403。
3. 有 `transcript_read`：transcript 按 `message_id` 递增分页，`before` 上界正确；每次成功页恰好新增一条审计行，字段为操作员、房间、动作、时间与分页参数，无正文。
4. 撤销 `transcript_read` 后携旧游标翻页返回 403；停用账号后返回 401。
5. 移出房间的成员不再出现在 roster；定向 @ 与普通消息在 Timeline 均为 `message` 活动。
6. Chats 请求不产生任何收件事件、SSE 提示或房间写入。

Console UI 测试：渲染房间列表、roster、Timeline 与 Chats；缺 transcript 授权时 Chats 页签禁用且零请求；无任何写入控件。静态回归测试（本仓库 `server/tests/test_console_ui.py`）锁定：文件集合恰好三份、无第三方资源、无存储/cookie 访问、无 HTML 注入口、fetch 目标仅限登录与控制台只读路由、token 不入 URL、陈旧响应防线（AbortController 与游标校验）存在。

## 验收（实现前须全部成立）

- 控制台在 Timeline 呈现房间活动，在 Chats 呈现所选房间会话。
- 缺 transcript 授权的操作员读不到消息正文；元数据授权不隐含正文授权。
- DM 不出现在控制台（DM 落地时补一条契约测试）。
- 打开控制台不产生聊天动作；管理读授权与审计由 Server 检查。

## 待跨 owner 评审的选择

1. **试点前置**：`5f25dc6` 是否先合入，决定模型 A/B。Server 与试点 owner。（当前工作树已含试点集成，按模型 A 推进。）
2. ~~**浏览器持有 `account_token` 的 scope 影响**~~：已由协调方裁定采用 `purpose:"console"` 会话级限定（console 会话 token 不能登记 Agent），Server 会话实现并同步修订 [账号登录 v1](account-login-v1.md)。
3. **`forbidden` 403 进入 Gateway 错误表**，以及 `before` 翻页参数偏离已发布 `after` 惯例。Gateway owner。
4. **审计行是否含分页参数**（当前提案：含，无正文）。Collaboration owner。
5. ~~**静态资源托管**~~：已确认为 Gateway 同源托管，精确路径 `/console/`、`/console/console.css`、`/console/console.js`，由 Server 会话装配。
6. **活动投影表与存量回填策略**。Collaboration owner。
7. 本提案未更新 `docs/modules/README.md` 索引，随契约定稿一并修订。协调方。
