# 标签规范

标签驱动 TeamUp 的自动化，人和 agent 都按本页打标签。本规范沿用 Linkie（PPMA 仓库）的标签体系，`area/*` 按 TeamUp 的组件划分。除本页列出的值外，不得新建标签。需要新的 `area/*` 或 `gate/*` 值时，在同一个 PR 里把它加进本页的清单。

## 约定

- 格式为 `轴/值`，小写，单词之间用连字符。
- 每个轴一种颜色，见下面的清单。
- 每个 issue 至少要有一个 `type/*`、一个 `source/*` 和一个 `priority/*`。缺少任何一项时，自动化加上 `status/triage`，并留一条说明缺什么的评论。带 `status/triage` 的 issue 不能开工。
- 各轴的数量由 `scripts/ci/labels.py lint` 检查：

| 轴 | 数量 |
| --- | --- |
| `type/*`、`source/*` | 一个或多个 |
| `priority/*` | 正好一个 |
| `size/*`、`agent/*` | 零个或一个 |
| `area/*`、`status/*`、`gate/*` | 任意个 |

## Issue 标签

### `type/*`：问题的性质（一个或多个）

需求混合时可以带多个，例如 `type/feature` 加 `type/docs`。

| 标签 | 含义 |
| --- | --- |
| `type/bug` | 行为与预期或文档不符 |
| `type/perf` | 行为不变，更快或更省（延迟、内存、吞吐、成本） |
| `type/feature` | 尚不存在的新能力 |
| `type/enhancement` | 改进已有能力，与性能无关 |
| `type/refactor` | 内部重构，行为不变 |
| `type/test` | 测试覆盖、不稳定测试、测试基础设施 |
| `type/ci` | 构建、CI、门禁、工具和开发环境 |
| `type/docs` | 只改文档 |
| `type/security` | 漏洞或加固 |
| `type/chore` | 依赖升级、日常整理 |

### `source/*`：在哪里发现（一个或多个）

同一个问题被多次独立发现时，带上每个来源。来源越多，越说明问题重要，应重新评估优先级。

| 标签 | 含义 |
| --- | --- |
| `source/incidental` | 做别的事情时顺带发现 |
| `source/planned` | 路线图或有计划的工作 |
| `source/review` | PR 审查中提出，但超出该 PR 的范围 |
| `source/ci` | main 分支上的 CI 失败 |
| `source/user` | 人报告的问题，包括维护者和团队成员 |
| `source/nightly` | nightly 发现 |
| `source/weekly` | weekly 发现 |
| `source/routine` | 其他定期检查发现（依赖、安全扫描） |

### `priority/*`：紧急程度（正好一个）

| 标签 | 含义 | 处理 |
| --- | --- | --- |
| `priority/critical` | main 损坏、数据丢失、安全漏洞、工作被阻塞 | 放下手头的事，通知人 |
| `priority/high` | 影响大 | 下一个处理 |
| `priority/medium` | 普通待办 | 拿不准时用这个 |
| `priority/low` | 有更好 | 没有更高优先级时才做 |

### `blocker`：发布门槛（可与任何优先级同时使用）

下一次发布或里程碑之前必须关闭。它与优先级相互独立：优先级回答"什么时候做"，`blocker` 回答"不做能不能发布"。

### 可选轴

| 标签 | 含义 |
| --- | --- |
| `area/<组件>` | 组件或子系统，取值见下文 |
| `size/s` | 大约一小时以内 |
| `size/m` | 大约半天 |
| `size/l` | 实施前需要设计或计划 |
| `agent/ok` | 维护者同意 agent 无人值守地处理 |
| `agent/human-only` | 需要人来决定，agent 不得实现 |

**分类不等于授权。** 自动打标签只会建议 `type`、`source`、`priority`、`area` 和 `size`。只有维护者能加或撤销 `agent/ok`。`agent/human-only` 是否决，优先于 `agent/ok`。

### `status/*`：issue 的生命周期

| 标签 | 由谁设置 | 含义 |
| --- | --- | --- |
| `status/triage` | 自动化 | 缺少必需的轴 |
| `status/claimed` | 开工的人或 agent | 防止两边同时做同一个 issue |
| `status/blocked` | 人或 agent | 在等另一个 issue 或 PR（在评论里给出链接） |
| `status/needs-info` | 人或 agent | 需要澄清才能继续 |
| `status/review-needed` | 自动化 | 人提交的 issue，等维护者看过；总是和 `agent/human-only` 一起设置 |

重复和不修复用 GitHub 的关闭原因表示，不用标签。

**人提交的 issue。** 作者不是机器人的新 issue 同时加上 `agent/human-only` 和 `status/review-needed`。如果还没有负责人，就指派给仓库 owner，并留一条提到 owner 的评论。重复运行结果相同。维护者主动移除的标签不会被加回去，除非再次手动运行。机器人提交的 issue（冻结、weekly 失败、依赖升级）不做这一步。已经带 `agent/ok` 的 issue 也不做。

## 自动打标签

新 issue 打开后，hp-laptop 上的模型从本页清单中选择 `type`、`source`、`priority`、`area` 和 `size`。`scripts/ci/autolabel.py` 负责校验：

- 只保留清单中这五个轴的标签；
- 不会设置 `agent/*`、`blocker` 或 `priority/critical`；
- 没有给出优先级时用 `priority/medium`，没有给出来源时用 `source/user`；
- issue 已经由人打过 `type`、`source`、`priority`、`agent` 或 `blocker` 时跳过。

issue 正文是不可信数据，只作为模型的输入，不会进入 shell。仓库变量 `AUTOLABEL_MODE` 可以取以下值：

- `apply`：默认，直接加标签；
- `suggest`：只评论建议；
- `off`：关闭。

`AUTOLABEL_MODEL` 可以覆盖默认模型。

## PR 标签

PR 的标签描述状态，`type/*` 从关联的 issue 继承。

| 标签 | 由谁设置 | 含义 |
| --- | --- | --- |
| `ready` | 仅维护者 | 维护者审过：放行第 1 层的拦截，放行 AI 审查，或在冻结期间放行合入。`require_ready` 关闭时（当前）只作可选放行。推送新提交后自动移除 |
| `pr/hold` | 仅人 | 门禁绿了也不合入 |
| `pr/no-issue` | 自动化 | 标题和正文都没有引用本仓库的 issue（`#N`、`Closes #N`、`Refs #N` 等，引用 PR 不算）。门禁同时拦截合入（`1 · Linked Issue`），owner 加 `ready` 可放行 |
| `gate/<检查>-failed` | 自动化 | 该检查失败；重新通过后移除 |
| `type/*`（镜像） | 自动化 | 从关联 issue 复制，用于发布说明分组 |

仓库级的运行状态标签：

| 标签 | 由谁设置 | 含义 |
| --- | --- | --- |
| `ci/freeze` | 自动化 | main 或 nightly 失败。`require_ready` 打开时只有带 `ready` 或引用该 issue 的 PR 能合入；关闭时（当前）只作提醒，不拦截合入 |
| `weekly-failure` | 自动化 | weekly 真实 Agent 端到端失败 |
| `opencode-review` | 维护者 | 请求对 PR 进行一次 OpenCode 审查 |

## Agent 规则

1. 提交 issue 时总要设置 `type/*`、`source/*` 和 `priority/*`。适用的 `type/*` 全部加上，不要为了只留一个而删掉。拿不准优先级时用 `priority/medium`，并在正文里说明。能判断时再加 `area/*` 和 `size/*`。
2. 做别的事情时发现的问题标为 `source/incidental`，另开 issue，不要塞进当前 PR。
   - 先搜索已有 issue，不要重复提交。
   - 问题已存在时，补上自己的 `source/*` 和缺少的 `type/*`，并评论说明在哪里遇到、为什么对当前工作重要。
   - 影响因此变大时，在评论里建议调整优先级。自己最多只能把优先级调到 `priority/high`。
3. 开工前重新读取 issue。已经指派给别人时不要开工，留言给对方。否则指派给你代表的开发者，加上 `status/claimed`，再读取一次，确认只有你一个负责人。放弃时移除这两项。
4. 不要凭自己的判断加或移除 `pr/hold`、`ready`、`agent/ok`、`agent/human-only` 和 `blocker`。只有拿得出具体证据时才提交 `priority/critical`，并通知人。
5. 不要手动设置 `pr/*` 和 `gate/*` 标签，它们由自动化管理。
6. 不要新建本规范以外的标签。

## 组件取值

`area/*` 按 [架构](../architecture/README.md) 的组件图和仓库结构划分。标为"敏感"的区域总是需要人审查，不交给 agent 无人值守处理。

| 标签 | 覆盖范围 | 敏感 |
| --- | --- | --- |
| `area/collaboration` | `server/src/teamup_server/collaboration.py`：成员、房间、私聊、公告、消息、收件箱、停用；业务 SQLite 的唯一 owner | 是 |
| `area/gateway` | `server/src/teamup_server/{gateway,main}.py`：HTTPS API、SSE、协议编解码、服务端配置 | |
| `area/presence` | Presence：心跳、健康查询、harness 观测 | |
| `area/accounts` | 登录、账号与凭证：`account_cli.py`、Client 的 `_login.py` | 是 |
| `area/client` | `client/`：会话协调器、守护进程、IPC、本机配置、与 Server 的连接 | |
| `area/harness` | Harness Driver 与唤醒：桌面提醒、Claude Code 与 Codex 唤醒 | |
| `area/mcp` | `plugin/mcp`：MCP 工具适配器（`teamup_*` 工具） | |
| `area/plugin` | `plugin/` 的打包与技能（`plugin/skills`） | |
| `area/contracts` | `docs/modules/`、`docs/protocols/`、`docs/architecture/`、`docs/specs/`：公开契约 | 是 |
| `area/ci` | `.github/`、`scripts/ci/`、`tests/scenarios/`、`dashboards/` | 是 |

`gate/*` 对应第 2–3 层的检查，每种失败一个标签，名称取自 job 名的短横线形式：

| 标签 | 失败的检查 |
| --- | --- |
| `gate/lint-failed` | `2 · Lint`（ruff） |
| `gate/architecture-failed` | `2 · Architecture`（依赖方向） |
| `gate/linux-ut-failed` | `3 · Linux UT` |
| `gate/linux-st-failed` | `3 · Linux ST` |
| `gate/ai-review-failed` | `3 · AI Review` 运行失败 |
| `gate/type-check-failed` | `2 · Type Check`（mypy） |
| `gate/secrets-failed` | `2 · Secrets`（gitleaks） |
| `gate/workflow-lint-failed` | `2 · Workflow Lint`（actionlint、zizmor） |
| `gate/dependencies-failed` | `2 · Dependencies`（锁文件一致性） |
| `gate/contract-failed` | `3 · Contract` |
| `gate/install-failed` | `3 · Install` |
| `gate/linux-st-cross-failed` | `3 · Linux ST · 跨组件` |

## 清单

标签名、颜色和描述以本表为准。运行 `python3 -m scripts.ci.labels sync` 创建或更新标签，不会删除表外的标签。

| 标签 | 颜色 | 描述 |
| --- | --- | --- |
| type/bug | 1D76DB | 行为与预期不符 |
| type/perf | 1D76DB | 行为不变，更快或更省 |
| type/feature | 1D76DB | 新能力 |
| type/enhancement | 1D76DB | 改进已有能力 |
| type/refactor | 1D76DB | 重构，行为不变 |
| type/test | 1D76DB | 测试与测试基础设施 |
| type/ci | 1D76DB | 构建、CI、门禁、工具 |
| type/docs | 1D76DB | 只改文档 |
| type/security | 1D76DB | 漏洞或加固 |
| type/chore | 1D76DB | 依赖与日常整理 |
| source/incidental | 5319E7 | 做别的事情时发现 |
| source/planned | 5319E7 | 路线图或计划内工作 |
| source/review | 5319E7 | PR 审查中提出，超出该 PR 范围 |
| source/ci | 5319E7 | main 上的 CI 失败 |
| source/user | 5319E7 | 人报告 |
| source/nightly | 5319E7 | nightly 发现 |
| source/weekly | 5319E7 | weekly 发现 |
| source/routine | 5319E7 | 其他定期检查发现 |
| priority/critical | B60205 | 放下手头的事，通知人 |
| priority/high | D93F0B | 下一个处理 |
| priority/medium | FBCA04 | 普通待办 |
| priority/low | 0E8A16 | 有更好 |
| blocker | 000000 | 下一次发布前必须关闭 |
| size/s | BFD4F2 | 约一小时以内 |
| size/m | BFD4F2 | 约半天 |
| size/l | BFD4F2 | 需要先设计 |
| agent/ok | 0E8A16 | agent 可以无人值守处理 |
| agent/human-only | D876E3 | 需要人来决定 |
| status/triage | EDEDED | 缺少必需的标签 |
| status/claimed | EDEDED | 已有人在做 |
| status/blocked | EDEDED | 在等另一个 issue 或 PR |
| status/needs-info | EDEDED | 需要澄清 |
| status/review-needed | EDEDED | 人提交的 issue，等维护者查看 |
| ready | 0E8A16 | 维护者放行：受保护改动、AI 审查或冻结期合入 |
| pr/hold | B60205 | 不要合入 |
| pr/no-issue | FBCA04 | 没有关联 issue |
| ci/freeze | D93F0B | main 或 nightly 失败（是否拦截合入见 require_ready） |
| weekly-failure | FBCA04 | weekly 真实 Agent 端到端失败 |
| opencode-review | 5319E7 | 请求 OpenCode 审查 |
| area/collaboration | C5DEF5 | 成员、房间、私聊、公告、消息、收件箱 |
| area/gateway | C5DEF5 | HTTPS API、SSE、协议 |
| area/presence | C5DEF5 | 心跳与健康 |
| area/accounts | C5DEF5 | 登录、账号与凭证 |
| area/client | C5DEF5 | Client 会话协调器与守护进程 |
| area/harness | C5DEF5 | Harness Driver、桌面提醒与唤醒 |
| area/mcp | C5DEF5 | MCP 工具适配器 |
| area/plugin | C5DEF5 | 插件打包与技能 |
| area/contracts | C5DEF5 | 公开契约、协议与架构文档 |
| area/ci | C5DEF5 | CI、场景套件与看板 |
| gate/lint-failed | E99695 | lint 检查失败 |
| gate/architecture-failed | E99695 | 依赖方向检查失败 |
| gate/linux-ut-failed | E99695 | Linux 单元测试失败 |
| gate/linux-st-failed | E99695 | Linux 系统测试失败 |
| gate/ai-review-failed | E99695 | AI 审查失败 |
| gate/type-check-failed | E99695 | 类型检查失败 |
| gate/secrets-failed | E99695 | 密钥扫描发现问题 |
| gate/workflow-lint-failed | E99695 | 工作流检查失败 |
| gate/dependencies-failed | E99695 | 锁文件与依赖声明不一致 |
| gate/contract-failed | E99695 | 契约测试失败 |
| gate/install-failed | E99695 | 安装冒烟测试失败 |
| gate/linux-st-cross-failed | E99695 | 跨组件系统测试失败 |
