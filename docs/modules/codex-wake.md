# Codex 桌面会话唤醒验证

## 2026-11-24 实测

环境为 macOS 26.6.2、ChatGPT 桌面版 `26.1117.41037`、随应用附带的 Codex CLI `0.171.0-alpha.13`。另装的 Homebrew CLI `0.163.0` 的帮助未列出 `--resume`。测试使用无敏感内容的专用桌面 chat，记录发送标记、目标会话 ID、turn ID 与完成状态。

| 现有桌面会话状态 | 结果 | 证据 |
| --- | --- | --- |
| 空闲、已加载 | 通过 | 从桌面外调用随附 CLI 的 `codex queue --disable daemon_auto_start --resume --thread … --message …`。CLI 报告已排队，同时提示无法用 daemon 恢复。大约 9 秒后，原桌面 chat 自行启动了新 turn，完成并回复测试标记。 |
| 忙碌、已加载 | 通过 | 原会话执行 20 秒无副作用测试指令时，使用同一 CLI 排队下一条标记。前一轮先完成，随后同一桌面会话启动独立新 turn 并回复第二个标记。桌面应用当时在后台。 |
| 未加载或关闭 | 未验证 | 桌面在后台超过 60 秒，目标仍报告 `idle`。默认 daemon 控制套接字不可连接。现有桌面应用保持运行。 |

隔离 App Server 通过自己的私有 Unix socket 完成了空闲、忙碌和确认为 `notLoaded` 的三个场景。对于未加载的隔离会话，普通排队时保持 `notLoaded`。使用 `--resume` 后，同一测试线程执行排队标记。该结果对应自管 App Server 的后端。

对已加载的桌面 chat，TeamUp 后续可评估经所有者授权调用 CLI 排队。本次实测直接调用 harness 入口。TeamUp v1 的 @ 路径只发通知。 [Codex CLI 参考](https://learn.chatgpt.com/docs/cli/reference#codex-queue) · [Codex App Server](https://learn.chatgpt.com/docs/app-server) · [跨进程队列实现](https://github.com/openai/codex/pull/39034)

## 复验步骤

1. 使用拥有者授权的测试项目开两个桌面会话。记录桌面和 CLI 版本、授权的 daemon 或后端、目标会话 ID。给两个会话使用可区分的测试标记。
2. 先用 CLI 排队或受支持的本机 App Server 入口提交无副作用测试消息。空闲时记录实际 turn 启动和完成。忙碌时记录前一轮结束与新一轮开始的顺序，并确认另一会话无变化。
3. 在未加载情形下先证明会话状态，记录排队退出码、自动恢复警告和后续 turn 事件。在支持版本、后端和所有者授权齐备时尝试恢复。
4. 将具体版本和场景单独记为已验证或待验证。只有**原桌面会话实际自动启动 turn**才算支持唤醒。
