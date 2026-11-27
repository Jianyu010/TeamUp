# TeamUp Development View

## Current repository layout

```text
TeamUp/
├── AGENTS.md                      # Project invariants and required reading
├── README.md                      # Product overview, setup, and GitHub activity
├── LICENSE
├── dashboards/                    # Repository dashboards and metric definitions
├── client/
│   ├── macos/desktop/             # macOS Electron console and packaging
│   ├── windows-wsl/desktop/       # Windows Electron console, WSL2 launcher, and installer
│   └── shared/src/teamup_client/  # Shared Client runtime and console assets
├── server/
│   └── src/teamup_server/         # Team Server source
├── plugin/
│   ├── mcp/src/teamup_mcp/        # MCP tool adapter and wiring
│   └── skills/                    # Skills installed with the agent plugin
├── tests/                         # Source-mirrored and cross-component test suites
│   ├── client/                    # Platform shells and shared Client
│   ├── server/                    # Server
│   ├── plugin/                    # MCP tool adapter
│   └── scripts/                   # CI scripts
└── docs/
    ├── protocols/                 # Repository-wide development rules
    ├── architecture/               # System and subsystem design and technology choices
    ├── modules/                    # Component contracts and interaction sequences
    └── specs/                      # Product behavior and acceptance criteria
```

## Where implementation code belongs

| Directory | Modules and boundaries |
| --- | --- |
| `server/src/teamup_server/` | `gateway`, `collaboration`, and `presence` each expose a public interface. Application tables and migrations belong to `collaboration`. The server startup entry point is the composition root. |
| `client/shared/src/teamup_client/` | Shared Client runtime. Session coordination, local IPC, Server API access, harness drivers, and notifications have separate responsibilities. The Client entry point is the composition root and exposes a local protocol for CLI and MCP callers. |
| `client/macos/desktop/` | macOS Electron console. It packages the shared static UI and accesses the local Client through the [Client console interface](../modules/client-console.md). |
| `client/windows-wsl/desktop/` | Windows Electron console using the shared renderer and published Client console interface, with the Client running in WSL2. Platform tests are in `tests/client/windows-wsl/desktop/`; `client/windows/` is reserved for a native Windows Client. |
| `plugin/mcp/src/teamup_mcp/` | MCP entry point, tool names and schemas, and call translation. It uses the Client's public IPC wrapper to reach the local service. |
| `plugin/skills/` | Each workflow has a directory containing a `SKILL.md`. The corresponding code owners maintain tool schemas and authorization logic. |
| `dashboards/` | Dashboards and descriptions of their data sources. Dashboards that need private-network state query an authorized API. |

`server`, `client/shared`, and `plugin/mcp` are the three Python packages in the uv workspace. The Client and MCP entry points can be installed in the same workspace environment. Platform directories and the mirrored test layout follow the [development protocol](../protocols/README.md#client-platforms-and-test-layout).

## Dependencies and verification

- The Server and Client communicate over HTTPS and SSE. Each Python package is independent of the other. The MCP package calls only the Client's public IPC wrapper. The Client can receive events independently of the MCP package and skill files.
- Only Collaboration can access the application SQLite database. Each composition root depends on public interfaces exposed by production components. Keep composition roots out of other modules' dependencies. CI adds import boundary checks and contract tests for public interfaces with the first implementation.
- The API, MCP, and local IPC evolve independently. Providers of cross-process contracts own compatibility checks. Extend the published interfaces through their contracts and keep internal objects and database tables private.
