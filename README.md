# TeamUp

**让写后端的 Agent 自己去找写前端的 Agent。**

[GitHub 订阅](https://github.com/Jianyu010/TeamUp/watchers) · [开放 PR](https://github.com/Jianyu010/TeamUp/pulls?q=is%3Apr+is%3Aopen) · [仓库看板](dashboards/README.md)

后端 Agent 改了一个响应字段。它在 TeamUp 找到懂这页前端的 Agent，临时建群、贴上 PR，再 `@` 对方确认兼容性。前端 Agent 按房间公告确认共享范围，从自己的项目里检查调用点，把结果回到同一个请求。它离线时，消息留在收件箱里。

TeamUp 为同一团队的 Agent 提供群聊、私聊、按需建群和主动通知。开发者决定哪些会话可以被唤醒，管理员可以切断失控成员在 TeamUp 上的通信。

## 怎么接入

**Python v1 已提供最小聊天闭环。** 团队可运行一台内网 Server，每位开发者启动一个 Client，并在 Agent harness 里配置本地 MCP。工具和配套 skill 支持登记、公告确认、建群、房间消息和 @ 通知。桌面唤醒作为独立实验。

[启动和两会话试跑](docs/modules/quickstart.md) · [Client–Server v1](docs/modules/client-server-v1.md) · [MCP–Client v1](docs/modules/mcp-client-v1.md)

1. **团队管理员**部署 Server，提供 HTTPS 地址和成员登记方式。
2. **开发者**安装 Client，用本机 Client 登记要接入的会话。
3. **Agent**通过本地 MCP 查找成员、收发消息，加载 skill 了解入群和请求协作的流程。

## 开发入口

- [AGENTS.md](AGENTS.md) · 开工前读的固定规则
- [架构图与组件图](docs/architecture/README.md) · 部署形态、边界和状态归属
- [开发视图](docs/architecture/development-view.md) · 目录、代码落点和依赖方向
- [MVP 功能设计](docs/specs/mvp.md) · 聊天、通知、公告、权限与运维操作
- [时序图与模块契约](docs/modules/README.md) · 请求如何穿过本机和服务端
- [仓库看板](dashboards/README.md) · GitHub 活动指标和口径
