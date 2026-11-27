# Weekly Agent E2E（WE-xx）

本套件让真实的 LLM Agent 通过 TeamUp 协作，再从 TeamUp 状态判定结果。判定不用模型。契约见 [CI 套件共享契约](../../../docs/ci/contract.md)。

## 场景

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| WE-01 | 两个 Agent 私聊 | A 通过 DM 提问，B 在同一 DM 回复请求编号和自己知道的端口号，A 再用该端口号确认。三条消息按顺序保存在 DM 历史中 |
| WE-02 | 三个 Agent 的房间 | 三方都确认了当前公告版本，各自发过消息。Tester 分别 @ 另两方，被 @ 的两方都在 @ 之后回复 |
| WE-03 | 离线时被 @ | B 的 Client 停止。A @B 后再启动 B 的 Client。B 只收到一条补发通知，被唤醒一次，只回复一次 |
| WE-04 | 公告中途变更 | 保留历史编号。由 nightly 的 NS-15.04 验证 |
| WE-05 | 五个 Agent 站会 | Backend 发起，经 Frontend、Tester、Docs 到 Ops 逐级 @ 交接。五方都发过状态 |
| WE-06 | 三个 Agent 交接房间任务 | Release Lead 创建并指派任务。Tester 领取任务并记录测试夹具编号。Reviewer 从任务历史读取编号，领取任务，记录核验结果并完成任务 |

WE-06 模拟三人发布核验流程，共六轮。每周负载测试由 `python -m tests.scenarios.perf --suite weekly` 单独运行，使用脚本化参与者覆盖 10、30、50、100 人的任务组。

## Agent 如何接入

- 每个 Agent 是一个 opencode 会话，模型为 `csi-provider/DeepSeek-V4.1-Flash-1017`，可用 `TEAMUP_AGENT_MODEL` 覆盖。
- 每个 Agent 有自己的 opencode 配置目录和数据目录（`XDG_CONFIG_HOME`、`XDG_DATA_HOME`），工作目录为空目录，环境变量只传 `PATH`、`HOME`、`LANG`、`TZ`。
- `csi-provider` 指向本机中继 `http://127.0.0.1:18081/v1`（`TEAMUP_LLM_BASE_URL` 可改）。中继注入密钥。配置和代码都不含也不读取密钥。
- 配置里有一个 `local` MCP 服务器，运行 `python -m teamup_mcp.main`，环境为该 Agent 独享的 `TEAMUP_SOCKET` 和 `TEAMUP_BINDING_KEY`。
- 工具只开放 TeamUp MCP 工具（`tu_teamup_*`）。shell、读写文件、网络等一律 `deny`，运行不能触及本机。
- Agent 的系统提示由角色、私有事实和 [协作 skill](../../../plugin/skills/teamup-collaboration/SKILL.md) 正文组成。
- opencode 路径由 `TEAMUP_OPENCODE` 指定，缺省依次找 `~/opencode-review/opencode/.../opencode-linux-x64/bin/opencode`、`.bin/opencode`、`/snap/bin/opencode` 和 `PATH`。
- 每个场景自带一个回环 Server 和每个 Agent 一个 Client 守护进程，数据在 `/tmp/tuw-*`，结束即删除。

## 已知缺口：唤醒由编排器扮演

TeamUp Client 只有 Claude Code 和 Codex 两种唤醒 driver，没有 opencode driver。因此 **weekly 不覆盖真实的 Claude Code 和 Codex 唤醒**。

编排器代为唤醒：

1. 每个 Client 的 `notify_command` 指向记录器，每条桌面通知追加一行到 `desktop.jsonl`。这是公开的 Client 出口，且不会把通知标为已读。
2. 编排器轮询各 Agent 的记录文件。出现新通知且该 Agent 空闲时，用 `opencode run --session <id>` 在同一会话里启动它的下一轮，提示词说明“你被 @ 了”，并要求先查看通知和房间历史，已回复过则什么也不做。
3. 所有 Agent 都空闲且判定条件未满足时，等待数秒后判失败，不会反复催促。

DM 通知只保存在 Client 本地，不会自动启动 Agent。WE-01 在问句和回复分别写入 DM 后显式启动 B 和 A 的下一轮。WE-06 在任务创建和测试记录写入后显式启动 Tester 和 Reviewer。

## 判定与指标

判定通过 MCP 读取 TeamUp 状态。房间聊天使用房间历史，用 `board_ack_required` 判断成员是否确认了当前公告。WE-01 以双方身份读取同一个 DM 的历史。WE-06 读取任务记录和事件，核对参与者、事件顺序和完成状态。成员 ID 取自 Agent 自己的 `teamup_register` 结果。

每个场景记录以下指标，`labels` 为 `{"scenario": "WE-02", "agents": 3}`：

| 名称 | 单位 | 含义 |
| --- | --- | --- |
| `wall_time` | s | 场景总耗时 |
| `agent_turns` | count | `opencode run` 次数 |
| `model_calls` | count | 模型步数（opencode `step_finish` 事件数） |
| `teamup_tool_calls` | count | TeamUp 工具调用数，取自 opencode 事件流 |
| `wake_to_reply_latency` | s | 编排器启动唤醒轮到该轮 `send_message` 完成的时间 |
| `llm_tokens` | tokens | opencode 报告的 token 总数，`extra` 含输入和输出 |

每个 Agent 每一轮的提示词、工具调用输入输出和最终文字写入 `$TEAMUP_RESULTS_DIR/weekly/transcripts/<场景>-<Agent>.jsonl`。写入前把本次运行的所有密钥替换为 `<redacted>`。

## 预算

- 全套件目标不超过 150 个 Agent 轮次，实际计划约 30 轮。
- 每个场景有轮次和时长上限（见 `scenarios.py`），`--max-turns` 可再调低。超限判 `fail`。
- 全套件达到 80 分钟或 120 轮后，未开始的场景记为 `error`。契约上限是 90 分钟。
- 条件一旦满足立即结束，不再启动新的唤醒轮。已开始的轮次会跑完，保证记录完整。

## 2026-11-24 hp-laptop 实测

该次运行使用 WE-01 的旧版双成员房间流程。WE-01、02、03、05 全部通过，WE-04 跳过。共 25 轮、120 次模型步数，约 34 万 token，总耗时约 9.5 分钟。

| 场景 | 轮次 | 耗时 | 唤醒到回复 |
| --- | --- | --- | --- |
| WE-01 | 5 | 116 s | 约 17 s |
| WE-02 | 6 | 131 s | 约 19 s |
| WE-03 | 4 | 110 s | 12 s |
| WE-05 | 10 | 204 s | 约 17 s |

## 运行

```sh
# 不调用模型，验证接线（macOS 和 Linux 均可）
uv run --no-sync python -m tests.scenarios.weekly --dry-run --out /tmp/weekly.json
uv run --no-sync python -m tests.scenarios.weekly --dry-run --only WE-01,WE-06 --out /tmp/weekly-handoffs.json

# 真实 Agent（需要 opencode 和本机中继）
uv run --no-sync python -m tests.scenarios.weekly --out results/weekly/summary.json
uv run --no-sync python -m tests.scenarios.weekly --only WE-02
```

`--dry-run` 用脚本化 Agent 经真实 MCP 执行同样的步骤，零模型调用，不需要 opencode。

退出码：所有未跳过的场景都通过为 0，否则为 1。无论结果如何都会写出摘要。

环境变量：`TEAMUP_RESULTS_DIR`、`TEAMUP_SHA`、`TEAMUP_VERSION`、`TEAMUP_HOST`（见契约）、`TEAMUP_AGENT_MODEL`、`TEAMUP_OPENCODE`、`TEAMUP_LLM_BASE_URL`。

PR 门禁的 `pytest` 不会收集本目录：模块名都不以 `test_` 开头。
