# TeamUp 开发协议

本页适用于所有开发者和 Agent。固定不变量见 [AGENTS.md](../../AGENTS.md)。

## 变更契约

1. 先读当前能力的规格、架构和模块契约，确认影响范围与 owner。目录位置只用于导航，公开入口由契约声明。
2. 涉及公开字段、授权、错误、事件、重试或数据归属时，先在对应契约写出变更。记录调用场景、提供方、受影响的调用方和兼容性。
3. 由提供方与受影响的调用方 owner 确认契约后修改实现。遇到所需边界尚未提供时，先补契约，避免在调用方另开通道。

## Client 平台与测试目录

- 跨平台 Client 运行时和控制台静态资源放在 `client/shared/`。平台专属代码放在 `client/macos/`、`client/windows/` 或 `client/windows-wsl/`。面向 Windows 且依赖 WSL 的客户端放在 `client/windows-wsl/`，包括该应用的 Windows 启动器和安装器。
- 测试和测试专用资源统一放在仓库根目录的 `tests/`。面向单个组件的测试按被测源码目录镜像，例如 `client/macos/desktop/` 对应 `tests/client/macos/desktop/`，`server/src/teamup_server/` 对应 `tests/server/src/teamup_server/`。
- 跨组件的集成、系统、契约、安装与场景测试按套件放在 `tests/` 下。

## 验证和状态

- 记录提案、已确定的设计、已实现的行为和实测结果。实测附环境及可重复的检查步骤。
- 变更依赖边界时，检查跨组件导入与公开入口。变更授权或状态语义时，验证正常路径、跨权限路径、重试和相关并发路径。
- 受影响的规范、架构和模块文档随同修改。已接受的规则在相应的正式位置生效。
