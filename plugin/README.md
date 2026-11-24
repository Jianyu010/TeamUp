# TeamUp Agent 插件

插件是 Agent harness 的接入和分发层。`mcp/` 实现本地 MCP Server，`skills/` 存放随插件安装的 Agent Skills，供 Agent 在调用工具时参考。属主绑定、权限和后台接收由本机 Client 管理。

- [MCP 实现](mcp/README.md)
- [Skills](skills/README.md)
- [Client 和 MCP 契约](../docs/modules/client-mcp.md)
