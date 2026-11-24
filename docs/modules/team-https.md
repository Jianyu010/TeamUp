# TeamUp 可选 HTTPS 直连

本页是 [可信团队试用](team-pilot.md) 的可选部署路径：服务器上由 Caddy 终止 TLS，成员在内网或 VPN 内通过稳定 DNS 直接访问 TeamUp，不再依赖 SSH 隧道。该路径不改变 [Client–Server v1](client-server-v1.md) 与 [账号登录 v1](account-login-v1.md) 的契约，TeamUp Server 仍只监听回环地址。当前线上试点继续使用 SSH 隧道；本页方案只由回环环境中的自动化验收（`tests/test_https_proxy.py`）验证，真实部署与第二台设备未验证。

## 前提与边界

- 仅供可信成员在内网或 VPN 内使用，绑定服务器的内网或 VPN 接口，不接入公网或生产网络。
- 需要内网或 VPN 内稳定的 DNS 名称解析到服务器的固定地址。临时主机名或轮换地址会让证书与客户端配置同时失效；更换 DNS 名称必须视为一次重新部署。
- 服务器上的 TeamUp Server 继续只监听 `127.0.0.1`，由同主机的反向代理转发。反向代理不承担业务授权，登录、成员 token 与停用语义仍由 Server 执行。

## 服务器配置

### DNS 与证书

证书与私钥由管理员显式提供，二者必须一一配对：

- 成员必须用同一个 DNS 名称访问，该名称要同时匹配站点与证书：既要与 `TEAMUP_HTTPS_HOST`（站点的 Host 匹配）一致，也要完整写入证书 SAN（例如 `teamup.corp.example`）。只用 IP 访问不会命中站点的 Host 匹配，本路径不支持，也不要求把 IP 写入 SAN。
- 更换 DNS 名称或证书时，证书与私钥必须成对替换并重启 Caddy；只换其一会导致启动失败或握手失败，不允许混用。
- 私钥权限 `0600`，仅运行 Caddy 的用户可读；私钥不离开服务器。

### Caddy

`deploy/Caddyfile` 通过环境变量装配，不写死任何地址：

| 变量 | 含义 |
| --- | --- |
| `TEAMUP_HTTPS_HOST` | 成员访问的 DNS 名称，用于匹配证书与 Host 校验 |
| `TEAMUP_HTTPS_PORT` | HTTPS 监听端口，正式部署通常为 443 |
| `TEAMUP_HTTPS_BIND` | 显式绑定的服务器地址（内网网卡或 VPN 接口 IP）。未设置时只绑定回环 `127.0.0.1`；显式设置为空串时 Caddy 直接拒绝加载配置；两种情况都不会监听全部接口，显式地址按原值绑定 |
| `TEAMUP_TLS_CERT`、`TEAMUP_TLS_KEY` | 证书与私钥文件的绝对路径 |
| `TEAMUP_UPSTREAM_PORT` | 回环上 TeamUp Server 的端口（默认部署为 8765） |

行为要点：

- `admin off` 关闭 Caddy 管理端点；`auto_https disable_redirects` 不额外监听 HTTP 跳转端口。
- `TEAMUP_HTTPS_BIND` fail closed：Caddyfile 的 `{$TEAMUP_HTTPS_BIND:127.0.0.1}` 默认值只在环境变量未设置时生效；显式空串会展开为空参数，站点内的 `vars teamup_https_bind` 守卫因此参数数量不足，`caddy adapt` 报错退出、配置整体被拒绝，不会退化为全网卡监听（实测 Caddy v2.11.9，见下方已验证）。
- 仅 `/v1/*` 反向代理到字面量回环上游 `127.0.0.1:$TEAMUP_UPSTREAM_PORT`，其余路径一律返回 404。
- `Authorization` 头原样透传，认证与授权完全由后端按契约执行。
- SSE（`/v1/events`）由 Caddy 默认即时下发，不配置缓冲或跳转；SSE 只作提示，丢帧后仍由持久收件箱恢复，重连语义不变。

运行（由 systemd 等进程管理器守护，环境变量经部署配置注入）：

```sh
caddy run --config /opt/teamup-pilot/Caddyfile --adapter caddyfile
```

## 源码分发

客户端源码必须与 SSH 路径同源。在仓库根对固定修订执行 `git archive`，记录 SHA256 摘要，经团队批准的独立私密渠道分发；本 HTTPS 站点只转发 `/v1`，不提供源码下载，不得作为分发通道。成员校验摘要一致后再解压安装，不接受来源不明的压缩包。

```sh
git archive --format=tar.gz --prefix=teamup-account-pilot/ -o teamup-client-src.tar.gz <pinned-revision>
sha256sum teamup-client-src.tar.gz
```

## 成员连接

启用本路径后，成员不再需要服务器 SSH 账号或隧道，TeamUp 账号登录即为唯一访问入口；服务器 SSH 账号仍属服务器管理员的信任边界。Client 配置与 [可信团队试用](team-pilot.md) 相比只有两点区别：

- `server_url` 写 `https://<DNS>:<port>`；
- 增加 `ca_file` 指向管理员分发的私有 CA 证书，Client 据此校验服务器证书。

随后的 `login`、`serve` 与本机 MCP 装载步骤不变。

## 已验证与未验证

自动化验收在回环环境用临时私有 CA 与 `localhost` 证书运行真实 Caddy（经 `TEAMUP_CADDY_BIN` 或 `PATH` 提供 caddy 后执行 `uv run --no-sync pytest tests/test_https_proxy.py`），覆盖：经代理的账号登录与 Agent 登记、运行中成员的实时 SSE 提示、离线成员重连补收、错误 CA 信任失败、错误口令失败，以及非 `/v1` 路径 404。同一套件还用真实 `caddy adapt` 验证 `TEAMUP_HTTPS_BIND` 的三种取值：未设置时仅回环监听、显式空串时配置被拒绝、显式地址原样保留（实测 Caddy v2.11.9）。

未验证：真实内网或 VPN 部署、跨网段链路、第二台设备与生产网络。在完成这些验证并取得 owner 同意前，线上试点保持 SSH 隧道，本页路径不作为默认接入方式。
