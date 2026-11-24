# TeamUp 看板

## 仓库活动

根目录 README 直达订阅和开放 PR 的 GitHub 页面。数字以 GitHub 的实时数据为准。

| 指标 | 口径 | GitHub 数据 |
| --- | --- | --- |
| 订阅数 | Watch 仓库的账号数 | [Watchers](https://github.com/Jianyu010/TeamUp/watchers) |
| 开放 PR | 当前为 open 的 PR 数 | [开放 PR](https://github.com/Jianyu010/TeamUp/pulls?q=is%3Apr+is%3Aopen) |
| 合并 PR | 已合并的 PR，可在 GitHub 中按日期筛选 | [合并 PR](https://github.com/Jianyu010/TeamUp/pulls?q=is%3Apr+is%3Amerged) |

仓库活动只使用公开 GitHub 数据。内网运行状态使用 TeamUp 的授权查询接口，由团队在自己的环境查看。

通过 GitHub CLI 获取当前数值，供后续看板或 README 自动化使用。

```sh
gh api repos/Jianyu010/TeamUp --jq .subscribers_count
gh api -X GET search/issues -f 'q=repo:Jianyu010/TeamUp is:pr is:open' --jq .total_count
gh api -X GET search/issues -f 'q=repo:Jianyu010/TeamUp is:pr is:merged' --jq .total_count
```

## CI 看板

`dashboards/site/` 是静态页面，发布到 `ci-data` 分支根目录。页面用相对路径读取同分支的 `data/nightly.jsonl`、`data/weekly.jsonl`、`data/repo.jsonl`、`data/releases.jsonl` 和 `data/ci.jsonl`。文件缺失时显示空状态，不报错。

| 页面 | 内容 |
| --- | --- |
| `index.html` | 最近 nightly/weekly 状态、提交、最新发布与仓库摘要 |
| `repo.html` | Stars、Watchers、Forks、Issue/PR、发布下载与 14 天访问 |
| `performance.html` | nightly 指标、最近场景与通过率 |
| `agents.html` | weekly 指标、最近场景与场景状态历史 |
| `ci.html` | CI（PR 与 main）、AI Review 的每日通过率（含首次通过），nightly/weekly 通过率，合入吞吐、冻结状态与场景稳定性 |

仓库统计由 `scripts/ci/repo_stats.py` 生成，每个自然日一行。traffic 接口无权限时对应字段写 `null`，看板显示为空，不当作 0。

本地预览使用样例数据：

```sh
dashboards/site/preview.sh
```

脚本把 `dashboards/site/sample-data/*.jsonl` 复制到临时目录的 `data/`，再用 `python3 -m http.server` 提供服务。
