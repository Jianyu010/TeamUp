# TeamUp Server backup and recovery

The Server operator takes snapshots for disaster recovery. Collaboration remains the only application component that accesses the live business database. The operator takes read-only copies through SQLite's online backup API under the OS identity that already owns the database. The Gateway, Client and MCP keep their existing contracts.

## Take a snapshot

Run the command on the database host as the database owner. Use a private directory for staging. The production capture stages in RAM. The destination parent must already exist, belong to the current OS user, and deny access to group and other users.

```sh
install -d -m 700 "$HOME/teamup-backup-staging"
python3 scripts/ops/snapshot_sqlite.py \
  /path/to/live/teamup.db \
  "$HOME/teamup-backup-staging/teamup-20261126T081500Z.sqlite3"
```

Use a new destination path for each run. The command captures committed writes even when they still reside in the WAL. The live database keeps its journal mode. The command creates a standalone destination with mode `0600`. Success means SQLite's integrity and foreign-key checks passed and the destination was published. An existing destination or SQLite sidecar is an error and stays intact. If capture or validation fails, the command returns a nonzero exit code and publishes no snapshot. Take another snapshot at a fresh path if a run fails.

Treat the snapshot as production data. It includes private conversations and authentication records. Restrict access to recovery operators. Transfer snapshots only over the authenticated SSH connection to the backup host.

## Verify a restore

1. Copy the snapshot into a private staging directory in the isolated recovery environment. Verify the recorded checksum and run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on the restored file. Expect `ok` and zero foreign-key violations.
2. Make a disposable working copy and start the matching TeamUp Server on loopback with an isolated runtime configuration. Use an authorized recovery identity to verify representative room and task history through the TeamUp API.
3. Record the recovery point and time needed to recover. Keep the original backup for further attempts. Review credentials, retention, and pending delivery state before reopening a restored service to clients.

Off-host storage, hourly scheduling, and alerting are tracked in [issue #88](https://github.com/Jianyu010/TeamUp/issues/88).

## Scheduled off-host backups

Run the receiver on an independent Linux host with durable storage. Use a dedicated SSH key restricted to a server-side dispatcher. The dispatcher accepts only the exact configured source names, runs the associated capture command as that database's owner, and permits no shell, forwarding, or arbitrary paths. Each capture takes a standalone snapshot in a private RAM-backed directory. The SSH response carries the snapshot on stdout and one `TEAMUP_BACKUP_SHA256 <hex>` line on stderr. A nonzero exit invalidates the response.

The receiver verifies the snapshot digest for each source before publishing one timestamped generation and its receipt. A failed run leaves previous generations available. Use a timer to request both sources hourly, keep all generations for seven days and the latest generation for each UTC day through day 30, and check for staleness after two hours. Keep the receiver directory private and run the restore command in an isolated recovery environment. Files are stored as SQLite on the receiver.

### Install the source command

1. Install `snapshot_sqlite.py`, `capture_backup.py`, and `dispatch_backup.py` together in a root-owned directory such as `/usr/local/libexec/teamup-backup`. Create a dedicated SSH account with a locked password and an empty, root-owned home. Install only the backup receiver's public key for this account.
2. Put the allowed sources in a root-owned JSON file such as `/etc/teamup-backup/sources.json`. Each name maps to the existing database owner and configured database path.

   ```json
   {
     "pilot": {"owner": "teamup-pilot", "database": "/var/lib/teamup-pilot/teamup.db"},
     "secondary": {"owner": "ubuntu", "database": "/home/ubuntu/.local/state/teamup/teamup.db"}
   }
   ```

3. Prefix the receiver key in `authorized_keys` with `restrict,command="/usr/bin/python3 /usr/local/libexec/teamup-backup/dispatch_backup.py /etc/teamup-backup/sources.json"`. Give the dedicated account one exact sudo rule per source and validate the file with `visudo -cf` before installation. Keep the source account's other sudo rules empty.

   ```sudoers
   teamup-backup ALL=(teamup-pilot) NOPASSWD: NOSETENV: /usr/bin/python3 /usr/local/libexec/teamup-backup/capture_backup.py --source /var/lib/teamup-pilot/teamup.db
   teamup-backup ALL=(ubuntu) NOPASSWD: NOSETENV: /usr/bin/python3 /usr/local/libexec/teamup-backup/capture_backup.py --source /home/ubuntu/.local/state/teamup/teamup.db
   ```

### Install the receiver

Install `pull_backup.py` and `restore_backup.py` with `snapshot_sqlite.py` in `~/.local/lib/teamup-backup`. Install the `deploy/backup/` units in `~/.config/systemd/user/`. The receiver's private SSH configuration should pin the verified source host key, use the dedicated identity, and set `IdentitiesOnly yes`, `RequestTTY no`, and `LogLevel ERROR`. Store configuration and snapshots outside application and CI working directories. Use a dedicated receiver account where possible. If the receiver shares a host or OS login with build tooling, verify that build processes run in a separate filesystem namespace and cannot see its real home or production backups. Processes running unsandboxed under the receiver’s account can read its files.

Create the backup directory with mode `0700` and `~/.config/teamup-backup/receiver.env` with mode `0600`. The four values below are read by the user services.

```ini
TEAMUP_BACKUP_ROOT=/home/receiver/teamup-backups
TEAMUP_BACKUP_SSH_CONFIG=/home/receiver/.config/teamup-backup/ssh_config
TEAMUP_BACKUP_HOST=teamup-backup-source
TEAMUP_BACKUP_SOURCES="--source pilot --source secondary"
```

Have an administrator enable lingering for the receiver account so its timers start at boot and keep running after logout.

```sh
sudo loginctl enable-linger receiver
```

Then run the following commands as the receiver account. The first check must print `yes`.

```sh
loginctl show-user "$USER" --property=Linger --value
systemctl --user daemon-reload
systemctl --user start teamup-backup.service
systemctl --user start teamup-backup-check.service teamup-backup-verify.service
systemctl --user enable --now teamup-backup.timer teamup-backup-check.timer teamup-backup-verify.timer
systemctl --user list-timers 'teamup-backup*'
```

The 15-minute check verifies the latest manifest, age, and snapshot sizes. The daily check hashes the latest generation again. Review the receiver’s systemd journal for failed transfers, retention errors, stale copies, and checksum failures.

The source capture budgets 120 seconds per database for the online copy and SQLite validation. To increase it, set the source’s `timeout_seconds` integer in `sources.json` and add the matching `--timeout-seconds N` to that source’s exact sudo rule. The receiver uses a 180-second SSH deadline per source. Set `TEAMUP_BACKUP_SSH_TIMEOUT_SECONDS` in `receiver.env` to a value that leaves time for capture and transfer. Adjust the pull service’s `TimeoutStartSec` to cover all sources and local publication. Both the standalone snapshot and restore commands accept `--timeout-seconds` for SQLite copy and validation.

### Restore a stored generation

Use `manifest.json` from the selected generation to choose the source file and checksum. Restore into a new private directory, then follow the isolated application checks above.

```sh
python3 "$HOME/.local/lib/teamup-backup/restore_backup.py" \
  --snapshot /path/to/generation/pilot.sqlite3 \
  --sha256 "$SNAPSHOT_SHA256" \
  --destination /path/to/private-restore/teamup.db
```

The backup and restore commands run outside TeamUp Server and Client. Deploy them independently of application releases.
