"""Receive and check complete TeamUp SQLite backup generations over SSH."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from errno import ELOOP
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

SOURCE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
HOST_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
GENERATION_NAME = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")
ABANDONED_STAGE = re.compile(r"\.teamup-pull-[a-z0-9_]{8}")
EXPIRED_PREFIX = ".teamup-expired-"
GENERATION_FORMAT = "%Y%m%dT%H%M%S.%fZ"
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
RECEIPT = re.compile(rb"TEAMUP_BACKUP_SHA256 ([0-9a-f]{64})\n")
MANIFEST = "manifest.json"
MAX_MANIFEST_BYTES = 1024 * 1024
SSH_TIMEOUT_SECONDS = 180


class BackupError(ValueError):
    """A backup failure with a message safe for the operator journal."""


class RetentionError(BackupError):
    """A published generation whose retention pass failed."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def snapshot_name(source: str) -> str:
    return f"{source}.sqlite3"


@contextmanager
def private_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupError(
                "backup directories must be owned by the current user with mode 0700"
            )
        yield fd
    finally:
        os.close(fd)


def private_file(path: Path, *, create: bool = False):
    if create:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "w+b")
        except BaseException:
            os.close(fd)
            raise

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupError(
                "backup files must be regular, privately owned files with mode 0600"
            )
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def receive_snapshot(
    staging: Path, ssh_config: Path, host: str, source: str, timeout_seconds: float
) -> dict:
    name = snapshot_name(source)
    command = [
        "/usr/bin/ssh",
        "-F",
        str(ssh_config),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ClearAllForwardings=yes",
        "--",
        host,
        source,
    ]
    with (
        private_file(staging / name, create=True) as snapshot,
        TemporaryFile(dir=staging) as receipt,
    ):
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=snapshot,
                stderr=receipt,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise BackupError(f"SSH capture timed out for {source}") from None
        if result.returncode != 0:
            raise BackupError(f"SSH capture failed for {source}")

        receipt.seek(0)
        if not (match := RECEIPT.fullmatch(receipt.read(128))):
            raise BackupError(f"invalid SSH checksum receipt for {source}")
        size = os.fstat(snapshot.fileno()).st_size
        if size == 0:
            raise BackupError(f"empty snapshot received for {source}")
        snapshot.seek(0)
        digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
        if digest != match.group(1).decode("ascii"):
            raise BackupError(f"snapshot checksum mismatch for {source}")
        os.fsync(snapshot.fileno())
    return {"file": name, "sha256": digest, "size_bytes": size}


def list_generations(root: Path) -> list[tuple[datetime, Path]]:
    generations = []
    for path in root.iterdir():
        if GENERATION_NAME.fullmatch(path.name):
            try:
                stamp = datetime.strptime(path.name, GENERATION_FORMAT).replace(
                    tzinfo=UTC
                )
            except ValueError:
                continue
            generations.append((stamp, path))
    return sorted(generations, reverse=True)


def read_generation(path: Path, stamp: datetime) -> dict:
    with private_directory(path):
        with private_file(path / MANIFEST) as file:
            data = file.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise BackupError("generation manifest is too large")
        try:
            manifest = json.loads(data)
        except (ValueError, RecursionError):
            raise BackupError("invalid generation manifest") from None
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"version", "created_at", "sources"}
            or type(manifest["version"]) is not int
            or manifest["version"] != 1
            or manifest["created_at"] != utc_timestamp(stamp)
            or not isinstance(manifest["sources"], dict)
            or not manifest["sources"]
        ):
            raise BackupError("invalid generation manifest")

        entries = manifest["sources"]
        for source, record in entries.items():
            if (
                not SOURCE_LABEL.fullmatch(source)
                or not isinstance(record, dict)
                or set(record) != {"file", "sha256", "size_bytes"}
                or record["file"] != snapshot_name(source)
                or not isinstance(record["sha256"], str)
                or not HEX_DIGEST.fullmatch(record["sha256"])
                or type(record["size_bytes"]) is not int
                or record["size_bytes"] <= 0
            ):
                raise BackupError("invalid generation manifest")

        filenames = {snapshot_name(source) for source in entries}
        if {file.name for file in path.iterdir()} != filenames | {MANIFEST}:
            raise BackupError("generation files do not match the manifest")
        for name in filenames:
            with private_file(path / name):
                pass
    return entries


def verify_snapshots(
    generation: Path, entries: dict, *, metadata_only: bool = False
) -> None:
    for source, record in entries.items():
        with private_file(generation / snapshot_name(source)) as snapshot:
            if os.fstat(snapshot.fileno()).st_size != record["size_bytes"] or (
                not metadata_only
                and hashlib.file_digest(snapshot, "sha256").hexdigest()
                != record["sha256"]
            ):
                raise BackupError(f"backup snapshot failed verification for {source}")


def clean_abandoned_directories(root: Path) -> None:
    for path in root.iterdir():
        expired = path.name.startswith(EXPIRED_PREFIX) and GENERATION_NAME.fullmatch(
            path.name.removeprefix(EXPIRED_PREFIX)
        )
        if not expired and not ABANDONED_STAGE.fullmatch(path.name):
            continue
        if path.is_symlink() or not path.is_dir():
            continue
        with private_directory(path):
            shutil.rmtree(path)


def prune_generations(root: Path, root_fd: int, now: datetime) -> None:
    clean_abandoned_directories(root)
    daily_candidates = {}
    for stamp, path in list_generations(root):
        if stamp > now:
            continue
        try:
            entries = read_generation(path, stamp)
        except (BackupError, FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            if exc.errno == ELOOP:
                continue
            raise

        day = stamp.date()
        hourly = now - stamp <= timedelta(days=7)
        daily = (now.date() - day).days <= 30
        if hourly or (daily and day not in daily_candidates):
            daily_candidates.setdefault(day, (path, entries))
            continue
        if daily and (candidate := daily_candidates[day]) is not None:
            try:
                verify_snapshots(*candidate)
            except (BackupError, OSError):
                daily_candidates[day] = (path, entries)
                continue
            daily_candidates[day] = None

        # A retry can finish cleanup even if deletion was interrupted halfway.
        expired = root / (EXPIRED_PREFIX + path.name)
        path.rename(expired)
        os.fsync(root_fd)
        shutil.rmtree(expired)


def pull(
    root: Path, ssh_config: Path, host: str, sources: list[str], timeout_seconds: float
) -> Path:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise BackupError("SSH timeout must be a positive, finite number of seconds")
    if not HOST_ALIAS.fullmatch(host):
        raise BackupError("invalid SSH host alias")
    with private_directory(root) as root_fd:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("another backup pull holds the repository lock") from None

        with TemporaryDirectory(prefix=".teamup-pull-", dir=root) as staging_name:
            staging = Path(staging_name)
            staging.chmod(0o700)
            receipts = {
                source: receive_snapshot(
                    staging, ssh_config, host, source, timeout_seconds
                )
                for source in sources
            }
            stamp = utc_now()
            destination = root / stamp.strftime(GENERATION_FORMAT)
            manifest = {
                "version": 1,
                "created_at": utc_timestamp(stamp),
                "sources": receipts,
            }
            data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            if len(data) > MAX_MANIFEST_BYTES:
                raise BackupError("generation manifest is too large")
            with private_file(staging / MANIFEST, create=True) as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            with private_directory(staging) as stage_fd:
                os.fsync(stage_fd)

            if os.path.lexists(destination):
                raise BackupError("generation already exists")
            os.rename(staging, destination)
            try:
                os.fsync(root_fd)
            except OSError:
                os.rename(destination, staging)
                raise

        try:
            prune_generations(root, root_fd, stamp)
            os.fsync(root_fd)
        except (BackupError, OSError):
            raise RetentionError(
                f"Generation {destination.name} published. Backup retention failed."
            ) from None
    return destination


def check(
    root: Path, sources: list[str], max_age_hours: float, *, metadata_only: bool = False
) -> Path:
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise BackupError("maximum age must be a positive, finite number of hours")
    with private_directory(root):
        generations = list_generations(root)
        if not generations:
            raise BackupError("no backup generation exists")
        stamp, generation = generations[0]
        entries = read_generation(generation, stamp)
        if set(entries) != set(sources):
            raise BackupError(
                "latest generation does not contain the configured sources"
            )

        verify_snapshots(generation, entries, metadata_only=metadata_only)

        age = (utc_now() - stamp).total_seconds() / 3600
        if age < 0:
            raise BackupError("latest generation has a future timestamp")
        if age > max_age_hours:
            raise BackupError("latest generation is stale")
    return generation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pull_parser = commands.add_parser("pull", help="receive and publish a generation")
    check_parser = commands.add_parser(
        "check", help="verify the latest generation and its age"
    )
    for command in (pull_parser, check_parser):
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--source", action="append", required=True)
    pull_parser.add_argument("--ssh-config", type=Path, required=True)
    pull_parser.add_argument("--host", required=True)
    pull_parser.add_argument(
        "--ssh-timeout-seconds", type=float, default=SSH_TIMEOUT_SECONDS
    )
    check_parser.add_argument("--max-age-hours", type=float, required=True)
    check_parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="check manifest, sizes, and age without rehashing snapshots",
    )
    args = parser.parse_args(argv)

    try:
        if any(not SOURCE_LABEL.fullmatch(source) for source in args.source):
            raise BackupError("invalid backup source label")
        if len(set(args.source)) != len(args.source):
            raise BackupError("duplicate backup source label")
        if args.command == "pull":
            generation = pull(
                args.root,
                args.ssh_config,
                args.host,
                args.source,
                args.ssh_timeout_seconds,
            )
            print(f"Published backup generation {generation.name}")
        else:
            generation = check(
                args.root,
                args.source,
                args.max_age_hours,
                metadata_only=args.metadata_only,
            )
            print(f"Verified backup generation {generation.name}")
    except RetentionError as exc:
        print(exc, file=sys.stderr)
        return 1
    except BackupError as exc:
        print(f"Backup {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        message = os.strerror(exc.errno) if exc.errno is not None else "local I/O error"
        print(f"Backup {args.command} failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
