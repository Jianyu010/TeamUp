import hashlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ops/capture_backup.py"


def capture(source, scratch):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(source),
            "--scratch",
            str(scratch),
        ],
        capture_output=True,
        check=False,
        timeout=20,
    )


def test_streams_validated_snapshot_and_hash_receipt(tmp_path):
    source = tmp_path / "source.sqlite3"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE messages (body TEXT)")
        connection.execute("INSERT INTO messages VALUES (?)", ("private test message",))

    result = capture(source, scratch)

    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(result.stdout).hexdigest()
    assert result.stderr.decode().splitlines() == [f"TEAMUP_BACKUP_SHA256 {digest}"]
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(result.stdout)
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("SELECT body FROM messages").fetchone() == (
            "private test message",
        )
    assert list(scratch.iterdir()) == []


def test_failed_capture_emits_no_snapshot_or_source_details(tmp_path):
    source = tmp_path / "private-source.sqlite3"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source.write_bytes(b"not a database")

    result = capture(source, scratch)

    assert result.returncode != 0
    assert (
        result.stderr
        == b"Snapshot capture failed. Check source access and integrity.\n"
    )
    assert not result.stdout
    assert list(scratch.iterdir()) == []
