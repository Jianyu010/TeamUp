# CI 套件共享契约

本页是 nightly 场景、性能基准、weekly Agent E2E、看板和发布脚本之间的接口。任何一方修改字段、路径或命令，先改本页。

## 目录

```text
tests/scenarios/
  README.md               # 场景目录：ID、标题、覆盖的能力、所属套件
  conftest.py             # 仅当 TEAMUP_SCENARIOS 包含本套件时才收集；写 summary.json
  _stack.py               # 共享夹具：启动 Server、Client 守护进程、MCP stdio 会话
  nightly/test_*.py       # 免模型的功能场景（NS-xx）
  perf/                   # 免模型的性能基准（NP-xx），python -m tests.scenarios.perf
  weekly/                 # 真实 Agent 场景（WE-xx），python -m tests.scenarios.weekly
scripts/ci/               # 门禁、合入、发布、发布结果、仓库统计；仅标准库
dashboards/site/          # 静态看板页面，发布到 ci-data 分支根目录
```

PR 门禁运行的 `pytest`（根 `pyproject.toml` 的 testpaths）**不得**收集 `tests/scenarios/`。`tests/scenarios/conftest.py` 在环境变量 `TEAMUP_SCENARIOS` 未包含对应套件名时通过 `collect_ignore_glob` 忽略全部场景。

## 调用方式

所有命令在仓库根目录执行，Python 环境由 `uv sync --frozen --all-packages` 准备，`--no-sync` 运行。

| 套件 | 命令 | 时长上限 | 是否调用模型 |
| --- | --- | --- | --- |
| nightly 场景 | `TEAMUP_SCENARIOS=nightly uv run --no-sync pytest tests/scenarios/nightly -q` | 15 分钟 | 否 |
| nightly 性能 | `uv run --no-sync python -m tests.scenarios.perf --out results/nightly/perf.json` | 25 分钟 | 否 |
| weekly Agent E2E | `uv run --no-sync python -m tests.scenarios.weekly --out results/weekly/summary.json` | 90 分钟 | 是（opencode，csi 网关） |

环境变量 `TEAMUP_RESULTS_DIR`（默认 `results`）决定 summary 落点。`conftest.py` 写 `results/nightly/scenarios.json`。工作流随后用 `scripts/ci/results.py merge` 合成 `results/nightly/summary.json`。

## 结果文件（schema 1）

```json
{
  "schema": 1,
  "suite": "nightly",
  "sha": "<40 位提交>",
  "version": "0.1.1.dev3",
  "started_at": "2026-11-25T02:00:00Z",
  "finished_at": "2026-11-25T02:21:13Z",
  "host": "hp-laptop",
  "scenarios": [
    {"id": "NS-01", "title": "Workspace installs from the lockfile", "status": "pass", "duration_s": 41.2, "detail": ""}
  ],
  "metrics": [
    {
      "id": "NP-01",
      "name": "dm_delivery_latency",
      "unit": "ms",
      "labels": {"agents": 2},
      "stats": {"n": 200, "p50": 12.1, "p95": 20.4, "p99": 31.0, "mean": 13.0, "max": 44.2},
      "extra": {}
    }
  ]
}
```

- `status` 取 `pass`、`fail`、`error`、`skip`。`detail` 为失败原因摘要，不超过 2000 字符，不含密钥。
- `scenarios` 中任何 `fail` 或 `error` 都让该套件判红。性能基准只记录指标；基准自身无法完成时登记一条 `NP-xx` 场景为 `error`。
- `labels` 只用标量，看板用 `name` + `labels` 区分序列。数值统一保留 1 位小数。
- `sha`、`version`、`host` 由工作流经环境变量 `TEAMUP_SHA`、`TEAMUP_VERSION`、`TEAMUP_HOST` 传入，缺省时写 `null`。

## 数据分支 `ci-data`

`scripts/ci/results.py publish` 通过 GitHub contents API 写入，不使用 git push。

```text
data/nightly.jsonl     # 每次 nightly 一行（完整 summary）
data/weekly.jsonl      # 每次 weekly 一行
data/repo.jsonl        # 每天一行仓库统计（见下）
data/releases.jsonl    # 每次发布一行 {"tag","version","kind","sha","published_at","prs":[...]}
index.html  repo.html  performance.html  agents.html  assets/*   # 从 dashboards/site/ 同步
```

仓库统计行：

```json
{"date": "2026-11-25", "collected_at": "...", "stars": 3, "watchers": 2, "forks": 0,
 "open_issues": 4, "open_prs": 6, "issues_opened_1d": 1, "issues_closed_1d": 0,
 "prs_opened_1d": 2, "prs_merged_1d": 1, "release_downloads_total": 0,
 "clones_14d": null, "unique_cloners_14d": null, "views_14d": null, "unique_visitors_14d": null,
 "latest_release": "v0.1.1", "latest_release_at": "...", "days_since_release": 0.4}
```

不可读取的字段（例如 traffic 接口无权限）写 `null`，看板显示为空，不当作 0。

## 版本

`scripts/ci/version.py` 是唯一的版本计算入口：最新 `vX.Y.Z` 标签之后，`main` 上每个首父提交计为一次合入，构建版本为 `X.Y.(Z+1).devN`。nightly 绿且 N > 0 时发布 `vX.Y.(Z+1)`。weekly 绿且自上一个 `vX.Y.0` 以来有合入时发布 `vX.(Y+1).0`。
