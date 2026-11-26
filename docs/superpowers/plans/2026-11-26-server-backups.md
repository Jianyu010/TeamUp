# TeamUp Server Backups Implementation Plan

**Goal** Recover the TeamUp Server databases after host loss, operator error, or a failed upgrade, with at most one hour of lost writes in normal operation.

**Architecture** Run the backup as an operator job outside the application request path. Take a consistent SQLite snapshot of each configured TeamUp database using SQLite's online backup API. Validate the snapshots and transfer them over SSH before publishing them into an independently administered, versioned off-host repository on the independent backup host. Restore into an isolated TeamUp instance before promoting a recovered copy.

**Tech stack** Python 3.12, SQLite, systemd, restricted SSH, and systemd user timers.

Tracking issue [#88](https://github.com/Jianyu010/TeamUp/issues/88).

## 1. Create and validate standalone snapshots

- [x] Publish the operator interface in `docs/modules/server-backup.md` and link it from `docs/README.md`. Collaboration retains ownership of live writes. The operator CLI does not add an application route, grant, or schema change.
- [x] Add `tests/scripts/ops/test_snapshot_sqlite.py`. Run the CLI through `subprocess` against a WAL database with auto-checkpointing disabled. Keep the writer open while committing rows and assert that the resulting standalone file contains them. Cover missing or malformed sources, foreign-key damage, private file modes, and attempts to reuse a destination path.
- [x] Implement `python3 scripts/ops/snapshot_sqlite.py SOURCE DESTINATION`. Open the source with `mode=ro`, use `Connection.backup()` into owner-only staging, run integrity and foreign-key checks, and publish only on success without overwriting a prior backup. Run `uv run --no-sync pytest -q tests/scripts/ops tests/test_boundaries.py`.

The local recovery drill ran the snapshot command against a saved pilot database, started TeamUp on a disposable copy, and read a room message and task event through authenticated APIs.

## 2. Configure off-host storage

- [ ] Use `hp-laptop` as the receiver. Give it a dedicated SSH key for a fixed source dispatcher. Each source command runs under its database owner and may send its validated snapshot only.
- [ ] Keep snapshots in a private receiver directory. SSH provides encryption in transit. The owner approved storing standalone SQLite files on the HP receiver.
- [ ] Implement the source capture and receiver commands. Stream the snapshot and verify its SHA-256 receipt over SSH. Publish only complete generations with their manifest. Keep prior generations on retry or transfer failure.
- [ ] Test transfer, retention and restore with synthetic data before production activation.

## 3. Schedule and prove recovery

- [ ] Install a persistent hourly systemd user timer on the HP receiver. Start with 7 days of hourly copies and 30 days of daily copies, subject to team data policy. Manage expiry on the receiver and keep upgrade snapshots until post-upgrade verification succeeds. Run a separate freshness check and journal an error after two hours without a successful off-host run and verify the alert by suppressing a run in staging.
- [ ] Recover from a stored generation on an isolated host. Verify manifest hashes, run SQLite integrity and foreign-key checks, start the matching TeamUp release on loopback, and read a representative room transcript and task history through authenticated TeamUp API calls. Verify that runtime configuration can be rebuilt by authorized operators and address credentials and transient delivery state before reopening restored service.
- [ ] Record date, recovery point, elapsed recovery time, and results in the operator runbook. Repeat quarterly and after changes to storage, SSH access, or database migrations.
