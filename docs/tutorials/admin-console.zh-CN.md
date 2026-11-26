# 使用 TeamUp 管理控制台

[English](admin-console.md)

## 登录并查看服务器状态

1. 连接 TeamUp 服务器。试用环境通过 SSH 接入时，请保持 [SSH 隧道](../modules/team-pilot.zh-CN.md#开发者连接与安装)运行。使用 HTTPS 时，请访问已配置的服务器地址，并确认 macOS 或浏览器信任服务器证书。
2. 打开 Client Console，使用 TeamUp 账号登录。选择顶部 **Live** 左侧的 **Admin**。在 macOS 上，**Admin** 和 **View → Admin** 都会在 TeamUp.app 中打开独立窗口。在浏览器版客户端中，**Admin** 会打开新的浏览器标签页。
3. 使用 HTTPS 或 [SSH 私有 socket](../modules/team-pilot.zh-CN.md#开发者连接与安装) 时，TeamUp.app 的管理窗口会自动沿用客户端的登录。如果直接在浏览器中打开服务器控制台，请使用 TeamUp 用户名和密码登录。如果试用环境的隧道已经转发到本机的 18765 端口，也可以[直接打开服务器控制台](http://127.0.0.1:18765/console/)。使用其他服务器时，请访问该服务器的 `/console/`。
4. 在 **Rooms** 中查看房间成员和近期活动。**Timeline** 显示房间事件；拥有该房间的记录读取权限时，可以在 **Chats** 中查看消息。
5. 在 **Server Dashboard** 中查看系统状态、请求流量、延迟和房间负载。在 **Room load** 中选择房间即可查看该房间的指标。拥有日志权限时，选择最长一小时的时间范围，然后点击 **Load events**。
6. 在 **CI Dashboards** 中查看仓库活动和 CI 结果。操作完成后，选择 **Sign out**。

服务器在每次请求时检查权限。只有拥有控制台权限的账号才会在客户端看到 **Admin** 入口。**Server Dashboard** 和 **Accounts** 各自需要对应的授权。客户端登录过期后，请重启本地 Client 并重新登录。管理窗口也通过 SSH 私有 socket 访问服务器。如果使用浏览器版控制台，请保持回环 TCP 转发运行。

## 创建账号

1. 打开 **Accounts** 标签页，找到 **Create account** 表单。
2. 输入 3–64 个字符的用户名，以小写字母开头，其余字符可以是小写字母、数字、下划线或连字符。
3. 输入并确认 12–128 个字符的临时密码。点击 **Create account**，确认新账号出现在列表中且状态为 **Active**。
4. 通过团队使用的私密凭证传递渠道，将用户名和临时密码交给新用户。对方可以用这组凭证登录自己的 Client Console。

## 重置密码或停用账号

- 在对应账号行选择 **Reset password**。输入并确认新密码后保存。该账号的用户会话会结束，已登记的 Agent 可以继续工作。
- 在对应账号行选择 **Disable** 并确认用户名。停用为永久操作，会同时终止用户登录和 Agent 访问权限。已有房间记录仍会保留该账号的成员信息。

首次开通控制台和账号管理权限由服务器管理员在服务器上完成。之后可以在 Admin 控制台中完成上述操作。参见 [Admin Console 权限契约](../modules/admin-console.md#accounts)。
