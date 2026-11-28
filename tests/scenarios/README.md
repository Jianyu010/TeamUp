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
| NS-08 | 两人私聊 | 双方通过 MCP 打开同一私聊、互发消息、收到通知并按顺序读取历史。第三方无法读写，也不会收到私聊通知 |
| NS-09 | 收件箱事件、确认、游标与 SSE 提示 | `message` 与 `mention` 事件，确认幂等且仅限本人，`after` 与 `limit`，未确认公告的房间暂缓投递，SSE `inbox_changed` |
| NS-10 | 账号登录、账号下登记 Agent、停用账号 | `teamup-server accounts` 与 `teamup-client login`（用伪终端输入口令），人类 token 与成员 token 不互通，停用后新请求 401 且打开的 SSE 在保活间隔内结束 |
| NS-11 | TLS：自签证书加 Client `ca_file` | 未安装 `openssl` 时跳过 |
| NS-12 | Client 拒绝不受信任证书的 Server | 未配置 `ca_file` 时 Client 返回 `unavailable`，不降级 |
| NS-13 | 经 Client 与 MCP 的登记和角色查找 | MCP 工具与 HTTP 语义一致 |
| NS-14 | 经 Client 与 MCP 的公告错误码 | MCP 返回的错误码与 HTTP 一致 |
| NS-15.01–10 | Client Console | 分别验证登录、房间发现、历史访问、公告编辑冲突、对话中途变更、浏览器操作、子房间与邀请，以及领导者离开时的交接对话框 |
| NS-16.01–07 | Admin Console | 分别验证入口授权、对话记录权限、账号创建、重置、停用、浏览器操作和维护权限 |
| NS-17A–D | 房间归档 | 分别验证作者权限、停止投递、历史分页和精确的闲置截止时间 |
| NS-18 | 房间任务 | 分别验证指派可见性、MCP 任务历史与重启持久性，以及处理进度后失效的提醒 |
| NS-19A–D | 聚焦房间租约 | 在受控时钟上运行真实 Server 生命周期。分别验证提前 15 分钟的租约通知（经周期清扫到达并触发收件箱提示）、仅限接收者且可重复的确认、窗口内续租与续租后的新窗口；到期时刻的拒绝与归档、未完成任务在父房间的一次性接续及其对 restricted 内容的保护；领导权变更后通知转给新 leader；同一窗口内同一 leader 只收到一条通知（已知缺陷，`xfail(strict=True)`） |
| NS-20A–L | 已安装的 Server 和 Client 房间同步 | 验证快照与并发写入、没有 SSE 提示时的重启追赶、成员与加入版本隔离、空授权页前进、关闭房间后拒绝延迟页、移除与发送的两种顺序、同地址恢复时的代际隔离和收据重试。另验证登记响应丢失后找回同一身份，以及重连发现身份变化时暂停读取、发送、登记和退出 |

## 性能基准

| ID | 标题 | 指标 |
| --- | --- | --- |
| NP-01 | 两个 Agent 的 @ 通知延迟 | `dm_delivery_latency`（ms）。从 A 的 IPC 发送开始，到 B 的 Client 打印通知为止，包含 Client A、Server、SSE 和 Client B。始终用两人房间加 @，保留历史序列。实际私聊见 NP-14 |
| NP-02 | 五个 Agent 的房间聊天 | 低、中、高三档（2、10、40 条/秒）下的 `room_post_latency`、`room_fanout_latency`（ms），`room_throughput`（msg/s），`room_errors`、`room_undelivered`（count）。接收端用公开的 `/v1/events` 和 `/v1/inbox` |
| NP-03 | 历史增长 | 1k、10k、50k 条消息处的 `history_post_latency`、`history_latest_page_latency`、`history_full_scan`（ms）和 `history_db_size`（bytes，对数据库文件 `os.stat`） |
| NP-04 | 房间数量 | nightly 为 100、300 个房间，weekly 为 1,000、10,000 个房间。检查创建、目录分页和成员的房间列表 |
| NP-05–07、NP-13 | 并发发送、在线连接和 Presence | 单独改变发送并发、空闲 SSE 连接和心跳人数，再比较空闲连接与发送同时发生时的延迟 |
| NP-08 | 归档负载 | 分别验证归档与实时发送的并发行为、归档列表及历史读取 |
| NP-09 | 任务和停滞提醒 | nightly 任务组为 3、4、5 人，weekly 为 10、30、50、100 人。验证加入、进度重试、分页同步、完成竞争，以及启用和停用提醒时的对照负载 |
| NP-10 | 在线备份与恢复 | 仅 weekly。并发写入期间调用备份和校验流程，启动隔离 Server 并验证恢复数据 |
| NP-11–12 | Console 负载 | Client 刷新和历史分页，以及 Admin 轮询对聊天室延迟的影响 |
| NP-14–15 | 实际私聊延迟 | 测量持久通知可见时间，以及 Client 重连后离线消息的恢复时间 |
| NP-16 | 聚焦房间租约、续租与任务接续 | nightly 为 4 个父房间各 16 个子房间、3 轮续租，weekly 为 10 个父房间各 100 个子房间、4 轮续租，`--quick` 为 2×4、2 轮。子房间按角色分为到期、续租、手动关闭和待通知四类，并发创建。验证父房间归档与子房间创建的竞争、重复续租请求只续一次、启动清扫把未续租和多次续租的租约一并归档，以及父房间任务与未完成来源一一对应且重启后不重复。报告各类请求在并发下的延迟（ms）、重启到各到期房间可见的时间 `child_expiry_observed_lag` 和整轮清扫收敛时间 `startup_sweep_settle` |
| NP-17 | 房间同步离线恢复 | 仅 weekly。6 个 Client 离线期间通过旧接口发送 10,001 条消息，并归档另一个房间。验证旧游标失效后重建的视图与新 Client 一致，私有任务不可见，归档仍可按权限阅读。`--quick` 用 2 个 Client 和 120 条消息验证分页追赶。各 Client 依次登录同一账号，再并发读取房间。`room_sync_client_recovery`（ms）测量每个 Client 请求当前房间到历史返回的时间 |

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

浏览器场景使用 Chromium。可用 `uv pip install --python .venv/bin/python playwright==1.65.2` 和 `.venv/bin/python -m playwright install --only-shell chromium` 安装。流水线设置 `TEAMUP_REQUIRE_BROWSER=1`，缺少浏览器依赖时报告失败。

性能基准。`--quick` 采用小数据集来验证脚本；正式运行 NP-02 每档持续 30 秒，NP-03 播种到 50k 条：

```sh
uv run --no-sync python -m tests.scenarios.perf --quick --out results/nightly/perf.json
uv run --no-sync python -m tests.scenarios.perf --out results/nightly/perf.json
uv run --no-sync python -m tests.scenarios.perf --only NP-01,NP-03
uv run --no-sync python -m tests.scenarios.perf --suite weekly
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

`_focused_rooms.py` 为 NS-19 和 NP-16 提供受控时钟的进程内 Server（`FocusedServer`）。它沿用生产的生命周期，所以启动清扫和 15 秒周期的维护照常运行；`restart()` 在同一数据库上重新启动并立即触发启动清扫。回环 HTTP 客户端设置 `trust_env=False`，系统或环境里的代理不会截留本机流量。`FocusedApi.respond` 模拟响应中的 leader：按 epoch 确认领导权质询并确认所有通知，时钟前进前必须先调用它。

## 2026-11-28 聚焦房间实测

在 Linux x86_64 开发机上运行，浏览器场景因缺少 Playwright 跳过。

| 检查 | 结果 | 耗时 |
| --- | --- | --- |
| NS-19A | 通过，租约通知在周期清扫后到达并带 `inbox_changed` 提示 | 16 s，其中最多 15 s 是等待下一次周期清扫 |
| NS-19B、NS-19C | 通过 | 各约 1 s |
| NS-19D | 已知缺陷，`xfail(strict=True)` | 约 1 s |
| NP-16 `--quick`（2×4，2 轮） | 通过 | 1.1 s |
| NP-16 nightly（4×16，3 轮） | 通过 | 3.6 s |
| NP-16 weekly（10×100，4 轮） | 通过 | 41 s |

NP-16 的续租轮次和到期都是时钟跳转，受测的清扫是启动清扫，所以耗时只随请求数量和并发变化。weekly 一档有 1,000 个子房间，其中 500 个在同一次启动清扫里到期，从重启到全部可见约 0.4 s。
