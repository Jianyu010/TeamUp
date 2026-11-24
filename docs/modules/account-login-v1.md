# TeamUp 账号登录 v1

首版面向同一团队的可信试用机。开发者各有一个 TeamUp 账号，可绑定多个 Agent。应用账号与 SSH 系统账号独立。在共享 SSH 账号能取得 root 的测试环境里，所有服务器使用者都必须受信。账号、口令摘要、登录会话及成员停用的写入归 Collaboration。

## 账号和管理

- 用户名为 3–64 个小写 ASCII 字符，以字母开头，可含数字、下划线和连字符。账号有不可变的 UUID `owner_id`。
- 管理员在服务器上以服务账号调用 `teamup-server accounts --db PATH create USERNAME` 或 `reset-password USERNAME`，口令从终端无回显输入两遍。可用 `list` 查看状态，用 `disable USERNAME` 停用。管理命令通过 Collaboration 的公开入口执行，并使用同一个业务数据库。Server 的网络入口不提供开户或停用接口。
- 口令须有 12–128 个字符，保留原文，以 Argon2id 摘要存储。重置口令使该开发者的所有人类登录会话失效，已经授予 Agent 的凭证仍有效。永久停用账号使人类登录和所属 Agent 的凭证同时失效，保留历史里的成员 ID。停用操作可重复执行，账号不复用。

## Client–Server 增量

| 方法与路径 | 输入 | 结果 |
| --- | --- | --- |
| `POST /v1/auth/login` | `{"username":"alice","password":"…"}` | HTTP 200，`{"access_token":"…","expires_at":1780000000}`。到期为 Unix 秒，寿命 8 小时。新 token 按随机 32 字节生成，服务端持久化摘要。 |
| `POST /v1/members` | 按现有契约提交绑定。Bearer 可使用有效的人类登录 token | 归属由有效登录会话的 `owner_id` 决定。其余请求、响应和幂等规则按 [原契约](client-server-v1.md)。 |

人类登录 token 仅能登记 Agent。成员 token 仅能调用成员 API。两类 token 和旧登记密钥不得相互兼用。已签发的人类 token 摘要在到期或撤销后保留类型记录。旧 `TEAMUP_ENROLLMENT_KEYS` 在已显式配置的 v0 实例上继续可用，省略变量表示不发旧登记密钥。已有 SQLite 数据库自动升级，原 v0 成员身份和历史可读。本次试用使用新数据库和账号登录。

错误沿用 v1 JSON 形状。错误口令、无效账号和已停用账号统一返回 401 `unauthorized`。非法 JSON 或字段为 422 `invalid_request`。登录对同一用户名每分钟最多 5 次失败，对实例的口令检查总数限每分钟 60 次，超过返回 429 `rate_limited`。限额按单实例进程记录。响应、错误和日志不回显输入口令。

停用和令牌检查以同一 SQLite 权限状态为准。停用提交后，成员的新业务请求均返回 401。已打开的 SSE 在下一次提示或保活前重新检查权限，保活间隔最多 15 秒。已经交付给 Client 的通知保留在本地按绑定查询。

## Client 启动配置

`teamup-client login --config PATH --username USERNAME` 在本机终端无回显读取口令，使用现有 Server URL 和 TLS 信任配置登录，再将 `account_token` 原子写入权限为 0600 的本机配置，成功输出不含 token。初始配置可省略凭证字段。登录失败或等待期间配置文件被修改时，保留用户的文件并提示重试。登录前停止使用同一配置的 Client，成功后重新启动。Client 配置中的 `account_token` 和旧 `enrollment_key` 二选一。

MCP 工具、IPC、Agent 绑定字段和成员凭证形状沿用 [MCP–Client v1](mcp-client-v1.md)。Client 在登记前用其成员 token 查询已有身份，成功即复用。未登记的绑定才用人类登录 token 登记，所以人类登录到期或重新登录不会使现有 Agent 中断。已停用账号的 Agent 仍由 Server 拒绝。

## 试用部署

远端 Server 监听回环，由团队现有 SSH 连接转发到各自的本机回环。SSH 对转发链路加密，TeamUp 账号和密码由开发者只在自己的 Client 中使用。服务器内用独立系统账号运行服务。试用阶段的验收包含两人登录、注册、聊天和 @，以及停用一人后原成员凭证不能继续拉取或发消息。
