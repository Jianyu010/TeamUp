# TeamUp 可信团队试用

[English](team-pilot.md)

试用机通过 SSH 隧道提供 TeamUp。每位开发者用自己的 TeamUp 账号登录。试用机的 SSH 账号属于信任边界，共用账号下的服务器管理员能访问 TeamUp 的运行数据。

不需要 SSH 隧道时可参考可选的 [HTTPS 直连](team-https.zh-CN.md)。当前线上试点仍使用 SSH 隧道，HTTPS 直连尚未在真实部署中验证。

## 管理员开户

已获授权的管理员可以在客户端选择 **Admin**，在 [Accounts](../tutorials/admin-console.zh-CN.md#创建账号) 中开户。

在服务器上执行下面的命令，为每个人指定一个 TeamUp 用户名。创建与重置口令时从终端输入临时口令，并通过团队约定的私密渠道交给本人。

```sh
sudo -u teamup-pilot /opt/teamup-pilot/bin/teamup-server accounts --db /var/lib/teamup-pilot/teamup.db create alice
sudo -u teamup-pilot /opt/teamup-pilot/bin/teamup-server accounts --db /var/lib/teamup-pilot/teamup.db list
```

同一入口支持 `reset-password alice` 和 `disable alice`。停用会使登录和该账号的 Agent 凭证一起失效。

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

按 [Client 配置示例](quickstart.zh-CN.md#启动-client) 把生成的条目加入权限为 `0600` 的私有 JSON 的 `installs`。将 `server_url` 设为 `http://127.0.0.1:18765`，省略 `enrollment_key`。打开 TeamUp.app，在控制台输入 TeamUp 用户名和口令。使用浏览器时，先运行 `uv run --no-sync teamup-client serve --config /private/path/client.json --console`，再打开输出的回环地址登录。

macOS 控制台的 **Remember me on this Mac** 需要验证 HTTPS 服务端或使用私有 Unix socket 转发。保留原来的 `server_url`。使用 SSH 隧道时，在仅当前用户可进入的目录内增加一个本地 socket 转发，例如在 macOS 上执行下列命令。仍需 TCP 转发的工具可在同一 SSH 命令中保留原来的 `-L 127.0.0.1:18765:127.0.0.1:8765` 选项。

```sh
ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes \
  -L "$HOME/.config/teamup/server.sock:127.0.0.1:8765" ubuntu@SSH_HOST
```

在私有 Client JSON 中增加 `"server_socket": "/absolute/path/to/server.sock"`，重启 Client 后登录。socket 和它的直接父目录必须归当前用户所有，且不能向其他用户开放。

无控制台的终端或后台部署可以继续使用以下命令。

```sh
uv run --no-sync teamup-client login --config /private/path/client.json --username alice
uv run --no-sync teamup-client serve --config /private/path/client.json
```

按 [本机 MCP 装载步骤](quickstart.zh-CN.md#装载本机-mcp) 给 Claude Code 安装配置一次 stdio MCP。各会话单独登记和批准，恢复同一会话会沿用原成员。人类登录的 token 到期后，需要登记新会话时再登录一次。
