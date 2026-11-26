# TeamUp Server backup and recovery

The Server operator takes snapshots for disaster recovery. Collaboration remains the only application component that accesses the live business database. The operator takes read-only copies through SQLite's online backup API under the OS identity that already owns the database. The Gateway, Client and MCP keep their existing contracts.

## Take a snapshot

Run the command on the database host as the database owner. Use a private directory on encrypted storage or a private RAM-backed directory for plaintext staging. The destination parent must already exist, belong to the current OS user, and deny access to group and other users.

```sh
install -d -m 700 "$HOME/teamup-backup-staging"
python3 scripts/ops/snapshot_sqlite.py \
  /path/to/live/teamup.db \
  "$HOME/teamup-backup-staging/teamup-20261126T081500Z.sqlite3"
```

Use a new destination path for each run. The command captures committed writes even when they still reside in the WAL. The live database keeps its journal mode. The command creates a standalone destination with mode `0600`. Success means SQLite's integrity and foreign-key checks passed and the destination was published. An existing destination or SQLite sidecar is an error and stays intact. If capture or validation fails, the command returns a nonzero exit code and publishes no snapshot. Take another snapshot at a fresh path if a run fails.

Treat the snapshot as production data. It includes private conversations and authentication records. Restrict access to recovery operators. Encrypt the snapshot before transferring it to the off-host backup repository. Keep the decryption key with recovery operators.

## Verify a restore

1. Decrypt or copy the snapshot into a private staging directory in the isolated recovery environment. Verify the recorded checksum and run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on the restored file. Expect `ok` and zero foreign-key violations.
2. Make a disposable working copy and start the matching TeamUp Server on loopback with an isolated runtime configuration. Use an authorized recovery identity to verify representative room and task history through the TeamUp API.
3. Record the recovery point and time needed to recover. Keep the original backup for further attempts. Review credentials, retention, and pending delivery state before reopening a restored service to clients.

Encrypted off-host storage, hourly scheduling, and alerting are tracked in [issue #88](https://github.com/Jianyu010/TeamUp/issues/88).
