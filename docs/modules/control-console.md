# Control Console（只读控制台）

**Stage 2 草案（已按协调方裁定范围实施）。** 本期为 Server 侧操作员鉴权、授权与静态托管的地基：`purpose:"console"` 登录、账号级 `console_read` 与按房间 `transcript_read` 授权（含 CLI 配置）、`GET /v1/console/rooms` 作为首个真实元数据读取、以及 Gateway 同源托管控制台静态包。Timeline、roster 详情、规范 transcript 及其审计、活动投影与存量回填均**留待后续阶段**（叠加于 #6/#3 之上另行交付），本期不触碰房间发送与收件路径。控制台 UI 静态包与消费方契约已由协调方确认（见下文“Console UI 消费方契约”），UI 已按其实现。本期为草稿交付：v2→v3 试点迁移（会话 purpose）允许在 #6 推进版本时由集成协调方 rebase；不进行线上部署。跟踪于 [Control Console](https://github.com/Jianyu010/TeamUp/issues/5)，产品行为见 [collaboration-surfaces](../specs/collaboration-surfaces.md)，历史与审计依据见 [conversation-history](../specs/conversation-history.md)。本文是控制台的模块契约：操作员鉴权、只读 API、审计、实施与测试步骤。按 [开发协议](../protocols/README.md)，新路由、schema 和权限须先过契约评审再写代码。

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

采用模型 A（复用账号登录试点；独立操作员凭证的备选方案已否决）。

**模型 A：复用账号登录试点。** 试点（commit `5f25dc6`，已集成入本工作树）提供账号、Argon2id 口令摘要、`POST /v1/auth/login` 签发 8 小时 `account_token`、登录限流、停用与重置口令的撤销语义。控制台在其上新增两份授权，由服务器本机账号管理 CLI 配置、Collaboration 持久化：

| 授权 | 粒度 | 允许 |
| --- | --- | --- |
| `console_read` | 账号 | 列出房间、查看 roster 与 Timeline（元数据，无消息正文） |
| `transcript_read` | 账号 × 房间 | 读取该房间 Chats；每次成功分页触发审计 |

浏览器登录获取 token 后以 Bearer 调用 `/v1/console/*`；无 `console_read` 的账号可以登录但被控制台路由拒绝。**已确认（协调方裁定，本期实施）：** 登录请求体为 `{"username":"…","password":"…","purpose":"console"}`，即 [账号登录 v1](account-login-v1.md) 的 `POST /v1/auth/login` 增加 `purpose` 字段（可选，唯一非空合法值为 `"console"`，语义详见该契约）；`purpose` 为 `console` 的会话签发的 token 只能调用控制台只读路由，**不能登记 Agent**（不能调用 `POST /v1/members`），也不能调用普通成员 API；登记用途登录 token、成员 token 与旧登记密钥不能授权任何控制台路由。响应仍为 `{"access_token":"…","expires_at":…}`（Unix 秒）。

### 授权配置（仅限服务器本机 CLI）

`teamup-server accounts --db PATH` 新增四个子命令，全部走 Collaboration 公开操作、在 Collaboration 事务内落库：

| 子命令 | 参数 | 行为 |
| --- | --- | --- |
| `grant-console` | `USERNAME` | 授予账号级 `console_read`；幂等 |
| `revoke-console` | `USERNAME` | 撤销账号级 `console_read`（不影响 `transcript_read`）；幂等；账号不存在返回 not_found |
| `grant-transcript` | `USERNAME ROOM_ID` | 授予该账号在该房间的 `transcript_read`；幂等；账号或房间不存在返回 not_found，房间 ID 非法返回 invalid_request |
| `revoke-transcript` | `USERNAME ROOM_ID` | 撤销该授权；幂等 |

授权由 Collaboration 单一持有：`operator_grants` 表（`owner_id`、`scope` ∈ {`console_read`,`transcript_read`}、`room_id`：`console_read` 行为 NULL，`transcript_read` 行指向房间）。Server 网络入口不提供任何授权写接口。CLI 输出不含任何凭证。

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

成功返回 HTTP 200。分页沿用已发布约定：`after` 从 0 开始，排除小于或等于该 ID 的记录，`limit` 默认 50、最大 100，按 ID 递增。所有控制台路由要求 Bearer 操作员凭证（`purpose:"console"` 会话、账号未停用、会话未到期未撤销）且账号持有 `console_read`；凭证缺一格（非 console 会话、成员 token、登记密钥、无效或过期）一律 401 `unauthorized`，凭证有效但缺 `console_read` 为 403 `forbidden`。

| 方法和路径 | 请求 | 成功结果 | 阶段 |
| --- | --- | --- | --- |
| `GET /v1/console/rooms` | — | `{"rooms":[ConsoleRoom]}`，按 `room_id` 稳定排序。只含团队房间，不含 DM。本期 `last_activity` 恒为 null（活动投影为后续阶段） | **本期（stage 2）** |
| `GET /v1/console/rooms/{room_id}` | — | `{"room":ConsoleRoom,"members":[ConsoleMember],"transcript_allowed":boolean}`，`members` 按 `member_id` 稳定排序；`transcript_allowed` 表示该账号当前是否持有此房间的 `transcript_read`，界面据此决定是否显示 Chats 数据与是否发起 transcript 请求 | 后续（roster 详情） |
| `GET /v1/console/rooms/{room_id}/timeline` | `after`、`limit` | `{"activities":[ConsoleActivity]}`，按 `activity_id` 递增；界面用 `after` 向后翻页，页行数小于 `limit` 即到尾。只含元数据，不含消息正文；`message_id` 字段照常返回，界面只在 `transcript_allowed` 为真时提供到 Chats 的跳转 | 后续（依赖 #6/#3） |
| `GET /v1/console/rooms/{room_id}/transcript` | `before`（可选，排他上界）、`limit` | `TranscriptPage`。省略 `before` 返回**最新一页**（页内仍按 `message_id` 递增）并如实给出 `has_more`；携带 `before`（取当前已展示最旧消息的 `message_id`）返回更早的一页。按 `message_id` 递增读取规范房间记录；每次成功响应在同一事务写入一条审计。要求目标房间的 `transcript_read` | 后续（依赖 #6/#3） |

后续阶段路由在本期尚未挂载：对它们的请求按 Gateway 既有语义返回 404 `not_found`。鉴权助手在使用的同一事务内重查 console 会话有效性与所需授权（本期即 `list_console_rooms` 的单事务路径），授权不缓存、不作为投影使用。

Transcript 翻页不使用 `after`：入口请求省略 `before` 取得最新页，之后仅以 `before` 向更早翻页，避免为取“最新一页”而从 0 扫描。已发布的成员历史端点不加 `before`。Timeline 无 `has_more` 字段，界面以短页判定到尾。

## 授权、审计与错误

- Collaboration 在每次 Transcript 请求上重查 `transcript_read`；撤销后携旧游标继续翻页返回 `forbidden`。（后续阶段；本期已实现同事务重查的鉴权路径形态。）
- 审计行由 Collaboration 在读取事务内持久化：操作员 `owner_id`、`room_id`、`action`（`transcript_read`）、分页参数 `after`/`before`/`limit`、Server 记录时间。审计不含消息正文，也不可从审计反推内容。（后续阶段，随 transcript 路由交付。）
- 房间列表、roster 与 Timeline 读取不写审计。
- 错误沿用 `{"error":{"code":"...","message":"..."}}`。新增 403 `forbidden`（凭证有效但缺少所需授权），已加入 Gateway 错误表，仅控制台路由使用：本期即为持有效 console 会话但缺 `console_read` 的请求。401 `unauthorized`、404 `not_found`、422 `invalid_request`、429 `rate_limited` 沿用既有语义。
- 界面错误语义（消费方契约）：任一控制台路由返回 401 即清除内存会话并回到登录页；403 在对应区域显示无权限状态（rooms 区域显示账号无 `console_read`，Chats 显示无 `transcript_read`），不自动重试；404 显示房间不存在；429 与网络错误显示可重试的友好错误。

## 静态托管（本期交付）

- Gateway 同源托管**恰好三个**路径：`/console/`（HTML）、`/console/console.css`、`/console/console.js`，与静态包 `server/src/teamup_server/console/` 内文件一一对应、字节一致。不做目录列举、不接受任何其他 `/console/…` 路径（一律 404 `not_found`），方法不符返回 405。`redirect_slashes` 保持关闭，`/console`（无斜杠）不是入口。
- 响应头：`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-cache`（无修订机制前避免陈旧资源）；媒体类型分别为 `text/html; charset=utf-8`、`text/css; charset=utf-8`、`text/javascript; charset=utf-8`。
- 静态路由不需鉴权，页面内容不含任何私有数据（token 只在 API 鉴权后经 `/v1/console/*` 返回）。控制台无写入 UI；静态托管只做装配，不含业务逻辑。

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

### 本期（stage 2，已实施）

1. Collaboration schema v3：`account_sessions` 增加 `purpose` 列（存量行迁移为登记用途，Collaboration 内部 v2→v3 迁移，无外部迁移工具；该试点迁移可能在 #6 推进版本时由集成协调方 rebase）；新增 `operator_grants` 表（`console_read`、`transcript_read`）。`room_activity` 与 `transcript_access_audit` 两张表及其迁移随后续阶段交付。
2. 账号管理 CLI 新增 `grant-console`、`revoke-console`、`grant-transcript`、`revoke-transcript` 子命令，走 Collaboration 公开操作。
3. Collaboration 实现 `list_console_rooms`：在同一事务内校验 console 会话（目的、账号未停用、未到期未撤销）与 `console_read` 授权，再读房间元数据；无授权的会话不可缓存授权结论。
4. Gateway：`POST /v1/auth/login` 接受可选 `purpose:"console"`；挂 `GET /v1/console/rooms`；错误表加 403 `forbidden`；按精确三路径同源托管静态包并带上文安全响应头。

### 后续（叠加 #6/#3，另行契约修订）

5. `room_activity` 投影表与存量回填，roster 详情与 Timeline 路由。
6. `transcript_access_audit` 与 transcript 路由（含 `before` 分页、同事务审计与撤销语义）。
7. DM 落地时补“DM 不进控制台”的契约测试。

Console UI（静态包 `server/src/teamup_server/console/`，文件 `index.html`、`console.css`、`console.js`，已交付并由本期 Gateway 挂载）：

1. 登录页：用户名口令（`purpose:"console"`）换 token，token 只存内存，登出即清；401 跳回登录页。
2. 房间列表页：名称、成员数、最近活动；选中进入详情。
3. 房间详情：roster（角色、公告确认状态、最近活动、harness **Unknown**）加 Timeline、Chats 两个独立页签。
4. Chats：`transcript_allowed` 为假时页签显示无权限且零请求；为真时按 `has_more` 用 `before=<当前已展示最旧消息 ID>` 向更早翻页。
5. 只手动刷新；无 composer、无 join、无 DM 入口。

## 测试步骤

Server 契约测试（`server/tests/test_console_server.py`，本期）：

1. 无 `console_read` 的账号以 `purpose:"console"` 登录后访问 `/v1/console/rooms` 返回 403 `forbidden`；无效、过期、被撤销（停用/重置口令）的 console 会话返回 401。
2. 跨用途隔离双向验证：console 会话 token 不能登记（`POST /v1/members` 401）、不能调用普通成员 API（401）、不能作为 `member_token`（422）；登记用途登录 token、成员 token、旧登记密钥调用控制台路由 401；省略 `purpose` 的登录保持既有行为。
3. v2 存量库迁移：升级后旧会话按登记用途继续可用，控制台拒绝之；`user_version` 升至 3。
4. `grant-console`/`revoke-console` 经 CLI 生效（403↔200 幂等往返）；`grant-transcript`/`revoke-transcript` CLI 幂等、对缺失账号/房间报错。
5. rooms 载荷：按 `room_id` 稳定排序，字段恰为 `room_id`/`name`/`member_count`/`last_activity`（本期恒 null）；读房间不产生收件事件、SSE 提示或房间写入。
6. 静态托管：三个精确路径字节一致、媒体类型与安全响应头正确；其余 `/console/…` 路径 404、方法不符 405；HTML 只以同源相对路径引用两份资源；（可选）构建 wheel 验证静态包随分发包含。
7. 后续阶段随路由交付：transcript 分页与审计、撤销后旧游标 403、roster 与 Timeline 语义（见上文“后续”）。

Console UI 静态回归测试（`server/tests/test_console_ui.py`，已交付）：文件集合恰好三份、无第三方资源、无存储/cookie 访问、无 HTML 注入口、fetch 目标仅限登录与控制台只读路由、token 不入 URL、陈旧响应防线存在。

## 验收

### 本期（stage 2）

- `purpose:"console"` 登录、授权与 `/v1/console/rooms` 元数据读取按本文生效；跨用途隔离双向成立。
- 静态包三路径经 Gateway 同源托管且带严格 CSP；无任意文件服务、无写入 UI。
- 打开控制台不产生任何聊天动作；授权检查由 Server 在使用事务内执行。

### 后续（随 #6/#3 交付）

- 控制台在 Timeline 呈现房间活动，在 Chats 呈现所选房间会话。
- 缺 transcript 授权的操作员读不到消息正文；元数据授权不隐含正文授权。
- DM 不出现在控制台（DM 落地时补一条契约测试）。

## 待跨 owner 评审的选择

1. ~~**试点前置**~~：工作树已含试点集成，按模型 A 推进。
2. ~~**浏览器持有 `account_token` 的 scope 影响**~~：已由协调方裁定采用 `purpose:"console"` 会话级限定，[账号登录 v1](account-login-v1.md) 已同步修订。
3. `forbidden` 403 已进 Gateway 错误表（本期随 rooms 路由使用）；`before` 翻页参数偏离已发布 `after` 惯例，随 transcript 路由再评审。Gateway owner。
4. **审计行是否含分页参数**（当前提案：含，无正文）。Collaboration owner。（后续阶段。）
5. ~~**静态资源托管**~~：已确认并由本期实现：Gateway 同源托管精确路径 `/console/`、`/console/console.css`、`/console/console.js`，严格 CSP，无任意文件服务。
6. **活动投影表与存量回填策略**。Collaboration owner。（后续阶段。）
7. `docs/modules/README.md` 索引已随本期 stage 2 增补条目。协调方。
