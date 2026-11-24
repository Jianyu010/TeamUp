# TeamUp Server

在团队内网运行一个 Server，对本机 Client 提供 HTTPS 和 SSE。Collaboration 拥有协作状态，Gateway 管理网络连接，Presence 暂存在线观测。所有与房间、成员和停用有关的授权通过 Collaboration 判定。

- `src/teamup_server/` 是源码位置。
- [启动步骤](../docs/modules/quickstart.md) 和 [Client–Server v1](../docs/modules/client-server-v1.md)
- [系统与组件](../docs/architecture/README.md) 定义部署与状态归属。
- [Server 契约及关键时序](../docs/modules/teamup-api.md) 定义各公开操作的语义。
