# Claude Code 桌面会话唤醒验证

## 方法

Claude Code 的本机跨会话消息使用每会话 Unix socket。受支持的会话可从 `/status` 查看 Peer address，也可在测试会话的终端工具中只读 `CLAUDE_CODE_MESSAGING_SOCKET`。对于同用户的 macOS 会话，手册说明外部连接可以使用该 socket，接收方仍执行其 inbound 许可规则。 [Claude Code 本机收件箱](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)

1. 在 Claude Desktop Code 的本机环境新建无敏感内容的测试会话，先发一条 seed marker 验证目标。使用有交互权限确认的模式，例如 Manual。
2. 只对该测试会话，读取其本机 socket 路径。记录测试环境和状态。连接端检查 socket 属于同一个系统用户。
3. 在目标空闲时，使用本机 socket 发送一条带唯一标记的无副作用测试消息。当前实验使用以换行结尾的 JSON，字段 `type` 为 `user`，`message` 中含 `role` 和 `content`。记录 socket 写入和原桌面窗口回复，两者各自取证。 [本机消息帧复现记录](https://github.com/anthropics/claude-code/issues/103048)
4. 原会话无需人工提交新提示便启动 turn，并回显目标标记时，记为唤醒通过。收到或写入 socket 只能证明到达相应通信边界。忙碌和未加载需要另测。

## 2026-11-24 实测

macOS 26.6.2，Claude Desktop `6.28899.6`，测试会话运行的随附 Claude Code 为 `2.1.330`，另装 CLI 为 `2.1.312`。Desktop 已登录，另装 CLI 的 OAuth 会话已过期，`--resume` 在模型运行前即失败。桌面测试会话的 `/status` 未显示 Peer address，通过该会话的终端工具读取到了本机 socket 路径。

**空闲的真实桌面会话通过。** 向用户确认的同用户 socket 写入一条带标记的请求后，原会话自行启动并返回精确标记。测试会话的本地记录中，seed 回复、socket 请求、wake 回复按此顺序出现，三项的 session ID 相同。桌面截图也显示该输入标为跨会话消息，并显示精确回复。忙碌、已结束或未加载的 Claude Desktop 会话尚未验证。TeamUp v1 的 @ 路径只发通知，harness 唤醒仍由独立实验验证。
