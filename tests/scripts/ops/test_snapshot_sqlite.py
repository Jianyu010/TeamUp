import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts.ops import snapshot_sqlite

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ops/snapshot_sqlite.py"


def run_snapshot(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(destination)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_copies_commits_in_live_wal_to_private_standalone_file(tmp_path):
    source = tmp_path / "live with spaces #?.db"
    destination = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE messages (body TEXT)")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        writer.execute("INSERT INTO messages VALUES ('committed in WAL')")
        writer.commit()
        writer.execute("INSERT INTO messages VALUES ('uncommitted')")
        assert source.with_name(source.name + "-wal").stat().st_size > 0

        result = run_snapshot(source, destination)

        assert result.returncode == 0, result.stderr
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        with closing(
            sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
        ) as copy:
            assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert copy.execute("SELECT body FROM messages").fetchall() == [
                ("committed in WAL",),
            ]
        assert not destination.with_name(destination.name + "-wal").exists()


def test_existing_snapshot_stays_intact(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE messages (body TEXT)")
    assert run_snapshot(source, destination).returncode == 0
    original = destination.read_bytes()
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO messages VALUES ('new commit')")
        db.commit()

    result = run_snapshot(source, destination)

    assert result.returncode != 0
    assert "destination already exists" in result.stderr.lower()
    assert destination.read_bytes() == original


@pytest.mark.parametrize("source_bytes", [None, b"", b"not a SQLite database"])
def test_invalid_source_leaves_no_snapshot_or_temporary_files(tmp_path, source_bytes):
    source = tmp_path / "live.db"
    destination = tmp_path / "snapshot.sqlite3"
    if source_bytes is not None:
        source.write_bytes(source_bytes)

    result = run_snapshot(source, destination)

    assert result.returncode != 0
    assert "snapshot failed" in result.stderr.lower()
    assert set(tmp_path.iterdir()) == ({source} if source_bytes is not None else set())


def test_foreign_key_damage_is_not_published(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent (id));
            INSERT INTO child VALUES (123);
        """)

    result = run_snapshot(source, destination)

    assert result.returncode != 0
    assert "foreign key check failed" in result.stderr.lower()
    assert list(tmp_path.iterdir()) == [source]


def test_rejects_shared_staging_directory(tmp_path):
    source = tmp_path / "live.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE messages (body TEXT)")
    public = tmp_path / "shared"
    public.mkdir(mode=0o755)

    result = run_snapshot(source, public / "snapshot.sqlite3")

    assert result.returncode != 0
    assert "destination directory must be private" in result.stderr.lower()
    assert list(public.iterdir()) == []


def test_directory_sync_failure_does_not_leave_published_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "live.db"
    destination = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE messages (body TEXT)")

    real_fsync = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(snapshot_sqlite.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="simulated directory sync failure"):
        snapshot_sqlite.snapshot_database(source, destination)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_destination_sidecar_blocks_publication(tmp_path, suffix):
    source = tmp_path / "live.db"
    destination = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE messages (body TEXT)")
    sidecar = destination.with_name(destination.name + suffix)
    sidecar.symlink_to(tmp_path / "unavailable")

    result = run_snapshot(source, destination)

    assert result.returncode != 0
    assert "destination has an existing sqlite sidecar" in result.stderr.lower()
    assert not destination.exists()
    assert sidecar.is_symlink()


def test_validation_obeys_snapshot_deadline(tmp_path, monkeypatch):
    source = tmp_path / "live.db"
    destination = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE messages (body TEXT)")
        db.executemany("INSERT INTO messages VALUES (?)", [("message",)] * 3000)
    times = iter((0, 0))
    monkeypatch.setattr(snapshot_sqlite, "monotonic", lambda: next(times, 2))

    with pytest.raises(TimeoutError, match="SQLite snapshot deadline exceeded"):
        snapshot_sqlite.snapshot_database(source, destination, timeout_seconds=1)

    assert set(tmp_path.iterdir()) == {source}
