"""Restrict a backup SSH key to requesting configured SQLite snapshots."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

CAPTURE = Path(__file__).resolve().with_name("capture_backup.py")


def capture_command(config: Path, requested: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", requested):
        raise ValueError("invalid source name")
    with config.open(encoding="utf-8") as file:
        source = json.load(file)[requested]
    owner = source["owner"]
    database = source["database"]
    if owner == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", owner):
        raise ValueError("invalid database owner")
    if not Path(database).is_absolute():
        raise ValueError("expected an absolute database path")
    command = [
        "/usr/bin/sudo",
        "-n",
        "-u",
        owner,
        "--",
        "/usr/bin/python3",
        str(CAPTURE),
        "--source",
        database,
    ]
    if "timeout_seconds" in source:
        timeout = source["timeout_seconds"]
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("invalid snapshot timeout")
        command.extend(("--timeout-seconds", str(timeout)))
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        command = capture_command(
            args.config, os.environ.get("SSH_ORIGINAL_COMMAND", "")
        )
        os.execv(command[0], command)
    except (KeyError, OSError, TypeError, ValueError):
        print("Backup request denied.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
