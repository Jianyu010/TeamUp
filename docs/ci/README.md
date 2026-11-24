# CI、自动合入与发布

TeamUp 的 main 分支只接受合入门禁的自动合入。仓库 owner（`Jianyu010`）可以手动合入，其他人不行。本页说明分层结构、运行位置、维护者操作和发布规则。套件之间的字段、路径和命令以 [共享契约](contract.md) 为准，issue 与 PR 的标签以 [标签规范](labeling.md) 为准。

## 分层

命名沿用 DigitalMate 的规则：job 显示为 `<层号> · <名称>`，runner 标签是名称的小写短横线形式，runner 注册名是标签加两位序号，如 `linux-ut-01`。

| 层 | Job | 何时运行 | 失败后果 |
| --- | --- | --- | --- |
| 1 · 路由 | 由门禁判定：改受保护路径、删除测试、新增 skip/xfail，或改动超过 300 个文件 | 每次判定 | 等待 owner 审查并加 `ready` |
| 2 · 静态检查 | `2 · Lint`（ruff）、`2 · Architecture`（依赖方向） | 每次推送，草稿也运行 | 门禁红，不合入 |
| 3 · 运行代码的检查 | `3 · Linux UT`（组件与 CI 单测）、`3 · Linux ST`（全仓 pytest，不含场景）、`3 · AI Review` | PR 标为 ready for review 之后；AI Review 在 CI 通过后由门禁派发 | 门禁红；审查有未解决发现时不合入 |
| 4 · 端到端 | `4 · Nightly`（免模型场景和性能）、`4 · Weekly`（真实 Agent） | main 上按计划运行 | main 冻结（nightly），或开 `weekly-failure` issue |
| 5 · 合入 | `5 · Merge Gate`（`merge-gate` 状态）、`5 · Merge` | 检查结束、标签变化或每 15 分钟的扫描 | PR 留在门前 |

草稿只运行第 2 层，不占 UT、ST 和审查的名额。转为 ready for review 时 `CI` 重新运行，第 3 层随之启动。

受保护路径和必需检查写在 [`.github/ci/policy.json`](../../.github/ci/policy.json)。该文件本身也受保护。

## 合入规则

- main 的分支保护要求状态 `merge-gate` 并且必须走 PR。管理员不受限，所以 owner 可以手动合入。
- `merge-gate` 只在以下条件全部满足时变绿：
  - 不是草稿；
  - 没有 `pr/hold`；
  - 必需检查都通过；
  - 当前 head 的 `3 · AI Review` 通过，或维护者加了 `ready`；
  - 第 1 层路由没有拦截。
- 合入者在 hp-laptop 的 `merge-01` 运行器上执行，令牌是仓库密钥 `TEAMUP_MERGE_TOKEN`。它每次合入一个 PR，用 squash 方式合入。
- `ready` 只能由 admin 或 maintain 角色添加。PR 在加标签后又推送新提交时，标签会被自动移除。
- `pr/hold` 让 PR 停在门前，直到移除该标签。

### 冻结

以下情况会开一个带 `ci/freeze` 标签的 issue：

- main 上的 `CI` 推送运行失败；
- nightly 失败。

冻结期间只合入带 `ready` 的 PR，通常是修复本身。main 恢复绿色、nightly 再次通过后，issue 自动关闭。weekly 失败不会冻结 main，只开 `weekly-failure` issue。

## 运行器

所有任务都在 owner 的 hp-laptop 上运行，不使用 GitHub 托管的运行器。每个实例在 bwrap 沙箱里运行，有独立的 home，由 `~/opencode-review/runner/runner-ctl` 监管。注册脚本是 [`scripts/ci/hp-laptop/register-runners.sh`](../../scripts/ci/hp-laptop/register-runners.sh)。

| Runner | 标签 | 运行的代码 | 承接的 job |
| --- | --- | --- | --- |
| `lint-01` | `lint` | PR 代码 | `2 · Lint` |
| `architecture-01` | `architecture` | PR 代码 | `2 · Architecture` |
| `linux-ut-01`–`03` | `linux-ut` | PR 代码 | `3 · Linux UT` |
| `linux-st-01`–`03` | `linux-st` | PR 代码 | `3 · Linux ST` |
| `ai-review-01`–`03` | `ai-review` | main 的审查工具 | `3 · AI Review`（并发还受共享审查名额限制） |
| `e2e-01` | `e2e` | main | `4 · Nightly`、`4 · Weekly`（同一时间只跑一个，性能数据不受干扰） |
| `merge-gate-01` | `merge-gate` | 只运行 main 的脚本 | `5 · Merge Gate`、撤销、main 健康检查、审查后的唤醒 |
| `merge-01` | `merge` | 只运行 main 的脚本 | `5 · Merge` |
| `gate-tick-01` | `gate-tick` | main | 每 15 分钟一次的调度节拍 |
| `utility-01` | `utility` | 只运行 main 的脚本 | 套件报告与发布、看板、issue 与 PR 标签 |

耗时的第 3 层各有三个并发名额。hp-laptop 本地的实例目录带 `teamup-` 前缀（如 `~/gh-runner/teamup-linux-ut-01`），与同机其他仓库的 runner 区分；在 GitHub 上注册的名字不带前缀。

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

PR 上的 `gate/*-failed`、从 issue 镜像的 `type/*` 和 `pr/no-issue` 由 `gate-queue.yml` 的 `pr-labels` 维护。这些标签只用于提示，门禁不读取它们。发布说明按 `type/*` 分组。

## 维护者操作

- **放行受保护的改动。** 审查 diff 后加 `ready`。
- **暂停某个 PR。** 加 `pr/hold`。
- **重新判定。** 在 Actions 手动运行 `Gate queue`，可以填写 PR 编号。
- **轮换合入令牌。** 换掉密钥 `TEAMUP_MERGE_TOKEN`。它是 fine-grained 令牌，只授权本仓库，权限为：
  - Contents、Pull requests、Issues、Workflows：读写；
  - Metadata：只读；
  - Administration：只读，用于读取访问统计，可选。
- **添加运行器。** 按注册脚本的注释，通过 stdin 传入注册令牌，不要写在命令行上。
