# TeamUp Server Backups Implementation Plan

**Goal** Recover the TeamUp Server databases after host loss, operator error, or a failed upgrade, with at most one hour of lost writes in normal operation.

**Architecture** Run the backup as an operator job outside the application request path. Take a consistent SQLite snapshot of each configured TeamUp database using SQLite's online backup API. Validate and encrypt the snapshots before writing them into an independently administered, versioned off-host repository. Restore into an isolated TeamUp instance before promoting a recovered copy.

**Tech stack** Python 3.12, SQLite, systemd, public-key file encryption, versioned object storage.

Tracking issue [#88](https://github.com/Jianyu010/TeamUp/issues/88).

## 1. Create and validate standalone snapshots

- [x] Publish the operator interface in `docs/modules/server-backup.md` and link it from `docs/README.md`. Collaboration retains ownership of live writes. The operator CLI does not add an application route, grant, or schema change.
- [x] Add `tests/scripts/ops/test_snapshot_sqlite.py`. Run the CLI through `subprocess` against a WAL database with auto-checkpointing disabled. Keep the writer open while committing rows and assert that the resulting standalone file contains them. Cover missing or malformed sources, foreign-key damage, private file modes, and attempts to reuse a destination path.
- [x] Implement `python3 scripts/ops/snapshot_sqlite.py SOURCE DESTINATION`. Open the source with `mode=ro`, use `Connection.backup()` into owner-only staging, run integrity and foreign-key checks, and publish only on success without overwriting a prior backup. Run `uv run --no-sync pytest -q tests/scripts/ops tests/test_boundaries.py`.

The local recovery drill ran the snapshot command against a saved pilot database, started TeamUp on a disposable copy, and read a room message and task event through authenticated APIs.

## 2. Configure encrypted off-host storage

- [ ] Confirm the team's approved object-storage account, recovery-key custodians, and who can authorize retention changes. Provision a private, versioned bucket in an independently administered account. Enable a retention policy that preserves older versions through a server credential compromise.
- [ ] Use `age` with a configured public recipient. Store only the public recipient on the backup host. Verify a recipient can decrypt on a recovery host before enabling unattended runs. Keep the private recovery key in team-approved custody and exercise recovery without access to the source server.
- [ ] Add a deployment wrapper under `deploy/` that runs the snapshot command as each configured database owner in private staging, encrypts using `age --recipient`, and passes only ciphertext to the uploader under unique UTC keys. Include an encrypted manifest with capture times, per-file digests, and the Server release reference. Configure an upload identity with create-only access and a separate restore identity. Treat a run as successful only when every source and its manifest are confirmed in off-host storage.
- [ ] Exercise the upload path with synthetic databases in a test bucket. Simulate a failed upload and verify that it cannot advance the successful-backup marker. Confirm older objects are still available after rerunning the job.

## 3. Schedule and prove recovery

- [ ] Install a persistent hourly systemd timer on the approved backup runner. Start with 7 days of hourly copies and 30 days of daily copies, subject to team data policy. Manage expiry at the store and keep upgrade snapshots until post-upgrade verification succeeds. Alert independently after two hours without a successful off-host run and verify the alert by suppressing a run in staging.
- [ ] Recover from stored ciphertext on an isolated host. Verify manifest hashes, run SQLite integrity and foreign-key checks, start the matching TeamUp release on loopback, and read a representative room transcript and task history through authenticated TeamUp API calls. Verify that runtime configuration can be rebuilt by authorized operators and address credentials and transient delivery state before reopening restored service.
- [ ] Record date, recovery point, elapsed recovery time, and results in the operator runbook. Repeat quarterly and after changes to storage, encryption, or database migrations.
