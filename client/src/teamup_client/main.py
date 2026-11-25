"""Composition and command-line entry points for TeamUp Client."""

import argparse
import asyncio
import json
import os
import secrets
import signal
import sqlite3
import sys
from contextlib import nullcontext, suppress
from functools import partial
from pathlib import Path

from ._config import ConfigError, locked_config, owner_key, read_config
from ._console import serve_console
from ._console_data import ConsoleReader
from ._coordinator import Coordinator
from ._daemon import handle
from ._desktop import DesktopNotifier, default_command
from ._harness import default_codex_command, drivers
from ._local import LocalError, listen
from ._login import login
from ._notice_output import NoticeOutput
from ._notices import open_store
from ._protocol import is_string, is_uuid
from ._upstream import connect
from .ipc import ClientError, call


async def _serve(path, *, console=False):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        with (
            locked_config(path, remove_stale_socket=True) as (config, _, _),
            open_store(config.socket_path) as notices,
        ):
            async with connect(config) as upstream:
                command = config.notify_command
                notifier = DesktopNotifier(
                    default_command() if command is None else command
                )
                codex = config.codex_command
                coordinator = Coordinator(
                    config,
                    upstream,
                    notices,
                    NoticeOutput(),
                    notifier,
                    drivers(default_codex_command() if codex is None else codex),
                )
                on_call = partial(handle, coordinator=coordinator)
                viewer = (
                    serve_console(
                        ConsoleReader(coordinator.console_sessions, upstream.request)
                    )
                    if console
                    else nullcontext()
                )
                async with (
                    listen(config.socket_path, on_call),
                    coordinator.watching_inboxes(),
                    viewer as console_url,
                ):
                    if console_url:
                        print(console_url, flush=True)
                    await stop.wait()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def _owner(args):
    config = read_config(args.config)
    key = owner_key(config)
    if key is None:
        raise ConfigError("Owner commands need account_token or enrollment_key")
    if args.command == "wake":
        method, params = (
            "set_wake",
            {
                "harness_instance_id": args.harness_instance_id,
                "session_key": args.session_key,
                "rooms": args.rooms,
                "senders": args.senders,
            },
        )
    elif args.decision is None:
        method, params = "sessions", {}
    else:
        if args.session_key is None:
            raise ConfigError("Name the harness instance and session to decide")
        method, params = (
            "decide_session",
            {
                "harness_instance_id": args.harness_instance_id,
                "session_key": args.session_key,
                "state": {"approve": "approved", "reject": "rejected"}[args.decision],
            },
        )
    return await call(config.socket_path, key, method, params)


async def _manage_member(args):
    config = read_config(args.config)
    if config.account_token is None:
        raise ConfigError("Run teamup-client login to manage members")
    if not is_uuid(args.member_id):
        raise ConfigError("Member ID must be a UUID")
    path = f"/members/{args.member_id}"
    async with connect(config) as upstream:
        if args.action == "retire":
            return await upstream.request(
                "POST", f"{path}/retire", config.account_token, member_auth=False
            )
        return await upstream.request(
            "PATCH",
            path,
            config.account_token,
            body={"role_description": args.role_description},
            member_auth=False,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="teamup-client")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True, type=Path)
    serve.add_argument(
        "--console", action="store_true", help="Start the local browser console"
    )
    sign_in = commands.add_parser("login")
    sign_in.add_argument("--config", required=True, type=Path)
    sign_in.add_argument("--username", required=True)
    members = commands.add_parser(
        "members", help="Manage members owned by your account"
    )
    members.add_argument("--config", required=True, type=Path)
    member_actions = members.add_subparsers(dest="action", required=True)
    rename = member_actions.add_parser(
        "rename", help="Update a member's role description"
    )
    rename.add_argument("member_id")
    rename.add_argument("role_description")
    retire = member_actions.add_parser("retire", help="Permanently retire a member")
    retire.add_argument("member_id")
    commands.add_parser("init-binding")
    install = commands.add_parser(
        "init-install",
        help="Generate a key that identifies sessions within a harness install",
    )
    install.add_argument("harness_instance_id")
    sessions = commands.add_parser("sessions", help="List or decide agent sessions")
    sessions.add_argument("--config", required=True, type=Path)
    sessions.add_argument("decision", nargs="?", choices=("approve", "reject"))
    sessions.add_argument("harness_instance_id", nargs="?")
    sessions.add_argument("session_key", nargs="?")
    wake = commands.add_parser(
        "wake", help="Allow mentions to start turns in one session"
    )
    wake.add_argument("--config", required=True, type=Path)
    wake.add_argument("harness_instance_id")
    wake.add_argument("session_key")
    wake.add_argument("--room", action="append", default=[], dest="rooms")
    wake.add_argument("--sender", action="append", default=[], dest="senders")
    notifications = commands.add_parser("notifications")
    notifications.add_argument("--after", type=int)
    notifications.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    try:
        if args.command == "init-binding":
            print(
                json.dumps(
                    {
                        "binding_key": secrets.token_urlsafe(32),
                        "member_token": secrets.token_urlsafe(32),
                        "harness_instance_id": "edit-harness-instance-id",
                        "session_key": "edit-session-key",
                    },
                    indent=2,
                )
            )
        elif args.command == "init-install":
            if not is_string(args.harness_instance_id, 128):
                raise ConfigError(
                    "The harness instance ID must contain 1–128 characters"
                )
            print(
                json.dumps(
                    {
                        "binding_key": secrets.token_urlsafe(32),
                        "harness_instance_id": args.harness_instance_id,
                    },
                    indent=2,
                )
            )
        elif args.command in ("sessions", "wake"):
            print(json.dumps(asyncio.run(_owner(args)), ensure_ascii=False, indent=2))
        elif args.command == "members":
            updated = asyncio.run(_manage_member(args))
            print(json.dumps(updated, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            with suppress(KeyboardInterrupt):
                asyncio.run(_serve(args.config, console=args.console))
        elif args.command == "login":
            login(args.config, args.username)
            print("TeamUp login saved.")
        else:
            socket_path = os.getenv("TEAMUP_SOCKET")
            binding_key = os.getenv("TEAMUP_BINDING_KEY")
            if not socket_path or not binding_key:
                raise ConfigError("Set TEAMUP_SOCKET and TEAMUP_BINDING_KEY")
            params = {
                key: value
                for key, value in {"after": args.after, "limit": args.limit}.items()
                if value is not None
            }
            result = asyncio.run(
                call(socket_path, binding_key, "notifications", params)
            )
            print(json.dumps(result, ensure_ascii=False))
    except (ConfigError, LocalError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ClientError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": exc.message}}),
            file=sys.stderr,
        )
        return 1
    except (OSError, sqlite3.Error):
        print(
            "Could not access the Client socket or notification storage",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
