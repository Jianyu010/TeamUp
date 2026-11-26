# TeamUp 账号登录与试用机实施计划

[账号登录契约](../../modules/account-login-v1.md) 覆盖管理命令、端口、权限和部署。

1. 在 `tests/server/src/teamup_server/` 写失败用例覆盖开户、不同身份、登录和令牌隔离、停用后读写拒绝、原数据库升级。通过 `server/src/teamup_server/` 的 Collaboration 公开入口持有账号状态，补齐 Gateway API 和本机管理命令。
2. 在 `tests/client/shared/src/teamup_client/` 写失败用例覆盖登录保存、配置权限、已有身份恢复和返回错误。通过 `client/shared/src/teamup_client/` 接入已有 HTTP 客户端和 IPC。用 `uv run --no-sync pytest -q` 回归旧路径。
3. 新增端到端账户验收，使用三个真实入口 `teamup-server`、`teamup-client`、`teamup-mcp` 及两个开发者绑定。先在本机执行，再经试用机 SSH 隧道复验。
4. 将实际提交构建为服务器 wheel，在 Linux 试用机安装为回环服务。备好账号管理、SSH 隧道及 Client 安装说明。将轮换密码和停止服务留在授权的管理员入口。
