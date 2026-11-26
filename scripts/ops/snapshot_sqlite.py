"""Take a validated SQLite snapshot as a database host operator."""

import argparse
import math
import os
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

DEFAULT_TIMEOUT_SECONDS = 60


def copy_database(source: Path, snapshot: Path, timeout_seconds: float) -> None:
    deadline = monotonic() + timeout_seconds

    def expired() -> int:
        return int(monotonic() >= deadline)

    def check_deadline(status: int, _remaining: int, _total: int) -> None:
        if status != sqlite3.SQLITE_DONE and expired():
            raise TimeoutError("SQLite snapshot deadline exceeded")

    if not source.is_file():
        raise ValueError("source database must be an existing regular file")
    live_uri = source.absolute().as_uri() + "?mode=ro"
    busy_timeout = min(timeout_seconds, 5)
    snapshot.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(snapshot, timeout=busy_timeout)) as copy:
        with closing(sqlite3.connect(live_uri, uri=True, timeout=busy_timeout)) as live:
            if live.execute("PRAGMA page_count").fetchone() == (0,):
                raise ValueError("source database is empty")
            live.backup(copy, pages=256, progress=check_deadline, sleep=0.05)
        if expired():
            raise TimeoutError("SQLite snapshot deadline exceeded")
        copy.set_progress_handler(expired, 1000)
        try:
            if copy.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
                raise ValueError("could not create a standalone snapshot")
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("integrity check failed")
            if copy.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("foreign key check failed")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                raise TimeoutError("SQLite snapshot deadline exceeded") from None
            raise


def snapshot_database(
    source: Path, destination: Path, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("SQLite snapshot timeout must be a positive finite number")
    parent = destination.parent.resolve(strict=True)
    info = parent.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError(
            "destination directory must be private and owned by the current user"
        )
    destination = parent / destination.name
    if os.path.lexists(destination):
        raise FileExistsError("destination already exists")

    if any(
        os.path.lexists(str(destination) + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    ):
        raise FileExistsError("destination has an existing SQLite sidecar")

    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    published = False
    try:
        with TemporaryDirectory(prefix=".teamup-snapshot-", dir=parent) as staging:
            snapshot = Path(staging) / "snapshot.sqlite3"
            copy_database(source, snapshot, timeout_seconds)
            with snapshot.open("rb") as file:
                os.fsync(file.fileno())
            try:
                os.link(snapshot, destination.name, dst_dir_fd=directory)
            except FileExistsError:
                raise FileExistsError("destination already exists") from None
            published = True
        os.fsync(directory)
    except OSError:
        if published:
            os.unlink(destination.name, dir_fd=directory)
        raise
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="live database path")
    parser.add_argument(
        "destination", type=Path, help="new snapshot path in private staging"
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    args = parser.parse_args()
    try:
        snapshot_database(
            args.source, args.destination, timeout_seconds=args.timeout_seconds
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
