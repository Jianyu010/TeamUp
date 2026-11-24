# CI、自动合入与发布

TeamUp 的 main 分支只接受合入门禁的自动合入。仓库 owner（`Jianyu010`）可以手动合入，其他人不行。本页说明分层结构、运行位置、维护者操作和发布规则。套件之间的字段、路径和命令以 [共享契约](contract.md) 为准。

## 分层

| 层 | 内容 | 在哪里 | 失败后果 |
| --- | --- | --- | --- |
| 1 · 路由 | 改受保护路径、删除测试、新增 skip/xfail，或改动超过 300 个文件 | `scripts/ci/gate.py` 的 `routing_problems` | 等待 owner 审查并加 `ready` |
| 2 · 快速检查 | `2 · lint`（ruff）、`2 · architecture`（依赖方向）、`2 · unit` | `pr.yml` | 门禁红，不合入 |
| 3 · 系统测试与 AI 审查 | `3 · system`（全仓 pytest，不含场景）；OpenCode 审查 | `pr.yml`、`opencode-review.yml` | 门禁红；审查有未解决发现时不合入 |
| 4 · 端到端 | nightly（免模型场景和性能）、weekly（真实 Agent） | `nightly.yml`、`weekly.yml` | main 冻结（nightly），或开 `weekly-failure` issue |
| 5 · 合入 | `merge-gate` 状态，可信合入者按队列合入 | `gate.yml`、`gate-queue.yml`、`scripts/ci/merge.py` | PR 留在门前 |

受保护路径和必需检查写在 [`.github/ci/policy.json`](../../.github/ci/policy.json)。该文件本身也受保护。

## 合入规则

- main 的分支保护要求状态 `merge-gate` 并且必须走 PR。管理员不受限，所以 owner 可以手动合入。
- `merge-gate` 只在以下条件全部满足时变绿：
  - 不是草稿；
  - 没有 `pr/hold`；
  - 必需检查都通过；
  - 当前 head 的 OpenCode 审查通过，或维护者加了 `ready`；
  - 第 1 层路由没有拦截。
- 合入者在 hp-laptop 的 `teamup-merge` 运行器上执行，令牌是仓库密钥 `TEAMUP_MERGE_TOKEN`。它每次合入一个 PR，用 squash 方式合入。
- `ready` 只能由 admin 或 maintain 角色添加。PR 在加标签后又推送新提交时，标签会被自动移除。
- `pr/hold` 让 PR 停在门前，直到移除该标签。

### 冻结

以下情况会开一个带 `ci/freeze` 标签的 issue：

- main 上的 `PR checks` 推送运行失败；
- nightly 失败。

冻结期间只合入带 `ready` 的 PR，通常是修复本身。main 恢复绿色、nightly 再次通过后，issue 自动关闭。weekly 失败不会冻结 main，只开 `weekly-failure` issue。

## 运行器

所有任务都在 owner 的 hp-laptop 上运行，不使用 GitHub 托管的运行器。每个实例在 bwrap 沙箱里运行，有独立的 home，由 `~/opencode-review/runner/runner-ctl` 监管。注册脚本是 [`scripts/ci/hp-laptop/register-runners.sh`](../../scripts/ci/hp-laptop/register-runners.sh)。

| 实例 | 标签 | 运行的代码 | 用途 |
| --- | --- | --- | --- |
| `teamup-gate-1`、`-2` | `teamup-gate` | PR 代码 | 第 2–3 层检查 |
| `teamup-merge-1` | `teamup-merge` | 只运行 main 的脚本 | 门禁、合入队列、报告、发布 |
| `teamup-review-1` | `opencode-review` | main 的审查工具 | OpenCode 审查 |
| `teamup-tick` | `teamup-tick` | main | 每 15 分钟一次的调度节拍 |
| `teamup-suite-1` | `teamup-suite` | main | nightly 与 weekly 套件 |

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
| CI | PR checks、main 和 AI 审查的每日通过率，nightly/weekly 通过率，合入吞吐，冻结状态，场景稳定性 |
| 仓库 | Stars、Forks、Issue/PR、下载量、距上次发布时间 |
| 性能 | nightly 性能：私聊延迟、5 Agent 房间三档负载、历史增长下的退化 |
| Agent | weekly 真实 Agent 场景与指标 |

## 维护者操作

- **放行受保护的改动。** 审查 diff 后加 `ready`。
- **暂停某个 PR。** 加 `pr/hold`。
- **重新判定。** 在 Actions 手动运行 `Gate queue`，可以填写 PR 编号。
- **轮换合入令牌。** 换掉密钥 `TEAMUP_MERGE_TOKEN`。它是 fine-grained 令牌，只授权本仓库，权限为：
  - Contents、Pull requests、Issues、Workflows：读写；
  - Metadata：只读；
  - Administration：只读，用于读取访问统计，可选。
- **添加运行器。** 按注册脚本的注释，通过 stdin 传入注册令牌，不要写在命令行上。
