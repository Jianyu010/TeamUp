"""Local account administration through the public Collaboration operations."""

import argparse
import sys
from getpass import GetPassWarning, getpass
from pathlib import Path
from warnings import catch_warnings, simplefilter

from .collaboration import Collaboration, CollaborationError


def parser() -> argparse.ArgumentParser:
    accounts = argparse.ArgumentParser(
        prog="teamup-server accounts", description="Administer TeamUp accounts"
    )
    accounts.add_argument("--db", required=True, type=Path)
    commands = accounts.add_subparsers(dest="command", required=True)
    for name in ("create", "reset-password", "disable"):
        commands.add_parser(name).add_argument("username")
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
        else:
            collaboration.disable_account(args.username)
    except CollaborationError as exc:
        messages = {
            "invalid_request": (
                "Use a username of 3–64 lowercase ASCII characters starting with a letter "
                "(letters, digits, _ and -), and a password of 12–128 characters."
            ),
            "not_found": "Account does not exist.",
            "conflict": "Account already exists."
            if args.command == "create"
            else "Account is disabled.",
        }
        raise ValueError(messages[exc.code]) from None
    print(f"{args.command} succeeded for {args.username}")
