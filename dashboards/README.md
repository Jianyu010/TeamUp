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
