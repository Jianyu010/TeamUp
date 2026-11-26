"""Capture a validated SQLite snapshot for a restricted SSH backup request."""

import argparse
import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from snapshot_sqlite import snapshot_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--scratch", type=Path, default=Path("/dev/shm"))
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()

    try:
        with TemporaryDirectory(prefix="teamup-capture-", dir=args.scratch) as staging:
            snapshot = Path(staging) / "snapshot.sqlite3"
            snapshot_database(
                args.source, snapshot, timeout_seconds=args.timeout_seconds
            )
            with snapshot.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                shutil.copyfileobj(stream, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        print(f"TEAMUP_BACKUP_SHA256 {digest}", file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error):
        print(
            "Snapshot capture failed. Check source access and integrity.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
