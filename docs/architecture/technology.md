# TeamUp 技术选型

MVP 按 **Python 3.12+** 排布 Server、Client 和 MCP 源码，使用类型注解和明确的公开包入口。首版主要验证一次跨机器协作中的授权、事务、收件、恢复和 harness 接入，工作量集中在网络和进程交互。

| 选项 | 适用条件与代价 |
| --- | --- |
| **Python** | 同一语言完成 HTTPS/SSE、SQLite、MCP 和本机进程集成。团队能尽快跑完两机验收。交付时需要管理 Python 环境，模块隔离靠包公开入口、依赖检查和契约测试。 |
| **Rust** | 若 Client 明确需要轻量原生可执行文件、较紧的资源上限或长期跨平台后台部署，值得评估。官方 `rmcp` 可作为 MCP 接入起点。首版采用 Rust 会增加并发与打包的验证工作。 |
| **C++** | 适合已有必须链接的 C++ 库或运行时约束。首版各组件目前没有这类依赖。选择 C++ 需要额外维护 MCP、网络和跨平台构建集成。 |

Server 采用 FastAPI 和单实例 SQLite。Client 采用异步 HTTP 和本机 IPC，MCP 进程使用官方 Python SDK 的 stdio 支持。`uv` workspace 为三个包提供统一依赖解析，并保留各包各自的入口。首个实现锁定依赖版本并提供安装和启动命令。 [FastAPI](https://fastapi.tiangolo.com/advanced/custom-response/) · [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) · [MCP Rust SDK](https://github.com/modelcontextprotocol/rust-sdk) · [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)

接入更多 harness 通过新增 Driver 实现。语言选择独立于 MCP、TeamUp API 和本机 IPC 契约。若以后测得部署或性能瓶颈，可按公开契约替换对应组件。
