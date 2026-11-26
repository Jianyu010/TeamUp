# TeamUp 开发视图

## 当前仓库排布

```text
TeamUp/
├── AGENTS.md                      # 固定不变量与必读入口
├── README.md                      # 项目效果、接入方式、GitHub 活动
├── LICENSE
├── dashboards/                    # 仓库看板与指标口径
├── client/
│   ├── macos/desktop/              # macOS Electron 控制台与打包
│   └── shared/src/teamup_client/   # 共用 Client 运行时与控制台资源
├── server/
│   └── src/teamup_server/          # 团队 Server 源码
├── plugin/
│   ├── mcp/src/teamup_mcp/         # MCP 工具适配与组装
│   └── skills/                    # 随 Agent 插件安装的 skills
├── tests/                         # 源码镜像与跨组件测试套件
│   ├── client/                    # macOS 外壳、共用 Client
│   ├── server/                    # Server
│   ├── plugin/                    # MCP 工具适配
│   └── scripts/                   # CI 脚本
└── docs/
    ├── protocols/                 # 全仓开发准则
    ├── architecture/              # 系统和子系统设计、技术选型
    ├── modules/                   # 组件契约、交互与时序
    └── specs/                     # 产品行为与验收
```

## 实现时的代码落点

| 目录 | 模块与边界 |
| --- | --- |
| `server/src/teamup_server/` | `gateway`、`collaboration`、`presence` 各有公开入口。业务表与迁移在 `collaboration` 内。Serve 入口是组合根。 |
| `client/shared/src/teamup_client/` | 共用 Client 运行时。会话协调器、本机 IPC、Server API、harness driver 和通知各有自己的职责。Client 入口是组合根，公开本机协议供 CLI 和 MCP 使用。 |
| `client/macos/desktop/` | macOS Electron 控制台，打包共用的静态界面，通过 [Client 控制台接口](../modules/client-console.md)访问本机 Client。 |
| `plugin/mcp/src/teamup_mcp/` | MCP 入口、工具名称和 schema、调用转换。通过 Client 的公开 IPC 调用封装访问本机服务。 |
| `plugin/skills/` | 每个工作流一个 `SKILL.md` 目录。工具 schema 和授权逻辑由各自的代码 owner 维护。 |
| `dashboards/` | 看板和数据来源说明。需要内网状态时使用授权 API。 |

`server`、`client/shared`、`plugin/mcp` 是 uv workspace 的三个 Python 包。Client 和 MCP 入口可在同一个 workspace 环境安装。平台目录和测试镜像遵循 [开发协议](../protocols/README.md#client-平台与测试目录)。

## 依赖与验证

- Server 和 Client 通过 HTTPS/SSE 协议交互，Python 包不相互导入。MCP 包只使用 Client 发布的 IPC 调用封装。Client 无需导入 MCP 包或读取 skills 即可接收事件。
- 业务 SQLite 访问只允许出现在 Collaboration。各组合根依赖生产组件的公开入口，其他模块不得依赖组合根。CI 随首个实现加入导入边界检查和公开端口的契约测试。
- API、MCP 和本机 IPC 分别演进。跨进程契约的提供方负责兼容性检查，禁止通过共享内部对象或数据库表扩展接口。
