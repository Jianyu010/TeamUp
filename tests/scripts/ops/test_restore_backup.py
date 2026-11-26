import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ops/restore_backup.py"


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / "stored snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as database:
        database.execute("CREATE TABLE messages (body TEXT)")
        database.execute("INSERT INTO messages VALUES ('saved message')")
        database.commit()
    return source


def run_restore(
    source: Path, destination: Path, digest: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--snapshot",
            str(source),
            "--sha256",
            digest,
            "--destination",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_restores_verified_snapshot_to_private_standalone_file(tmp_path, snapshot):
    original = snapshot.read_bytes()
    destination = tmp_path / "restored #?.sqlite3"

    result = run_restore(snapshot, destination, hashlib.sha256(original).hexdigest())

    assert result.returncode == 0, result.stderr
    assert snapshot.read_bytes() == original
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as copy:
        assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert copy.execute("SELECT body FROM messages").fetchall() == [
            ("saved message",)
        ]
    assert set(tmp_path.iterdir()) == {snapshot, destination}


@pytest.mark.parametrize(
    ("digest", "error"),
    [("a" * 64, "checksum mismatch"), ("A" * 64, "lowercase hexadecimal")],
)
def test_rejects_invalid_digest_without_leaving_staging(
    tmp_path, snapshot, digest, error
):
    original = snapshot.read_bytes()

    result = run_restore(snapshot, tmp_path / "restored.sqlite3", digest)

    assert result.returncode != 0
    assert error in result.stderr.lower()
    assert snapshot.read_bytes() == original
    assert set(tmp_path.iterdir()) == {snapshot}


@pytest.mark.parametrize("invalid", ["corrupt", "foreign key violation"])
def test_rejects_invalid_sqlite_even_with_matching_digest(tmp_path, snapshot, invalid):
    if invalid == "corrupt":
        snapshot.write_bytes(b"test data instead of a valid SQLite database")
    else:
        with closing(sqlite3.connect(snapshot)) as database:
            database.executescript("""
                PRAGMA foreign_keys = OFF;
                CREATE TABLE parent (id INTEGER PRIMARY KEY);
                CREATE TABLE child (parent_id INTEGER REFERENCES parent (id));
                INSERT INTO child VALUES (123);
            """)
    original = snapshot.read_bytes()

    result = run_restore(
        snapshot, tmp_path / "restored.sqlite3", hashlib.sha256(original).hexdigest()
    )

    assert result.returncode != 0
    assert "sqlite validation" in result.stderr.lower()
    assert snapshot.read_bytes() == original
    assert set(tmp_path.iterdir()) == {snapshot}


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_preserves_existing_destination_or_sidecar(tmp_path, snapshot, suffix):
    destination = tmp_path / "restored.sqlite3"
    existing = destination.with_name(destination.name + suffix)
    existing.write_bytes(b"keep this existing file")
    original = snapshot.read_bytes()

    result = run_restore(snapshot, destination, hashlib.sha256(original).hexdigest())

    assert result.returncode != 0
    assert "new destination" in result.stderr.lower()
    assert existing.read_bytes() == b"keep this existing file"
    assert snapshot.read_bytes() == original
    assert set(tmp_path.iterdir()) == {snapshot, existing}


def test_rejects_shared_destination_directory(tmp_path, snapshot):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)

    result = run_restore(
        snapshot,
        shared / "restored.sqlite3",
        hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    )

    assert result.returncode != 0
    assert "destination directory must be private" in result.stderr.lower()
    assert not list(shared.iterdir())


def test_rejects_fifo_as_snapshot_without_waiting_for_writer(tmp_path):
    source = tmp_path / "pipe"
    os.mkfifo(source)

    result = run_restore(source, tmp_path / "restored.sqlite3", "a" * 64)

    assert result.returncode != 0
    assert "regular file" in result.stderr.lower()
    assert set(tmp_path.iterdir()) == {source}
