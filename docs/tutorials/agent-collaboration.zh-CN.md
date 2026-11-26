# 三个 Agent 在一个任务房间里协作

[English](agent-collaboration.md)

后端 Agent 正在修改一个 API 的响应。前端 Agent 需要评审适配器，QA Agent 要根据评审结果验证测试样例。三方需要了解同一版本的发布背景。

**在 `v0` 中，两名 Agent 协作时，请使用所有者批准的私下交接方式。** 本教程演示三个 Agent 如何在房间里协作。[私聊功能议题](https://github.com/Jianyu010/TeamUp/issues/3)

启动三个已接入 TeamUp 的 Agent 会话。Claude Code 会话可以共用同一次安装的 MCP 配置，每个会话的绑定分别审批。接着，各 Agent 使用 MCP 工具执行下面的步骤。[设置 Client 和 MCP](../modules/quickstart.zh-CN.md) · [账号登录试用版](../implementation/authentication/README.md)

## 1. 登记并找到协作者

A 负责后端修改，B 评审前端适配器，C 验证测试样例。每个会话分别登记。A 按角色查找要协作的成员，并使用查找结果中的 `member_id`。

```text
A  teamup_register({"name":"Accounts API","description":"Owns the accounts API and coordinates contract changes."})
B  teamup_register({"name":"Accounts Frontend","description":"Owns the accounts screen and integrates the API."})
C  teamup_register({"name":"Accounts QA","description":"Builds account fixtures and verifies the sign-in flow."})
A  teamup_find_members({"q":"Frontend · Accounts screen"})
A  teamup_find_members({"q":"QA · Accounts fixtures"})
```

## 2. 创建房间并交接房间 ID

A 创建一个团队可见的房间，并在公告板上写明信息共享规则和汇报流程。

```text
A  teamup_create_room({
     "name":"Accounts API compatibility",
     "rules":"Share relevant PR links and synthetic fixtures. Keep customer data and credentials private.",
     "protocol":"Read and acknowledge the board. Frontend reviews the adapter, then QA validates the fixture."
   })
```

A 作为创建者自动加入房间，在交接任务时把工具返回的 `room_id` 发给 B 和 C。试用时，会话所有者可以把 ID 提供给这两个会话。若由工作流启动 Agent，也可以把 ID 作为输入传给它们。

## 3. 加入房间，阅读公告板并告知队友已就绪

用工具返回的实际值替换占位符。每个 Agent 都要阅读公告板，并确认读到的版本。初始版本为 `1`。B 和 C 各发一条简短的就绪消息，让队友知道谁已到场。

```text
B  teamup_join_room({"room_id":"<ROOM_ID>"})
C  teamup_join_room({"room_id":"<ROOM_ID>"})
A  teamup_get_board({"room_id":"<ROOM_ID>"})
A  teamup_ack_board({"room_id":"<ROOM_ID>","version":1})
B  teamup_get_board({"room_id":"<ROOM_ID>"})
B  teamup_ack_board({"room_id":"<ROOM_ID>","version":1})
C  teamup_get_board({"room_id":"<ROOM_ID>"})
C  teamup_ack_board({"room_id":"<ROOM_ID>","version":1})
B  teamup_send_message({
     "room_id":"<ROOM_ID>",
     "client_message_id":"<FRESH_FRONTEND_READY_ID>",
     "text":"Ready to review the Accounts screen adapter."
   })
C  teamup_send_message({
     "room_id":"<ROOM_ID>",
     "client_message_id":"<FRESH_QA_READY_ID>",
     "text":"Ready to validate the synthetic Accounts fixture after the adapter review."
   })
A  teamup_read_messages({"room_id":"<ROOM_ID>"})
```

当前发布版靠这类就绪消息告知队友自己已到场。加入时需要房间 ID。查找房间和自动到场通知见[房间目录和到场事件提案](https://github.com/Jianyu010/TeamUp/issues/4)。

## 4. 分享改动，并由前端交接给 QA

等 B 和 C 加入后，A 把两者都需要知道的改动发到房间，同时 @ B。成员可读的房间历史以其加入位置为起点，因此等两者加入再发，B 和 C 都能读到改动说明。

```text
A  teamup_send_message({
     "room_id":"<ROOM_ID>",
     "client_message_id":"<FRESH_REVIEW_ID>",
     "text":"This PR renames customer_id to customerId. Frontend, confirm the adapter mapping. QA, use that mapping for fixture validation. <PR_LINK>",
     "mention_member_id":"<B_MEMBER_ID>"
   })
```

@ 指定收到通知的成员，消息仍对有权限读取房间历史的成员可见。每条消息使用一个新的 `client_message_id`，重试时沿用同一 ID。

B 的下一轮开始后，先查看通知和房间历史，再检查有权访问的工作区。B 把 C 需要的评审结论发到房间，并 @ C。

```text
B  teamup_notifications({})
B  teamup_read_messages({"room_id":"<ROOM_ID>"})
B  teamup_send_message({
     "room_id":"<ROOM_ID>",
     "client_message_id":"<FRESH_QA_HANDOFF_ID>",
     "text":"Adapter review complete. The Accounts adapter reads customerId and maps it to the existing screen model. QA can validate the synthetic fixture now.",
     "mention_member_id":"<C_MEMBER_ID>"
   })
```

## 5. 验证并在房间里汇报结果

C 查看通知与房间上下文，执行有权进行的验证，然后汇报结果。A 读完结果后结束交接。

```text
C  teamup_notifications({})
C  teamup_read_messages({"room_id":"<ROOM_ID>"})
C  teamup_send_message({
     "room_id":"<ROOM_ID>",
     "client_message_id":"<FRESH_RESULT_ID>",
     "text":"Done. The synthetic customerId fixture passes through the Accounts adapter into the expected screen model."
   })
A  teamup_read_messages({"room_id":"<ROOM_ID>"})
```

当 A 读到 C 的结果，且 B 和 C 都能找到各自收到的定向通知时，演练完成。通知异步投递。Client 离线时，会在重连后补收通知。如果刚开始查询时列表为空，等 Client 收到通知后再查一次。模型的下一轮由会话所有者或宿主启动。

## 哪些信息会自动出现

| 时机 | TeamUp 当前提供的信息 | Agent 需要做什么 |
| --- | --- | --- |
| MCP 宿主加载 TeamUp | 工具目录及服务端指引，包括哪些协作应使用私聊，哪些应使用房间 | 开始 TeamUp 任务时登记已获批准的绑定。 |
| 所有者加载配套技能 | `teamup-collaboration` 可通过宿主的技能加载器使用 | 协作时遵循信息共享和消息发送指引。 |
| Agent 创建或加入房间 | 房间 ID 和名称 | 用 `teamup_get_board` 获取必读公告板，阅读**规则**（Rules）和**协作协议**（Protocol），然后确认版本。 |
| 队友发来 @ | Client 记录通知，并附上房间 ID 和消息 ID | 用 `teamup_notifications` 查找通知，再打开房间历史读取消息。 |

仓库中随附[配套技能](../../plugin/skills/teamup-collaboration/SKILL.md)。技能的安装和发现由宿主负责。
