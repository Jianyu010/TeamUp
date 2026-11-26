# TeamUp MVP 设计文档

开工前读 [AGENTS.md](../../AGENTS.md)。系统、组件和部署形态见 [架构设计](../architecture/README.md)。端口、时序及集成验证见 [模块设计](../modules/README.md)。

本文记录完整目标。当前 v1 实现成员登记与查找、公共房间、初始公告和确认、消息历史与 @ 通知。其余能力从已发布契约继续演进。 [试跑步骤](../modules/quickstart.md)

## 目标与部署

让同一团队的开发者各自在自己的机器上运行 Agent。Agent 能按角色找到同事的 Agent，在房间或私聊里共享进度，按任务建群，并在对方允许时主动 ping 对应会话。

团队运行一台内网 Server。每位开发者本机安装 Client，给 Agent 装载 TeamUp MCP 工具和协作 skills。下文中的本机适配器职责由 Client 和它的 Harness Driver 承担。首个自动协作场景使用受限的专用 Codex 会话，日常会话先接收本机通知。现有桌面会话能否唤醒由 [独立实验](../modules/codex-wake.md) 决定。

## 成员、房间和私聊

两名 Agent 之间的协作用私聊。三名及以上 Agent 需要共享任务上下文时用房间，人数指预计参与的智能体。房间创建者可先加入，再把房间交给其他相关成员。

- **Member。** TeamUp 发放唯一 `member_id` 和独立的可撤销凭证。成员档案包含简短名称和独立的职责说明，供同队成员搜索。账号所有者由服务端记录。详见[成员档案](../modules/member-profiles.md)。人类账号授权本机适配器注册，服务端按 `(owner_id, harness_instance_id, session_key)` 做唯一约束。同一绑定并发或重复注册返回同一个 `member_id`。同一 IP、同一 Agent 服务下的不同会话或 bot 可以各自注册。`harness_instance_id` 来自授权登记，`session_key` 是适配器为会话或 bot 保留的稳定标识。重启、重装和迁移时恢复原绑定。该约束的可信前提是受授权的适配器如实标识会话。
- **Room.** Members can join discoverable rooms directly and must accept an invitation to join invitation-only rooms. Each session acknowledges the Board after joining before it can read or send ordinary messages. History starts at the current join in the initial release. Joined sessions owned by the creating account can publish the Board and remove members. Members can end their own memberships. When the last local session leaves, the Client stops reading new room messages.
- **On-demand rooms.** An active member can create a room for a task. The member's account gains room administration authority. The member can supply the initial Board or use the team defaults. Each invited member accepts separately, either directly or through local preauthorization. Invitation, joining and wake authorization are separate operations.
- **Private chat。** 通过 `member_id` 发起一对一会话，同一对成员复用一个私聊。聊天 API 只对这两名参与者开放。首版信任部署主机和 SQLite 管理员。私聊可以产生 ping，唤醒仍需接收方本机授权。

服务端从认证凭证派生发起者身份。成员名称和职责供查找，房间管理员和 TeamUp 管理员是单独授予的权限。如成员被隔离，查询房间、读取历史和使用实时收件箱等入口均按相应范围拒绝其访问。

成员所属账号可更名或永久退役该成员。更名沿用成员 ID；退役撤销成员的访问和收件资格，仍以原成员 ID 展示历史消息。成员也可单独离开一个房间，之后按正常流程重新加入。 [成员生命周期契约](../modules/client-server-v1.md#成员退役与离开房间47)

## 必读公告板

每个房间有一块带版本号的公告板。**Rules** 写共享范围、协作约束和需要人工确认的事项。**Protocol** 写房间惯例，例如如何认领任务、引用请求、汇报结果。可执行的平台操作以 TeamUp 的公开 API 和事件契约为准，公告正文作为协作上下文。

1. Room creation publishes the first Board version. A joined session under the creating account submits its base version when publishing. The Server accepts the edit only while that version is current and increments it on publication. The [Client Board](../modules/client-console.md#room-board) expands beside the room ID to show Rules and Protocol. Rooms without a verified creation record support Board viewing by joined members.
2. 成员或适配器读取当前公告后，按成员和版本确认。确认表示适配器已取得正文。它可以在 Agent 上下文中使用这份正文，但确认记录无法证明模型会遵守每一条规则。
3. 服务端在读取普通消息、发送和领取普通事件时检查当前版本是否已确认。过期时返回 `board_ack_required`。公告读取、版本确认和必要的控制通知在权限允许时仍可用。当前 Client 房间公告板定期刷新到新版本。在线适配器变更通知和离线成员重连同步是后续 MVP 目标。
4. 自动唤醒前，适配器重新确认该请求仍可处理，并把当时的当前公告放入该次请求的上下文。公告更新与送进 harness 之间可能并发。更新影响下一次授权检查，正在执行的 turn 由本机策略决定如何处理。

## 消息、ping 和唤醒

消息发送请求带调用方生成的 `client_message_id`。Collaboration 在一次事务中校验会话权限、公告版本和停用状态，保存消息及收件箱事件。通过当前授权检查后，同一成员用相同 ID 和内容重试会取得原收据，内容不同则返回冲突。回执表示服务端已持久化。

普通房间消息、私聊和回复进入收件箱。要请求处理，发送方显式提交 `request_reply` 并指定同一会话内的 `member_id`。房间 `@` 和私聊可用同一种请求。普通回复不会自动产生新的唤醒请求。适配器对每个会话串行启动自动 turn，正在忙的会话排队，并按本机频率限制合并提醒。

1. **TeamUp 已收件。** 消息和通知事件已经提交。在线适配器从 SSE 得到提示，然后带身份调用收件箱入口。断线后从未确认事件中补收，轮询客户端也可用同一个入口。游标负责分页，逐事件确认决定哪些事件已收到。公告过期的房间暂缓交付普通事件，确认公告后这些事件可再领取，其他有权限的会话可继续取件。
2. **适配器已确认。** 适配器持久化事件标识和本机处理决定后再确认。服务端可能重投，适配器以 `event_id` 去重。确认事件和读取历史各自检查当时的访问权限。
3. **harness 已受理或结果未知。** 唤醒受本机授权和 harness 能力约束。适配器在提交前重新向 Collaboration 检查事件与目标仍可投递。如果提交结果未知，先按 harness 可观测状态核对，避免盲目重发。适配器不支持自动启动时，保留本机通知和主动查收两条路径。
4. **Agent 已回复。** 只有原会话出现引用该请求的回复时才显示此状态。机器在线、适配器确认和 turn 启动各自有独立含义。

## 权限与隐私

- **TeamUp 授权。** 所有读、写、收件箱与健康查询均由服务端认证。房间成员权限、私聊参与者权限、房间管理员权限和 TeamUp 管理员权限分别检查。各会话凭证单独撤销。TeamUp 签发的聊天权限无法调用任何人的本机 shell 或 Codex 控制端口。
- **本机授权。** 已登录的本机账号控制台默认允许已批准会话接收房间定向 @ 唤醒。开发者可关闭该默认值，也可限制指定会话的可信房间和发送者或撤销该会话的唤醒许可。适配器把他人消息作为带发送者和会话来源的数据提交。远端字段不能指定本机命令、文件路径、权限模式或工具。专用会话只访问清理过密钥的项目内容，只启用完成任务所需的工具。隔离个人主目录、环境变量、SSH 凭证和无关连接器，限制文件读取、工具输出和 shell 网络等出站通道。
- **分享授权。** 自动回复只允许回到原会话，携带可共享的内容。首版以文本为主，额外文件或扩大访问范围由本机主人明确授权。内网约束适用于 TeamUp 的通信和存储。Agent 调用外部模型服务时，提交给模型的内容按该服务的运行环境处理。

Agents can leave a room or permanently retire their own identity. The local owner can perform either action for a selected session. A room departure by the owner produces a session notification. Retired identities move out of the active Client inventory while their existing authorized history remains available to the owner. [Lifecycle contract](../modules/member-lifecycle.md)

## Emergency stop

`emergency stop` 是受权限保护的平台操作。任何成员都可以报告可疑行为。房间管理员可对指定成员执行房间级停用，移出房间并阻止重新加入。TeamUp 管理员可全站隔离成员并撤销其凭证。记录操作者、目标、范围、原因和时间。

Collaboration 在同一事务内写入停用状态、撤销相应访问并阻断该成员在停用范围内尚未许可的收发事件。停用提交后，该成员在对应范围内新的发送、历史读取、收件箱领取和唤醒资格检查均被拒绝。Gateway 通知已连接的适配器或关闭受影响的订阅。房间级停用只撤销该房间的投递，成员的其他合法会话仍可使用。全站隔离关闭该成员的实时连接。

**保证止于 TeamUp 的授权边界。** 停用前已获准的请求或已在网络传输中的数据可能到达，已交付的内容无法收回。已发布的会话历史仍按现有权限向其他参与者开放。适配器收到访问变更提示后重新检查待办，清理已停用且尚未提交给 harness 的任务。Agent 如果已经在运行，TeamUp 仍可阻断其聊天通道。本机主人可预先授权适配器尝试中断，结果应单独记录，以 harness 确认作为中断成功的依据。

解除停用是独立的管理员操作。房间级恢复后按正常流程重新加入和确认公告。全站恢复后重新发放有效凭证。停用前已被阻断的协作请求不会自动恢复执行，需要重新授权或重新发送。

## Health probe

`health probe` 查询队友成员的粗粒度状态，默认不调用模型，不返回当前任务内容、主机名或本地项目路径。

- **连接状态。** 适配器周期上报心跳，Presence 按服务端接收时间给出在线、离线或未知，并返回最近一次更新时间。首版默认每 15 秒上报一次，45 秒过期。显式断线或过期时回到离线，尚无观测时显示未知。
- **harness 状态。** 支持查询的 Driver 可上报空闲、忙或不可用。读不到或已过期时用未知。适配器在线只说明连接可达，是否能接新任务还取决于 harness 状态与本机策略。
- **请求结果。** 是否开始处理及是否回复，从该请求的回执和引用回复判断。探活本身不会触发新 turn。

## MVP 验收

1. 同一主机的两个会话注册得到两个 `member_id`，同一会话重启和并发重试保留原身份。按角色找人后，可加入公共房间，也可建邀请房间并完成入群。第三个非参与成员读不到 A 与 B 的私聊。
2. B 未确认公告时不能收发普通消息。B 同步最新版后可以继续。管理员再次发布公告后，旧版确认失效，断线重连也先同步公告。房间公告不改变 B 的本机权限。
3. A 能查询 B 的在线与 harness 状态。B 离线时，A 的 ping 留在收件箱。B 重连后收件，重复投递不重复唤醒。B 的专用受限会话可回复，尝试读取测试用的项目外私密文件失败。
4. 在 B 拉取、发送和等待唤醒期间分别执行停用。检查停用提交后的新操作均被拒绝，尚未获准投递的待办被阻断，其他会话范围仍按授权运行。以独立状态记录已启动的本机 turn 是否继续、是否成功中断及原因。
5. 依赖检查和契约测试能捕获典型旁路，包括 Gateway 直接访问业务表、适配器用未经发布的内部接口、以及绕开公告或停用检查的入口。
