# CI、自动合入与发布

TeamUp 的 main 分支只接受合入门禁的自动合入。仓库 owner（`Jianyu010`）可以手动合入，其他人不行。本页说明分层结构、运行位置、维护者操作和发布规则。套件之间的字段、路径和命令以 [共享契约](contract.md) 为准，issue 与 PR 的标签以 [标签规范](labeling.md) 为准。

## 分层

命名沿用 DigitalMate 的规则：job 显示为 `<层号> · <名称>`，runner 标签是名称的小写短横线形式，runner 注册名是标签加两位序号，如 `linux-ut-01`。

| 层 | Job | 何时运行 | 失败后果 |
| --- | --- | --- | --- |
| 1 · 路由 | 由门禁判定：没有关联 issue；改受保护路径、删除测试、新增 skip/xfail，或改动超过 300 个文件 | 每次判定 | 关联 issue；`require_ready` 打开时，其余几项还要等待 owner 审查并加 `ready` |
| 2 · 静态检查 | `2 · Lint`（ruff）、`2 · Architecture`（依赖方向） | 每次推送，草稿也运行 | 门禁红，不合入 |
| 3 · 运行代码的检查 | `3 · Linux UT`（组件与 CI 单测）、`3 · Linux ST`（跨进程与 MCP 集成）、`3 · AI Review` | PR 标为 ready for review 之后；AI Review 在 CI 通过后由门禁派发 | 门禁红；P0/P1 发现未修复、P2 发现未回复时 `3 · Review Findings` 为红，不合入 |
| 4 · 端到端 | `4 · Nightly`（免模型场景和性能）、`4 · Weekly`（真实 Agent） | main 上按计划运行 | main 冻结（nightly），或开 `weekly-failure` issue |
| 5 · 合入 | `5 · Merge Gate`（`merge-gate` 状态）、`5 · Merge` | 检查结束、标签变化或每 15 分钟的扫描 | PR 留在门前 |

草稿只运行第 2 层，不占 UT、ST 和审查的名额。转为 ready for review 时 `CI` 重新运行，第 3 层随之启动。

### 第 1–3 层补充检查（契约）

下列检查按本节实现，均已启用；标"是"的检查列在 `.github/ci/policy.json` 的必需检查里。job 名、runner 标签、命令和失败标签以本表为准，实现方不得私自改名。

| Job | 层 | runner（标签 = 名称的短横线形式） | 命令或判定 | 必需 | 失败标签 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| `1 · PR Routing` | 1 | 门禁写入的提交状态（`merge-gate-01`） | 门禁的路由结论：PR 走自动路径还是等待 owner | 否，只报告 | – | 启用 |
| `1 · Protected Changes` | 1 | 同上 | 改了受保护路径、删除测试或新增 skip/xfail 时为红，owner 加 `ready` 后为绿；`require_ready` 关闭时只报告，保持绿色并注明 | 否，只报告 | – | 启用 |
| `1 · Automation Limits` | 1 | 同上 | PR 作者不受信、审查重跑次数用尽，或（`require_ready` 打开时）改动超过 300 个文件时为红 | 否，只报告 | – | 启用 |
| `1 · Linked Issue` | 1 | 同上 | PR 的标题或正文没有引用本仓库的 issue（`#N` 或 `Jianyu010/TeamUp#N`，引用的是 PR 不算）时为红；门禁据此拦截，owner 加 `ready` 可放行 | 否，门禁执行 | – | 启用 |
| `3 · Review Findings` | 3 | 同上 | 按下文"AI 审查发现"判定：有未修复的 P0/P1 或未回复的 P2 时为红；当前 head 还没有审查时为 pending | 否，门禁执行 | – | 启用 |
| `2 · Type Check` | 2 | `type-check-01` | `uv run --no-sync mypy`，配置在根 `pyproject.toml` 的 `[tool.mypy]`，覆盖 `server/src`、`client/shared/src`、`plugin/mcp/src` | 是 | `gate/type-check-failed` | 启用 |
| `2 · Secrets` | 2 | `secrets-01` | `gitleaks dir . --config .gitleaks.toml --redact --exit-code 1` | 是 | `gate/secrets-failed` | 启用 |
| `2 · Workflow Lint` | 2 | `workflow-lint-01` | `actionlint -config-file .github/actionlint.yaml`（列出自托管 runner 标签）与 `zizmor --offline .github/workflows`（配置 `.github/zizmor.yml`） | 是 | `gate/workflow-lint-failed` | 启用 |
| `2 · Dependencies` | 2 | `dependencies-01` | `uv lock --check`：`uv.lock` 与各 `pyproject.toml` 一致（先把锁文件副本改写到清华镜像）；`pip-audit` 漏洞报告写入 job 摘要，只报告不失败 | 是（仅锁文件检查） | `gate/dependencies-failed` | 启用 |
| `3 · Contract` | 3 | `contract-01` | `uv run --no-sync pytest -q tests/contract`：HTTP API、SSE 事件和 MCP 工具 schema 与 `tests/contract/golden/` 的快照一致 | 是 | `gate/contract-failed` | 启用 |
| `3 · Install` | 3 | `install-01`–`03` | `uv run --no-sync pytest -q tests/install`：构建三个包的 wheel，装进全新虚拟环境，命令行入口可用 | 是 | `gate/install-failed` | 启用 |
| `3 · Linux UT` 覆盖率下限 | 3 | `linux-ut-01`–`03` | `pytest --cov`，下限写在根 `pyproject.toml` 的 `[tool.coverage.report] fail_under`（现为 48%，实测约 50%） | 是（随 `3 · Linux UT`） | `gate/linux-ut-failed` | 启用 |
| `3 · Linux ST · 跨组件` | 3 | `linux-st-cross-01`–`03` | `uv run --no-sync pytest -q tests/system`：Server、Client 守护进程和 MCP 一起启动，走完登记、建房、公告、消息与提醒的链路 | 是 | `gate/linux-st-cross-failed` | 启用 |

第 2 层的四项在草稿上也运行；第 3 层只在 PR 标为 ready for review 之后运行。第 1 层的三个状态由门禁写入，不运行 PR 代码，也不在必需检查里，合入与否仍只看 `merge-gate`。

受保护路径和必需检查写在 [`.github/ci/policy.json`](../../.github/ci/policy.json)。该文件本身也受保护。

`3 · Linux UT` 在 `tests/server/`、`tests/client/shared/`、`tests/client/macos/` 与 `tests/scripts/ci/` 运行 Python 测试。`3 · Linux ST` 运行 `tests/integration/` 和 `tests/plugin/mcp/`。macOS 控制台和共用界面的 Node 测试通过 `npm --prefix client/macos/desktop test` 运行。

## 合入规则

- main 的分支保护要求状态 `merge-gate` 并且必须走 PR。管理员不受限，所以 owner 可以手动合入。
- `merge-gate` 只在以下条件全部满足时变绿：
  - 不是草稿；
  - 没有 `pr/hold`；
  - 必需检查都通过；
  - 当前 head 的 `3 · AI Review` 通过，审查发现按下文处理完，或维护者加了 `ready`；
  - PR 关联了至少一个 issue（改标题或正文后门禁重新判定，删掉引用会让门禁回到 pending）；
  - 第 1 层路由没有拦截。
- 合入者在 hp-laptop 的 `merge-01` 运行器上执行，令牌是仓库密钥 `TEAMUP_MERGE_TOKEN`。它每次合入一个 PR，用 squash 方式合入。
- `ready` 只能由 admin 或 maintain 角色添加。见下文的 `require_ready` 开关：关闭时，受保护改动、超大 PR 和冻结都不再需要 `ready`；不受信作者的 PR，以及讨论被标为已解决却没有修复的 P0/P1，仍然只能由维护者加 `ready` 放行。PR 在加标签后又推送新提交时，标签会被自动移除。
- `pr/hold` 让 PR 停在门前，直到移除该标签。

### owner 审查开关（`require_ready`）

`.github/ci/policy.json` 的 `require_ready` 决定哪些情况必须由 owner 加 `ready`。当前为 `false`（owner 的决定：`ready` 拖慢合入，暂时取消）。

| 情况 | `require_ready: true` | `require_ready: false`（当前） |
| --- | --- | --- |
| 改受保护路径、删除测试、新增 skip/xfail | 等 owner 加 `ready` | 自动路径照常判定；`1 · Protected Changes` 保持绿色并注明 |
| 改动超过 300 个文件 | 等 owner 加 `ready`，`1 · Automation Limits` 为红 | 不拦截，也不再报告；超过 3000 个文件（GitHub 列表上限，可能列不全）时不论开关都要 AI 审查 |
| main 或 nightly 冻结 | 只合入带 `ready` 或引用冻结 issue 的 PR | 不拦截合入；冻结 issue 照常打开和关闭，用于提醒 |

不受开关影响、仍然生效的规则：必需检查全绿、`3 · AI Review` 通过并按下文处理完发现、关联 issue、`pr/hold`、草稿不合入，以及不受信作者的 PR 等待维护者。`ready` 仍可用作放行（例如未修复的 P0/P1），只是不再是必需的。PR 作者一般就是 owner 的账号（agent 也用它），所以"只豁免 owner"等同于对所有 PR 关闭，开关因此按仓库设置。

### AI 审查发现

审查器给每条发现定严重度，对应关系是 `critical` = P0、`high` = P1、`medium` = P2。门禁读审查留下的发现台账（ledger），把结论写成提交状态 `3 · Review Findings`：

- **P0、P1 必须修复。** 只有审查器在后续轮次判定为已修复（`fixed`），或认可作者的解释（`answered`），才算处理完。作者回复或把讨论标为已解决都不够；推送修复后，审查器会在下一轮复查。
- **P2 必须回复。** PR 作者或 owner 在该发现的讨论里回复修复方式或不改的理由，或把讨论标为已解决，或在 PR 对话里以 `F<n>: ...` 开头回复。审查器判定为已修复的也算处理完。
- 不满足时 `3 · Review Findings` 为红，`merge-gate` 保持 pending 并写明还缺哪些发现。
- 被标为已解决、但没有修复的 P0/P1 不会再被审查器复查，只能由 owner 审过后加 `ready` 放行。

### 冻结

以下情况会开一个带 `ci/freeze` 标签的 issue：

- main 上的 `CI` 推送运行失败；
- nightly 失败。

`require_ready` 打开时，冻结期间只合入两种 PR：带 `ready` 的 PR；或在标题、正文里用 `Refs #N`（或 `Ref`、`Part of`）引用了某个打开的冻结 issue 的修复 PR，它仍要门禁全绿，但不必等 owner。用 `Closes`、`Fixes`、`Resolves` 引用冻结 issue 的 PR 不走这条路：那样修复一合入 issue 就被关闭，不会等到 main 真的恢复绿色。合入前合入者会重读 PR，引用被删掉或改成关闭关键字时不合入。main 恢复绿色、nightly 再次通过后，issue 自动关闭。weekly 失败不会冻结 main，只开 `weekly-failure` issue。

## 运行器

所有任务都在 owner 的 hp-laptop 上运行，不使用 GitHub 托管的运行器。每个实例在 bwrap 沙箱里运行，有独立的 home，由 `~/opencode-review/runner/runner-ctl` 监管。注册脚本是 [`scripts/ci/hp-laptop/register-runners.sh`](../../scripts/ci/hp-laptop/register-runners.sh)。

| Runner | 标签 | 运行的代码 | 承接的 job |
| --- | --- | --- | --- |
| `lint-01` | `lint` | PR 代码 | `2 · Lint` |
| `architecture-01` | `architecture` | PR 代码 | `2 · Architecture` |
| `type-check-01` | `type-check` | PR 代码 | `2 · Type Check` |
| `secrets-01` | `secrets` | PR 代码 | `2 · Secrets` |
| `workflow-lint-01` | `workflow-lint` | PR 代码 | `2 · Workflow Lint` |
| `dependencies-01` | `dependencies` | PR 代码 | `2 · Dependencies` |
| `linux-ut-01`–`03` | `linux-ut` | PR 代码 | `3 · Linux UT` |
| `linux-st-01`–`03` | `linux-st` | PR 代码 | `3 · Linux ST` |
| `linux-st-cross-01`–`03` | `linux-st-cross` | PR 代码 | `3 · Linux ST · 跨组件` |
| `contract-01` | `contract` | PR 代码 | `3 · Contract` |
| `install-01`–`03` | `install` | PR 代码 | `3 · Install` |
| `ai-review-01`–`03` | `ai-review` | main 的审查工具 | `3 · AI Review`（并发还受共享审查名额限制） |
| `e2e-01` | `e2e` | main | `4 · Nightly`、`4 · Weekly`（同一时间只跑一个，性能数据不受干扰） |
| `merge-gate-01` | `merge-gate` | 只运行 main 的脚本 | `5 · Merge Gate`、撤销、main 健康检查、审查后的唤醒 |
| `merge-01` | `merge` | 只运行 main 的脚本 | `5 · Merge` |
| `gate-tick-01` | `gate-tick` | main | 每 15 分钟一次的调度节拍 |
| `utility-01` | `utility` | 只运行 main 的脚本 | 套件报告与发布、看板、issue 与 PR 标签 |

耗时的第 3 层各有三个并发名额。第 2 层静态检查用到的 gitleaks、actionlint、shellcheck、zizmor 和 pip-audit 由 [`scripts/ci/hp-laptop/install-tools.sh`](../../scripts/ci/hp-laptop/install-tools.sh) 装进对应实例的 home（二进制校验 sha256）。hp-laptop 本地的实例目录带 `teamup-` 前缀（如 `~/gh-runner/teamup-linux-ut-01`），与同机其他仓库的 runner 区分；在 GitHub 上注册的名字不带前缀。

注意事项：

- 运行 PR 代码的实例拿不到任何密钥。
- `pull_request_target` 触发的任务通过 contents API 读取 main 的脚本，从不检出 PR 代码。
- hp-laptop 经常连不上 github.com 的 git 服务，所以代码树通过 API tarball 获取。
- PyPI 走清华镜像：先改写一份临时 `uv.lock` 的 URL，再执行 `uv sync --frozen`。
- 模型调用走本机中转 `127.0.0.1:18081`。网关密钥不进入任何运行器 home。

## 调度

GitHub 的 cron 常常推迟 3–5 小时，所以 `gate-tick.yml` 每 15 分钟运行一次，负责：

- 推进合入队列；
- 按 `policy.json` 的 `cadence` 判断 nightly、weekly 和看板是否到期，到期就派发。

| 任务 | 时间（Asia/Shanghai） |
| --- | --- |
| nightly | 每天 02:00 |
| weekly | 每周日 04:00 |
| 看板统计 | 每天 09:30 |

三者都可以在 Actions 页面手动派发。

## 版本与发布

标签格式为 `vX.Y.Z`。基线标签是 `v0.1.0`。

- **开发版本。** main 上的构建记为 `X.Y.(Z+1).devN`，N 是自最新标签以来 main 的第一父提交数。版本号由 `scripts/ci/version.py` 计算，写入结果文件的 `version` 字段。
- **补丁版本。** nightly 全绿，且自上一个标签以来有新提交（N>0）时，打标签 `X.Y.(Z+1)`。
- **次版本。** weekly 全绿，且自 `vX.Y.0` 以来有合入时，打标签 `X.(Y+1).0`。
- **发布说明。** 由 `scripts/ci/release.py` 按确定规则生成，不调用模型。内容包括：
  - 按类别分组的已合入 PR；
  - 关闭的 issue；
  - 贡献者；
  - 本次套件的证据表（场景数、通过数、运行链接）。
- 发布只会前进。目标提交不比最新标签新时，脚本拒绝发布。

## 看板

结果和统计写入 `ci-data` 分支，由 GitHub Pages 从该分支根目录发布。页面说明见 [dashboards/README.md](../../dashboards/README.md)。

| 页面 | 数据 |
| --- | --- |
| 概览 | 最新 nightly/weekly 状态、版本、仓库摘要 |
| CI | PR 与 main 上的 CI、AI Review 的每日通过率，nightly/weekly 通过率，合入吞吐，冻结状态，场景稳定性 |
| 仓库 | Stars、Forks、Issue/PR、下载量、距上次发布时间 |
| 性能 | nightly 性能：私聊延迟、5 Agent 房间三档负载、历史增长下的退化 |
| Agent | weekly 真实 Agent 场景与指标 |

## 标签

新 issue 由 `issues.yml` 依次处理：

1. 人提交的 issue 标为 `agent/human-only` 和 `status/review-needed`，并指派给 owner；
2. hp-laptop 上的模型按 [标签规范](labeling.md) 打上 `type`、`source`、`priority`、`area` 和 `size`；
3. 检查必需的轴，缺少时加上 `status/triage`。

PR 上的 `gate/*-failed`、从 issue 镜像的 `type/*` 和 `pr/no-issue` 由 `gate-queue.yml` 的 `pr-labels` 维护。这些标签只用于提示，门禁不读取它们；没有关联 issue 的拦截由门禁自己判定（`1 · Linked Issue`），与 `pr/no-issue` 用同一条规则。发布说明按 `type/*` 分组。

## 维护者操作

- **放行受保护的改动。** `require_ready` 打开时，审查 diff 后加 `ready`；关闭时不需要。
- **暂停某个 PR。** 加 `pr/hold`。
- **重新判定。** 在 Actions 手动运行 `Gate queue`，可以填写 PR 编号。
- **轮换合入令牌。** 换掉密钥 `TEAMUP_MERGE_TOKEN`。它是 fine-grained 令牌，只授权本仓库，权限为：
  - Contents、Pull requests、Issues、Workflows：读写；
  - Metadata：只读；
  - Administration：只读，用于读取访问统计，可选。
- **添加运行器。** 按注册脚本的注释，通过 stdin 传入注册令牌，不要写在命令行上。
