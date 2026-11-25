# TeamUp 可信团队试用

试用机通过 SSH 隧道提供 TeamUp。每位开发者用自己的 TeamUp 账号登录。试用机的 SSH 账号属于信任边界，共用账号下的服务器管理员能访问 TeamUp 的运行数据。

不需要 SSH 隧道时可参考可选的 [HTTPS 直连](team-https.md)。当前线上试点仍使用 SSH 隧道，HTTPS 直连尚未在真实部署中验证。

## 管理员开户

在服务器上执行下面的命令，为每个人指定一个 TeamUp 用户名。创建与重置口令时从终端输入临时口令，并通过团队约定的私密渠道交给本人。

```sh
sudo -u teamup-pilot /opt/teamup-pilot/bin/teamup-server accounts --db /var/lib/teamup-pilot/teamup.db create alice
sudo -u teamup-pilot /opt/teamup-pilot/bin/teamup-server accounts --db /var/lib/teamup-pilot/teamup.db list
```

同一入口支持 `reset-password alice` 和 `disable alice`。停用会使登录和该账号的 Agent 凭证一起失效。

维护人员通过同一个服务器本机入口领取权限。在服务器上以 `teamup-pilot` 服务账号运行 `teamup-server accounts --db /var/lib/teamup-pilot/teamup.db grant-console USERNAME` 和 `grant-maintenance USERNAME`。需要查阅脱敏事件时再运行 `grant-logs USERNAME`。隧道建立后，使用 TeamUp 账号登录 `http://127.0.0.1:18765/dashboard/`。TeamUp.app 的 **View → Server Dashboard** 使用同一入口。权限和查询语义见 [维护控制台](maintenance-console.md)。

## 开发者连接与安装

在 Client 所在的 macOS 或 Linux 终端运行 SSH 隧道，并保留该终端。Windows 试跑时，在运行 Client 的 WSL2 发行版里启动隧道。

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18765:127.0.0.1:8765 ubuntu@SSH_HOST
```

另开一个本机终端，取回配套源码并安装，然后为本机 Claude Code 安装生成一份接入配置。

```sh
scp ubuntu@SSH_HOST:/opt/teamup-pilot/teamup-client-src.tar.gz .
tar -xzf teamup-client-src.tar.gz
cd teamup-account-pilot
uv sync --locked --all-packages --python 3.12
uv run --no-sync teamup-client init-install claude-code-local
```

按 [Client 配置示例](quickstart.md#启动-client) 把生成的条目加入权限为 `0600` 的私有 JSON 的 `installs`。将 `server_url` 设为 `http://127.0.0.1:18765`，省略 `enrollment_key`。在此配置里登录一次，然后启动服务。

```sh
uv run --no-sync teamup-client login --config /private/path/client.json --username alice
uv run --no-sync teamup-client serve --config /private/path/client.json
```

按 [本机 MCP 装载步骤](quickstart.md#装载本机-mcp) 给 Claude Code 安装配置一次 stdio MCP。各会话单独登记和批准，恢复同一会话会沿用原成员。人类登录的 token 到期后，需要登记新会话时再登录一次。
