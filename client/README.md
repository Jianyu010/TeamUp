# TeamUp Client

每位开发者安装一个 Client，在本机维护多个独立会话的绑定、凭证和唤醒许可。常驻进程连接团队 Server，负责事件收取、补收、提醒和 harness 调用。CLI 和 MCP 通过同一个本机公开入口请求操作。

- `src/teamup_client/` 是源码位置。
- [启动与配置](../docs/modules/quickstart.md) 和 [本机 IPC v1](../docs/modules/mcp-client-v1.md)
- [开发视图](../docs/architecture/development-view.md) 定义模块导入方向。
- [Client 和 MCP 契约](../docs/modules/client-mcp.md) 定义本机接口、绑定和会话生命周期。

[Build and open the desktop console](desktop/README.md) as TeamUp.app on macOS or TeamUp.exe on Windows x64 with a WSL2 Client backend. The browser entry is available with `teamup-client serve --config PATH --console`. See the [console contract](../docs/modules/client-console.md) for supported reads and task actions.
