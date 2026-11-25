"""Local account administration through the public Collaboration operations."""

import argparse
import sys
from getpass import GetPassWarning, getpass
from pathlib import Path
from warnings import catch_warnings, simplefilter

from .collaboration import Collaboration, CollaborationError

_MAINTENANCE_GRANTS = {
    "grant-maintenance": ("maintenance_read", True),
    "revoke-maintenance": ("maintenance_read", False),
    "grant-logs": ("logs_read", True),
    "revoke-logs": ("logs_read", False),
}


def parser() -> argparse.ArgumentParser:
    accounts = argparse.ArgumentParser(
        prog="teamup-server accounts", description="Administer TeamUp accounts"
    )
    accounts.add_argument("--db", required=True, type=Path)
    commands = accounts.add_subparsers(dest="command", required=True)
    for name in (
        "create",
        "reset-password",
        "disable",
        "grant-console",
        "revoke-console",
        *_MAINTENANCE_GRANTS,
    ):
        commands.add_parser(name).add_argument("username")
    for name in ("grant-transcript", "revoke-transcript"):
        room_command = commands.add_parser(name)
        room_command.add_argument("username")
        room_command.add_argument("room_id")
    commands.add_parser("list")
    return accounts


def _read_password() -> str:
    if not sys.stdin.isatty():
        raise ValueError("Enter the password in a terminal with echo disabled.")
    with catch_warnings():
        simplefilter("error", GetPassWarning)
        try:
            password = getpass("Password: ")
            confirmation = getpass("Confirm password: ")
        except (EOFError, GetPassWarning, KeyboardInterrupt):
            raise ValueError("Password entry was cancelled or unavailable.") from None
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    return password


def run(args: argparse.Namespace, collaboration: Collaboration) -> None:
    try:
        if args.command == "list":
            for account in collaboration.list_accounts():
                status = "disabled" if account["disabled"] else "active"
                print(f"{account['username']}\t{status}\t{account['owner_id']}")
            return

        if args.command == "create":
            collaboration.create_account(args.username, _read_password())
        elif args.command == "reset-password":
            collaboration.reset_account_password(args.username, _read_password())
        elif args.command == "grant-console":
            collaboration.grant_console_read(args.username)
        elif args.command == "revoke-console":
            collaboration.revoke_console_read(args.username)
        elif args.command in _MAINTENANCE_GRANTS:
            scope, granted = _MAINTENANCE_GRANTS[args.command]
            collaboration.set_maintenance_grant(args.username, scope, granted)
        elif args.command == "grant-transcript":
            collaboration.grant_transcript_read(args.username, args.room_id)
        elif args.command == "revoke-transcript":
            collaboration.revoke_transcript_read(args.username, args.room_id)
        else:
            collaboration.disable_account(args.username)
    except CollaborationError as exc:
        raise ValueError(_failure_message(args.command, exc.code)) from None
    print(f"{args.command} succeeded for {args.username}")


def _failure_message(command: str, code: str) -> str:
    if code == "not_found":
        if command in ("grant-transcript", "revoke-transcript"):
            return "Account or room does not exist."
        return "Account does not exist."
    if code == "conflict":
        return (
            "Account already exists." if command == "create" else "Account is disabled."
        )
    if command in _MAINTENANCE_GRANTS:
        return (
            "Use a username of 3–64 lowercase ASCII characters starting with a letter "
            "(letters, digits, _ and -)."
        )
    if command in ("grant-transcript", "revoke-transcript"):
        return (
            "Use a username of 3–64 lowercase ASCII characters starting with a letter "
            "(letters, digits, _ and -), and a standard UUID room ID."
        )
    return (
        "Use a username of 3–64 lowercase ASCII characters starting with a letter "
        "(letters, digits, _ and -), and a password of 12–128 characters."
    )
