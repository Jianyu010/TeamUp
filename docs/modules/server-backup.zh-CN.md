# TeamUp Server 备份与恢复

[English](server-backup.md)

Server 运维人员获取快照，用于灾难恢复。Collaboration 仍是唯一访问在线业务数据库的应用组件。运维人员以现有数据库所有者的操作系统身份，通过 SQLite 在线备份 API 以只读方式获取副本。Gateway、Client 和 MCP 继续使用现有契约。

## 获取快照

在数据库主机上以数据库所有者身份运行以下命令。使用仅自己可访问的目录暂存快照。生产环境在内存中暂存。目标文件的父目录必须已存在，归当前操作系统用户所有，并禁止组用户和其他用户访问。

```sh
install -d -m 700 "$HOME/teamup-backup-staging"
python3 scripts/ops/snapshot_sqlite.py \
  /path/to/live/teamup.db \
  "$HOME/teamup-backup-staging/teamup-20261126T081500Z.sqlite3"
```

每次运行都使用新的目标路径。快照包含已提交的写入，包括仍位于 WAL（预写日志）中的写入。在线数据库保持原有日志模式。命令生成一个权限为 `0600` 的独立 SQLite 文件，完整性和外键检查通过后才发布快照。如果目标文件或关联的 SQLite 辅助文件已经存在，命令报错并保留原文件。获取或验证失败时，命令以非零状态退出，目标路径上不会发布快照。重试时使用新的路径。

按生产数据管理快照。快照包含私聊和认证记录，只允许负责恢复的运维人员访问，并且只能通过已认证的 SSH 连接传输到备份主机。

## 验证恢复结果

1. 将快照复制到隔离恢复环境的私有暂存目录。核对记录的校验和，然后对恢复后的文件运行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`。预期分别返回 `ok` 和零条外键违规记录。
2. 创建一份可丢弃的工作副本，在隔离的运行配置下启动匹配版本的 TeamUp Server，只监听回环地址。使用获得授权的恢复身份，通过 TeamUp API 核对有代表性的房间和任务历史。
3. 记录恢复点和恢复耗时。保留原始备份，以便再次尝试。恢复后的服务重新向 Client 开放前，检查凭证、数据保留策略和待投递状态。

异地存储、每小时调度和告警的跟进记录见 [issue #88](https://github.com/Jianyu010/TeamUp/issues/88)。

## 定时异地备份

在独立的 Linux 主机上运行备份接收端，并使用持久存储。为接收端配置专用 SSH 密钥，限制它只能在源服务器上运行分派程序。分派程序只接受配置中精确匹配的源名称，以相应数据库的所有者身份运行采集命令，禁止使用 shell、转发和任意路径。每次采集都在仅该用户可访问的内存目录中生成独立快照。SSH 响应通过标准输出传回快照，通过标准错误输出一行 `TEAMUP_BACKUP_SHA256 <hex>`。若退出状态为非零，整份响应无效。

接收端先核对每个来源的快照摘要，再发布一组带时间戳的备份和相应回执。单次运行失败时，仍可使用先前的备份。通过定时器每小时从两个来源拉取备份。最近 7 天内的每组备份都保留，之后到第 30 天为止，每个 UTC 日只保留当天最新的一组。超过 2 小时无新备份时应能检测到。接收端的目录仅限授权人员访问，恢复命令在隔离的恢复环境中运行。接收端以 SQLite 文件保存备份。

### 安装备份源命令

1. 将 `snapshot_sqlite.py`、`capture_backup.py` 和 `dispatch_backup.py` 一起安装到 root 所有的目录，例如 `/usr/local/libexec/teamup-backup`。创建专用 SSH 账号，锁定密码，并为其设置一个空的、归 root 所有的主目录。只为这个账号安装备份接收端的公钥。
2. 将允许访问的备份源写入归 root 所有的 JSON 文件，例如 `/etc/teamup-backup/sources.json`。为每个源名称指定现有数据库所有者和配置中的数据库路径。

   ```json
   {
     "pilot": {"owner": "teamup-pilot", "database": "/var/lib/teamup-pilot/teamup.db"},
     "secondary": {"owner": "ubuntu", "database": "/home/ubuntu/.local/state/teamup/teamup.db"}
   }
   ```

3. 在 `authorized_keys` 中给接收端公钥加上 `restrict,command="/usr/bin/python3 /usr/local/libexec/teamup-backup/dispatch_backup.py /etc/teamup-backup/sources.json"` 前缀。为专用账号按备份源逐一设置精确匹配的 sudo 规则，安装前用 `visudo -cf` 检查该文件。此账号只需这些 sudo 规则。

   ```sudoers
   teamup-backup ALL=(teamup-pilot) NOPASSWD: NOSETENV: /usr/bin/python3 /usr/local/libexec/teamup-backup/capture_backup.py --source /var/lib/teamup-pilot/teamup.db
   teamup-backup ALL=(ubuntu) NOPASSWD: NOSETENV: /usr/bin/python3 /usr/local/libexec/teamup-backup/capture_backup.py --source /home/ubuntu/.local/state/teamup/teamup.db
   ```

### 安装备份接收端

将 `pull_backup.py`、`restore_backup.py` 和 `snapshot_sqlite.py` 安装到 `~/.local/lib/teamup-backup`，并将 `deploy/backup/` 下的单元文件安装到 `~/.config/systemd/user/`。在接收端的私有 SSH 配置中固定已核验的源主机密钥，使用专用身份，并设置 `IdentitiesOnly yes`、`RequestTTY no` 和 `LogLevel ERROR`。将配置和快照放在应用和 CI 的工作目录以外。条件允许时，为接收端使用专用账号。如果接收端与构建工具共用主机或操作系统账号，确认构建进程在独立的文件系统命名空间内运行，无法访问接收端实际的主目录或生产备份。以接收端账号运行且未受沙箱限制的进程可以读取这些文件。

创建权限为 `0700` 的备份目录和权限为 `0600` 的 `~/.config/teamup-backup/receiver.env`。用户级服务会读取下面四项配置。

```ini
TEAMUP_BACKUP_ROOT=/home/receiver/teamup-backups
TEAMUP_BACKUP_SSH_CONFIG=/home/receiver/.config/teamup-backup/ssh_config
TEAMUP_BACKUP_HOST=teamup-backup-source
TEAMUP_BACKUP_SOURCES="--source pilot --source secondary"
```

由管理员为接收端账号启用 linger，使定时器在开机时启动，并在该账号退出登录后继续运行。

```sh
sudo loginctl enable-linger receiver
```

然后以接收端账号运行以下命令。第一条命令必须输出 `yes`。

```sh
loginctl show-user "$USER" --property=Linger --value
systemctl --user daemon-reload
systemctl --user start teamup-backup.service
systemctl --user start teamup-backup-check.service teamup-backup-verify.service
systemctl --user enable --now teamup-backup.timer teamup-backup-check.timer teamup-backup-verify.timer
systemctl --user list-timers 'teamup-backup*'
```

每 15 分钟检查一次最新备份的清单、生成时间和快照大小。每日校验会重新计算最新一组备份的哈希值。查看接收端的 systemd 日志，排查传输失败、保留策略执行出错、备份过久未更新和校验和不匹配。

源端为每个数据库的在线复制和 SQLite 验证预留 120 秒。需要延长时间时，在 `sources.json` 中将该来源的 `timeout_seconds` 设为所需的整数，并将对应的 `--timeout-seconds N` 加入该来源的精确 sudo 规则。接收端为每个来源设定 180 秒的 SSH 时限。在 `receiver.env` 中设置 `TEAMUP_BACKUP_SSH_TIMEOUT_SECONDS` 时，要为采集和传输都留足时间。调整拉取服务的 `TimeoutStartSec`，使其能够涵盖所有来源以及本地发布所需的时间。独立的快照命令和恢复命令都接受 `--timeout-seconds`，用于设置 SQLite 复制和验证的时限。

### 从已保存的备份恢复

根据选定备份中的 `manifest.json` 确定要恢复的源文件和校验和。将文件恢复到新的私有目录，再按上文所述在隔离环境中验证应用。

```sh
python3 "$HOME/.local/lib/teamup-backup/restore_backup.py" \
  --snapshot /path/to/generation/pilot.sqlite3 \
  --sha256 "$SNAPSHOT_SHA256" \
  --destination /path/to/private-restore/teamup.db
```

备份和恢复命令在 TeamUp Server 和 Client 之外运行，独立于应用版本部署。
