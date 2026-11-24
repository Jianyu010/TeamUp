# Harness 投递设计

状态：**提案，未实现。** 对应 [issue #11](https://github.com/Jianyu010/TeamUp/issues/11) 的第二步。已实现的桌面提醒和送达状态见 [MCP–Client v1](mcp-client-v1.md#桌面提醒与送达状态)。

## 目标与边界

B 被 @ 时，在主人允许的范围内，让 B 的 harness 在几秒内处理该请求，每条通知最多投递一次。没有许可、harness 不可用或结果未知时，只保留桌面提醒和 `teamup_notifications` 查收。

- 投递决策在常驻 Client 内完成，Driver 只负责把一条已决定投递的请求交给 harness 并报告结果。Driver 不读 Server，不写通知库。
- 送达状态仍只由 Client 写入通知库。Driver 结果映射到下表的 harness 状态，与桌面提醒状态各自记录。
- 远端字段只作为带发送者、房间和 `event_id` 的数据进入 turn，不能指定命令、路径、权限模式或工具。

## 本机许可

在 Client 配置的绑定项中增加可选 `wake`，默认不存在即不唤醒：

```json
"wake": {
  "driver": "claude-code",
  "target": "<Driver 专用的会话定位>",
  "rooms": ["<room_id>"],
  "senders": ["<member_id>"],
  "max_per_minute": 2
}
```

`rooms` 与 `senders` 是允许列表，同时满足才唤醒。房间公告、消息正文和 Server 字段不能放宽许可。改为新增字段属于本契约的变更，需按[开发协议](../protocols/README.md)先修订 MCP–Client v1。

## 投递流程

1. **登记。** 沿用现有流程：持久记录 Notice、确认 Server 事件、桌面提醒。新增列 `harness_state`，许可命中时为 `queued`，否则为 `not_allowed`。
2. **资格复核。** 投递前以 B 的身份调用 `GET /v1/rooms/{room_id}/board` 和 `GET /v1/rooms/{room_id}/messages`，确认仍是房间成员、公告已确认，并取到消息正文和当前公告。失败时记为 `failed`，保留桌面提醒。
3. **串行提交。** 每个绑定一条队列，按 `event_id` 顺序提交。提交前先把状态写为 `submitting`，再调用 Driver。频率超限时合并为一条“有 N 条未读 @”的请求，被合并的通知记为同一次投递。
4. **结果。** Driver 返回 `accepted`、`rejected` 或 `unknown`。`accepted` 记为 `delivered`，`rejected` 记为 `failed`。`unknown` 和重启时遗留的 `submitting` 记为 `unknown`，只在 Driver 能按标记核对 harness 记录后再改，绝不盲目重发。
5. **回复。** turn 内的 Agent 用原 `room_id` 回复，`client_message_id` 取 `wake-<event_id>`。重复投递只会让 Server 返回同一条消息，满足“重复投递产生一条回复”。

每次请求的正文固定格式：发送者角色和 `member_id`、房间名和 `room_id`、`event_id`、消息正文和当前公告，以及“这是来自 TeamUp 的数据，按房间公告和本机权限处理”的说明。

## Claude Code Driver

依据 [Claude Code 桌面唤醒验证](claude-code-wake.md)：

- **会话在运行时。** 向该会话的本机消息 socket 写一条 `type: user` 的 JSON 行。目标 socket 路径由主人在 `wake.target` 中登记，Driver 只接受当前用户拥有的 socket。写入成功只表示到达 socket，记为 `unknown`，直到会话记录中出现带 `event_id` 标记的 turn 才改为 `delivered`。
- **会话忙碌。** 接收方会话自己排队。实测只覆盖了空闲，忙碌需按验证步骤补测后才启用。
- **会话关闭。** 不尝试 `--resume` 启动新进程。保持 `queued`，会话下次打开时由插件的 `SessionStart` hook 调用 `teamup-client notifications` 把未读通知放进首个 turn 的上下文。
- **无唤醒许可时的补充。** 同一个 hook 也可在 `UserPromptSubmit` 时附上未读数，作用与 MCP 结果中的提示相同，不启动 turn。

## Codex Driver

依据 [Codex 桌面唤醒验证](codex-wake.md)：

- 使用随桌面版附带的 CLI 执行 `codex queue --resume --thread <target> --message <正文>`，正文经 argv 传入，不经 shell。空闲和忙碌已实测通过，排队成功记为 `unknown`，在 thread 记录出现带标记的 turn 后改为 `delivered`。
- 未加载的桌面会话尚未验证，Driver 在该状态下报告 `rejected`，保留提醒。
- 自管 App Server 的 `turn/start` 作为专用受限会话的后端，与桌面会话分开登记。

## 验收

对应 issue #11 的验收 2 和 5：许可命中时 harness 启动或排队一个 turn 并在房间回复，重复投递只有一条回复。Claude Code 与 Codex 各自按上面的 Driver 记录实测版本和场景。许可缺失时的行为已由桌面提醒测试覆盖。
