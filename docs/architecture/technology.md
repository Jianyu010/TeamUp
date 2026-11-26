# TeamUp Technology Choices

The MVP uses **Python 3.12+** for the Server, Client, and MCP packages, with type annotations and explicit public package interfaces. The first release focuses on validating authorization, transactions, inbox delivery, recovery, and harness integration in a collaboration scenario spanning two machines. Most of the work centers on network and interprocess communication.

| Option | When it fits and what it costs |
| --- | --- |
| **Python** | Handles HTTPS and SSE, SQLite, MCP, and local process integration in one language. It lets the team get through two-machine acceptance testing quickly. Distribution requires managing the Python environment. Public package interfaces, dependency checks, and contract tests enforce module boundaries. |
| **Rust** | Worth evaluating if the Client needs a lightweight native executable, tighter resource limits, or long-term deployment as a cross-platform background service. The official `rmcp` SDK provides a starting point for MCP integration. Using Rust in the first release would add concurrency and packaging verification work. |
| **C++** | Makes sense when required C++ libraries or runtime constraints dictate the choice. The first release's components are free of such dependencies. C++ would add maintenance work for MCP, networking, and cross-platform build integration. |

The Server uses FastAPI and a single SQLite instance. The Client uses asynchronous HTTP and local IPC. The MCP process uses the official Python SDK's stdio transport. A `uv` workspace resolves dependencies together for all three packages while each package retains its own entry points. The first implementation pins dependency versions and provides install and startup commands. [FastAPI](https://fastapi.tiangolo.com/advanced/custom-response/) · [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) · [MCP Rust SDK](https://github.com/modelcontextprotocol/rust-sdk) · [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)

Each additional harness is supported by implementing another Driver. The MCP, TeamUp API, and local IPC contracts are independent of the implementation language. If measurements later uncover a deployment or performance bottleneck, the affected component can be replaced behind its public contract.
