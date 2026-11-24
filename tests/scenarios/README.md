# 场景与性能基准

本目录放 PR 门禁之外的跨进程套件。目录、命令和结果格式以 [CI 套件共享契约](../../docs/ci/contract.md) 为准。所有场景不调用模型，只经公开入口访问各组件：Server 的 HTTP 与 SSE、`teamup-server` 和 `teamup-client` 命令行、Client 的 IPC socket、`teamup-mcp` 的 stdio 工具。测试不导入内部模块，也不打开 Server 数据库。

## nightly 场景

| ID | 标题 | 守护的能力 |
| --- | --- | --- |
| NS-01 | 每个包装入空 venv，控制台脚本可运行 | 三个包能从本地路径独立安装，入口命令存在并响应，运行时用的是 venv 里的包而非 checkout |
| NS-02 | 已安装的 Server 与两个 Client 启动、接线并报告健康 | quickstart 的目录与权限（0700/0600），socket 出现，`teamup-client sessions` 报告绑定已批准，Client 经 IPC 登记成功 |
| NS-03 | 存活检查：未认证被拒、认证后可访问、重启后身份保持 | 统一的 401 错误形状，`/v1/members/me`，SSE 就绪帧，同一数据库重启后成员、历史和幂等回执不变 |
| NS-04 | HTTP 登记幂等与按角色查找 | 重复绑定复用身份，冲突与非法输入的错误码，角色子串查找大小写不敏感 |
| NS-05 | 建房、加入与非成员拒绝 | 非成员读写返回 `not_found`，重复加入无副作用，非法 @ 目标返回 `invalid_mention` |
| NS-06 | 房间公告读取与确认门槛 | `board_read_required`、`board_ack_required`、版本冲突，确认前历史和发送被阻止，创建者同样受限 |
| NS-07 | 房间消息、@ 实时通知、离线补收与幂等重发 | 在线 Client 收到通知与桌面提醒，离线后补收且只一次，相同 `client_message_id` 返回原回执，改动内容冲突 |
| NS-08 | 两人私下对话 | DM 未合入时以两人房间运行并在 `detail` 注明。检测到 DM 工具时跳过并提示补写。第三方不可读写 |
| NS-09 | 收件箱事件、确认、游标与 SSE 提示 | `message` 与 `mention` 事件，确认幂等且仅限本人，`after` 与 `limit`，未确认公告的房间暂缓投递，SSE `inbox_changed` |
| NS-10 | 账号登录、账号下登记 Agent、停用账号 | `teamup-server accounts` 与 `teamup-client login`（用伪终端输入口令），人类 token 与成员 token 不互通，停用后新请求 401 且打开的 SSE 在保活间隔内结束 |
| NS-11 | TLS：自签证书加 Client `ca_file` | 未安装 `openssl` 时跳过 |
| NS-12 | Client 拒绝不受信任证书的 Server | 未配置 `ca_file` 时 Client 返回 `unavailable`，不降级 |
| NS-13 | 经 Client 与 MCP 的登记和角色查找 | MCP 工具与 HTTP 语义一致 |
| NS-14 | 经 Client 与 MCP 的公告错误码 | MCP 返回的错误码与 HTTP 一致 |

## 性能基准

| ID | 标题 | 指标 |
| --- | --- | --- |
| NP-01 | 两个 Agent 的 @ 通知延迟 | `dm_delivery_latency`（ms）。从 A 的 IPC 发送开始，到 B 的 Client 打印通知为止，包含 Client A、Server、SSE 和 Client B。DM 合入前用两人房间加 @ |
| NP-02 | 五个 Agent 的房间聊天 | 低、中、高三档（2、10、40 条/秒）下的 `room_post_latency`、`room_fanout_latency`（ms），`room_throughput`（msg/s），`room_errors`、`room_undelivered`（count）。接收端用公开的 `/v1/events` 和 `/v1/inbox` |
| NP-03 | 历史增长 | 1k、10k、50k 条消息处的 `history_post_latency`、`history_latest_page_latency`、`history_full_scan`（ms）和 `history_db_size`（bytes，对数据库文件 `os.stat`） |

每条指标的 `labels` 区分序列，例如 `{"agents": 5, "load": "low"}`、`{"history": 10000}`。`extra` 记录 CPU 数、Python 版本和平台。NP-02、NP-03 里单值指标的 `stats` 五项相同。

## 本地运行

先准备环境：

```sh
uv sync --locked --all-packages --python 3.12
```

nightly 场景。未设置 `TEAMUP_SCENARIOS` 时，普通 `pytest` 不会收集本目录：

```sh
TEAMUP_SCENARIOS=nightly uv run --no-sync pytest tests/scenarios/nightly -q
```

NS-01 会用 `uv` 建新的 venv 并从本地路径安装，沿用环境里的 `UV_DEFAULT_INDEX` 或 `UV_INDEX_URL`，并用运行 pytest 的解释器（`--python sys.executable`）。无法访问 PyPI 的机器要先设置镜像。

性能基准。`--quick` 约 15 秒，用来验证脚本；正式运行 NP-02 每档持续 30 秒，NP-03 播种到 50k 条：

```sh
uv run --no-sync python -m tests.scenarios.perf --quick --out results/nightly/perf.json
uv run --no-sync python -m tests.scenarios.perf --out results/nightly/perf.json
uv run --no-sync python -m tests.scenarios.perf --only NP-01,NP-03
```

## 结果文件

两处都按契约写 schema 1。

| 来源 | 文件 | 内容 |
| --- | --- | --- |
| `conftest.py` 的会话钩子 | `$TEAMUP_RESULTS_DIR/nightly/scenarios.json` | `scenarios` 有每个 `@pytest.mark.scenario("NS-xx", "标题")` 的状态、耗时和失败摘要，`metrics` 为空 |
| `python -m tests.scenarios.perf` | `--out` 指定，默认 `$TEAMUP_RESULTS_DIR/nightly/perf.json` | `metrics` 有各基准的统计，`scenarios` 为每个 `NP-xx` 的 `pass` 或 `error` |

`TEAMUP_RESULTS_DIR` 缺省为 `results`。`TEAMUP_SHA`、`TEAMUP_VERSION`、`TEAMUP_HOST` 缺省写 `null`。预期失败的已知缺陷用 `xfail(strict=True)` 标记，结果里记为 `skip` 并在 `detail` 写明原因。xfail 意外通过会记为 `fail`。

## 共享夹具

`_stack.py` 提供临时工作区（位于 `/tmp`，避免 socket 路径过长）、Server 与 Client 守护进程启动、HTTP 客户端、MCP stdio 会话和通知记录器。失败或中断时进程会被清理，失败时把各进程日志输出到 stderr。`_results.py` 负责结果文件和统计。
