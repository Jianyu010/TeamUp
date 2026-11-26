import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from scripts.ops import pull_backup

NOW = datetime(2026, 11, 26, 8, 15, tzinfo=UTC)
SOURCES = ("pilot", "primary")
SNAPSHOTS = {
    source: b"SQLite format 3\x00snapshot-" + source.encode() for source in SOURCES
}


@pytest.fixture
def backup_root(tmp_path, monkeypatch):
    root = tmp_path / "backup root"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(pull_backup, "utc_now", lambda: NOW)
    return root


@pytest.fixture
def ssh(monkeypatch):
    responses = {
        source: (0, payload, receipt(payload)) for source, payload in SNAPSHOTS.items()
    }

    def run(command, *, stdin, stdout, stderr, timeout, check):
        assert command[:9] == [
            "/usr/bin/ssh",
            "-F",
            "/private/ssh config",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ClearAllForwardings=yes",
        ]
        assert command[9:-1] == ["--", "teamup-backup"]
        assert stdin == subprocess.DEVNULL
        assert timeout > 0
        assert check is False
        response = responses[command[-1]]
        if response is None:
            raise subprocess.TimeoutExpired(command, timeout, stderr=b"private detail")
        returncode, payload, diagnostic = response
        os.write(stdout.fileno(), payload)
        os.write(stderr.fileno(), diagnostic)
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(pull_backup.subprocess, "run", run)
    return responses


def receipt(payload):
    return (
        b"TEAMUP_BACKUP_SHA256 " + hashlib.sha256(payload).hexdigest().encode() + b"\n"
    )


def run_cli(command, root, *sources):
    options = [command, "--root", str(root)]
    if command == "pull":
        options += ["--ssh-config", "/private/ssh config", "--host", "teamup-backup"]
    else:
        options += ["--max-age-hours", "2"]
    for source in sources or SOURCES:
        options += ["--source", source]
    return pull_backup.main(options)


def generation_name(timestamp):
    return timestamp.strftime("%Y%m%dT%H%M%S.%fZ")


def manifest(timestamp):
    return {
        "version": 1,
        "created_at": timestamp.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "sources": {
            source: {
                "file": f"{source}.sqlite3",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for source, payload in SNAPSHOTS.items()
        },
    }


def write_generation(root, timestamp):
    generation = root / generation_name(timestamp)
    generation.mkdir(mode=0o700)
    (generation / "manifest.json").write_text(json.dumps(manifest(timestamp)))
    for source, payload in SNAPSHOTS.items():
        (generation / f"{source}.sqlite3").write_bytes(payload)
    for entry in generation.iterdir():
        entry.chmod(0o600)
    return generation


def test_publishes_one_private_generation_for_two_sources(backup_root, ssh):
    assert run_cli("pull", backup_root) == 0

    generation = backup_root / generation_name(NOW)
    assert list(backup_root.iterdir()) == [generation]
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    assert json.loads((generation / "manifest.json").read_text()) == manifest(NOW)
    for source, payload in SNAPSHOTS.items():
        assert (generation / f"{source}.sqlite3").read_bytes() == payload
    assert all(
        stat.S_IMODE(item.stat().st_mode) == 0o600 for item in generation.iterdir()
    )
    assert run_cli("check", backup_root) == 0


@pytest.mark.parametrize(
    "second_response",
    [
        (1, b"snapshot", b"private remote error"),
        (0, b"truncated", receipt(b"truncated snapshot")),
        (0, b"snapshot", receipt(b"snapshot")[:-2] + b"\n"),
        (0, b"snapshot", receipt(b"snapshot") + b"private remote error\n"),
        (0, b"", receipt(b"")),
        None,
    ],
)
def test_failed_second_source_preserves_prior_generation(
    backup_root, ssh, second_response, capsys
):
    prior = write_generation(backup_root, NOW - timedelta(days=31))
    ssh["primary"] = second_response

    assert run_cli("pull", backup_root) != 0

    assert list(backup_root.iterdir()) == [prior]
    assert (prior / "pilot.sqlite3").read_bytes() == SNAPSHOTS["pilot"]
    output = capsys.readouterr()
    assert output.out == ""
    assert "primary" in output.err
    assert "private remote error" not in output.err
    assert "private detail" not in output.err


@pytest.mark.parametrize("sources", [("pilot", "pilot"), ("../pilot",), ("pilot; id",)])
def test_rejects_unsafe_or_duplicate_sources(backup_root, sources):
    assert run_cli("pull", backup_root, *sources) != 0
    assert list(backup_root.iterdir()) == []


def test_rejects_a_public_or_symlink_root(backup_root, ssh):
    alias = backup_root.with_name("alias")
    alias.symlink_to(backup_root)
    assert run_cli("pull", alias) != 0

    backup_root.chmod(0o755)
    assert run_cli("pull", backup_root) != 0
    assert list(backup_root.iterdir()) == []


def test_pull_requires_an_exclusive_nonblocking_lock(backup_root, ssh):
    root_fd = os.open(backup_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_cli("pull", backup_root) != 0
    finally:
        os.close(root_fd)

    assert list(backup_root.iterdir()) == []


def test_existing_generation_is_never_replaced(backup_root, ssh):
    generation = write_generation(backup_root, NOW)
    existing = (generation / "manifest.json").read_bytes()

    assert run_cli("pull", backup_root) != 0

    assert list(backup_root.iterdir()) == [generation]
    assert (generation / "manifest.json").read_bytes() == existing


def test_unsynced_publication_is_rolled_back(backup_root, ssh, monkeypatch):
    prior = write_generation(backup_root, NOW - timedelta(hours=1))
    root_inode = backup_root.stat().st_ino
    fsync = os.fsync

    def fail_root_fsync(fd):
        if os.fstat(fd).st_ino == root_inode:
            raise OSError("simulated directory sync error")
        fsync(fd)

    monkeypatch.setattr(pull_backup.os, "fsync", fail_root_fsync)

    assert run_cli("pull", backup_root) != 0

    assert list(backup_root.iterdir()) == [prior]


def test_check_rejects_missing_and_stale_generations(backup_root):
    assert run_cli("check", backup_root) != 0
    write_generation(backup_root, NOW - timedelta(hours=2, seconds=1))
    assert run_cli("check", backup_root) != 0


@pytest.mark.parametrize(
    "damage", ["manifest", "same-size snapshot", "missing", "symlink"]
)
def test_check_fails_if_latest_generation_is_corrupt(backup_root, damage):
    write_generation(backup_root, NOW - timedelta(minutes=60))
    latest = write_generation(backup_root, NOW - timedelta(minutes=1))
    target = latest / "pilot.sqlite3"
    if damage == "manifest":
        (latest / "manifest.json").write_text("{}")
    elif damage == "same-size snapshot":
        target.write_bytes(b"X" * len(SNAPSHOTS["pilot"]))
    else:
        target.unlink()
        if damage == "symlink":
            target.symlink_to(
                backup_root / generation_name(NOW - timedelta(minutes=60)) / target.name
            )

    assert run_cli("check", backup_root) != 0


def test_check_requires_all_configured_sources(backup_root):
    write_generation(backup_root, NOW)

    assert run_cli("check", backup_root, "pilot", "other") != 0


def test_retention_keeps_hourly_and_newest_daily_without_touching_other_paths(
    backup_root, ssh
):
    hourly = [
        write_generation(backup_root, NOW - timedelta(days=7, hours=-offset))
        for offset in (0, 1)
    ]
    daily = write_generation(backup_root, NOW - timedelta(days=8))
    superseded = write_generation(backup_root, NOW - timedelta(days=8, hours=1))
    day_30 = write_generation(backup_root, NOW - timedelta(days=30, hours=1))
    expired = write_generation(backup_root, NOW - timedelta(days=31))
    unrelated = backup_root / "unrelated"
    unrelated.mkdir()
    unrelated_link = backup_root / generation_name(NOW - timedelta(days=32))
    unrelated_link.symlink_to(unrelated)
    unrecognized = write_generation(backup_root, NOW - timedelta(days=33))
    (unrecognized / "unrelated").write_text("keep me")

    assert run_cli("pull", backup_root) == 0

    assert all(
        path.is_dir() for path in [*hourly, daily, day_30, unrelated, unrecognized]
    )
    assert unrelated_link.is_symlink()
    assert (unrecognized / "unrelated").read_text() == "keep me"
    assert not superseded.exists()
    assert not expired.exists()


def test_retention_does_not_follow_symlinked_generation_files(backup_root, ssh):
    expired = write_generation(backup_root, NOW - timedelta(days=31))
    target = backup_root / "unrelated.sqlite3"
    target.write_bytes(SNAPSHOTS["pilot"])
    target.chmod(0o600)
    snapshot = expired / "pilot.sqlite3"
    snapshot.unlink()
    snapshot.symlink_to(target)

    assert run_cli("pull", backup_root) == 0

    assert snapshot.is_symlink()
    assert target.read_bytes() == SNAPSHOTS["pilot"]


def test_pruning_failure_reports_successful_publication_separately(
    backup_root, ssh, monkeypatch, capsys
):
    def fail_pruning(*args):
        raise OSError("private storage detail")

    monkeypatch.setattr(pull_backup, "prune_generations", fail_pruning)

    assert run_cli("pull", backup_root) != 0

    assert (backup_root / generation_name(NOW) / "manifest.json").is_file()
    error = capsys.readouterr().err.lower()
    assert "published" in error
    assert "retention failed" in error
    assert "private storage detail" not in error


def test_interrupted_retention_is_retried_without_touching_unrelated_paths(
    backup_root, ssh, monkeypatch
):
    expired = write_generation(backup_root, NOW - timedelta(days=31))
    abandoned = backup_root / ".teamup-pull-abcd1234"
    abandoned.mkdir(mode=0o700)
    (abandoned / "pilot.sqlite3").write_bytes(b"unfinished")
    unrelated = backup_root / ".teamup-pull-keep"
    unrelated.mkdir(mode=0o700)
    real_rmtree = shutil.rmtree

    def interrupt_pruning(path, *args, **kwargs):
        if str(path).split("/")[-1] == f".teamup-expired-{expired.name}":
            raise OSError("simulated interrupted cleanup")
        return real_rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", interrupt_pruning)
        assert run_cli("pull", backup_root) != 0
    pending_cleanup = backup_root / f".teamup-expired-{expired.name}"
    assert pending_cleanup.is_dir()
    assert not expired.exists()
    assert not abandoned.exists()

    monkeypatch.setattr(pull_backup, "utc_now", lambda: NOW + timedelta(hours=1))
    assert run_cli("pull", backup_root) == 0
    assert not pending_cleanup.exists()
    assert unrelated.is_dir()


def test_retention_keeps_an_older_daily_copy_if_newest_is_corrupt(backup_root, ssh):
    newest_daily = write_generation(backup_root, NOW - timedelta(days=8))
    older_daily = write_generation(backup_root, NOW - timedelta(days=8, hours=1))
    (newest_daily / "pilot.sqlite3").write_bytes(b"corrupt")

    assert run_cli("pull", backup_root) == 0

    assert older_daily.is_dir()
