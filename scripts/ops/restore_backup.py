"""Restore a TeamUp SQLite backup verified against its SHA-256 receipt."""

import argparse
import hashlib
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from snapshot_sqlite import DEFAULT_TIMEOUT_SECONDS, snapshot_database


class RestoreError(Exception):
    """A restore failure with a safe message for the operator."""


def copy_verified_snapshot(source: Path, staged: Path, expected_digest: str) -> None:
    digest = hashlib.sha256()
    # A named pipe must fail the regular-file check without waiting for a writer.
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NONBLOCK), "rb") as stored:
        if not stat.S_ISREG(os.fstat(stored.fileno()).st_mode):
            raise RestoreError("snapshot must be an existing regular file")
        staged.touch(mode=0o600, exist_ok=False)
        with staged.open("wb") as copy:
            while chunk := stored.read(1024 * 1024):
                copy.write(chunk)
                digest.update(chunk)
    if digest.hexdigest() != expected_digest:
        raise RestoreError("checksum mismatch. Check the backup receipt and snapshot")


def restore_database(
    source: Path, destination: Path, expected_digest: str, timeout_seconds: float
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise RestoreError("SHA-256 must be 64 lowercase hexadecimal characters")
    parent = destination.parent.resolve(strict=True)
    info = parent.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise RestoreError(
            "destination directory must be private and owned by the current user"
        )

    with TemporaryDirectory(prefix=".teamup-restore-", dir=parent) as staging:
        staged = Path(staging) / "verified.sqlite3"
        copy_verified_snapshot(source, staged, expected_digest)
        try:
            snapshot_database(
                staged, parent / destination.name, timeout_seconds=timeout_seconds
            )
        except (ValueError, sqlite3.Error) as exc:
            raise RestoreError(
                "snapshot failed SQLite validation. Choose a different backup"
            ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path, help="stored snapshot")
    parser.add_argument(
        "--sha256", required=True, help="SHA-256 from the backup receipt"
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        help="new path in a private directory",
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    args = parser.parse_args()
    try:
        restore_database(
            args.snapshot, args.destination, args.sha256, args.timeout_seconds
        )
    except RestoreError as exc:
        message = str(exc)
    except FileExistsError:
        message = "choose a new destination with no existing SQLite sidecars"
    except TimeoutError:
        message = "SQLite backup timed out. Retry the restore"
    except OSError:
        message = (
            "check snapshot access and free space in the private destination directory"
        )
    else:
        return 0
    print(f"Restore failed: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
