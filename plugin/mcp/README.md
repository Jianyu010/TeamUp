# 本地 MCP Server

Agent harness 以 stdio 启动本地 MCP 进程。这里负责工具定义、输入输出转换与调用本机 Client 公共接口。Python 源码放在 `src/teamup_mcp/`。运行凭证由 Client 保管，工具进程使用安装时授权的会话绑定。

[Client 和 MCP 契约](../../docs/modules/client-mcp.md)
