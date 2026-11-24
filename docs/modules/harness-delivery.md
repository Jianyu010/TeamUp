# Harness 投递设计

状态：**部分实现。** 会话表、会话接入与批准、唤醒许可和 Claude Code Driver 已按 [MCP–Client v1](mcp-client-v1.md#会话接入) 实现，并有自动化验收。Codex Driver、频率限制和 turn 标记核对尚未实现。对应 [issue #11](https://github.com/Jianyu010/TeamUp/issues/11) 的第二步。已实现的桌面提醒和送达状态见 [MCP–Client v1](mcp-client-v1.md#桌面提醒与送达状态)。

## 目标

A @ B 后，在 B 的主人允许时，B 所在的那条会话在几秒内收到请求并像会话间消息一样处理，每条通知最多投递一次。没有许可、会话不在线或结果未知时，保留桌面提醒和 `teamup_notifications` 查收。

```
A 的会话 ─send_message─▶ Server ─SSE 提示 + inbox─▶ B 的 Client
                                                      │ 1. member_id → 会话表
                                                      │ 2. 本机许可、资格复核
                                                      ▼
                                       Driver(kind) ─▶ B 的会话（Claude Code socket / Codex thread）
```

## 会话表

Client 维护 **成员 → 在线会话** 的映射。一个会话对应一个 TeamUp 成员，房间和角色都属于成员，因此不同会话可加入不同房间，也可用不同角色加入同一个房间。Server 把 @ 解析为 `member_id`，Client 据此找到会话，查找过程与房间无关。

每条记录与 harness 无关，harness 差异只在 `kind` 和对应 Driver 中：

| 字段 | 含义 |
| --- | --- |
| `harness`、`session_id` | harness 种类和它自己的会话标识，例如 Claude Code 的 `CLAUDE_CODE_SESSION_ID`、Codex 的 `threadId`。二者组成 Server 登记的 `(harness_instance_id, session_key)` |
| `member_id` | 该会话的成员。恢复同一会话时得到同一成员，房间成员关系随之保留 |
| `endpoint` | `kind` 加地址，例如 `claude-code` + socket 路径、`codex` + thread ID。Claude Code 另有本机消息 token，只放内存 |
| `lease` | 地址是否仍可用。Claude Code 以 MCP 进程的连接为准，Codex 以最近一次工具调用为准 |
| `wake` | 本机许可，默认关闭 |

会话表只用于本机投递，不作为 TeamUp 授权依据。它由 Client 独占写入，存于用户私有目录。

## 地址发现（2026-11-24 实测）

两项实测均在 macOS 26.6.2 上用记录型 stdio MCP Server 完成，只记录变量名和非秘密值。

- **Claude Code `2.1.330`**（桌面版随附二进制，`-p` 模式）：子 MCP 进程的环境中有该 `claude` 进程自己的 `CLAUDE_CODE_MESSAGING_SOCKET`（`/tmp/cc-socks/<claude pid>.sock`，文件存在）、新的 `CLAUDE_CODE_SESSION_ID` 和 `CLAUDE_CODE_MESSAGING_TOKEN`。认证失败也不影响 MCP 启动。因此 `teamup-mcp` 启动即可知道自己所在会话的地址。
- **Codex `0.171.0-alpha.13`**（`codex exec`）：MCP 进程启动时没有任何 `CODEX_*` 变量。每次 `tools/call` 的 `_meta` 带 `threadId`、`sessionId` 和 `x-codex-turn-metadata.thread_id`。桌面版 ChatGPT 的 app-server 也以干净环境启动 `teamup-mcp`，一个 app-server 下同时有 5 个 `teamup-mcp` 进程，进程与 thread 的对应关系未知。因此 Codex 会话的地址在其首次调用 TeamUp 工具时得到，并按每次调用的 `threadId` 计，不按进程计。

## 登记与租约

现有配置按会话写死 `TEAMUP_BINDING_KEY`。Claude Code 和 Codex 通常让所有会话共用同一份 MCP 配置，所有会话因而会成为同一个成员。本提案改为在会话接入时分配绑定：

1. **安装凭证。** `TEAMUP_BINDING_KEY` 改为每个 harness 安装一份的接入密钥，只证明调用方是主人批准过的 harness 安装，不再代表会话。旧的静态会话绑定继续有效。
2. **接入。** Claude Code 下，`teamup-mcp` 启动时向 Client 发 `attach`，携带 harness、会话 ID、socket 路径和 token，并保持这条 IPC 连接。Codex 下，`teamup-mcp` 在每次工具调用时把 `_meta.threadId` 随请求带给 Client。
3. **批准新会话。** 未见过的会话首次接入时，Client 记为待批准并发桌面提醒。主人用 `teamup-client sessions approve` 批准后，Client 以账号凭证登记该会话的成员。之后同一 `(harness, session_id)` 直接恢复原成员。是否自动批准某个 harness 的全部新会话，由主人在配置中明确选择。
4. **租约。** Claude Code 的条目在 `attach` 连接关闭时失效，会话退出即无残留地址。Codex thread 可被恢复，条目保留到主人移除，可投递性由 Driver 在投递时确认。
5. **校验。** Client 只接受当前系统用户拥有的 socket。消息 token 不写盘，不发往 Server。

## 本机许可

每个会话条目可设置 `wake`：允许的 `rooms` 和 `senders` 列表，同时满足才唤醒，另有每分钟次数上限。默认不唤醒。房间公告、消息正文和 Server 字段不能放宽许可。许可可在批准会话时一并设置，也可之后用 CLI 修改。

## 投递流程

1. **登记。** 沿用已实现流程：持久记录 Notice、确认 Server 事件、桌面提醒。新增 `harness_state`：许可命中且会话在线为 `queued`，许可命中但会话不在线为 `waiting`，未命中为 `not_allowed`。
2. **资格复核。** 投递前以该成员身份读取当前公告和这条消息，确认仍是房间成员、公告已确认。失败时记为 `failed`，保留桌面提醒。
3. **串行提交。** 每个会话一条队列，按 `event_id` 顺序提交。提交前先写 `submitting`。频率超限时合并为一条“有 N 条未读 @”的请求。`waiting` 的通知在会话接入后按顺序进入队列。
4. **结果。** Driver 返回 `accepted`、`rejected` 或 `unknown`，分别记为 `delivered`、`failed`、`unknown`。重启时遗留的 `submitting` 记为 `unknown`。`unknown` 只在 Driver 能按标记核对 harness 记录时改写，不盲目重发。
5. **回复。** turn 内的 Agent 在原 `room_id` 回复，`client_message_id` 取 `wake-<event_id>`。重复投递只会让 Server 返回同一条消息。

提交给会话的正文固定格式：发送者角色和 `member_id`、房间名和 `room_id`、`event_id`、消息正文和当前公告，并说明这是来自 TeamUp 的数据，按房间公告和本机权限处理。远端字段不能指定命令、路径、权限模式或工具。

## Driver

| `kind` | 提交 | 已验证 | 待验证 |
| --- | --- | --- | --- |
| `claude-code` | 向会话 socket 写一条 `type: user` 的 JSON 行，[方法](claude-code-wake.md) | 空闲桌面会话启动 turn | socket 是否要求 `CLAUDE_CODE_MESSAGING_TOKEN`；忙碌会话；`-p` 与桌面会话的差异 |
| `codex` | 随附 CLI 执行 `codex queue --resume --thread <id> --message <正文>`，经 argv 传入，[方法](codex-wake.md) | 空闲、忙碌的已加载桌面 chat | 未加载 thread；桌面版工具调用的 `_meta` 是否与 `codex exec` 相同 |

写入 socket 或排队成功只说明到达 harness 边界，记为 `unknown`。harness 记录中出现带 `event_id` 标记的 turn 后才记为 `delivered`。会话关闭时不另起新进程，保持 `waiting`，下次接入后投递。

## 契约变更

实现前须修订 [MCP–Client v1](mcp-client-v1.md)，并由 Client 与插件 owner 确认：

- IPC 新增 `attach`（长连接）和会话标识字段，`TEAMUP_BINDING_KEY` 的含义扩展为安装凭证，静态会话绑定保持兼容。
- Client CLI 新增会话列表、批准和许可设置命令。
- 通知库新增 `harness_state`。

Client–Server v1 不变，成员仍按 `(owner_id, harness_instance_id, session_key)` 唯一。

## 验收

对应 issue #11 的验收 2 和 5：同一台机器上两条会话以不同角色加入同一房间，@ 其中一条时只有它启动或排队 turn 并在房间回复，重复投递只有一条回复。另一条会话和未获许可的会话只得到桌面提醒。Claude Code 与 Codex 各自记录版本和场景。
